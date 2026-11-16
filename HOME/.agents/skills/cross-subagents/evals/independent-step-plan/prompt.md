---
tags: [skill-creator]
max_turns: 20
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

Plan three independent code review rounds: a fast inexpensive first pass outside
OpenAI/Anthropic/Google (cost cap 0.10), then two frontier reviewers from
different vendors within OpenAI/Anthropic/Google, each top or second tier.
Persist the run constraints so fallback cannot escape them. Explain execution
vs planning, quota replacement reporting, native receipts and concurrency/cost
gates if we later launch these concurrently. Do not execute child agents.
