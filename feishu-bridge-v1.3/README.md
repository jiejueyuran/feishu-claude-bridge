# Feishu ↔ Claude Code Bridge v1.3

将飞书群聊与 Claude Code CLI 连接起来。在飞书群里发消息，自动获得 Claude 的回复——支持多轮对话、工具调用（Bash/Read/Write/浏览器操作）、图片/文件消息、用户隔离和费用追踪。

## 架构

```
┌─────────┐   WebSocket    ┌──────────────────┐   subprocess    ┌────────────┐
│  Feishu  │ ←──────────→ │  bridge.py        │ ←────────────→ │ Claude CLI │
│  群聊    │   (events    │  EventFileWatcher │  (claude -p     │   (AI)     │
│          │    .jsonl)   │  + polling 备选    │   --resume)     │            │
└─────────┘               └──────────────────┘                 └────────────┘
                                 │
                          ┌──────┴──────┐
                          │ bridge_debug │
                          │  .log 状态文件 │
                          └─────────────┘
```

### 两种消息接收模式

| 模式 | 方式 | 延迟 | API 配额消耗 |
|------|------|------|-------------|
| **WebSocket (默认)** | 读取 `feishu-user-plugin` 的 `events.jsonl` | ~0.5s | 无需 REST API |
| **Polling (备选)** | 每 3s 轮询飞书 REST API | ~3-6s | 消耗配额 |

当 WebSocket 文件不可用时自动降级为 polling，并每 30s 尝试恢复。

## 功能特性

- **多轮对话** — 每个用户自动获得独立会话，支持 `claude --resume`，30 分钟不活动自动过期
- **用户隔离** — 不同用户的消息自动路由到不同会话，互不干扰
- **工具调用** — Claude 可在回复前执行 Bash/Read/Write/Playwright 等操作
- **多消息类型** — 支持文本、图片、文件、音频、视频、富文本等消息类型
- **费用追踪** — 每次回复附带 Token 消耗费用（USD）
- **心跳检测** — 每 5 分钟自动检查飞书 API、Claude CLI、events.jsonl 连通性
- **指数退避重试** — 网络错误、HTTP 429/5xx 自动重试，最多 3 次
- **PID 锁** — 防止同一目录下运行多个实例
- **events.jsonl 自动轮转** — 超过 10MB 自动截断
- **并发限制** — 最多保持 20 个活跃会话，超出时淘汰最旧的

## 前置要求

- **Python 3.8+**
- **Claude Code CLI** — `npm install -g @anthropic-ai/claude-code`
- **飞书自建应用** — 在 [飞书开放平台](https://open.feishu.cn/app) 创建

### 飞书应用配置

1. 前往 [飞书开放平台](https://open.feishu.cn/app) → 创建应用 → 类型选择"机器人"
2. **权限管理** → 添加 `im:message` 和 `im:resource` 权限
3. **安全设置** → 获取 App ID 和 App Secret
4. **事件订阅** → 不需要配置（通过 polling 或 feishu-user-plugin WebSocket）
5. **应用功能** → 开启机器人能力
6. 发布应用（版本管理 → 创建版本 → 发布）
7. 将机器人拉入群聊：群设置 → 机器人 → 添加机器人

## 快速开始

```bash
# 1. 安装依赖
pip install requests

# 2. 配置 feishu-user-plugin（可选，推荐—开启 WebSocket 模式）
npx feishu-user-plugin setup --profile default \
  --app-id cli_xxx --app-secret xxx
npx feishu-user-plugin oauth  # 获取 UAT

# 3. 配置 config.json
# 复制 config.example.json → config.json，填写群 ID

# 4. 启动
python bridge.py
```

如果 `feishu-user-plugin` 未安装，桥接自动降级为 REST API polling 模式。

## 配置文件

### `config.json`

```json
{
    "group_id": "oc_xxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "bot_app_id": "cli_xxxxxxxxxxxxxxxxxxxx",
    "poll_interval": 3,
    "state_file": ".feishu_bridge_state.json",
    "log_file": "bridge_debug.log",
    "credentials_file": ""
}
```

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `group_id` | 飞书群聊 ID（以 `oc_` 开头） | 必填 |
| `bot_app_id` | 飞书应用 ID（`cli_xxx`） | 必填 |
| `poll_interval` | polling 间隔（秒） | 3 |
| `state_file` | 状态文件路径 | `.feishu_bridge_state.json` |
| `log_file` | 调试日志路径 | `bridge_debug.log` |
| `credentials_file` | 凭据文件路径（空则使用默认路径） | `~/.feishu-user-plugin/credentials.json` |
| `claude_timeout` | Claude CLI 超时（秒） | 300 |
| `max_threads` | 最大并发会话数 | 20 |

### 凭据文件

默认路径 `~/.feishu-user-plugin/credentials.json`：

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

## 开机自启

使用 `install.ps1` 脚本创建计划任务：

```powershell
.\install.ps1
```

或者手动创建计划任务：

```bash
schtasks /create /tn "FeishuBridge" /tr "python C:\path\to\bridge.py" /sc onlogon /delay 0000:30
```

## 文件说明

| 文件 | 说明 |
|------|------|
| `bridge.py` | 主桥接脚本 |
| `config.json` | 配置文件（不提交 git） |
| `config.example.json` | 配置模板 |
| `install.ps1` | 安装/开机自启脚本 |
| `start.vbs` | Windows 静默启动（无窗口） |
| `.feishu_bridge_state.json` | 消息处理状态（自动生成） |
| `bridge_debug.log` | 调试日志（自动生成） |
| `bridge_output.log` | Claude CLI 输出日志（自动生成） |
| `bridge.pid` | PID 锁文件（自动生成） |

## 消息类型支持

| 类型 | 支持 | 说明 |
|------|------|------|
| 文本 | ✓ | 正常转发到 Claude |
| 图片 | ✓ | 标记 `[Image: key]` 传递给 Claude |
| 文件 | ✓ | 标记 `[File: name]` 传递给 Claude |
| 富文本 | ✓ | 自动提取纯文本摘要 |
| 音频 | ✓ | 标记 `[Audio]` 传递给 Claude |
| 视频 | ✓ | 标记 `[Video/Media]` 传递给 Claude |
| 表情 | ✓ | 标记 `[Sticker]` 传递给 Claude |
| 系统消息 | ✗ | 自动忽略 |

## Luminary Summit 2025

桥接启动后，在群聊中发送任意消息即可。Claude 默认可以执行 Bash/Read/Write 等操作。

- 每个用户首次发送消息会创建新会话
- 后续消息自动继续同一会话（30 分钟内）
- 用户之间会话完全隔离
- 回复末尾会显示费用和对话轮次

## 故障排除

**群内无回复？**
- 检查桥接是否运行：`tasklist | findstr python`
- 查看日志：`bridge_debug.log`
- 确认机器人已加入群聊且有 `im:message` 权限
- 确认 `config.json` 中 `group_id` 正确

**Token 错误**
- 检查凭据文件中的 App ID / App Secret 是否正确
- 确认飞书应用已发布

**Claude 无响应**
- 手动测试：`claude -p "hello" --permission-mode bypassPermissions --output-format json`
- 检查 Claude 是否已登录和授权

**图片/文件消息不显示**
- 图片和文件消息默认只在提示词中标记为 `[Image: ...]` 或 `[File: ...]`
- Claude 无法直接查看图片内容（仅识别到有图片的事实）

**Windows 编码问题**
- 桥接自动设置 `PYTHONIOENCODING=utf-8`
- 如果仍有乱码，手动设置：`$env:PYTHONIOENCODING='utf-8'`

**端口冲突 / 桥接启动失败**
- 删除 `bridge.pid` 后重试
- 检查是否有其他 Python 进程占用

## 限制

- Claude CLI 超时：300 秒（可在配置中调整）
- 飞书消息长度限制：约 30000 字符
- 超过 30000 字符的回复会被截断
- 最多 20 个活跃会话（超出时淘汰最旧的）
- 会话 30 分钟不活动自动过期

## License

MIT
