Frontmatter
===========

Everything that is not body text goes in a YAML block at the top of the draft.
It lets the draft carry the same fields the forge would ask for in its web form,
so posting it later is a mechanical step rather than a second round of
decisions.

~~~~ yaml
---
title: "Reject unsupported URL schemes in document loaders"
base: main
draft: false
labels:
  - bug
reviewers:
  - dahlia
assignees:
  - dahlia
milestone: "2.5"
pr: "1293"
---
~~~~

Only `title` is required. Every other key is left out when it has nothing to
say; empty lists and `null` placeholders make the file harder to read and add
nothing.


Keys
----

`title`
:   The PR title, in the same language as the body. Imperative mood, sentence
    case, no trailing period, and no kind prefix unless the selected template
    defines one. Name the outcome: “Pin OpenTelemetry JS SDK 2.x in the
    manual” tells the reader more than “Fix docs”.

`base`
:   The branch the PR targets, when it is not the repository's default branch.
    For a stacked PR, this is the branch below it, not `main`.

`draft`
:   `true` when the PR should open as a draft. Omit it otherwise; `false` is the
    default and adds nothing.

`labels`
:   Labels that already exist in the repository, by exact name. Reuse the ones
    the tracker applies to comparable PRs; a label nobody defined cannot be
    applied, and a near-miss (`bug-fix` for `bug`) looks careless.

`reviewers`
:   GitHub logins or team handles to request a review from. Choose people who
    have touched the area or who own the component, not the first name in the
    contributor list. Leave the key out when the conversation does not identify
    anyone; an unnecessary review request costs someone else's time.

`assignees`
:   Logins to assign, usually the author. Omit it when the repository does not
    use assignment.

`milestone`
:   A milestone from the repository's open milestones, when the conversation or
    roadmap points at one. Without a hint, leave it out. Guessing a version
    number is worse than having no milestone.

`pr`
:   The existing PR number, when the draft revises a body that is already
    posted. That is also when the filename takes the number:
    `1387-pin-opentelemetry-sdk.md`.

`related`
:   Other PRs or issues this one builds on, when the relationship is not a
    closing one. Prefer a sentence in the body (`Related to #1316.`) unless the
    list is genuinely a list. Do not invent a key the forge does not model.

All human-readable values are strings, quoted where YAML could read them as
something else. A title with a colon (`Parser: reject empty input`) is otherwise
parsed as a mapping, `0.5` becomes a number, and an unquoted `#123` becomes a
comment. Write `"#123"`, `"https://..."`, and quote the title and milestone.


Posting the draft
-----------------

~~~~ bash
# The API renders frontmatter as text, so post the body without it.
python3 <skill-dir>/scripts/copy_body.py draft.md --body-out /tmp/pr-body.md

gh pr create --repo OWNER/REPO \
  --title "Reject unsupported URL schemes in document loaders" \
  --body-file /tmp/pr-body.md \
  --base main --draft --label bug --reviewer dahlia --assignee dahlia
~~~~

Each flag maps to one key:

| Frontmatter key | Flag                         |
| --------------- | ---------------------------- |
| `title`         | `--title`                    |
| `base`          | `--base`                     |
| `draft`         | `--draft` (only when `true`) |
| `labels`        | `--label` (repeatable)       |
| `reviewers`     | `--reviewer` (repeatable)    |
| `assignees`     | `--assignee` (repeatable)    |
| `milestone`     | `--milestone`                |
| `pr`            | none; use `gh pr edit N`     |

Strip the frontmatter before passing the file to `--body-file`. The script does
that, and it also fills the clipboard with the same body. Passing the draft
itself would render the YAML as the first thing in the PR.

For an existing PR, use `gh pr edit N --title ... --body-file /tmp/pr-body.md`.
`gh pr edit` does not take every `create` flag; `gh pr edit --help` on the
machine settles what this release accepts.
