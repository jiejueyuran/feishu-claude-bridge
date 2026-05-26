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

# events.jsonl auto-rotation threshold (10MB)
MAX_EVENTS_FILE_SIZE = 10 * 1024 * 1024

# PID lock file path (same dir as state file)
PID_FILE = None


def acquire_pid_lock(pid_file, log_file=None):
    """Write PID to lock file. Returns False if another instance is running."""
    import atexit as _atexit

    def _cleanup():
        pass  # Don't remove PID file — keep it for stale detection on next startup

    try:
        os.makedirs(os.path.dirname(pid_file) or ".", exist_ok=True)
        if os.path.exists(pid_file):
            with open(pid_file) as f:
                old_pid = f.read().strip()
            if old_pid.isdigit():
                try:
                    import ctypes
                    handle = ctypes.windll.kernel32.OpenProcess(0x0400, False, int(old_pid))
                    if handle:
                        ctypes.windll.kernel32.CloseHandle(handle)
                        log(f"Bridge already running (PID {old_pid}), exiting", log_file)
                        return False
                    log(f"Stale PID file ({old_pid} not running), overwriting", log_file)
                except Exception:
                    log(f"Stale PID file ({old_pid} — can't check), overwriting", log_file)
        with open(pid_file, "w") as f:
            f.write(str(os.getpid()))
        log(f"PID lock acquired: {pid_file} → {os.getpid()}", log_file)
        _atexit.register(_cleanup)
        return True
    except Exception as e:
        log(f"PID lock warning: {e} — continuing without lock", log_file)
        return True


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


def api_call_with_retry(func, max_retries=3, base_delay=2, log_file=None, label=""):
    """Call a function with exponential backoff retry.

    Retries on ConnectionError, Timeout, HTTP 429/5xx.
    Returns (success_bool, result_or_error_message).
    """
    for attempt in range(max_retries + 1):
        try:
            result = func()
            return True, result
        except (requests.ConnectionError, requests.Timeout) as e:
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                log(f"  {label} network error, retry {attempt+1}/{max_retries} in {delay}s: {e}", log_file)
                time.sleep(delay)
                continue
            log(f"{label} failed after {max_retries} retries: {e}", log_file)
            return False, str(e)
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            if status in (429,) or status >= 500:
                if attempt < max_retries:
                    delay = base_delay * (2 ** attempt)
                    if status == 429:
                        retry_after = e.response.headers.get("Retry-After")
                        if retry_after:
                            delay = max(delay, int(retry_after))
                    log(f"  {label} HTTP {status}, retry {attempt+1}/{max_retries} in {delay}s", log_file)
                    time.sleep(delay)
                    continue
            log(f"{label} HTTP {status}: {e}", log_file)
            return False, str(e)
        except Exception as e:
            log(f"{label} unexpected error: {e}", log_file)
            return False, str(e)
    return False, "max retries exceeded"


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

        # Events.jsonl path (feishu-user-plugin WebSocket sidecar)
        self.events_file = raw.get(
            "events_file",
            os.path.join(
                os.path.expanduser("~"),
                ".feishu-user-plugin",
                "events.jsonl",
            ),
        )

        # Default credentials path relative to script or home
        if not self.credentials_file:
            home = os.path.expanduser("~")
            self.credentials_file = os.path.join(
                home, ".feishu-user-plugin", "credentials.json"
            )


class EventFileWatcher:
    """Tail ~/.feishu-user-plugin/events.jsonl for real-time Feishu events.

    The feishu-user-plugin maintains a persistent WebSocket connection to
    Feishu and writes incoming events to this JSONL file as they arrive.
    We track file position with seek/tell to read only new lines each poll.
    This eliminates the need for polling the Feishu REST API every 3 seconds.
    """

    def __init__(self, path, group_id, bot_app_id=None):
        self.path = path
        self.group_id = group_id
        self.bot_app_id = bot_app_id
        self._fp = None
        self._last_event_id = None
        self._fallback_count = 0

    def _open(self):
        """Open events file and seek to end to skip old events."""
        if self._fp and not self._fp.closed:
            return self._fp
        if not os.path.exists(self.path):
            return None
        try:
            fp = open(self.path, "r", encoding="utf-8")
            fp.seek(0, io.SEEK_END)
            self._fp = fp
            self._fallback_count = 0
            return fp
        except Exception:
            return None

    def poll(self):
        """Read new events since last check.

        Returns:
            list of parsed event dicts:
              [{id, content, time, root_id, sender, chat_id}, ...]
            or None if events file is unavailable (triggers polling fallback).
        """
        fp = self._open()
        if not fp:
            self._fallback_count += 1
            return None

        try:
            events = []
            while True:
                line = fp.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                    parsed = self._parse_event(raw)
                    if parsed:
                        events.append(parsed)
                except json.JSONDecodeError:
                    continue

            self._fallback_count = 0
            return events

        except Exception:
            self._fallback_count += 1
            if self._fp and not self._fp.closed:
                try:
                    self._fp.close()
                except Exception:
                    pass
            self._fp = None
            return None

    def _parse_event(self, raw):
        """Parse a raw JSONL event into internal message format.

        Returns None if the event should be skipped (wrong chat, bot, or
        unsupported message type).
        Supported types: text, image, file, audio, media, sticker, post.
        Returns a dict with at least id/content/msg_type/time/sender/chat_id.
        """
        event_type = raw.get("event_type", "")
        if event_type != "im.message.receive_v1":
            return None

        event = raw.get("event", {}) or {}
        message = event.get("message", {}) or {}
        sender = event.get("sender", {}) or {}

        # Filter by target group
        chat_id = message.get("chat_id", "")
        if chat_id != self.group_id:
            return None

        # Skip bot messages
        if sender.get("sender_type") == "app":
            return None

        content_raw = message.get("content", "")
        if not content_raw:
            return None

        message_type = message.get("message_type", "")

        msg_id = message.get("message_id", "")
        create_time = message.get("create_time", "")
        sender_id = sender.get("sender_id", {}) or {}
        open_id = sender_id.get("open_id", "")

        parsed_content = {}
        try:
            parsed_content = json.loads(content_raw)
        except (json.JSONDecodeError, TypeError):
            pass

        base = {
            "id": msg_id,
            "time": create_time,
            "root_id": "",
            "sender": open_id,
            "chat_id": chat_id,
            "msg_type": message_type,
        }

        if message_type == "text":
            text = content_raw
            if isinstance(parsed_content, dict):
                text = parsed_content.get("text", content_raw)
            if not text.strip():
                return None
            base["content"] = text
            return base

        elif message_type == "image":
            image_key = ""
            if isinstance(parsed_content, dict):
                image_key = parsed_content.get("image_key", "")
            if not image_key:
                return None
            base["content"] = f"[Image: {image_key}]"
            base["image_key"] = image_key
            return base

        elif message_type == "file":
            file_key = ""
            file_name = ""
            if isinstance(parsed_content, dict):
                file_key = parsed_content.get("file_key", "")
                file_name = parsed_content.get("file_name", "")
            if not file_key:
                return None
            base["content"] = f"[File: {file_name or file_key}]"
            base["file_key"] = file_key
            base["file_name"] = file_name
            return base

        elif message_type == "audio":
            file_key = ""
            if isinstance(parsed_content, dict):
                file_key = parsed_content.get("file_key", "")
            if not file_key:
                return None
            base["content"] = "[Audio]"
            base["file_key"] = file_key
            return base

        elif message_type == "media":
            # Media messages contain file_key (video) + optional image_key (cover)
            file_key = ""
            if isinstance(parsed_content, dict):
                file_key = parsed_content.get("file_key", "")
            if not file_key:
                return None
            base["content"] = "[Video/Media]"
            base["file_key"] = file_key
            return base

        elif message_type == "sticker":
            image_key = ""
            if isinstance(parsed_content, dict):
                image_key = parsed_content.get("image_key", "")
            if not image_key:
                return None
            base["content"] = "[Sticker]"
            base["image_key"] = image_key
            return base

        elif message_type == "post":
            # Rich text — extract plain text summary
            text_parts = []
            if isinstance(parsed_content, dict):
                for lang_key in ("zh_cn", "en_us", "ja_jp"):
                    lang_data = parsed_content.get(lang_key, {})
                    if isinstance(lang_data, dict):
                        paragraphs = lang_data.get("content", [])
                        for para in paragraphs:
                            if isinstance(para, list):
                                for elem in para:
                                    if isinstance(elem, dict):
                                        text_parts.append(
                                            elem.get("text", "")
                                        )
            summary = "".join(text_parts) if text_parts else "[Rich Text]"
            base["content"] = summary
            return base

        else:
            # Unknown type — skip
            return None

    def close(self):
        if self._fp and not self._fp.closed:
            try:
                self._fp.close()
            except Exception:
                pass
        self._fp = None


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

        def _do_get_token():
            resp = requests.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": app_id, "app_secret": app_secret},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                raise Exception(f"Token request failed: {data.get('msg', data)}")
            return data["tenant_access_token"]

        ok, result = api_call_with_retry(
            _do_get_token, max_retries=3, log_file=getattr(self.config, "log_file", None),
            label="get_token",
        )
        if not ok:
            raise Exception(f"Failed to get token after retries: {result}")
        self._token = result
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

        def _do_get():
            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                raise Exception(f"Read messages failed: {data.get('msg', data)}")
            return data.get("data", {}).get("items", [])

        ok, result = api_call_with_retry(
            _do_get, max_retries=2, base_delay=1,
            log_file=getattr(self.config, "log_file", None),
            label="get_messages",
        )
        if not ok:
            log(f"get_messages failed: {result}")
            return []
        return result

    def send_message(self, text, root_id=None):
        max_retries = 3
        for attempt in range(max_retries + 1):
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
            try:
                resp = requests.post(
                    "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                    headers=headers,
                    json=payload,
                    timeout=10,
                )
            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt < max_retries:
                    delay = 2 * (2 ** attempt)
                    log(f"  Send msg network err, retry {attempt+1}/{max_retries} in {delay}s: {e}")
                    time.sleep(delay)
                    self.expel_token()
                    continue
                log(f"Send msg failed after {max_retries} retries: {e}")
                return
            if resp.status_code in (429,) or resp.status_code >= 500:
                if attempt < max_retries:
                    delay = 2 * (2 ** attempt)
                    if resp.status_code == 429:
                        retry_after = resp.headers.get("Retry-After")
                        if retry_after:
                            delay = max(delay, int(retry_after))
                    log(f"  Send msg HTTP {resp.status_code}, retry {attempt+1}/{max_retries} in {delay}s")
                    time.sleep(delay)
                    self.expel_token()
                    continue
                log(f"Send msg HTTP {resp.status_code} after {max_retries} retries: {resp.text[:200]}")
                return
            if resp.status_code != 200:
                log(f"Send msg HTTP {resp.status_code}: {resp.text[:200]}")
                return
            data = resp.json()
            if data.get("code") != 0:
                log(f"Send msg failed: {data.get('msg', data.get('code'))}")
            return

    def is_bot_message(self, msg):
        sender = msg.get("sender", {})
        sender_type = sender.get("sender_type", "")
        sender_id = sender.get("id", "")
        return sender_type == "app" and sender_id == self.config.bot_app_id

    def download_resource(self, message_id, file_key, resource_type="image"):
        """Download an image or file attached to a message.

        Args:
            message_id: Feishu message ID (om_xxx).
            file_key: Resource key from message content.
            resource_type: "image" or "file".

        Returns:
            bytes content on success, None on failure.
        """
        def _do_download():
            token = self.get_token()
            url = (
                f"https://open.feishu.cn/open-apis/im/v1/messages/"
                f"{message_id}/resources/{file_key}?type={resource_type}"
            )
            resp = requests.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
            if resp.status_code == 200:
                return resp.content
            resp.raise_for_status()
            return None

        ok, result = api_call_with_retry(
            _do_download, max_retries=2, base_delay=1,
            log_file=getattr(self.config, "log_file", None),
            label=f"download_{resource_type}",
        )
        if not ok:
            log(f"Download {resource_type} failed: {result}")
            return None
        return result


def call_claude(prompt, session_id=None, timeout=300):
    """
    Call Claude CLI with tool use support.
    Returns (response_text, session_id, cost_usd, num_turns).
    """
    max_retries = 2
    for attempt in range(max_retries + 1):
        cmd = [
            "claude", "-p",
            "--output-format", "json",
            "--permission-mode", "bypassPermissions",
        ]
        if session_id:
            cmd.extend(["--resume", session_id])
        cmd.append(prompt)

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env={**os.environ},
                shell=sys.platform == "win32",
            )
        except subprocess.TimeoutExpired:
            if attempt < max_retries:
                log(f"  Claude timeout, retry {attempt+1}/{max_retries} with {timeout}s timeout")
                continue
            raise

        if result.returncode != 0:
            error_msg = result.stderr.strip()[:500] if result.stderr else "Unknown error"
            # Retry without session if session expired
            if session_id and ("session" in error_msg.lower() or "not found" in error_msg.lower()):
                log(f"  Session {session_id[:8]}... expired, starting new session")
                return call_claude(prompt, session_id=None, timeout=timeout)
            # Retry on transient failures
            if attempt < max_retries:
                delay = 2 * (2 ** attempt)
                log(f"  Claude error (code={result.returncode}), retry {attempt+1}/{max_retries} in {delay}s")
                time.sleep(delay)
                continue
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

    return "Claude error: max retries exceeded", None, 0, 0


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




def check_heartbeat(client, config, log_file):
    """Run quick connectivity checks and return (ok, message)."""
    issues = []

    # 1. Check events.jsonl file
    if config.events_file:
        if os.path.exists(config.events_file):
            try:
                fsize = os.path.getsize(config.events_file)
                mtime = os.path.getmtime(config.events_file)
                age = time.time() - mtime
                if age > 300:
                    issues.append(f"events.jsonl stale ({int(age)}s since last write)")
                log(f"  Heartbeat: events.jsonl OK ({fsize/1024:.0f}KB, {age:.0f}s old)", log_file)
            except Exception as e:
                issues.append(f"events.jsonl check: {e}")
        else:
            issues.append("events.jsonl not found")

    # 2. Try a lightweight Feishu API call (get_token)
    try:
        t0 = time.time()
        _ = client.get_token()
        latency = time.time() - t0
        log(f"  Heartbeat: Feishu API OK ({latency:.1f}s)", log_file)
    except Exception as e:
        issues.append(f"Feishu API: {e}")

    # 3. Check Claude CLI availability
    try:
        t0 = time.time()
        r = subprocess.run(
            ["claude", "--version"],
            capture_output=True, timeout=10,
            env={**os.environ},
            shell=sys.platform == "win32",
        )
        latency = time.time() - t0
        claude_ver = r.stdout.strip()[:50] if r.stdout else "?"
        log(f"  Heartbeat: Claude CLI OK ({claude_ver}, {latency:.1f}s)", log_file)
    except Exception as e:
        issues.append(f"Claude CLI: {e}")

    if issues:
        return False, "; ".join(issues)
    return True, "all checks passed"


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

    # ── Graceful shutdown handler (closure so config/state/watcher are in scope) ──
    def _shutdown(signum=None, frame=None):
        log("Shutting down gracefully...", log_file)
        try:
            save_state(config.state_file, state)
        except Exception:
            pass
        try:
            if watcher:
                watcher.close()
        except Exception:
            pass
        try:
            pid_file = os.path.join(os.path.dirname(os.path.abspath(config.state_file)) or ".", "bridge.pid")
            if os.path.exists(pid_file):
                os.remove(pid_file)
        except Exception:
            pass
        sys.exit(0)

    try:
        import signal
        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)
    except Exception:
        pass

    # ── PID lock (prevent dual instances) ──
    global PID_FILE
    PID_FILE = os.path.join(os.path.dirname(os.path.abspath(config.state_file)) or ".", "bridge.pid")
    if not acquire_pid_lock(PID_FILE, log_file):
        sys.exit(1)

    first_run = state["last_msg_id"] is None

    # Error tracking for circuit-breaker
    consecutive_errors = 0
    MAX_CONSECUTIVE_ERRORS = 10

    # Heartbeat interval
    last_heartbeat_time = 0
    HEARTBEAT_INTERVAL = 300  # every 5 minutes

    # EventFileWatcher: monitor feishu-user-plugin's events.jsonl sidecar
    watcher = None
    use_events = bool(config.events_file and os.path.exists(config.events_file))
    if config.events_file:
        watcher = EventFileWatcher(
            config.events_file, config.group_id, config.bot_app_id
        )
        if use_events:
            log(f"EventFileWatcher enabled: {config.events_file}", log_file)
        else:
            log(
                f"Events file not found ({config.events_file}), using polling fallback",
                log_file,
            )
    else:
        log("No events_file configured, using polling", log_file)

    # Track consecutive EventFileWatcher failures before switching to polling
    events_fail_count = 0
    MAX_EVENTS_FAIL = 3

    # Track how long since last EventFileWatcher re-check (polling mode only)
    last_events_recheck = 0
    EVENTS_RECHECK_INTERVAL = 30  # seconds

    while True:
        try:
            new_msgs = []

            # ── Phase 1: Collect new messages ──
            # Runtime dedup set: prevents cross-path duplicates within a single cycle
            seen_this_cycle = set()

            if use_events:
                # Fast path: read from events.jsonl (WebSocket-backed)
                raw_events = watcher.poll() if watcher else None
                if raw_events is None:
                    events_fail_count += 1
                    if events_fail_count >= MAX_EVENTS_FAIL:
                        log(
                            f"EventFileWatcher failed {MAX_EVENTS_FAIL}x, switching to polling",
                            log_file,
                        )
                        use_events = False
                        last_events_recheck = time.time()
                else:
                    events_fail_count = 0
                    log(f"EventFileWatcher: {len(raw_events)} raw event(s), processed_ids={len(state['processed_ids'])}", log_file)
                    for ev in raw_events:
                        is_dup = ev["id"] in state["processed_ids"] or ev["id"] in seen_this_cycle
                        log(f"  ev id={ev['id'][-16:]} content={ev['content'][:50]} dup={is_dup}", log_file)
                        if not is_dup:
                            seen_this_cycle.add(ev["id"])
                            new_msgs.append(ev)

            if not use_events:
                # Fall back to polling
                if watcher and not last_events_recheck:
                    # First failure — start recheck timer
                    last_events_recheck = time.time()
                elif watcher and time.time() - last_events_recheck > EVENTS_RECHECK_INTERVAL:
                    # Try re-enabling EventFileWatcher
                    log("Rechecking events.jsonl...", log_file)
                    watcher = EventFileWatcher(
                        config.events_file, config.group_id, config.bot_app_id
                    )
                    test = watcher.poll()
                    if test is not None:
                        log("Events file recovered, re-enabling EventFileWatcher", log_file)
                        use_events = True
                        events_fail_count = 0
                        last_events_recheck = 0
                        # Process any events that arrived during polling window
                        for ev in test:
                            if ev["id"] not in state["processed_ids"] and ev["id"] not in seen_this_cycle:
                                seen_this_cycle.add(ev["id"])
                                new_msgs.append(ev)
                    else:
                        last_events_recheck = time.time()

                # REST polling (only if no messages found yet this cycle)
                if not new_msgs:
                    messages = client.get_messages()
                    if messages:
                        for msg in reversed(messages):
                            msg_id = msg.get("message_id", "")
                            msg_type = msg.get("msg_type", "")
                            if msg_type == "system":
                                continue
                            if client.is_bot_message(msg):
                                continue
                            if msg_id in state["processed_ids"] or msg_id in seen_this_cycle:
                                continue
                            content_raw = msg.get("body", {}).get("content", "")
                            root_id = msg.get("root_id", "") or ""
                            sender_id = msg.get("sender", {}).get("id", "")

                            parsed = {}
                            try:
                                parsed = json.loads(content_raw) if content_raw else {}
                            except (json.JSONDecodeError, TypeError):
                                pass

                            entry = {
                                "id": msg_id,
                                "msg_type": msg_type,
                                "time": msg.get("create_time", ""),
                                "root_id": root_id,
                                "sender": sender_id,
                            }

                            if msg_type == "text":
                                text = content_raw
                                if isinstance(parsed, dict):
                                    text = parsed.get("text", content_raw)
                                if not text or not text.strip():
                                    continue
                                entry["content"] = text

                            elif msg_type == "image":
                                ik = parsed.get("image_key", "") if isinstance(parsed, dict) else ""
                                if not ik:
                                    continue
                                entry["content"] = f"[Image: {ik}]"
                                entry["image_key"] = ik

                            elif msg_type == "file":
                                fk = parsed.get("file_key", "") if isinstance(parsed, dict) else ""
                                fn = parsed.get("file_name", "") if isinstance(parsed, dict) else ""
                                if not fk:
                                    continue
                                entry["content"] = f"[File: {fn or fk}]"
                                entry["file_key"] = fk
                                entry["file_name"] = fn

                            elif msg_type == "audio":
                                fk = parsed.get("file_key", "") if isinstance(parsed, dict) else ""
                                if not fk:
                                    continue
                                entry["content"] = "[Audio]"
                                entry["file_key"] = fk

                            elif msg_type == "media":
                                fk = parsed.get("file_key", "") if isinstance(parsed, dict) else ""
                                if not fk:
                                    continue
                                entry["content"] = "[Video/Media]"
                                entry["file_key"] = fk

                            elif msg_type in ("sticker",):
                                ik = parsed.get("image_key", "") if isinstance(parsed, dict) else ""
                                if not ik:
                                    continue
                                entry["content"] = "[Sticker]"
                                entry["image_key"] = ik

                            elif msg_type == "post":
                                text_parts = []
                                if isinstance(parsed, dict):
                                    for lang_key in ("zh_cn", "en_us", "ja_jp"):
                                        lang_data = parsed.get(lang_key, {})
                                        if isinstance(lang_data, dict):
                                            paras = lang_data.get("content", [])
                                            for para in paras:
                                                if isinstance(para, list):
                                                    for elem in para:
                                                        if isinstance(elem, dict):
                                                            text_parts.append(elem.get("text", ""))
                                summary = "".join(text_parts) if text_parts else ""
                                if not summary.strip():
                                    entry["content"] = "[Rich Text]"
                                else:
                                    entry["content"] = summary

                            else:
                                continue

                            seen_this_cycle.add(msg_id)
                            new_msgs.append(entry)

            # ── Phase 2: Process messages ──
            if first_run and new_msgs:
                if use_events:
                    # EventFileWatcher seeks to end on open → all events are new.
                    # Process everything, just clear first_run flag.
                    first_run = False
                    log(
                        f"First run (events mode) — processing {len(new_msgs)} event(s)",
                        log_file,
                    )
                else:
                    # Polling mode: skip old messages, sync to latest.
                    last = new_msgs[-1]
                    state["last_msg_id"] = last["id"]
                    state["processed_ids"] = [last["id"]]
                    save_state(config.state_file, state)
                    first_run = False
                    log(f"First run — synced to latest message: {last['id'][-16:]}", log_file)
                    new_msgs = []  # Don't process old messages

            if new_msgs:  # Note: NOT elif — first_run events-mode also needs processing
                for msg in new_msgs:
                    ts = safe_format_time(msg["time"])
                    sender_open_id = msg.get("sender", "")
                    thread_id = (
                        f"thread:{msg['root_id']}:{sender_open_id}"
                        if msg["root_id"]
                        else f"user:{sender_open_id}"
                    )
                    log(f"New message ({ts}) [{thread_id[-16:]}]: {msg['content'][:200]}", log_file)

                    sid = get_session_id(state, thread_id)
                    session_hint = " (resuming)" if sid else ""
                    client.send_message(
                        f"⏳ Processing{session_hint}...",
                        root_id=msg["root_id"] or None,
                    )
                    log("Progress indicator sent", log_file)

                    # Mark as processed BEFORE Claude call to prevent duplicate processing
                    # during the long Claude response window (5-10s)
                    state["processed_ids"].append(msg["id"])
                    if len(state["processed_ids"]) > 100:
                        state["processed_ids"] = state["processed_ids"][-100:]
                    save_state(config.state_file, state)

                    # Build prompt with context about media type
                    msg_type = msg.get("msg_type", "text")
                    text_content = msg["content"]

                    # For non-text messages, add context prefix
                    if msg_type == "image":
                        text_content = (
                            f"[User sent an image. " +
                            (f"Image key: {msg.get('image_key', '')}] " if msg.get('image_key') else "] ") +
                            text_content
                        )
                    elif msg_type == "file":
                        text_content = (
                            f"[User sent a file. " +
                            (f"Name: {msg.get('file_name', 'unknown')}] " if msg.get('file_name') else "] ") +
                            text_content
                        )

                    log(f"Calling Claude CLI{' (resume ' + sid[:8] + '...)' if sid else ' (new session)'}", log_file)

                    try:
                        response, new_sid, cost, turns = call_claude(
                            text_content,
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

                    if new_sid:
                        set_session_id(state, thread_id, new_sid)
                        if len(state["sessions"]) > config.max_threads:
                            keys = list(state["sessions"].keys())
                            for k in keys[: -config.max_threads]:
                                del state["sessions"][k]

                    reply = format_response(response, cost, turns)

                    MAX_LEN = 30000
                    if len(reply) > MAX_LEN:
                        reply = reply[:MAX_LEN] + "\n\n...(truncated)"

                    try:
                        client.send_message(reply, root_id=msg["root_id"] or None)
                        log("Response sent to group", log_file)
                    except Exception as e:
                        log(f"Failed to send response: {e}", log_file)

                    state["last_msg_id"] = msg["id"]
                    save_state(config.state_file, state)

        except KeyboardInterrupt:
            log("Shutdown by user", log_file)
            _shutdown()
            break
        except Exception as e:
            consecutive_errors += 1
            log(f"Error #{consecutive_errors}: {e}", log_file)
            log(traceback.format_exc(), log_file)
            client.expel_token()

            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                log(f"Too many consecutive errors ({consecutive_errors}), running heartbeat...", log_file)
                hb_ok, hb_msg = check_heartbeat(client, config, log_file)
                log(f"Heartbeat result: {hb_msg}", log_file)
                if hb_ok:
                    log("Heartbeat OK, resetting error count", log_file)
                    consecutive_errors = 0
                else:
                    log(f"Heartbeat FAILED ({hb_msg}), waiting 30s before retry...", log_file)
                    time.sleep(30)
                    consecutive_errors = max(0, consecutive_errors - 2)  # gradual recovery

        # ── Periodic heartbeat check ──
        if time.time() - last_heartbeat_time > HEARTBEAT_INTERVAL:
            last_heartbeat_time = time.time()
            log("Running periodic heartbeat check...", log_file)
            try:
                hb_ok, hb_msg = check_heartbeat(client, config, log_file)
                if hb_ok:
                    log(f"Health check passed: {hb_msg}", log_file)
                    consecutive_errors = max(0, consecutive_errors - 1)  # one good cycle
                else:
                    log(f"Health check WARNING: {hb_msg}", log_file)
                    client.expel_token()  # force refresh on next API call
            except Exception as hb_e:
                log(f"Health check error: {hb_e}", log_file)

        # ── events.jsonl log rotation check (every ~200 iterations in events mode) ──
        if use_events and config.events_file:
            _rot_check_counter = getattr(main, "_rot_check_counter", 0) + 1
            main._rot_check_counter = _rot_check_counter
            if _rot_check_counter % 200 == 0:
                try:
                    fsize = os.path.getsize(config.events_file)
                    if fsize > MAX_EVENTS_FILE_SIZE:
                        log(f"events.jsonl {fsize/1024/1024:.1f}MB > {MAX_EVENTS_FILE_SIZE/1024/1024:.0f}MB, truncating", log_file)
                        if watcher:
                            watcher.close()
                        with open(config.events_file, "w", encoding="utf-8") as f:
                            f.truncate(0)
                        if use_events:
                            watcher = EventFileWatcher(config.events_file, config.group_id, config.bot_app_id)
                            _test = watcher.poll()
                            log(f"  events.jsonl truncated, watcher test: {'OK' if _test is not None else 'FAIL'}", log_file)
                except Exception as e:
                    log(f"  events.jsonl rotation check: {e}", log_file)

        # Adaptive sleep: shorter in events mode, standard polling interval otherwise
        sleep_sec = 0.5 if use_events else config.poll_interval
        time.sleep(sleep_sec)


if __name__ == "__main__":
    main()
