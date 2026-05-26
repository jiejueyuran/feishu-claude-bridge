# Feishu ↔ Claude Code Bridge

将飞书群消息转发到 Claude CLI 并自动回复，支持多版本迭代。

## 版本

- [v1.2](feishu-bridge-v1.2/) — 线程级上下文隔离 + 动态字符预算裁剪 + 网络重试
- [v1.1](feishu-bridge-v1.1/) — 多轮对话上下文（固定6轮）
- [v1.0](feishu-bridge-v1.0/) — 基础消息收发

## 项目结构

```
feishu-claude-bridge/
├── feishu-bridge-v1.0/     # 初始版本
├── feishu-bridge-v1.1/     # 上下文记忆
├── feishu-bridge-v1.2/     # 线程隔离 + 动态预算
└── memory-system/          # 记忆系统
```
