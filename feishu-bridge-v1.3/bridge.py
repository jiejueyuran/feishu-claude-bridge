#!/usr/bin/env python3
"""
Feishu ↔ Claude Code Bridge (v2)
Listens to a Feishu group chat, forwards messages to Claude CLI, and replies.
Supports Claude tool use (Bash/Read/Write/Playwright etc.) and multi-turn sessions.
"""

import json
import os
import io
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone

# Fix Windows console encoding for Unicode output
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

try:
    import requests
except ImportError:
    print("Missing dependency: requests")
    print("Install: pip install requests")
    sys.exit(1)


SESSION_EXPIRY_SECONDS = 1800  # Session expiry: 30 minutes of inactivity


def log(msg, log_file=None):
    ts = datetime.now().isoformat()
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    if log_file:
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
        except Exception:
            pass


class Config:
    def __init__(self, path):
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)

        self.group_id = raw["group_id"]
        self.bot_app_id = raw.get("bot_app_id", "")
        self.credentials_file = raw.get("credentials_file", "")
        self.poll_interval = raw.get("poll_interval", 3)
        self.state_file = raw.get("state_file", ".feishu_bridge_state.json")
        self.log_file = raw.get("log_file", "bridge_debug.log")
        self.claude_timeout = raw.get("claude_timeout", 300)
        self.max_threads = raw.get("max_threads", 20)

        # Default credentials path relative to script or home
        if not self.credentials_file:
            home = os.path.expanduser("~")
            self.credentials_file = os.path.join(
                home, ".feishu-user-plugin", "credentials.json"
            )


class FeishuClient:
    def __init__(self, config):
        self.config = config
        self._token = None

    def _load_credentials(self):
        path = self.config.credentials_file
        with open(path, encoding="utf-8") as f:
            creds = json.load(f)
        profile = creds.get("profiles", {}).get("default", creds)
        app_id = profile.get("LARK_APP_ID") or profile.get("app_id", "")
        app_secret = profile.get("LARK_APP_SECRET") or profile.get("app_secret", "")
        if not app_id or not app_secret:
            raise ValueError(
                f"Credentials file '{path}' missing LARK_APP_ID/LARK_APP_SECRET"
            )
        return app_id, app_secret

    def get_token(self):
        if self._token:
            return self._token
        app_id, app_secret = self._load_credentials()
        resp = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise Exception(f"Token request failed: {data}")
        self._token = data["tenant_access_token"]
        return self._token

    def expel_token(self):
        self._token = None

    def get_messages(self, page_size=10):
        token = self.get_token()
        headers = {"Authorization": f"Bearer {token}"}
        url = (
            f"https://open.feishu.cn/open-apis/im/v1/messages"
            f"?container_id_type=chat&container_id={self.config.group_id}"
            f"&page_size={page_size}&sort_type=ByCreateTimeDesc"
        )
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise Exception(f"Read messages failed: {data}")
        items = data.get("data", {}).get("items", [])
        return items

    def send_message(self, text, root_id=None):
        token = self.get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        payload = {
            "receive_id": self.config.group_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}),
        }
        if root_id:
            payload["root_id"] = root_id
        resp = requests.post(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            headers=headers,
            json=payload,
            timeout=10,
        )
        if resp.status_code != 200:
            log(f"Send message HTTP {resp.status_code}: {resp.text[:200]}")
            return
        data = resp.json()
        if data.get("code") != 0:
            log(f"Send message failed: {data.get('msg', data.get('code'))}")

    def is_bot_message(self, msg):
        sender = msg.get("sender", {})
        sender_type = sender.get("sender_type", "")
        sender_id = sender.get("id", "")
        return sender_type == "app" and sender_id == self.config.bot_app_id


def call_claude(prompt, session_id=None, timeout=300):
    """
    Call Claude CLI with tool use support.
    Returns (response_text, session_id, cost_usd, num_turns).
    """
    cmd = [
        "claude", "-p",
        "--output-format", "json",
        "--permission-mode", "bypassPermissions",
    ]
    if session_id:
        cmd.extend(["--resume", session_id])
    cmd.append(prompt)

    result = subprocess.run(
        cmd,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env={**os.environ},
        shell=sys.platform == "win32",
    )

    if result.returncode != 0:
        error_msg = result.stderr.strip()[:500] if result.stderr else "Unknown error"
        # Retry without session if session expired
        if session_id and ("session" in error_msg.lower() or "not found" in error_msg.lower()):
            log(f"  Session {session_id[:8]}... expired, starting new session")
            return call_claude(prompt, session_id=None, timeout=timeout)
        return f"Claude error (code={result.returncode}):\n{error_msg}", None, 0, 0

    stdout = result.stdout.strip()
    if not stdout:
        return "(no output)", None, 0, 0

    try:
        data = json.loads(stdout)
        text = data.get("result", stdout)
        sid = data.get("session_id")
        cost = data.get("total_cost_usd", 0)
        turns = data.get("num_turns", 0)
        return text, sid, cost, turns
    except json.JSONDecodeError:
        return stdout, None, 0, 0


def load_state(path):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
                # Migrate from old format
                if "sessions" not in data:
                    data["sessions"] = {}
                return data
        except Exception:
            pass
    return {"last_msg_id": None, "processed_ids": [], "sessions": {}}


def save_state(path, state):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def extract_text(content_str):
    """Extract plain text from Feishu message content."""
    if not content_str:
        return ""
    try:
        parsed = json.loads(content_str)
        if isinstance(parsed, dict):
            return parsed.get("text", content_str)
    except (json.JSONDecodeError, TypeError):
        pass
    return content_str if isinstance(content_str, str) else str(content_str)


def safe_format_time(time_str):
    """Parse Feishu timestamp string to HH:MM:SS (safe against Windows issues)."""
    if not time_str:
        return datetime.now().strftime("%H:%M:%S")
    try:
        ts = int(time_str)
        if ts > 10000000000:
            ts //= 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S")
    except Exception:
        return datetime.now().strftime("%H:%M:%S")


def get_session_id(state, thread_id):
    """Get session_id for a thread, checking expiry. Returns None if expired."""
    data = state["sessions"].get(thread_id)
    if not data:
        return None
    if isinstance(data, dict):
        sid = data.get("session_id")
        last_used = data.get("last_used", 0)
        if sid and time.time() - last_used > SESSION_EXPIRY_SECONDS:
            log(f"  Session {sid[:8]}... expired ({SESSION_EXPIRY_SECONDS//60}min inactivity)")
            del state["sessions"][thread_id]
            return None
        return sid
    # Legacy format: bare string
    return data


def set_session_id(state, thread_id, session_id):
    """Save session_id for a thread with current timestamp."""
    state["sessions"][thread_id] = {
        "session_id": session_id,
        "last_used": time.time(),
    }


def format_response(text, cost_usd=0, num_turns=0):
    """Append cost info to response."""
    if cost_usd > 0.01:
        text += f"\n\n💰 ${cost_usd:.3f}"
    if num_turns > 1:
        text += f" | 🔄 {num_turns} turns"
    return text


def main():
    config_path = os.environ.get("FEISHU_BRIDGE_CONFIG", "config.json")
    if not os.path.exists(config_path):
        log(f"Config file not found: {config_path}", "<config_error>")
        log(
            "Copy config.example.json to config.json and fill in your settings.",
            "<config_error>",
        )
        sys.exit(1)

    config = Config(config_path)
    client = FeishuClient(config)
    log_file = config.log_file

    log("=" * 50, log_file)
    log("Feishu ↔ Claude Code Bridge v1.3", log_file)
    log(f"Group: {config.group_id[-16:]} | Poll: {config.poll_interval}s | Timeout: {config.claude_timeout}s | PID: {os.getpid()}", log_file)
    log("Features: tool use | multi-turn sessions (resume) | user isolation | session expiry (30min) | cost tracking", log_file)
    log("=" * 50, log_file)

    state = load_state(config.state_file)
    first_run = state["last_msg_id"] is None

    while True:
        try:
            messages = client.get_messages()

            if not messages:
                time.sleep(config.poll_interval)
                continue

            # Process from oldest to newest
            new_msgs = []
            for msg in reversed(messages):
                msg_id = msg.get("message_id", "")
                msg_type = msg.get("msg_type", "")

                # Skip system messages
                if msg_type == "system":
                    continue

                # Skip bot's own messages
                if client.is_bot_message(msg):
                    continue

                # Skip already processed
                if msg_id in state["processed_ids"]:
                    continue

                content_raw = msg.get("body", {}).get("content", "")
                text_content = extract_text(content_raw)
                if not text_content.strip():
                    continue

                root_id = msg.get("root_id", "") or ""
                sender_id = msg.get("sender", {}).get("id", "")
                new_msgs.append(
                    {
                        "id": msg_id,
                        "content": text_content,
                        "time": msg.get("create_time", ""),
                        "root_id": root_id,
                        "sender": sender_id,
                    }
                )

            if first_run and new_msgs:
                last = new_msgs[-1]
                state["last_msg_id"] = last["id"]
                state["processed_ids"] = [last["id"]]
                save_state(config.state_file, state)
                first_run = False
                log(f"First run — synced to latest message: {last['id'][-16:]}", log_file)

            elif new_msgs:
                for msg in new_msgs:
                    ts = safe_format_time(msg["time"])
                    # User-isolated thread ID (each user gets their own session)
                    sender_open_id = msg.get("sender", "")
                    thread_id = (
                        f"thread:{msg['root_id']}:{sender_open_id}"
                        if msg["root_id"]
                        else f"user:{sender_open_id}"
                    )
                    log(f"New message ({ts}) [{thread_id[-16:]}]: {msg['content'][:200]}", log_file)

                    # Send progress indicator
                    sid = get_session_id(state, thread_id)
                    session_hint = " (resuming)" if sid else ""
                    client.send_message(
                        f"⏳ Processing{session_hint}...",
                        root_id=msg["root_id"] or None,
                    )
                    log("Progress indicator sent", log_file)

                    log(f"Calling Claude CLI{' (resume ' + sid[:8] + '...)' if sid else ' (new session)'}", log_file)

                    try:
                        response, new_sid, cost, turns = call_claude(
                            msg["content"],
                            session_id=sid,
                            timeout=config.claude_timeout,
                        )
                        log(f"Claude responded ({len(response)} chars, ${cost:.3f}, {turns} turns): {response[:200]}", log_file)
                    except subprocess.TimeoutExpired:
                        response = f"⚠️ Claude timed out ({config.claude_timeout}s). Please simplify or retry."
                        new_sid, cost, turns = None, 0, 0
                        log("Claude timed out", log_file)
                    except Exception as e:
                        response = f"⚠️ Execution error: {e}"
                        new_sid, cost, turns = None, 0, 0
                        log(f"Claude error: {e}", log_file)

                    # Save session_id with timestamp
                    if new_sid:
                        set_session_id(state, thread_id, new_sid)
                        # Clean up old sessions
                        if len(state["sessions"]) > config.max_threads:
                            keys = list(state["sessions"].keys())
                            for k in keys[: -config.max_threads]:
                                del state["sessions"][k]

                    # Format response with cost info
                    reply = format_response(response, cost, turns)

                    # Truncate
                    MAX_LEN = 30000
                    if len(reply) > MAX_LEN:
                        reply = reply[:MAX_LEN] + "\n\n...(truncated)"

                    # Send response
                    try:
                        client.send_message(reply, root_id=msg["root_id"] or None)
                        log("Response sent to group", log_file)
                    except Exception as e:
                        log(f"Failed to send response: {e}", log_file)

                    # Update state
                    state["last_msg_id"] = msg["id"]
                    state["processed_ids"].append(msg["id"])
                    if len(state["processed_ids"]) > 100:
                        state["processed_ids"] = state["processed_ids"][-100:]
                    save_state(config.state_file, state)

        except KeyboardInterrupt:
            log("Shutdown by user", log_file)
            break
        except Exception as e:
            log(f"Error: {e}", log_file)
            log(traceback.format_exc(), log_file)
            client.expel_token()  # Force token refresh on next iteration

        time.sleep(config.poll_interval)


if __name__ == "__main__":
    main()
