# How to Set Up the Memory System

## Prerequisites

- Claude Code installed
- Your project directory (any Claude Code working directory)

## Quick Setup (5 Minutes)

### 1. Create the directory structure

```bash
# In your Claude Code working directory
mkdir -p .claude/projects/$(basename $(pwd))/memory/{explicit,key}
```

Or place it wherever your Claude project's memory directory is configured.

### 2. Create MEMORY.md index

Create `MEMORY.md` in your memory directory:

```markdown
# Memory Index

## User Profile (explicit/)
- [My Profile](explicit/user-profile.md) — Name, role, preferences

## Key Context (key/)
- [Project Info](key/project-info.md) — Current project goals and status
```

### 3. Add your first memory

`my-project-context.md`:
```markdown
---
name: my-project-context
description: 3D action RPG built in UE5.6
type: project
---

Project codenamed "Example", a sandbox action RPG.

**Why:** Contract deliverable due 2026-06-30.

**How to apply:** Suggest features that fit within a 3-person team scope.
```

### 4. Tell Claude about it

In your next session, Claude reads `MEMORY.md` automatically. For existing sessions, just reference the file path.

## File Lifecycle

**Adding a memory:**
1. Write the `.md` file with frontmatter
2. Add a one-line entry to `MEMORY.md`

**Updating a memory:**
1. Edit the `.md` file directly
2. Keep `MEMORY.md` entry current

**Deleting a memory:**
1. Remove the entry from `MEMORY.md`
2. Delete the `.md` file

## Best Practices

- Keep MEMORY.md entries under 150 characters
- Group related memories with folders
- Convert relative dates to absolute dates in memory content
- Remove or update stale memories
- Don't save what you can derive from code or git

## Integration with Feishu Bridge

To use the memory system with the Feishu→Claude bridge:

1. The bridge runs `claude -p "..."` for each message — standalone, no history
2. Memory files in the project directory provide context for these invocations
3. Claude automatically reads `MEMORY.md` and loads relevant memories before processing

This means your Feishu messages benefit from persistent context even though each `claude -p` call is stateless.
