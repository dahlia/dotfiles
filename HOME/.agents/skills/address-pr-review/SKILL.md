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


Step 1 — Fetch unresolved review threads
----------------------------------------

The REST review-comments endpoint cannot tell you whether a thread is resolved,
so use GraphQL. One call gets you everything you need (thread node IDs for
resolving, comment databaseIds for replying, comment URLs for permalinks in
commit messages, file/line for reading the code in context):

~~~~ bash
gh api graphql -F owner=OWNER -F repo=REPO -F pr=PR_NUMBER -f query='
query($owner: String!, $repo: String!, $pr: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          isOutdated
          comments(first: 50) {
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

If there are zero unresolved threads, tell the user and skip ahead to Step 8
(the bot re-trigger may still apply, e.g. if the user is asking you to re-run
an automated review on a PR with no human comments).


Step 2 — Triage each thread
---------------------------

For each unresolved thread, read the surrounding code (`path` + `line` +
`diffHunk`) and decide:

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

Apply edits for the valid threads. Group commits by **topical relatedness**,
not by reviewer or by thread:

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

3.  The commit message body **must** include the permalink (`url` field from
    the GraphQL response in Step 1) of every comment that commit resolves, one
    URL per line, as bare URLs in the body. No section header (no `Addresses:`
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
git log --oneline "$BASE"..HEAD
~~~~

For each commit you just made, copy the hash directly from this output — full
40-char hash or short hash (7+ chars) both work as long as the short form is
unambiguous. Map each hash to the set of comment IDs / URLs it addresses;
you'll use this map in Step 6.

**Do not skip this step.** Posting a wrong hash makes the reply useless and the
PR confusing.


Step 6 — Reply on each thread
-----------------------------

For each thread you addressed, post a reply naming the commit that fixed it.
Use the original comment's `databaseId` from Step 1 — replies always thread off
the original, even when the conversation has continued:

~~~~ bash
gh api repos/OWNER/REPO/pulls/PR_NUMBER/comments/COMMENT_DATABASE_ID/replies \
  -X POST -f body="Addressed in abc1234."
~~~~

For each thread you're declining, post a short reply (1–2 sentences) explaining
why. Be concrete and non-defensive: name the constraint, convention, or intent
that makes the suggestion not apply. Match the language of the surrounding
conversation; if the reviewer wrote in Korean, reply in Korean.

### Two formatting rules for replies

**1. Bare commit hashes, no backticks.** GitHub auto-links commit hashes in
comments only when they are plain text. Wrapping a hash in backticks blocks the
auto-link and the reader loses the click-through. Other Markdown (bold,
italics, links, lists, fenced code blocks for actual code) is fine; just never
put the hash itself inside backticks or a code span.

 -  Good: `Addressed in abc1234: split the cache key as you suggested.`
 -  Good: `**Addressed in abc1234.** Tests added in def5678.`
 -  Bad: `` Addressed in `abc1234`. ``  (backticks defeat auto-linking)
 -  Bad: ```` Addressed in ```abc1234```. ```` (same problem)

**2. No em dashes (—) in replies.** Use a semicolon, colon, comma, or
parentheses instead, or break into two sentences. This applies to every reply
you post on a review thread, both the “addressed in X” replies and the
decline-with-explanation replies.

 -  Good: `Addressed in abc1234; the request ID is no longer part of the key.`
 -  Good: `Addressed in abc1234: the request ID is no longer part of the key.`
 -  Good:
    `Declined: this path is intentionally synchronous because the caller holds a transaction.`
 -  Bad: `Addressed in abc1234 — the request ID is no longer part of the key.`
 -  Bad: `Declined — this path is intentionally synchronous.`

Re-read each reply before posting and check both rules: every hash bare, no em
dashes anywhere.


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

The one judgment exception: if you posted a decline reply on a thread where the
reviewer is likely to push back and the conversation feels live, you can leave
it unresolved and let them respond. Use this sparingly — the default is resolve.


Step 8 — Re-trigger Codex / Gemini if they previously reviewed
--------------------------------------------------------------

List the reviewers and review-comment authors on the PR:

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

This is the end of the workflow. Briefly summarize to the user what you did:
how many threads addressed, how many declined, which commits you pushed,
whether you re-triggered any bots.


Common failure modes
--------------------

 -  **Treating REST `pulls/.../comments` as the source of truth for resolved
    status.** It doesn't expose `isResolved`. Always use GraphQL
    `reviewThreads`.
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
