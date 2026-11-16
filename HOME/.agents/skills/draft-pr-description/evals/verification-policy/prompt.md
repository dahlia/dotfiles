---
tags: [skill-creator]
max_turns: 20
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

Draft a short English PR title and body for each of these four independent
changes. This is a body-composition exercise, not a live-repository task:
all facts below are verified, and no context collection, external style review,
clipboard access, or posting is needed. Do not invent missing facts.

1. routine-gates: Pin a dependency to 2.4.1 because 2.4.0 rejects valid UTF-8
   filenames. No template. All usual lint, type checks, build, and tests passed.
   No known coverage gap.
2. platform-gap: Reconnect after suspend now uses platform-specific Linux and
   Windows paths. Linux reconnect was observed to work. Windows is untested
   because no Windows device is available. No template.
3. performance-evidence: Index lookup replaces a linear scan. A benchmark on
   the same 100000-record dataset falls from 420ms to 35ms. All routine gates
   passed. No template.
4. required-template: Reject empty widget labels and add a regression test.
   `npm test` was run and passes. The required template has exactly
   `## Summary` and `## Testing`; the latter requires the command and result.
