# Claude Memory System — Reusable Framework

A structured persistent-memory framework for Claude Code sessions. Designed for developers who want Claude to remember context, preferences, and project state across conversations.

## How It Works

Claude's memory system is file-based. Each memory is a Markdown file with YAML frontmatter, indexed by a central `MEMORY.md`. On every conversation, Claude reads the index and loads relevant memories as context.

```
memory/
├── MEMORY.md              ← Index (always loaded, ~first 200 lines)
├── explicit/              ← User-requested memories (name, contacts, keys)
├── key/                   ← Auto-captured behavior rules & project context
└── *.md                   ← Individual memory files
```

## Memory Types

| Type | Purpose | Example |
|------|---------|---------|
| `user` | Role, goals, expertise level | "User is a UE5 game developer" |
| `feedback` | Behavior guidance | "Always ask before deleting files" |
| `project` | Ongoing work context | "Merging feature branch this week" |
| `reference` | External resource pointers | "Bugs tracked in Linear INGEST project" |

## Memory File Structure

Each memory file uses YAML frontmatter:

```markdown
---
name: short-kebab-slug
description: One-line summary for relevance matching
type: feedback                # user | feedback | project | reference
---

The memory content. For feedback/project types, structure as:
- The rule or fact
- **Why:** the motivation
- **How to apply:** when to use this

Link related memories: [[related-memory-name]]
```

## Index File (MEMORY.md)

The index is a flat list of links with one-line hooks:

```markdown
- [记忆策略](key/memory-policy.md) — 事实信息需明确指令才记，行为规则可自然适配
- [UE5游戏项目](game-project-example.md) — 《项目名》箱庭动作RPG，UE5.6蓝图
```

- Keep entries under ~150 chars
- First 200 lines loaded every session
- Organize semantically by topic, not chronologically

## What NOT to Save

- Code patterns or architecture — read from source
- Git history — `git log` is authoritative
- Bug fixes — the fix is in the code
- Ephemeral task state — use task lists, not memory

## Design Principles

1. **Discoverability** — MEMORY.md index makes everything findable
2. **Staleness** — memories decay; verify against current state before acting
3. **Bounded context** — index is self-limiting (200 lines ~ 30-40 entries)
4. **Explicit vs implicit** — user-requested saves go in `explicit/`, auto-captured goes in `key/`

## Integration with This Bridge

The memory system pairs naturally with the Feishu bridge: when Claude processes Feishu messages via `bridge.py`, it can use memory to maintain conversational continuity across sessions — remembering user preferences, ongoing tasks, and project context.

## Directory Template

```
memory-system/
├── README.md                   ← This file
├── MEMORY_INDEX.example.md     ← Example index
├── templates/
│   ├── memory-user.md          ← Template for user profile
│   ├── memory-feedback.md      ← Template for behavior rules
│   ├── memory-project.md       ← Template for project context
│   └── memory-reference.md     ← Template for external pointers
└── how-to-setup.md             ← Installation guide
```
