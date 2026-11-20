---
tags: [skill-creator]
max_turns: 20
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

Please write a bug report for the GitHub repo `example/http-notes`. Recent
maintainer issues are in Korean, so use that language. This is an offline
exercise: treat these notes as verified, skip the external style review, and
don't post anything.

The bug form has `### 문제 상황` and `### 재현 방법` as its two required field
headings. The maintainers confirmed that opening a malformed request with the
sample in `examples/request.http` returns `200 OK` instead of
`400 Bad Request`. They want a third peer section for protocol context: mention
RFC 9110 §15.5.1 (https://www.rfc-editor.org/rfc/rfc9110.html#section-15.5.1),
and end the context with a reference to the maintainer's specific confirmation
at https://github.com/example/http-notes/issues/12#issuecomment-987654321.

Save the formatted issue draft as `draft.md` in the working directory.
