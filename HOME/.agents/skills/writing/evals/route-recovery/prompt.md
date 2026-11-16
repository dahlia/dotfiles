---
tags: [skill-creator]
max_turns: 20
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

Simulate the writing skill's review workflow, without making real external calls. You authored a short English release announcement using an OpenAI model. All listed routes satisfy the cost, capability and tool policies and discovery ranks them in the listed order:
A: Claude CLI / Anthropic / model-a / credential-a / pool-a.
B: Claude CLI / Google / model-b / credential-b / pool-b.
C: OpenCode / Google / model-c / credential-c / pool-c.
D: OpenCode / Google / model-d / credential-c / pool-d.
E: OpenCode / Anthropic / model-e / credential-e / pool-e.
A fails authentication. B times out on both its initial call and a fresh retry. C fails for insufficient headroom in pool-c only. D returns an empty response initially and again on its retry. E returns a valid, complete review with NO ACTIONABLE FINDINGS.
Follow your loaded skill's actual policy, including any exclusions it requires. Write result.md with the ordered attempts you would make, exclusion scopes, final review status, and whether the announcement can be delivered. Do not invent real model IDs, invoke CLIs, or edit the skill.
