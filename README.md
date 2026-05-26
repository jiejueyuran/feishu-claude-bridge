# Feishu ↔ Claude Code Bridge

将飞书群消息转发到 Claude CLI 并自动回复，支持多版本迭代。

## 版本

- [v1.4](feishu-bridge-v1.4/) — **最新** API重试层 + 心跳检测 + 图片/文件消息支持 + 熔断恢复 + 优雅关闭
- [v1.3](feishu-bridge-v1.3/) — 工具调用 + 多轮会话 + 用户隔离 + Session过期(30min) + 费用追踪
- [v1.2](feishu-bridge-v1.2/) — 线程级上下文隔离 + 动态字符预算裁剪 + 网络重试
- [v1.1](feishu-bridge-v1.1/) — 多轮对话上下文（固定6轮）
- [v1.0](feishu-bridge-v1.0/) — 基础消息收发

## 项目结构

```
feishu-claude-bridge/
├── feishu-bridge-v1.4/     # v1.4 — 最新版（API重试层 + 心跳 + 图片/文件消息 + 熔断恢复）
├── feishu-bridge-v1.3/     # v1.3 — 工具调用 + 多轮会话 + 用户隔离
├── feishu-bridge-v1.2/     # 线程隔离 + 动态预算
├── feishu-bridge-v1.1/     # 上下文记忆
└── feishu-bridge-v1.0/     # 基础版本
```
