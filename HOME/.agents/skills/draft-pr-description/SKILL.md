---
name: draft-pr-description
description: >
  Draft a pull request title and description that a reviewer can act on: read
  the diff and commits first, follow the repository's PR template and language,
  explain why the change exists and how it works instead of restating the diff,
  then save the draft outside the checkout, format it with hongdown, and copy
  the body to the clipboard. Use this skill whenever the user asks for a pull
  request description, PR body, PR title, "PR 초안", "PR 설명 써 줘", "PULL.md",
  "pull request description", "prepare the PR", or asks to turn a branch or
  commits into a pull request, even if they never say "description". Also use
  it when an existing PR body needs to be rewritten, shortened, or reworded.
---

Drafting a pull request description
===================================

A pull request description is not a changelog of the diff. The reviewer can
already see which files changed. What they cannot see is why the change is
needed, which alternatives were rejected, what behavior changes, what could
break, and where to look first. Spend the draft's words there, and leave the
file-by-file narrative out.

The draft is a Markdown file outside the checkout, not a posted pull request.
The user decides when it goes out.


Establish the change under review
---------------------------------

Find the repository, the base branch, and the head branch before writing
anything. A PR description is written against a specific range of commits, and
the range is usually `base...head`, not the working tree.

`scripts/collect_context.py` gathers the range, the commits, the diff stat, the
repository's PR template, recent merged PRs, and labels in one pass:

~~~~ bash
python3 <skill-dir>/scripts/collect_context.py
python3 <skill-dir>/scripts/collect_context.py --repo OWNER/REPO --base main
~~~~

Read its output. Then read the diff itself, not just the stat. The commit
messages say what the author thought they were doing; the diff says what
actually changed. When the two disagree, the diff wins.

Check whether a PR already exists for the head branch:

~~~~ bash
gh pr view --json number,title,state,url,isDraft,baseRefName
gh pr list --head "$(git branch --show-current)" --state all
~~~~

An existing PR changes the job from drafting to revising. Read its current body
and the review thread before rewriting, and keep the facts the thread has
settled. Do not replace a body that reviewers have already quoted unless the
user asked for a rewrite.

When the branch has no commits beyond the base, is the base branch itself, or
contains unrelated work, stop and ask which range the PR should cover. A
description built on the wrong range is confidently wrong.


Verify the premise against the diff
-----------------------------------

The user's summary of the change is a lead, not evidence. Before writing around
it, check the parts that are cheap to check:

 -  Does the diff actually make the behavior change the user described?
 -  Do the files, functions, and configuration keys they named exist in the
    range, and are they spelled the way the diff spells them?
 -  If they say a test, command, or CI job passes, was it actually run in this
    session? If not, do not repeat the claim as fact.
 -  If the change closes an issue, does the issue exist and does the diff
    address it?
 -  If the body will explain how the code or a tool works, check the code or
    run the command. Do not infer the algorithm from a single example.

If the check contradicts the request, stop. Do not hand over a draft that
repeats a claim the diff disproves, and do not quietly substitute the change
you did find and present it as theirs. Report back with the branch and range
you inspected, the relevant hunk quoted or summarized, how it differs from the
request, and the choices: describe the change that is actually there, amend the
branch first, or write it as asked if they have context you lack. Then wait.

This rule is about checkable facts, not about disagreeing with the framing. A
scope you would phrase differently, a title you would choose differently, or a
reviewer focus you would prioritize differently are editorial judgments; write
the PR they asked for.

Never invent a test result, a benchmark number, a migration step, or an issue
number. A missing sentence is better than a fabricated one. If verification was
not run, say what should be run or leave the claim out; do not imply CI passed.


Read the repository before writing a word
-----------------------------------------

Read the project's own material instead of assuming GitHub defaults.
`scripts/collect_context.py` reads most of it: PR templates, labels, milestones,
recent merged pull requests, and the language mix. Paths below are relative to
this skill's directory.

The recent merged PRs matter more than any single field. They show how this
project's maintainers phrase a description, whether they use headings, how much
detail they expect, and whether the body starts with the problem or with the
change. Match that register, not a generic PR template you have seen elsewhere.

When `gh` is missing, unauthenticated, or pointed at a forge it cannot reach,
read the checkout's template directory and recent commit messages directly, and
say which fields you could not verify instead of guessing them.

For a fork, templates, labels, and recent PRs come from the upstream repository,
because that is where the pull request lands. `collect_context.py` follows the
fork's parent automatically when `gh` can see it.

While you are there, look for an existing PR that already covers the same
ground. Two PRs for one change split the review and waste maintainer attention.

Read the contributing guide and any AI policy as well. Read the policy's scope,
not only its requirements: many projects require a disclosure for outside
contributions and exempt maintainers. Check whether the user is covered before
adding a line. The authenticated account's permissions settle it when `gh` is
available (`gh api repos/OWNER/REPO --jq '.permissions'`; `admin` or `maintain`
means a maintainer), and CODEOWNERS or the user's own statement can settle it
when `gh` cannot. When the exemption applies, omit the disclosure even though
the policy requires it for outside contributors. Do not add one “to be safe”;
the project has already decided who needs it, and an unnecessary disclosure is
a claim about authorship that dates the description.

When the policy covers the user and requires disclosure, follow its wording and
placement. State the tool and the extent of the assistance in a short sentence
or the trailer the policy specifies. Keep the commit-message trailer out of the
PR body; the commit skill owns that. When the user's status is unclear, ask
before adding or omitting a required line.


Match the tracker's language
----------------------------

Write the PR in the language the repository already uses, which is usually
English even when the user and the maintainers share another language. Judge it
from evidence, in roughly this order:

 -  recent merged pull requests, especially ones written by maintainers;
 -  the README, contributing guide, and PR template;
 -  the language the user used for their last few contributions there.

Do not infer it from the programming language of the code, the maintainers'
names or nationality, or the language of this conversation. A Korean-speaking
maintainer of an English-language project still wants an English PR.

If the sample is mixed or too small to tell, look for a stated policy in
`CONTRIBUTING.md`, the docs, or the templates themselves, and follow it. When
that settles nothing, ask the user which language to use. Say which evidence you
used when you report back, so the user can correct it in one line.

An explicit instruction from the user wins over the tracker's default. If they
ask for English in a Korean-language project, write English and mention the
mismatch once.

Once the language is chosen, write the entire draft in it, including the title
and any headings. Identifiers, error messages, and quoted output stay as they
are.


Follow the PR template
----------------------

Look for a template before inventing a structure.

On GitHub, templates live in the repository root, `docs/`, or `.github/`, named
`pull_request_template.md` or `PULL_REQUEST_TEMPLATE.md` in any capitalization.
A `.github/PULL_REQUEST_TEMPLATE/` directory can hold several templates for
different kinds of change. An organization can supply a default from its
`.github` repository when the project has none of its own. Read
*references/forges.md* when the project is not on GitHub.

Pick the template that matches the kind of change. A bug fix does not belong in
a feature-request template just because it is the only file present. If several
templates exist and none clearly fits, ask which one to use.

**Markdown templates** keep whatever structure their author wrote. Replace the
placeholder text, remove HTML comments that only explain the form, and keep the
section headings and their levels. Fill required sections from what you actually
know. When a section needs information the user never gave, ask for it once, in
a single batch, instead of inventing a version number, a test command, or a
screenshot.

Checkboxes become task lists. Check a box only where the user's information or
your own verified work supports it. A required acknowledgment nobody confirmed
stays unchecked, which means the draft is not ready to post; say so rather than
ticking it to make the file look complete.

Templates sometimes carry a title prefix (`[Bug]: `, `feat: `). Keep it in the
title. That is the one exception to the no-prefix rule below.


Keep the heading scale
----------------------

Without a template, a body never starts at `#`. The forge renders the PR title
as the page's first-level heading, so a body `#` duplicates it. With a template,
the template wins, even when it keeps an `#` of its own.

 -  If a template exists, keep its headings and their levels exactly. A new
    top-level section uses the template's shallowest level, and a section nested
    under an existing one drops a level.
 -  With no template, start at `##`.
 -  Do not renumber, restyle, or drop a heading the template defined.

Most PRs need no headings at all. A two-paragraph fix with `## Summary` above it
reads like ceremony, and the reviewer has to scan structure to find one fact.
Use `##` sections only when the change has distinct parts that a reader would
want to navigate: a migration procedure, a breaking change, a large feature with
several surfaces. Even then, two or three sections is usually the ceiling.


Write the title
---------------

The title names the outcome, not the activity. It should make sense to someone
scanning a release changelog who has not opened the diff.

 -  Imperative mood: “Pin OpenTelemetry JS SDK 2.x in the manual”, not “Pinned
    the SDK” or “This PR pins the SDK”.
 -  Sentence case. No Title Case, no trailing period.
 -  No `[Feature]`, `[Fix]`, or `feat:` prefix unless the template defines one.
    Labels and the diff already say what kind of change it is.
 -  Be specific about the surface. “Fix tab handling” is weaker than “Reject
    unsupported URL schemes in document loaders”.
 -  Keep it short enough to scan. If it needs a comma and two clauses, it is
    probably two ideas.

The title follows the same language as the body and the same proper-noun
spelling.


Write the body
--------------

Load the `writing` skill before writing a sentence of it when it is available;
the review section below covers its proofreading step.

The body answers the questions a reviewer will ask, in the order they will ask
them: why does this change exist, how does it work, what could break, and how do
you know it works. The diff answers “what changed”. Repeating that is the most
common way a PR description becomes long and useless.

**Why.** Open with the situation that makes the change necessary: the bug, the
missing capability, the constraint, the cost of the current behavior. If an
issue describes it, link the issue instead of retelling the whole thread. A
reviewer who understands the problem can judge the solution.

**How.** Explain the mechanism at the level of the decision, not the line. Name
the function or module that now owns the behavior, the invariant it preserves,
and the non-obvious choice you made. If you rejected an alternative, say why in
one sentence; that is often the most valuable paragraph in the PR. Skip the
walkthrough of every file, because the reviewer can read the diff.

**What changes.** State user-visible behavior, configuration, migration steps,
and breaking changes. If nothing changes for users, say so once. If a follow-up
is deliberately out of scope, name it and link the issue that tracks it. This
keeps the reviewer from asking for work that belongs elsewhere.

**Verification.** Report only what was actually run: the command, the relevant
result, and any gap. “`pnpm test` passes” is useful when it ran; “should pass”
is not evidence. If you could not run the tests, say which ones need a run
instead of implying they passed.

End with the issue relationship when there is one:

~~~~ markdown
Fixes #1292.
~~~~

Use `Fixes` or `Closes` only when merging should close the issue. Otherwise
write `Related to #1316.` A PR that says “Fixes” but does not fix it closes the
issue at merge time, which is worse than no link.

### Length and shape

Most PRs need two to four paragraphs. A one-line dependency bump needs one
paragraph and the verification. A large feature may need four paragraphs and a
short `##` section for migration. Length follows the risk and the number of
decisions, not the number of files.

Use prose. Bullets fragment reasoning and hide the connection between items.
Reserve them for genuinely discrete lists: migration steps, breaking changes,
a set of related fixes. Do not use bold lead-ins inside bullets, emoji, or a
“Summary / Changes / Testing” skeleton unless the template requires it.

Do not open with “This PR”, “This pull request aims to”, or “In this PR we”.
Start with the problem or the change itself. Do not close with “In summary” or
a restatement of the title. The reviewer has read both.

### Linking rules

 -  Refer to issues and pull requests by number alone (`#123`,
    `owner/repo#123`). Forges link those automatically, and a manual link
    breaks when the number is wrong.
 -  Link manually only when the target lives on another forge, for example a
    Codeberg issue referenced from a GitHub PR.
 -  When pointing at a single comment inside a discussion, use the comment
    permalink wrapped in angle brackets:
    `<https://github.com/fedify-dev/fedify/issues/664#issuecomment-4203452333>`.
    The brackets mark where the URL ends so a following period stays outside
    the link.
 -  Quote a technical specification by linking to the section that says it, not
    to the document's front page.
 -  Fediverse Enhancement Proposals always use `https://w3id.org/fep/xxxx`,
    never the repository's rendered path.
 -  Do not invent an issue number to make the body look complete. If you do not
    know it, leave the sentence out or ask.


House style
-----------

These rules apply to the title and body unless the repository's template or
contributing guide says otherwise.

**No em dashes.** Replace them with a comma, colon, semicolon, parentheses, or
a rewritten sentence. Hongdown can turn `--` in prose into an em dash, so check
the formatted file and rewrite any that appear. A spaced hyphen used as a dash
is the same problem.

**No spaces around slashes.** Write `src/foo.ts`, `HTTP/2`, `read/write`, and
`CI/CD`, not `src / foo.ts` or `CI / CD`. The slash joins the terms; spacing
breaks the join and makes paths hard to copy.

**Italicize filenames, paths, and extensions.** Write *src/index.ts*,
*docs/manual/opentelemetry.md*, *PULL.md*, and *.ts*, not backticked paths. This
is the house convention in the target repositories, and it keeps prose visually
distinct from code. A path inside a code block, a URL, or a literal command
stays as it is. Inline code is for identifiers, functions (`parseHeader()`),
commands, flags, environment variables, HTTP methods and status codes, SQL and
Redis commands, and specification type names.

**Sentence case for headings and titles.** “Breaking changes”, not “Breaking
Changes”. Proper nouns keep their capitalization. Do not number headings unless
the template does.

**Official names.** Follow the official spelling of products, projects,
protocols, and file formats: GitHub, macOS, Node.js, npm, Wi-Fi, VS Code,
HTTP/2. A lowercase official name at the start of a sentence gets a rewritten
sentence, not a capital letter.

**Concision.** Delete sentences that only announce importance (“It is worth
noting that”, “This is a crucial change”). Prefer concrete Saxon words: use,
show, help, not utilize, demonstrate, facilitate. Keep the reasoning; cut the
scaffolding.

**No anthropomorphism.** Software does not want, understand, or decide. Say what
the code checks, returns, or rejects. Ordinary shorthand such as “the server
rejects the request” is fine.


Fill in the frontmatter
-----------------------

Everything that is not the body goes in YAML frontmatter at the top of the
draft: title, base branch, draft status, labels, reviewers, assignees, and
milestone. *references/frontmatter.md* has the schema, the example, and how each
field maps to `gh pr create`.

Only `title` is required. Leave out every key that has nothing to say; empty
lists and `null` placeholders make the file harder to read. Quote strings that
could be read as something else, especially a title with a colon.

Choose labels, reviewers, and milestones from what the repository actually
offers. An invented label does not apply, and a reviewer who has never touched
the area wastes a request. When the conversation does not identify a reviewer
or milestone, leave the key out rather than guessing.

When the draft revises an existing PR, add its number as `pr:` and prefix the
filename with it, the same way an issue draft does.


Save the draft outside the repository
-------------------------------------

Never write the draft into the working tree. A stray Markdown file in a checkout
shows up in `git status`, gets committed by accident, and confuses the next
person who looks at the branch. Even a git-ignored `PULL.md` is one clone away
from leaking into a commit.

Keep drafts in one place instead, outside any checkout:

~~~~ text
${XDG_DATA_HOME:-$HOME/.local/share}/pr-drafts/<owner>-<repo>/<subject>.md
~~~~

For `dahlia/hongdown` on Linux that is
`~/.local/share/pr-drafts/dahlia-hongdown/reject-unsupported-url-schemes.md`.
Nested owners flatten into hyphens in the directory name, so
`gitlab-org/model/registry` becomes `gitlab-org-model-registry`. On macOS and
Windows, use the platform's data directory when `XDG_DATA_HOME` is unset.

The directory is keyed by the repository the PR targets, not by the session's
working directory, so one project's drafts collect in one place no matter where
the checkout lives. A batch that spans two repositories produces two
directories. When the user names a directory, use that instead.

When no remote can be resolved, use the checkout directory's basename as the
repository and `local` as the owner, and say in the handover that the slug is a
guess. Do not leave the draft in the checkout just because the forge is
unknown.

Name the file after the PR's subject in lowercase kebab case. When the draft
revises an existing PR, prefix the number: `1387-pin-opentelemetry-sdk.md`, and
record that number in the frontmatter.

Report the absolute path when you finish.


Format with hongdown
--------------------

The project's Markdown style is hongdown's style. Format every draft, and never
let it wrap lines: PR text is edited in the browser, and hard wraps turn each
edit into a reflow.

~~~~ bash
hongdown --write --no-line-width <draft.md>        # one file
hongdown --write --no-line-width <drafts-dir>/     # a whole directory
~~~~

If `hongdown` is not on `PATH`, try mise, then the release binary:

~~~~ bash
mise x aqua:dahlia/hongdown -- hongdown --write --no-line-width <paths>
~~~~

Only if both are missing, download the archive for this platform from
<https://github.com/dahlia/hongdown/releases/latest> into a temporary
directory, unpack it, and run the binary there.
`gh release view --repo dahlia/hongdown --json tagName,assets` lists the exact
asset names; match the target triple against `uname -sm`. Do not install
anything system-wide for a one-off format.

Check the result afterwards. Formatting reorders reference links and may turn
`--` in prose into an em dash, which the house style avoids; rewrite that
sentence and format again. A mangled code block or a lost frontmatter field is
also a formatting failure, not a cosmetic one. When a later edit changes the
draft, format it once more before handing it over.


Copy the body to the clipboard
------------------------------

The user usually wants to paste the body into the forge's web form. Copy it
there without the frontmatter or the title:

~~~~ bash
python3 <skill-dir>/scripts/copy_body.py <draft.md>
python3 <skill-dir>/scripts/copy_body.py <draft.md> --body-out /tmp/pr-body.md
~~~~

The script strips the frontmatter, sends the body to the platform clipboard,
and reports which command it used. It tries Wayland (`wl-copy`), X11 (`xclip`
or `xsel`), macOS (`pbcopy`), WSL (`clip.exe`), and Termux
(`termux-clipboard-set`) in that order. When none is available, it prints the
body and exits non-zero instead of pretending it copied something.

`--body-out` also writes the body to a file, which is what
`gh pr create --body-file` wants. Use it when the user is ready to post.

Clipboard access is a convenience, not a gate. If the environment has no
display or the command is missing, say so in the handover and leave the file
path; do not retry in a loop or install a clipboard package for one draft.


Review the prose
----------------

Load the `writing` skill when it is available and follow it for the body. It
carries the house rules on tone, structure, and proper nouns, and it applies to
PR descriptions as much as to blog posts.

The skill's own scope is English prose. When the tracker's language is not
English, take its structure, tone, and review procedure, and treat its
English-specific rules, such as spelling and punctuation conventions, as
advisory.

Run the cross-vendor style review that `writing` describes, following its rules
for selection, validation, and fallback. A PR description is public writing, so
size is not a reason to skip it. If no reviewer can be reached, self-review
against the checklist below and say in your report that the external review did
not happen.

Before handing the draft over:

 -  Does the first paragraph say why the change exists, and does the rest say
    how it works without walking the diff file by file?
 -  Is every version, path, command, and output in the draft something the user
    actually provided or you actually observed?
 -  If the diff contradicted the request, did you stop and report instead of
    drafting around it?
 -  Are test results reported only where they were actually run?
 -  Do the headings follow the template's levels, or, with no template, stay at
    `##` and below, with no headings at all on a short PR?
 -  Are labels, reviewers, and milestone real for this repository?
 -  If an AI policy applies, did you check whether the user is exempt, and did
    you include a disclosure only when the policy covers them?
 -  Is the language the same as the tracker's, throughout, including the title?
 -  No em dashes, no spaces around slashes, filenames and paths italicized, and
    sentence case headings?
 -  Is the issue relationship correct (`Fixes` only when merge should close it)?
 -  Is the draft outside the checkout, formatted with hongdown, and is the
    clipboard filled with the body alone?


Hand it over
------------

Tell the user where the draft is, what it contains in one line, whether the body
is on the clipboard, and what still needs a decision: a missing reviewer, a
milestone you left unset, a test you could not run, a template section that
needs their input. If they want it posted, `gh pr create` takes the fields
directly:

~~~~ bash
python3 <skill-dir>/scripts/copy_body.py draft.md --body-out /tmp/pr-body.md

gh pr create --repo OWNER/REPO \
  --title "Reject unsupported URL schemes in document loaders" \
  --body-file /tmp/pr-body.md \
  --base main --label bug --reviewer dahlia
~~~~

The body file is the draft with the frontmatter removed; the script does that.
Pass the draft itself as `--body-file` and the API shows the YAML as the first
thing in the PR.

`--draft`, `--label`, `--reviewer`, `--assignee`, and `--milestone` exist on
GitHub and on recent `gh` releases; `gh pr create --help` on the machine settles
what this one accepts. A draft that revises an existing PR is posted with
`gh pr edit N --body-file /tmp/pr-body.md` instead.


Reference
---------

 -  *references/structure.md*: shapes for small fixes, features, refactors,
    dependency and documentation changes, and large multi-part PRs, with
    examples of the heading scale in practice.
 -  *references/frontmatter.md*: the full frontmatter schema and the mapping to
    `gh pr create`.
 -  *references/forges.md*: where GitHub, GitLab, Gitea, and Forgejo keep their
    pull request templates, and how to read them without the GitHub CLI.
