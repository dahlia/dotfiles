---
name: draft-issue
description: >
  Draft an issue for a project's own tracker: pick the language the tracker
  actually uses, follow its issue template when one exists, keep the heading
  scale the template established, fill in labels, issue type, custom issue fields, milestone, and
  relationships between issues, then format the draft with hongdown. Use this
  skill whenever the user asks to write, draft, file, open, or rewrite an issue
  or bug report, mentions an issue template, or says things like "이슈 작성해 줘",
  "이슈 초안 만들어 줘", "이거 이슈로 정리해 줘", "버그 리포트 써 줘", or "GitHub에
  이슈 올릴 내용 정리", even when they never say the word "issue". Also use it
  when an existing issue's body needs to be restructured, filled out, or
  rewritten before posting, or when the user describes a problem and asks you
  to prepare something for the maintainers.
---

Drafting an issue
=================

An issue is a request for someone else's time. The person reading it usually
has the code but not your context, so the draft has to carry what happened,
what you expected instead, and how to see it for themselves. Everything below
serves that: the tracker's language, the template, the heading levels, the
right labels. Getting the prose right and ignoring the template produces an
issue that looks foreign on the project; getting the template right and
skipping the context produces a form nobody can act on.

The draft is a Markdown file, not a posted issue. The user decides when it goes
out.

Load `writing-code-forge` with the skill tool before drafting; if it is not
discoverable by name, read the sibling *writing-code-forge/SKILL.md* in the
same skills directory. Follow it for language, Markdown template handling,
heading levels, style, links, review, and formatting.


Pin down the target and the kind of issue
-----------------------------------------

Find the repository first. It may come from the user's message, from the
current checkout, or from a link they pasted. If the session spans several
repositories and the request does not say which one, ask before writing:
everything from the language to the labels depends on the answer.

When the draft revises an issue that is already posted, read it before changing
anything (`gh issue view N --json title,body,labels,milestone,state`). Keep its
language and the facts the thread has settled, and change only what the user
asked to change. A cleaner rewrite that drops the detail a maintainer asked for
is worse than the original.

Then decide what kind of issue this is, because it changes the body:

 -  **Bug**: something behaves worse than the project intends.
 -  **Feature**: something the project does not do yet but should.
 -  **Refactor or cleanup**: the behavior is fine, the shape of the code makes
    work harder.
 -  **Docs**: the written material is missing or wrong.
 -  **Question or discussion**: no change is being requested yet.

Mixed cases exist. A bug report that proposes a redesign is usually better as a
bug with a short sketch of the fix than as a feature request that buries the
reproduction steps.


Check the premise before you build on it
----------------------------------------

A draft is only as good as the claim underneath it. When the request rests on a
fact you can check cheaply, such as a command's output, an exit code, how a
released version behaves, or whether an option exists, check it before writing
around it. Not every issue needs this: a proposal, a docs gap, or a refactor
argument usually does not.

If the check contradicts what the user said, stop. Do not finish or hand over a
draft that repeats a claim you have just shown to be false, and do not quietly
substitute the defect you did find and call it their issue. Report back
instead, with enough detail that the user can check your work:

 -  the command, version, platform, and inputs you used;
 -  the output you got, quoted;
 -  how that differs from the report, in one sentence;
 -  what does reproduce, if anything, or that no failing case turned up;
 -  the choices: file the verified behavior as its own issue, add the evidence
    to an existing issue, or file it as written if they have context you lack.

Then wait for a decision. Verification is cheap and the user's knowledge is not
always in the prompt, so being told “that is not what I meant” is a better
outcome than a confident draft that gets closed as not reproducible. If a draft
file already exists when you find this, say where it is and that it is on hold
rather than presenting it as ready.

This rule is about verified facts, not about disagreeing with the request. A
maintainer's preferred wording, a feature whose value you would weigh
differently, or a scope you find ambitious are not factual errors; write the
issue they asked for.


Read the tracker before writing a word
--------------------------------------

Read the repository's own material instead of assuming GitHub defaults.
`scripts/collect_context.py` gathers most of it in one pass: templates, labels,
issue types, custom issue fields, milestones, and recent issues with their
text, which is also the evidence for the language decision. Paths below are
relative to this skill's directory.

~~~~ bash
python3 <skill-dir>/scripts/collect_context.py --repo OWNER/REPO
# or, inside a checkout:
python3 <skill-dir>/scripts/collect_context.py
~~~~

Read what it prints. The recent issues matter more than any single field: they
show how this project's maintainers actually phrase things, how much context
they expect, and whether the body uses headings at all.

When `gh` is missing, unauthenticated, or pointed at a forge it cannot reach,
read the checkout's template directory directly and say which fields you could
not verify instead of guessing them.

While you are there, search for duplicates. A new issue that repeats an open
one wastes the maintainer's attention and splits the discussion.

~~~~ bash
gh issue list --repo OWNER/REPO --state all --search "reference destination" --limit 20
~~~~

Try two or three keyword variants, including the name of the function or option
involved. If a near-duplicate exists, tell the user and draft an addition to
that issue instead, or a narrower issue that says what the existing one misses.


Follow the issue template
-------------------------

On GitHub, templates live in `.github/ISSUE_TEMPLATE/`: `*.yml` issue forms,
`*.md` templates, and a `config.yml` that controls the chooser. A legacy
`ISSUE_TEMPLATE.md` in the repository root, `docs/`, or `.github/` also counts,
and an organization can supply defaults from its `.github` repository when the
project has none of its own. Read *references/forges.md* when the project is not
on GitHub.

Pick the template that matches the kind of issue. Do not force a bug report
into a feature-request form because it is the only file present; if no template
fits, use the blank-issue path and say so. Some repositories set
`blank_issues_enabled: false` in `config.yml`; there, the closest form is the
only route and the draft has to answer it.

**Issue forms** (`*.yml`) render one heading per field, and that heading is the
field's `label`:

~~~~ markdown
### 🖥️ What operating system are you on?

Ubuntu 24.04, kernel 6.8.0-45-generic

### 🪲 What went wrong?

Pressing *Export* closes the dialog without writing a file.
~~~~

Write the draft as the rendered result, not as YAML. Use `###` for peer
sections and `####` inside a field. Apply the shared rules for missing required
information rather than inventing environment details.

Include optional fields when they carry something, and drop the ones that would
only contain “N/A”. Do not paste the form's instructions or placeholders into
the body.

Not every field is a label and a paragraph. A `checkboxes` field becomes a task
list; a `dropdown` becomes the selected option as plain text. `markdown`
fields are instructions to the submitter and never part of the issue.

Templates carry their own metadata header too: `name`, `about`, `title`,
`labels`, `assignees`, and sometimes `type` or `projects`. Strip that header
from the body, put its labels and type in the draft's frontmatter, and keep any
title prefix it defines (`[Bug]: `, for instance) in the title. That prefix is
the one exception to the no-prefix rule in *references/frontmatter.md*. Never
leave two frontmatter blocks in one file.

**No template** means you choose the shape. See *references/structure.md* for
the section patterns that work for each kind of issue.


Write the body
--------------

The body has to answer the questions the maintainer will ask, in the order they
will ask them: what is happening, what should happen, what did you do to see
it, and what have you already ruled out. Context earns its place by shortening
that path. A paragraph about how the user encountered the bug is useful when it
explains why the case matters; three paragraphs about their application
architecture usually are not.

Bugs need a reproduction. Show the smallest input, command, or sequence that
produces the wrong result, and show the actual output next to the expected one.
If the reproduction depends on a specific version or platform, say which.

Feature requests need a situation rather than a specification. Describe the
point where the missing capability forces a workaround, and what the workaround
costs. A feature without a scenario is a suggestion the maintainer has to
imagine a use for.

Refactors need the same treatment as bugs: a concrete place where the current
shape made a change expensive, not an abstract claim that the code is messy.

Solution directions are optional. When the user is a maintainer of the project,
the issue may settle the design outright. In that case, name the alternatives
that were on the table, say why each was rejected, and give the reason the
chosen one wins. Write that as prose. A table of options with columns for pros
and cons makes a design decision look like a procurement evaluation, and
maintainers rarely write that way in their own tracker.


Fill in the frontmatter
-----------------------

Everything that is not the body goes in YAML frontmatter at the top of the
draft: title, issue type, custom issue fields, labels, milestone, and
relationships to other issues. *references/frontmatter.md* has the schema, the
example, and how each field maps to `gh issue create` and GitHub GraphQL
mutations.

Choose labels and types from what the repository actually offers. An invented
label does not apply, and a type from another owner may not exist for
this repository. GitHub supports issue types and custom issue fields for both
organization-owned and personally owned repositories. Query the target
repository rather than inferring availability from its owner type. An API or
permission failure means availability is unknown, not that no fields exist.
Record applicable field values in `issue-fields` frontmatter using the actual
field names and allowed options; do not invent priority, effort, or dates.
Issue fields are distinct from issue-form body fields and project-only fields.
Milestones are optional: set one when the conversation or the project's roadmap
points at a release, and leave it out when nothing does.

When several issues are drafted together, check each pair for a relationship
that actually exists. Two drafts from the same session are not related just
because they were written together, and an invented dependency is worse than
none. When one depends on, blocks, or parents another, record it; a relation to
a draft in the same batch has no number yet, so point at that draft's filename
with a `draft:` prefix and replace it with the real number once the issue is
posted:

~~~~ yaml
blocked-by:
  - "draft:disable-auto-wrap"
~~~~


Save the draft outside the repository
-------------------------------------

Never write the draft into the working tree. A stray Markdown file in a
checkout shows up in `git status`, gets committed by accident, and confuses the
next person who looks at the branch.

Keep drafts in one place instead, outside any checkout:

~~~~ text
${XDG_DATA_HOME:-$HOME/.local/share}/issue-drafts/<owner>-<repo>/escape-tabs-in-reference-destinations.md
~~~~

For `dahlia/hongdown` on Linux that is
`~/.local/share/issue-drafts/dahlia-hongdown/escape-tabs-in-reference-destinations.md`.
Nested owners flatten into hyphens in the directory name, so
`gitlab-org/model/registry` becomes `gitlab-org-model-registry`. On macOS and
Windows, use the platform's data directory when `XDG_DATA_HOME` is unset.

The directory is keyed by the repository the draft targets, not by the session's
working directory, so one project's drafts collect in one place no matter where
the checkout lives. A batch produces one directory; a batch that spans two
repositories produces two. When the user names a directory, use that instead.

Name the file after the issue's subject in lowercase kebab case. When the draft
revises an issue that already exists, prefix the number: `39-escape-tabs.md`,
and record that number in the frontmatter.

Report the absolute paths when you finish.


Review the issue
----------------

Follow the review and formatting steps in `writing-code-forge`. Before handing
the draft over, also check:

 -  Does the body state what happened and what was expected, with enough detail
    to reproduce it?
 -  Would the maintainer need to ask a question that the draft could have
    answered?
 -  Is every version, path, and output in the draft something the user actually
    provided or you actually observed?
 -  If you tested the premise and it did not hold, did you stop and report
    instead of drafting around it?
 -  Are the labels, type, custom issue fields and their values, and milestone
    real for this repository?


Hand it over
------------

Tell the user where each draft is, what it contains in one line, and what still
needs a decision: missing environment details, a milestone you left unset, a
relation you were unsure about. If they want it posted, `gh issue create` takes
the fields directly:

~~~~ bash
gh issue create --repo OWNER/REPO \
  --title "Escape tabs in reference destinations" \
  --body-file body.md \
  --label bug --type Bug --milestone 0.5.0 \
  --parent 30 --blocked-by 42
~~~~

`body.md` is the draft with the frontmatter removed; *references/frontmatter.md*
has the one-liner that strips it. Pass the draft as-is and the API shows the
YAML as the first thing in the issue.

`--type`, `--parent`, `--blocked-by`, and `--blocking` require support from
the target GitHub host. Check `gh issue create --help` on the machine before
using them. GitHub CLI 2.97.0 provides these flags, but has no `relates-to`
flag. For GitHub repositories, apply `relates-to` and any relationships
unsupported by the installed CLI after creation through `gh api graphql`,
following [Posting GitHub
relationships](references/frontmatter.md#posting-github-relationships). A body
mention does not create a native relationship. A draft that revises an
existing issue is posted with `gh issue edit N --body-file body.md` instead;
apply its requested relationships separately as well.


Reference
---------

 -  *references/structure.md*: section patterns for bugs, features, refactors,
    docs, and small issues, with examples of the heading scale in practice.
 -  *references/frontmatter.md*: the full frontmatter schema and the mapping to
    `gh issue create`.
 -  *references/forges.md*: where GitHub, GitLab, Gitea, and Forgejo keep their
    templates, and how to read them without the GitHub CLI.
