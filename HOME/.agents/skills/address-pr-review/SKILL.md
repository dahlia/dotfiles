---
name: address-pr-review
description: Work through unresolved review comments on a GitHub pull request end-to-end — read each thread, fix the code where the reviewer is right, post a short reply explaining yourself where they aren't, group the fixes into well-scoped commits whose messages link to the comments they address, push, post follow-up replies naming the commit that resolved each thread (with bare commit hashes so GitHub auto-links them), resolve every triaged thread, and re-trigger Codex / Gemini if either bot reviewed the PR. Use this skill whenever the user asks to "address PR review", "handle review comments", "respond to review feedback", "apply review suggestions", "go through the review on PR #N", "리뷰 처리", "리뷰 댓글 반영", or anything else that boils down to "deal with the review on this PR" — even when they don't say the word "skill" and even when they only mention one piece of the workflow (e.g. "just resolve the threads"), because doing one piece in isolation usually leaves the PR in a half-addressed state.
---

Address PR review comments
==========================

End-to-end workflow for clearing unresolved review feedback on a GitHub pull
request. The whole thing is one task: triage → fix → commit → push → reply →
resolve → re-trigger bots. Skipping any step leaves the PR looking half-handled
to reviewers, so do them all.


Inputs
------

The user gives one of:

 -  A PR number or URL → use it directly.
 -  Nothing → assume the PR matching the current branch (`gh pr view`).

If the working directory isn't the repo for the PR, stop and ask. If the PR's
branch isn't checked out, check it out and pull before changing anything —
committing on top of stale code wastes everyone's time.

Capture `owner`, `repo`, `pr_number`, and the PR's base branch up front; you'll
need them in nearly every step:

~~~~ bash
gh pr view PR_OR_BLANK --json number,headRefName,baseRefName,url,headRepository,headRepositoryOwner,state
~~~~

If the PR state is `CLOSED` or `MERGED`, stop and ask the user — pushing more
commits won't do what they want.


Step 1 — Fetch review threads and review bodies
----------------------------------------------

The REST review-comments endpoint cannot tell you whether a thread is resolved,
so use GraphQL for inline threads. This call gets their metadata (thread node
IDs for resolving, comment databaseIds for replying, comment URLs for permalinks in
commit messages, file/line for reading the code in context):

~~~~ bash
gh api graphql -F owner=OWNER -F repo=REPO -F pr=PR_NUMBER -f query='
query($owner: String!, $repo: String!, $pr: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      reviewThreads(first: 100) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          isOutdated
          comments(first: 50) {
            pageInfo { hasNextPage endCursor }
            nodes {
              id
              databaseId
              author { login }
              body
              path
              line
              originalLine
              url
              diffHunk
            }
          }
        }
      }
    }
  }
}'
~~~~

Follow `pageInfo.hasNextPage` with `after: endCursor` for both threads and
comments; the first page is not necessarily the complete review.

Filter to threads where `isResolved == false`. For each unresolved thread,
remember:

 -  `id` — the thread's GraphQL node ID, needed for resolving in Step 7.
 -  First comment's `databaseId` — needed for posting a REST reply in Step 6.
    (Replies always thread off the *original* comment's databaseId, even if the
    thread has back-and-forth.)
 -  First comment's `url` — the permalink that goes in the commit message.
 -  `path`, `line` (or `originalLine` if `line` is null because the thread is
    outdated), `diffHunk` — needed to read the code in context.
 -  The first comment's `body` — the actual feedback to triage.

Note any later comments in each thread too: if the reviewer and author have
already gone back and forth, the latest state of the conversation matters more
than the original comment.

### CodeRabbit findings in the review body

Also fetch the PR reviews themselves, even when there are zero unresolved
inline threads:

~~~~ bash
gh api --paginate repos/OWNER/REPO/pulls/PR_NUMBER/reviews \
  --jq '.[] | {id, author: .user.login, body, html_url, submitted_at, commit_id, state}'
~~~~

CodeRabbit (`coderabbitai`, also `coderabbitai[bot]`) can put **Outside diff
range comments** in the overall review's `body`. This is a `PullRequestReview`
body, not a `reviewThreads.comments[].body` or a PR conversation comment.
`reviewThreads` and REST `pulls/.../comments` alone do not contain these
findings. Read the complete CodeRabbit review bodies, including nested
`<details>` sections; a summary count or login-only query is not enough.

Extract each individual finding in that section, preserving its parent review
`id`/`html_url`, file path, line range, and full feedback. Treat it as a review
item alongside inline threads. Do not treat walkthroughs, statistics, or
non-actionable summaries as findings. Inspect all review pages, not only the
latest review: a later review without the section does not close older items.

Deduplicate repeated findings across reviews and inline threads by the actual
concern and location. Check current code, subsequent discussion, and existing
fixes/replies before deciding whether an older finding still needs work; review
state (including `DISMISSED`) is not a per-finding resolution flag. To check
earlier body-only replies, fetch top-level PR discussion as well:

~~~~ bash
gh api --paginate repos/OWNER/REPO/issues/PR_NUMBER/comments
~~~~

Keep an explicit disposition for each finding: fix, decline with evidence,
already addressed with evidence, or pending. For body-only items, use the parent
review's `html_url` as the commit reference and record path/line or a short
finding label to distinguish items sharing that URL.

Only report nothing to address after checking **both** unresolved inline
threads and outstanding review-body findings. If either fetch fails or is
incomplete, report that limitation instead of claiming the PR is clear. If
neither source has outstanding items, stop without re-triggering bots.


Step 2 — Triage each thread
---------------------------

For each unresolved thread or outstanding review-body finding, read the
surrounding code and decide. Body-only findings may have no `diffHunk`; use
their path/line range and inspect the current file directly:

 -  **Valid** — the reviewer is right, or there's a reasonable interpretation
    under which they're right, or you can address the underlying concern even
    if the literal suggestion is off. Plan a fix.
 -  **Invalid** — the reviewer misread the code, the suggestion conflicts with
    project conventions you can verify in-tree, the request is out of scope for
    this PR, or the code is intentionally that way for a reason worth
    explaining. Plan a short reply.

Default toward “valid”: reviewers know their codebase, and reply-and-decline
should be the minority outcome. If the literal suggestion is wrong but the
underlying concern is real, fix the underlying concern and say so in the reply.

If you're genuinely unsure whether a comment is valid, ask the user before
doing anything irreversible. Don't guess on judgment calls.


Step 3 — Make the fixes and commit
----------------------------------

Apply edits for the valid review items, including body-only findings. Group
commits by **topical relatedness**, not by reviewer or by thread:

 -  Multiple threads converging on the same issue → **one** commit.
 -  Unrelated fixes → **separate** commits.
 -  A single fix that touches many files → still **one** commit.

A commit's scope is “what changed and why,” not “which review thread asked for
it.” Two unrelated comments that both happen to touch the same file should
still be two commits; one comment that requires changes across five files is
still one commit.

For each commit:

1.  Stage the files for that fix (`git add <specific paths>`, never `-A`/`-.`).

2.  Invoke the `commit` skill to write the commit. Don't write the commit
    yourself — defer to the skill so message style, hook handling, and the
    `Assisted-by` trailer stay consistent. After the skill commits, you'll add
    the `Addresses:` block; see the next bullet for how.

3.  The commit message body **must** include the permalink of every review
    item the commit addresses (`url` for inline comments, `html_url` for parent
    reviews of body-only findings from Step 1), one distinct URL per line,
    as bare URLs in the body. No section header (no `Addresses:`
    line); no `-` bullet prefix; just the URLs themselves, separated from the
    rest of the body by a blank line. This matches the `commit` skill's
    convention of putting bare reference URLs in the body. Pass this
    requirement into the commit-skill invocation so it's included from the
    start. Example body:

    ~~~~
    Use a stable cache key for the resolver lookup

    The previous key embedded the request ID, so two requests for the
    same resource never shared a cache entry.

    https://github.com/acme/widgets/pull/482#discussion_r1234567890
    https://github.com/acme/widgets/pull/482#discussion_r1234567891

    Assisted-by: Codex:gpt-5.5
    ~~~~

Don't amend earlier commits to bolt on later fixes — make new commits.
Reviewers who already saw your earlier work will get confused by force-pushed
history.


Step 4 — Push
-------------

~~~~ bash
git push
~~~~

That's almost always enough — you're appending commits to an existing PR
branch, not rewriting it. If the push is rejected because the branch has
diverged, stop and investigate; don't reflexively force-push.


Step 5 — Look up the exact commit hashes via `git log`
------------------------------------------------------

This is the step most likely to be silently wrong. Hashes you “remember” from a
few minutes ago may be from before a rebase, an amend, or a hook-induced
re-commit. `git log` is the only authority. Run it after pushing:

~~~~ bash
BASE=$(git merge-base "origin/$(gh pr view --json baseRefName -q .baseRefName)" HEAD)
git log --format='%H %s' "$BASE"..HEAD
~~~~

For each commit you just made, copy the full hash directly from this output.
Prefer the full hash in replies; use a short, unambiguous hash only when there
is a reason, such as a repository convention. Map each hash to the set of
comment IDs / URLs it addresses; you'll use this map in Step 6.

**Do not skip this step.** Posting a wrong hash makes the reply useless and the
PR confusing.


Step 6 — Reply on each thread
-----------------------------

Prepare every reply using the writing and formatting guidance below before
posting. For each thread you addressed, name the commit that fixed it.
Use the original comment's `databaseId` from Step 1 — replies always thread off
the original, even when the conversation has continued:

~~~~ bash
gh api repos/OWNER/REPO/pulls/PR_NUMBER/comments/COMMENT_DATABASE_ID/replies \
  -X POST -F body=@REPLY_FILE
~~~~

For each thread you're declining, post a short reply (1–2 sentences) explaining
why. Be concrete and non-defensive: name the constraint, convention, or intent
that makes the suggestion not apply. Match the language of the surrounding
conversation; if the reviewer wrote in Korean, reply in Korean.

### Replying to body-only findings

A review body has a review ID, not an original inline comment `databaseId`.
Do not pass its ID to the inline reply endpoint. If the finding also has an
inline thread, reply there and track both occurrences as one item. Otherwise,
post a top-level PR comment using `gh pr comment PR_NUMBER --body-file FILE`.
Link the parent review and identify each finding by path/line or a short label;
state its fix with a verified bare commit hash, reasoned decline, or evidence
that it was already addressed. Group body-only replies when useful, but give
each finding an explicit outcome. Check existing replies to avoid duplicates.

### Writing and formatting replies

Apply this guidance to inline thread replies and top-level PR comments about
body-only findings, whether fixing, declining, or documenting an existing fix.
First inspect repository guidance for issue/PR writing, such as *AGENTS.md*,
*CONTRIBUTING.md*, linked style guides, and *.github/* templates. Follow any
applicable repository rules first; the defaults below apply only where they
do not conflict with those rules.

 -  Write commit hashes as plain text, without backticks or other Markdown
    wrappers, so GitHub can auto-link them. Prefer the full hash verified in
    Step 5.
 -  Italicize filenames, filesystem paths, extensions, and simple glob patterns
    in prose: *parser.ts*, *src/parser.ts*, *.ts*, and *\*.test.ts*. Escape
    literal Markdown metacharacters so the rendered glob remains intact.
    URL paths are different: use code spans, such as `/api/users`, rather than
    italics. Keep code identifiers and actual code in code spans/blocks.
 -  Avoid em dashes within sentences; use a colon, semicolon, comma,
    parentheses, or separate sentences. An em dash may join an item label to
    its content, but prefer a colon even there.
 -  Prefer italics to bold for emphasis in sentences, and emphasize sparingly.
    Bold is acceptable for item labels and key columns in tables.
 -  Draft with straight quotes and straight apostrophes. Let Hongdown apply
    the configured typography; do not manually introduce curly characters or
    undo its punctuation formatting afterward.
 -  Do not hard-wrap prose. Keep each paragraph on one source line; preserve
    meaningful breaks for lists and code blocks.

Use [Wikipedia's Signs of AI writing] as a reference when reviewing the draft,
rather than as a mechanical banned-word list. Keep replies specific to the
finding: state the change and relevant validation, or the concrete evidence
for declining it. Remove inflated claims, generic praise, vague attribution,
formulaic contrasts, filler transitions, and redundant summaries. Do not
claim tests or checks that you did not run, or add elaborate headings and
lists to a reply that needs only a sentence or two.

[Wikipedia's Signs of AI writing]: https://en.wikipedia.org/wiki/Wikipedia:Signs_of_AI_writing

### Format the final body with Hongdown

Save the draft to a UTF-8 Markdown file and format it with Hongdown before
posting. Always include `--no-line-width` to prevent hard wrapping:

~~~~ bash
hongdown --no-line-width --write REPLY_FILE
hongdown --no-line-width --check REPLY_FILE
~~~~

Run from the repository directory so its Hongdown configuration applies. If
repository writing rules require different formatter settings, use a
comment-specific configuration with `--config` instead of changing repository
or global configuration. Keep `--no-line-width` in the invocation; if an
explicit repository rule requires wrapping, honor that higher-priority rule
in the final body.

If `hongdown` is absent from PATH or its shim cannot run, use mise:

~~~~ bash
mise x aqua:dahlia/hongdown -- hongdown --no-line-width --write REPLY_FILE
mise x aqua:dahlia/hongdown -- hongdown --no-line-width --check REPLY_FILE
~~~~

If mise is unavailable or cannot provide Hongdown, download the latest
platform/architecture-appropriate binary from [Hongdown's latest release],
extract it into a temporary directory, and invoke that binary with
`--no-line-width`. Consult the [Hongdown README] for current usage and
configuration details. If formatting still fails, retain the draft and report
the failure rather than posting an unformatted body.

Read the formatted file before posting: verify the hashes against Step 5,
the rendered path/glob emphasis, URL code spans, repository conventions, and
factual claims. Any subsequent edit must go through Hongdown again. Publish
the exact formatted file using `-F body=@REPLY_FILE` for an inline REST reply
or `gh pr comment PR_NUMBER --body-file REPLY_FILE` for a body-only finding;
do not reconstruct the body in a shell string.

[Hongdown's latest release]: https://github.com/dahlia/hongdown/releases/latest
[Hongdown README]: https://raw.githubusercontent.com/dahlia/hongdown/refs/heads/main/README.md


Step 7 — Resolve the threads
----------------------------

Resolve **every** thread you triaged in Step 2 — both the ones you fixed and
the ones you declined. “Resolved” means “this conversation is concluded,” not
“the reviewer was right.” A reasoned decline is concluded.

~~~~ bash
gh api graphql -F threadId=THREAD_NODE_ID -f query='
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}'
~~~~

Body-only findings have no thread node ID and cannot be resolved with
`resolveReviewThread`. Record their disposition and reply instead; do not
invent IDs or claim GitHub marked them resolved.

The one judgment exception: if you posted a decline reply on a thread where the
reviewer is likely to push back and the conversation feels live, you can leave
it unresolved and let them respond. Use this sparingly — the default is resolve.


Step 8 — Re-trigger Codex / Gemini if they previously reviewed
--------------------------------------------------------------

**Only run this step if you actually pushed at least one new commit in Step 4.**
If every thread was declined as invalid and no code changed, the bots have
nothing new to look at; re-running them just produces a duplicate review and
adds noise to the PR. In that case, skip this step entirely and continue to Step 9.

If you did push commits, list the reviewers and review-comment authors on the
PR:

~~~~ bash
gh api repos/OWNER/REPO/pulls/PR_NUMBER/reviews --jq '.[].user.login' | sort -u
gh api repos/OWNER/REPO/pulls/PR_NUMBER/comments --jq '.[].user.login' | sort -u
~~~~

 -  If any login looks like **Codex** (typically contains `codex`, e.g.
    `chatgpt-codex-connector`), post a top-level PR comment:

    ~~~~ bash
    gh pr comment PR_NUMBER --body "@codex review"
    ~~~~

 -  If any login looks like **Gemini** (typically `gemini-code-assist[bot]` or
    contains `gemini`), post:

    ~~~~ bash
    gh pr comment PR_NUMBER --body "/gemini review"
    ~~~~

Both apply if both bots reviewed. Neither applies if neither did — skip the
step entirely. These are top-level PR comments (`gh pr comment`), not replies
on specific threads.


Step 9 — Verify remaining feedback and summarize
------------------------------------------------

Before reporting completion, fetch review threads and CodeRabbit review
bodies again using Step 1, including pagination. Reconcile them with your
dispositions and check for newly posted or edited findings. Zero unresolved
threads alone does not prove that review-body findings were handled. Report
any remaining or newly arrived items as pending; do not loop indefinitely
waiting for bots.

Briefly summarize how many inline threads and body-only findings you addressed
or declined, which commits you pushed, whether you re-triggered bots, and any
pending findings or incomplete fetches.


Common failure modes
--------------------

 -  **Treating REST `pulls/.../comments` as the source of truth for resolved
    status.** It doesn't expose `isResolved`. Always use GraphQL
    `reviewThreads`.
 -  **Stopping at zero unresolved threads.** CodeRabbit outside-diff findings
    can exist only in a review body. Fetch paginated `pulls/.../reviews`, read
    the nested sections, and track their outcomes separately.
 -  **Replying to the latest comment in a thread instead of the original.** The
    reply endpoint takes the *original* review comment's `databaseId`; that's
    how GitHub knows which thread to attach the reply to.
 -  **Backticking the commit hash in replies.** Re-read every reply before
    posting. Bare hash. No backticks. No code span.
 -  **Forgetting to resolve declined threads.** Resolution is about the
    conversation being done, not about who was right.
 -  **Skipping `git log` and trusting a remembered hash.** The hash you
    remember may be from a pre-hook commit that no longer exists. Always
    re-read after pushing.
 -  **Amending to “tidy up” instead of adding new commits.** Reviewers who've
    already looked at your branch get confused by force-pushed history. Add
    commits forward.
 -  **Triggering the wrong bot incantation.** Codex uses `@codex review`
    (mention syntax). Gemini uses `/gemini review` (slash command). They are
    not interchangeable.
