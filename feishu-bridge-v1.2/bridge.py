#!/usr/bin/env python3
"""
Feishu ↔ Claude Code Bridge v1.2
Listens to a Feishu group chat, forwards messages to Claude CLI, and replies.
v1.2: Thread-isolated context + dynamic token budgeting + network retry.
"""
import json, time, os, subprocess, sys, requests, io, random
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
from datetime import datetime, timezone

# === Config: loaded from config.json (or fallback to hardcoded defaults) ===
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

DEFAULTS = dict(
    group_id="oc_xxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    bot_app_id="cli_xxxxxxxxxxxxxxxxxxxx",
    poll_interval=3,
    state_file=".feishu_bridge_state.json",
    log_file="bridge_debug.log",
    credentials_file="",
    max_context_chars=4000,
    max_history_storage=50,
    max_threads=20,
    max_retries=3,
    retry_base_delay=2,
    main_thread_key="__main__",
)

config = dict(DEFAULTS)
if os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE, encoding="utf-8") as f:
        config.update(json.load(f))

GROUP_ID = config["group_id"]
POLL_INTERVAL = config["poll_interval"]
STATE_FILE = config["state_file"]
LOG_FILE = config["log_file"]
CREDENTIALS_FILE = config["credentials_file"] or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", ".feishu-user-plugin", "credentials.json"
)
BOT_APP_ID = config["bot_app_id"]
MAX_CONTEXT_CHARS = config["max_context_chars"]
MAX_HISTORY_STORAGE = config["max_history_storage"]
MAX_THREADS = config["max_threads"]
MAX_RETRIES = config["max_retries"]
RETRY_BASE_DELAY = config["retry_base_delay"]
MAIN_THREAD_KEY = config["main_thread_key"]


# === Helpers ===

def log(msg):
    ts = datetime.now().isoformat()
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
    except:
        pass


def retry_request(name, fn, *args, **kwargs):
    """Network request wrapper with exponential backoff."""
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 1)
            if attempt < MAX_RETRIES:
                log(f"  retry[{name}] attempt {attempt}: {e} -> {delay:.1f}s")
                time.sleep(delay)
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            if status < 500:
                raise
            last_exc = e
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
            if attempt < MAX_RETRIES:
                log(f"  retry[{name}] HTTP {status} attempt {attempt}: -> {delay:.1f}s")
                time.sleep(delay)
    raise last_exc


# === Feishu API ===

def get_bot_token():
    """Get bot tenant_access_token."""
    with open(CREDENTIALS_FILE) as f:
        creds = json.load(f)
    p = creds["profiles"]["default"]
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": p["LARK_APP_ID"], "app_secret": p["LARK_APP_SECRET"]},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise Exception(f"Token error: {data}")
    return data["tenant_access_token"]


def get_messages(token, page_size=10):
    """Fetch recent group messages."""
    headers = {"Authorization": f"Bearer {token}"}
    url = (f"https://open.feishu.cn/open-apis/im/v1/messages"
           f"?container_id_type=chat&container_id={GROUP_ID}"
           f"&page_size={page_size}&sort_type=ByCreateTimeDesc")
    resp = requests.get(url, headers=headers, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise Exception(f"Messages error: {data}")
    return data["data"]["items"]


def send_message(token, text, msg_type="text", root_id=None):
    """Send message to the group (supports thread reply)."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "receive_id": GROUP_ID,
        "msg_type": msg_type,
        "content": json.dumps({"text": text}),
    }
    if root_id:
        payload["root_id"] = root_id
    resp = requests.post(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
        headers=headers, json=payload, timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        log(f"  send_message error: {data.get('msg')}")


# === State & History ===

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            data = json.load(f)
            data.setdefault("thread_history", {})
            return data
    return {"last_msg_id": None, "processed_ids": [], "thread_history": {}}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_thread_history(state, thread_id):
    return state["thread_history"].get(thread_id, [])


def add_to_thread_history(state, thread_id, user_msg, assistant_msg):
    history = state["thread_history"].get(thread_id, [])
    history.append({"user": user_msg, "assistant": assistant_msg})
    if len(history) > MAX_HISTORY_STORAGE:
        history = history[-MAX_HISTORY_STORAGE:]
    state["thread_history"][thread_id] = history
    if len(state["thread_history"]) > MAX_THREADS:
        keys = list(state["thread_history"].keys())
        for k in keys[:-MAX_THREADS]:
            del state["thread_history"][k]


def trim_history_to_budget(history, budget, new_msg_len):
    """Keep as many recent turns as fit within the character budget."""
    overhead = len("用户: \nClaude: \n")
    new_overhead = len("用户: \n")
    available = budget - new_msg_len - new_overhead
    keep = 0
    for turn in reversed(history):
        cost = len(turn["user"]) + len(turn["assistant"]) + overhead
        if available - cost < 0:
            break
        available -= cost
        keep += 1
    return history[-keep:] if keep > 0 else []


def build_contextual_prompt(history, new_message):
    """Build a prompt with thread context (dynamically trimmed)."""
    usable = trim_history_to_budget(history, MAX_CONTEXT_CHARS, len(new_message))
    lines = []
    for turn in usable:
        lines.append(f"用户: {turn['user']}")
        lines.append(f"Claude: {turn['assistant']}")
    lines.append(f"用户: {new_message}")
    return "\n".join(lines)


# === Claude CLI ===

def call_claude(prompt):
    """Execute Claude CLI in one-shot mode."""
    result = subprocess.run(
        ["claude", "-p", prompt],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env={**os.environ},
        shell=True,
    )
    if result.returncode != 0:
        return (f"Claude error (code={result.returncode}):\n"
                f"{result.stderr.strip()[:500]}")
    output = result.stdout.strip()
    return output if output else "(no output)"


# === Main Loop ===

def main():
    log("=" * 50)
    log("Feishu ↔ Claude Code Bridge v1.2")
    log(f"Group: {GROUP_ID} | Poll: {POLL_INTERVAL}s | PID: {os.getpid()}")
    log(f"Context: {MAX_CONTEXT_CHARS} chars budget, {MAX_HISTORY_STORAGE} history storage")
    log(f"Retry: {MAX_RETRIES} attempts, base delay {RETRY_BASE_DELAY}s")
    log("=" * 50)

    state = load_state()
    first_run = state["last_msg_id"] is None

    while True:
        try:
            token = retry_request("get_bot_token", get_bot_token)
            messages = retry_request("get_messages", get_messages, token)

            if not messages:
                time.sleep(POLL_INTERVAL)
                continue

            # Collect new messages (oldest first)
            new_msgs = []
            for msg in reversed(messages):
                msg_id = msg.get("message_id", "")
                msg_type = msg.get("msg_type", "")
                content = msg.get("body", {}).get("content", "")

                if msg_type == "system":
                    continue
                sender_type = msg.get("sender", {}).get("sender_type", "")
                sender_id = msg.get("sender", {}).get("id", "")
                if sender_type == "app" and sender_id == BOT_APP_ID:
                    continue
                if msg_id in state["processed_ids"]:
                    continue

                new_msgs.append({
                    "id": msg_id,
                    "type": msg_type,
                    "sender": sender_id,
                    "content": content,
                    "time": msg.get("create_time", ""),
                })

            if first_run and new_msgs:
                last_new = new_msgs[-1]
                state["last_msg_id"] = last_new["id"]
                state["processed_ids"] = [last_new["id"]]
                save_state(state)
                first_run = False
                log(f"First run synced to: {last_new['id'][-20:]}")
            elif new_msgs:
                for msg in new_msgs:
                    try:
                        text_content = json.loads(msg["content"]).get("text", msg["content"])
                    except:
                        text_content = msg["content"]

                    if not text_content.strip():
                        continue

                    try:
                        ts_raw = int(msg["time"])
                        if ts_raw > 10000000000:
                            ts_raw //= 1000
                        ts = datetime.fromtimestamp(ts_raw, tz=timezone.utc).strftime("%H:%M:%S")
                    except Exception:
                        ts = datetime.now().strftime("%H:%M:%S")

                    root_id = msg.get("root_id", "") or ""
                    thread_id = root_id if root_id else MAIN_THREAD_KEY
                    log(f"New msg ({ts}) [{thread_id[-16:]}]: {text_content[:200]}")

                    # Send acknowledgement in-thread
                    log("Sending progress...")
                    retry_request("send_ack", send_message, token,
                                  "⏳ Received, Claude processing...", root_id=root_id or None)

                    # Build contextual prompt
                    log("Calling Claude CLI...")
                    history = get_thread_history(state, thread_id)
                    contextual_prompt = build_contextual_prompt(history, text_content)
                    log(f"  Context: {len(history)} stored -> {len(contextual_prompt)} chars/{MAX_CONTEXT_CHARS} budget")

                    try:
                        response = call_claude(contextual_prompt)
                        log(f"Claude reply ({len(response)} chars): {response[:200]}")
                    except subprocess.TimeoutExpired:
                        response = "Timeout (120s). Please retry or simplify."
                        log("Claude timeout")
                    except Exception as e:
                        response = f"Error: {e}"
                        log(f"Claude error: {e}")

                    MAX_LEN = 30000
                    if len(response) > MAX_LEN:
                        response = response[:MAX_LEN] + "\n\n...(truncated)"

                    try:
                        retry_request("send_reply", send_message, token,
                                      response, root_id=root_id or None)
                        log("  Reply sent")
                    except Exception as e:
                        log(f"  Send failed: {e}")

                    state["last_msg_id"] = msg["id"]
                    state["processed_ids"].append(msg["id"])
                    if not response.startswith("⚠") and not response.startswith("Error") and not response.startswith("Timeout"):
                        add_to_thread_history(state, thread_id, text_content, response)
                    if len(state["processed_ids"]) > 100:
                        state["processed_ids"] = state["processed_ids"][-100:]
                    save_state(state)

        except KeyboardInterrupt:
            log("User terminated")
            break
        except Exception as e:
            log(f"Error: {e}")
            import traceback
            log(traceback.format_exc())

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
