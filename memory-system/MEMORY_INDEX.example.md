# Memory Index

This is an example MEMORY.md. When Claude starts a session, it reads this file (first ~200 lines) to learn what context is available. Each entry is a link to a memory file with a one-line description.

## User Profile (explicit/)
- [My Profile](explicit/user-profile.md) — 姓名、角色、专业领域（仅存用户明确要求记住的信息）

## Behavior Rules (key/)
- [记忆策略](key/feedback_memory_policy.md) — 事实信息需明确指令才记，行为规则可自然适配
- [删除确认规则](key/feedback_delete_confirmation.md) — 删除前必须先列清单给用户确认
- [中文回答](key/feedback_answer_in_chinese.md) — 用户要求用中文回答问题

## Project Context (key/)
- [当前项目](key/current-project.md) — 项目名称、技术栈、里程碑、团队规模
- [活跃分支](key/active-branch.md) — 当前正在开发的分支和目标

## External References (key/)
- [Bug 追踪](key/bug-tracker.md) — bugs 在 Linear 项目 "INGEST" 中追踪
- [监控面板](key/monitoring.md) — Grafana 延迟面板用于 oncall

## Guidelines

- Keep entries under 150 characters
- Group by topic (User / Feedback / Project / Reference)
- Update entries when context changes
- Remove stale or completed entries
