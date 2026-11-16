---
tags: [skill-creator]
max_turns: 20
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

Plan a read-only independent visual code review of /repo/ui and /scratch/screen.png.
The host vendor is OpenAI. Require actual image capability, shell checks,
isolation and protected-input verification. The coordinator supplies quota in
/scratch/quota.json: do not query account usage live. No edits or permission
escalation. Produce commands and explain how you verify completion, unavailable
routes and denials. Do not execute child agents.
