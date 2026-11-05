Shaping the body
================

The shape follows the change and the risk, not the number of files. These
patterns are for drafts with no template; a template always wins.


Start from the size
-------------------

A one-line dependency bump is one paragraph and a sentence about testing. A
bug fix with a subtle invariant is two or three paragraphs. A feature that
changes a public API may need four paragraphs and one `##` section for
migration.

Rough guide:

 -  One or two short paragraphs: a typo fix, a dependency bump, a small
    documentation correction, a narrow bug fix.
 -  Two or three paragraphs: a normal fix or feature with one non-obvious
    decision.
 -  Two or three `##` sections: a large feature, a breaking change with
    migration steps, a refactor whose scope needs explanation.

The count comes from the content. Do not pad to reach a pattern, and do not cut
the one paragraph that explains a risky decision to keep the PR short.


Small fixes
-----------

Lead with the symptom and the cause, then say what changed. Mention the test
only if it is not obvious.

~~~~ markdown
Loading a document from a `file:` URL bypassed the scheme check whenever
`allowPrivateAddress` was enabled, so a crafted redirect could make the loader
read from the local filesystem.

`getDocumentLoader()` and `getAuthenticatedDocumentLoader()` now reject
anything other than HTTP and HTTPS before fetching, independent of the address
setting. The guard lives in *packages/vocab-runtime/src/docloader.ts* and in
*packages/fedify/src/utils/docloader.ts*, which `doubleKnock()` calls before
following a redirect.

Fixes #1292.
~~~~

Notice what is absent: a list of changed files, a restatement of the title, and
a “Testing” heading for a change whose tests are in the diff.


Normal fixes
------------

Add the constraint that made the obvious fix wrong, or the alternative that was
rejected. This is where a reviewer learns whether the change is safe.

~~~~ markdown
The queue worker treated a cancelled HTTP request as a failed delivery and
retried it, which duplicated activities that had already reached the remote
inbox.

Cancellation now ends the attempt without scheduling a retry. The worker keeps
the response if one was established, so a cancellation after a `202 Accepted`
still records delivery. Transport failures keep the old retry behavior.

This deliberately does not distinguish a client disconnect from a server
timeout. Both surface as a cancelled request in the current runtime, and
splitting them needs the execution signal tracked in #1311.
~~~~

The last paragraph is the kind of scope note that prevents a review comment
asking for work that belongs in another PR.


Features
--------

Describe the situation that motivates the feature before the API. A reviewer
who understands the workaround can judge whether the new surface is the right
one.

~~~~ markdown
Applications that need durable records of outgoing ActivityPub work currently
have to reconstruct them from tracing spans, which disappear when sampling is
off and do not survive a queue handoff.

This adds `setOutboundObservers()` for broadcast preparation, destination
planning, send attempts, HTTP exchanges, and queue-worker results. Observer
failures are isolated from delivery, so a broken observer cannot turn a
successful send into a retry.

The API preserves explicit execution identities across queue continuations.
Applications own persistence and aggregate state; enqueue acceptance and a
remote `2xx` response do not establish that the remote application acted on the
activity.

Related to #1316.
~~~~

When the feature has a migration or a breaking change, give it a `##` section.
Do not bury it in the third paragraph.


Refactors
---------

A refactor PR has to show the cost of the old shape and the invariant the new
shape preserves. “Clean up” is not a reason a reviewer can evaluate.

~~~~ markdown
Adding the `markdown` output format meant editing four separate `match`
statements, and the one in `render()` still fell back to plain text, so
`--format markdown` produced the right file name and the wrong contents.

Output selection now goes through one `Renderer` trait that each format
implements. The four call sites call `renderer.for_format()`, and an unknown
format is a startup error instead of a silent fallback.

No behavior changes for the existing formats; the new format gains the same
tests as the others.
~~~~


Dependency and documentation changes
------------------------------------

Say what forced the bump or the edit, not just what version moved. For a
dependency, name the incompatibility and the constraint. For documentation,
name the wrong or missing sentence and what a reader would conclude from it.

~~~~ markdown
The manual installed the OpenTelemetry packages without versions. Since SDK 3.x
became `latest` on npm, following it produced a peer conflict with Fedify 2.x,
which is built against SDK 2.x.

This pins the install commands to the versions the workspace catalog already
uses and installs `@opentelemetry/api` explicitly, because both chapters import
it and Deno does not expose transitive npm packages to bare imports.

No changelog fragment; this is a documentation-only change.

Fixes #1344.
~~~~


Large or multi-part changes
---------------------------

A PR that touches several surfaces may need `##` sections. Keep them few and
name them for the reader's question, not for the code's directory layout.

~~~~ markdown
## Migration

Existing `KvStore` implementations keep working. Implementations that support
atomic batches should add `casMany()`; the conformance suite in
*packages/kv/src/conformance.ts* covers the new contract.

## Breaking changes

`cas()` now throws `KvConflictError` instead of returning `false`. Callers that
branch on the return value should catch the error or use `casMany()`.

## Out of scope

Redis Cluster key scope and adapter codecs are tracked in #1402.
~~~~

Three sections is usually the ceiling. If the draft needs five, the PR is
probably too large to review well; say so in the handover instead of writing a
manual for it.


What does not need its own section
----------------------------------

 -  “Summary” at the top that repeats the title.
 -  “Changes” listing every file the diff already shows.
 -  “Testing” with “N/A” or “should pass”.
 -  “Screenshots” for a change with no visual surface.
 -  A closing “Thoughts?” unless a decision is genuinely open.
 -  A checklist the template did not require, especially items like “I have
    read the contributing guide” that the author cannot verify on the reader's
    behalf.
