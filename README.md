# Feishu ↔ Claude Code Bridge v1.2

将飞书群消息转发到 Claude CLI 并自动回复，支持**线程级别隔离的多轮对话上下文**。

## 架构

```
用户在飞书群发消息
       │
       ▼
┌─────────────────┐     ┌──────────────────┐     ┌─────────────┐
│  bridge.py      │────▶│  Feishu API      │◀───▶│ 飞书群       │
│  (轮询 3s)      │     │  (BOT token)    │     │ (消息接收)   │
└────────┬────────┘     └──────────────────┘     └─────────────┘
         │
         ▼
┌─────────────────┐
│  claude -p       │  ← 每次调用传递上下文 prompt
│  (一次性执行)    │
└────────┬────────┘
         │
         ▼
┌─────────────────┐     ┌──────────────────┐
│  `.feishu_bridge │     │  线程历史         │
│  _state.json`    │     │  (thread_id→[])  │
└─────────────────┘     └──────────────────┘
```

## v1.2 新增功能

### 1. 线程级上下文隔离

飞书的"话题回复"（thread reply）各自拥有独立上下文；主聊天共用一个 `__main__` 上下文。

```
群聊消息（无 root_id）      → 共享 __main__ 上下文
回复某条消息（有 root_id）  → 走该线程独立上下文
```

### 2. 动态字符预算裁剪

旧版固定保留 N 轮对话，v1.2 改用字符预算：

- 默认 `max_context_chars: 4000` 字符预算
- 短对话 → 保留更多轮次
- 长回答 → 自动少保留几轮
- 始终在预算内塞入最多历史

### 3. 网络重试（指数退避）

所有飞书 API 调用带自动重试：

| 尝试次数 | 等待时间 |
|---------|---------|
| 第 1 次失败 | ~2s 重试 |
| 第 2 次失败 | ~4s 重试 |
| 第 3 次失败 | 抛出异常 |

- 4xx 错误不重试（认证失败等）
- 5xx 和网络超时才重试

### 4. 历史持久化

对话历史存储在 `.feishu_bridge_state.json`，桥接重启不丢失。存储 50 轮，prompt 构建时按预算动态裁剪。

## 配置文件

复制 `config.example.json` 为 `config.json`，按需修改：

```json
{
  "group_id": "oc_xxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "bot_app_id": "cli_xxxxxxxxxxxxxxxxxxxx",
  "poll_interval": 3,
  "state_file": ".feishu_bridge_state.json",
  "log_file": "bridge_debug.log",
  "credentials_file": "",
  "max_context_chars": 4000,
  "max_history_storage": 50,
  "max_threads": 20,
  "max_retries": 3,
  "retry_base_delay": 2,
  "main_thread_key": "__main__"
}
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `group_id` | — | 飞书群 chat_id |
| `bot_app_id` | — | 飞书机器人 app_id |
| `poll_interval` | 3 | 轮询间隔（秒） |
| `max_context_chars` | 4000 | 上下文 prompt 字符预算 |
| `max_history_storage` | 50 | 存储保留的最大轮次 |
| `max_threads` | 20 | 最多同时追踪的线程数 |
| `max_retries` | 3 | 网络错误重试次数 |
| `retry_base_delay` | 2 | 重试初始等待秒数 |

## 安装

```powershell
# 1. 安装依赖
pip install requests

# 2. 复制配置文件
copy config.example.json config.json
# 编辑 config.json 填入你的飞书机器人信息

# 3. 运行
python bridge.py

# 或双击 start.vbs 静默运行
```

或用 `install.ps1` 交互式安装：

```powershell
.\install.ps1
```

## 消息流示例

```
[用户]  UE5的Enhanced Input怎么用？
        │
        ▼ 桥接轮询检测到新消息
[Bot]   ⏳ 收到，Claude 处理中...
        │
        ▼ Claude CLI 执行（无历史，首次对话）
[Bot]   Enhanced Input 是 UE5 的新输入系统...
        └── 保存在 __main__ 线程历史（第1轮）

[用户]  给个跳跃的例子（同一话题，无线程）
        │
        ▼ 桥接读取 __main__ 历史（1轮）
[Bot]   在项目设置中启用 Enhanced Input...（知道上文）
        └── 保存在 __main__ 线程历史（第2轮）

[用户]  ▶ 回复上一条"在项目设置..."（形成线程）
        │
        ▼ 桥接读取该线程历史（独立于 __main__）
[Bot]   具体步骤：打开 Edit → Project Settings...
        └── 保存在该线程历史，与主聊天互不干扰
```

## 与 v1.0 / v1.1 的区别

| 特性 | v1.0 | v1.1 | v1.2 |
|------|------|------|------|
| 基础消息收发 | ✅ | ✅ | ✅ |
| 对话上下文 | ❌ | ✅ 固定6轮 | ✅ 动态预算 |
| 线程隔离 | ❌ | ❌ | ✅ |
| 网络重试 | ❌ | ❌ | ✅ |
| 可配置参数 | 硬编码 | config.json | config.json+ |
| 开机自启 | ✅ | ✅ | ✅ |

## 局限

- 每次调用 `claude -p` 是全新进程，上下文靠手动拼接历史实现
- 只支持文本消息（图片/文件/表情包被跳过）
- 每条消息 120 秒超时限制
- 4000 字符上下文预算（可通过 `max_context_chars` 调整）

## 文件结构

```
feishu-bridge-v1.2/
├── bridge.py             # 主程序
├── config.example.json   # 配置示例
├── install.ps1           # 安装脚本
├── start.vbs             # 静默启动
└── README.md             # 本文件
```
