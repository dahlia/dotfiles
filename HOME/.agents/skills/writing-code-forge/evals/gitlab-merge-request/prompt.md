---
tags: [skill-creator]
max_turns: 20
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

Can you draft the merge request for `group/http-notes` on GitLab? The
English-language project has no MR template and generally uses one or two
paragraphs. Use these verified facts instead of looking up the repository. This
is an offline exercise: don't post or use the clipboard, and skip the external
style review.

The branch changes `src/request.ts` so `parseRequest()` returns
`400 Bad Request` for malformed requests instead of passing them to the
handler. The change follows an earlier merge request numbered 17 in the same
repository. Issue 42 in that repository tracks several parser improvements;
this only addresses one of them, so the issue must stay open. The upstream
report on a different forge is https://codeberg.org/example/parser/issues/9. No
test results were supplied.

Save the formatted title and body as `draft.md` in the working directory.
