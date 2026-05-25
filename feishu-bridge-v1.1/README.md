# Feishu ↔ Claude Bridge v1.1

Bridge messages from a Feishu (飞书) group chat to Claude CLI and back. Send a message in your Feishu group, get a Claude-powered reply — with conversation context across messages.

## v1.1 New: Conversation Context

Unlike v1.0 which treated every message independently, **v1.1 maintains dialogue history** across exchanges. Claude remembers what was said before, enabling multi-turn conversations and follow-up questions.

- History stored in `.feishu_conversation_history.json` (auto-managed)
- Automatically trimmed to the last 15 exchanges to prevent token overflow
- Long responses are truncated to 2000 chars in stored history (full response still sent)
- Previous context is prepended as a Chinese preamble before each new message

## How It Works

```
You → Feishu Group → Feishu API → bridge.py → Claude CLI → bridge.py → Feishu API → Group
```

The bridge polls the Feishu group for new messages every N seconds, feeds them to `claude -p "..."`, and posts the response back to the group.

## Prerequisites

- **Python 3.8+** — for running the bridge script
- **Claude Code CLI** — `npm install -g @anthropic-ai/claude-code`
- **Feishu Bot App** — you need to create one at [Feishu Open Platform](https://open.feishu.cn/app)

## Files

| File | Purpose |
|------|---------|
| `bridge.py` | Main bridge script — polls Feishu, calls Claude, replies |
| `config.json` | Your configuration (ignored by git, never commit) |
| `config.example.json` | Configuration template |
| `install.ps1` | Interactive setup script |
| `start.vbs` | Silent Windows launcher (no console window) |
| `.feishu_bridge_state.json` | Tracks processed messages (auto-generated) |
| `.feishu_conversation_history.json` | Conversation context history (auto-generated) |
| `bridge_debug.log` | Debug log (auto-generated) |

## Quick Start

```bash
# 1. Clone and install dependencies
pip install requests

# 2. Run the setup script (interactive)
pwsh ./install.ps1

# 3. Start the bridge
python bridge.py
```

Send `hello` in your Feishu group. If the bridge is running, you'll get a reply.

## Manual Configuration

### 1. Credentials

Create `~/.feishu-user-plugin/credentials.json`:
```json
{
  "profiles": {
    "default": {
      "LARK_APP_ID": "cli_xxxxxxxxxxxxxxxxxxxx",
      "LARK_APP_SECRET": "your-app-secret-here"
    }
  }
}
```

### 2. Config

Copy `config.example.json` to `config.json` and fill in:

| Field | Description |
|-------|-------------|
| `group_id` | Group chat ID (`oc_xxxx...`) |
| `bot_app_id` | Bot App ID (`cli_xxxx...`) |
| `poll_interval` | Poll interval in seconds (default 3) |
| `state_file` | Path to state file (default `.feishu_bridge_state.json`) |
| `log_file` | Path to debug log (default `bridge_debug.log`) |
| `credentials_file` | Path to credentials (leave empty for default) |
| `history_file` | Path to conversation history (default `.feishu_conversation_history.json`) |
| `max_history_rounds` | Max conversation turns kept in context (default 15) |

### 3. Start

```bash
python bridge.py
```

Or double-click `start.vbs` to run silently (Windows).

## Auto-Start on Login (Windows)

Run the install script and choose "yes" for auto-start:
```powershell
.\install.ps1
```

This creates a scheduled task that launches the bridge silently at login.

## Troubleshooting

**No reply in group?**
- Check the bridge is running (`tasklist | grep python`)
- Check `bridge_debug.log` for errors
- Verify the bot is added to the group and has `im:message` scope
- Make sure `config.json` has the correct `group_id`

**"获取 token 失败" / Token errors**
- Verify `credentials.json` has correct App ID / App Secret
- Check Feishu app is published and enabled

**Claude not responding**
- Run `claude -p "test"` manually to verify Claude CLI works
- Check Claude is authenticated (`claude` → login if needed)

**Windows encoding issues**
- The bridge sets `PYTHONIOENCODING=utf-8` automatically
- If you see garbled output, set it manually: `$env:PYTHONIOENCODING='utf-8'`

## Limits

- Claude CLI timeout: 120 seconds per message
- Feishu message length limit: ~30000 characters
- Responses longer than 30000 chars are truncated
- History trimmed to the last 15 rounds (configurable via `max_history_rounds`)
- The bridge processes one message at a time, oldest first

## License

MIT
