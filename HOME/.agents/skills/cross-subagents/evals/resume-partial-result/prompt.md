---
tags: [skill-creator]
max_turns: 20
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

Plan the follow-up to run release-check, initial task review-a, which used
--keep-session, read-only, --allow-shell, --isolate, --guard,
--context-file /scratch/change.patch and --add-dir /deps/lib.
Use /scratch/followup.md as the new prompt and a new task ID. The previous
attempt failed but produced partial text, and its metadata lists a denied
check. Explain what to read, how to resume safely, and how to report a clean
review with no actionable findings. Do not execute child agents.
