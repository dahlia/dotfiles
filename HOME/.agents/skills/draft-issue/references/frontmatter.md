Frontmatter
===========

Everything that is not body text goes in a YAML block at the top of the draft.
It lets the draft carry the same fields the forge would ask for in its web
form, so posting it later is a mechanical step rather than a second round of
decisions.

~~~~ yaml
---
title: Escape tabs in reference destinations
type: Bug
labels:
  - bug
milestone: 0.5.0
relates-to:
  - "#39"
blocked-by:
  - "#42"
blocks:
  - "#44"
parent: "#30"
---
~~~~

Only `title` is required. Every other key is left out when it has nothing to
say; empty lists and `null` placeholders make the file harder to read and add
nothing.


Keys
----

`title`
:   The issue title, in the same language as the body. Name the specific
    behavior or need: “Escape tabs in reference destinations” tells the reader
    more than “Formatter bug”. Sentence case, no trailing period, and no `[Bug]`
    prefix, since the type and labels already say that. The exception is a
    prefix the selected template defines, which the draft keeps.

`type`
:   The issue type, for organizations that define them on GitHub (`Bug`,
    `Feature`, `Task`, and whatever else the organization configured). Read the
    real list with `collect_context.py` or the GraphQL query in
    *references/forges.md*. Repositories outside an organization have no issue
    types, so omit the key.

`labels`
:   Labels that already exist in the repository, by exact name. Reuse the ones
    the tracker applies to comparable issues; a label nobody defined cannot be
    applied and a near-miss (`bug-fix` for `bug`) just looks careless.

`milestone`
:   A milestone from the repository's open milestones, when the conversation or
    roadmap points at one. Without a hint, leave it out. Guessing a version
    number is worse than having no milestone.

`relates-to`
:   Issues connected to this one without a directional constraint.

`blocked-by`
:   Issues that must be resolved before this one can be.

`blocks`
:   Issues that cannot proceed until this one is resolved.

`parent`
:   The parent issue, when this draft is a sub-issue.

`sub-issues`
:   Children this draft will create or track, when the parent issue is the
    draft itself.

`issue`
:   The existing issue number, when the draft revises a body that is already
    posted. That is also when the filename takes the number:
    `39-escape-tabs.md`.

All issue references are strings, quoted, because an unquoted `#123` is a YAML
comment: `"#123"`. The same care applies to every other human-readable value.
Quote the title, the milestone, and any label or reference that could be read as
something else: a title with a colon (`Parser: reject empty input`) is otherwise
parsed as a mapping, and `0.5` becomes a number. A bare number (`123`) loses the
fact that it is an issue reference, and a link
(`https://github.com/.../issues/123`) breaks as soon as the issue moves or the
repository is renamed. `owner/repo#123` works when the issue is in another
repository.

Relational keys are not free text. GitHub models them as relates-to, blocking,
blocked-by, and parent or sub-issue relationships, and other forges may
support only a subset. When the relationship does not fit one of these,
mention it in a sentence in the body instead of inventing a key.


Relations between drafts
------------------------

A batch of drafts is written before any of them has a number, so a draft cannot
point at its sibling with `#123`. Use the sibling's filename without the `.md`
extension, prefixed with `draft:`:

~~~~ yaml
# wrap-heading-anchors.md
blocked-by:
  - "draft:escape-tabs"
~~~~

Post blockers and parents first when practical, then replace each `draft:`
reference with the real number as it becomes known. If a target is not posted
yet, defer that relationship until both issues exist. An unresolved reference
in a draft that is not posted yet is normal; one left in a posted issue no
longer applies and no longer links. A leading number in a filename means “this
draft revises issue #N”, so do not number drafts just to order them.


Posting the draft
-----------------

~~~~ bash
# The API renders frontmatter as text, so post the body without it.
awk 'NR == 1 && /^---$/ { fm = 1; next } fm && /^---$/ { fm = 0; next } !fm' \
  draft.md > body.md

gh issue create --repo OWNER/REPO \
  --title "Escape tabs in reference destinations" \
  --body-file body.md \
  --label bug --type Bug --milestone 0.5.0 \
  --parent 30 --blocked-by 42 --blocking 44
~~~~

For GitHub CLI 2.97.0, the mapping is:

| Frontmatter key | CLI flag or follow-up operation          |
| --------------- | ---------------------------------------- |
| `title`         | `--title`                                |
| `type`          | `--type`                                 |
| `labels`        | `--label` (repeatable)                   |
| `milestone`     | `--milestone`                            |
| `parent`        | `--parent`, or `addSubIssue`             |
| `sub-issues`    | `addSubIssue` after creation             |
| `blocked-by`    | `--blocked-by`, or `addBlockedBy`        |
| `blocks`        | `--blocking`, or reversed `addBlockedBy` |
| `relates-to`    | `addRelatesTo` after creation            |

Check the installed `gh issue create --help` rather than assuming these flags
exist in every release. When a relationship has no CLI flag, use the GitHub
GraphQL procedure below after creating the issue. API availability also
depends on the target GitHub host; another forge needs its own API.


Posting GitHub relationships
----------------------------

Use this procedure only when the repository is hosted on GitHub and the user
has asked to publish or update the issue. Drafting alone does not authorize
creation or relationship mutations.

Save the URL returned by creation, resolve all `draft:` references to posted
issue numbers, and fetch each endpoint's GraphQL node ID. These mutations take
node IDs, not issue numbers or REST numeric IDs:

~~~~ bash
# Use the URL returned by gh issue create for the new issue.
issue_id=$(gh issue view "$issue_url" --json id --jq .id)
related_id=$(gh issue view 39 --repo OWNER/REPO --json id --jq .id)

gh api graphql \
  -f query='mutation($issue: ID!, $related: ID!) {
    addRelatesTo(input: {issueId: $issue, relatedIssueId: $related}) {
      issue { id }
      relatedIssue { id }
    }
  }' \
  -f issue="$issue_id" -f related="$related_id"
~~~~

For `owner/repo#N`, resolve the ID in that repository rather than the draft's
repository. For GitHub Enterprise, use that host consistently for lookups and
`gh api graphql --hostname HOST`; both endpoints must be on the same host.

Apply each requested relationship with these inputs, where `new` is the posted
draft's node ID and `target` is the referenced issue's node ID:

| Frontmatter key | Mutation       | Input                                   |
| --------------- | -------------- | --------------------------------------- |
| `relates-to`    | `addRelatesTo` | `issueId: new, relatedIssueId: target`  |
| `blocked-by`    | `addBlockedBy` | `issueId: new, blockingIssueId: target` |
| `blocks`        | `addBlockedBy` | `issueId: target, blockingIssueId: new` |
| `parent`        | `addSubIssue`  | `issueId: target, subIssueId: new`      |
| `sub-issues`    | `addSubIssue`  | `issueId: new, subIssueId: target`      |

For example, to make the posted draft blocked by #42:

~~~~ bash
blocker_id=$(gh issue view 42 --repo OWNER/REPO --json id --jq .id)
gh api graphql \
  -f query='mutation($issue: ID!, $blocker: ID!) {
    addBlockedBy(input: {issueId: $issue, blockingIssueId: $blocker}) {
      issue { id }
      blockingIssue { id }
    }
  }' \
  -f issue="$issue_id" -f blocker="$blocker_id"
~~~~

Skip relationships already set by creation flags. Check GraphQL errors and
read back the relevant `Issue` fields (`relatesTo`, `blockedBy`, `blocking`,
`parent`, or `subIssues`), paginating connections as needed to verify the
requested targets. A body reference to #39 is not a native `relates-to` edge.
If a mutation fails, keep the created issue's URL and report which
relationships remain pending; do not rerun issue creation. Leave an existing
parent in place unless the user requested replacing it.

See GitHub's [issue GraphQL reference] for `addRelatesTo`, `addBlockedBy`,
`addSubIssue`, and their input types, and the [GitHub CLI 2.97.0 release
notes] for the CLI flags.

After formatting, read the frontmatter back and confirm every field kept its
value and type. Hongdown leaves the block alone, but a later hand edit or a
sloppy merge can break it, and an unquoted title with a colon silently turns the
block into a different document.


[issue GraphQL reference]: https://docs.github.com/en/graphql/reference/issues
[GitHub CLI 2.97.0 release notes]: https://github.com/cli/cli/releases/tag/v2.97.0
