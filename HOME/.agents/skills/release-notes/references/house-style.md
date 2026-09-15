House style
===========

Conventions recovered from the maintainer's existing release notes across
upyo, optique, logtape, fedify, botkit, and hollo. When the project has its
own previous release notes, those outrank anything generic here.


Title
-----

`<Project> <version>: <theme phrase>`, in sentence case, where the theme
phrase is a comma-separated list of three to five of the most interesting
features, ending with “and”. Code identifiers and package names keep their
backticks or italics inside the title.

Examples:

 -  Upyo 0.6.0: MIME composition, streaming attachments, and calendar
    invitations
 -  Fedify 2.3.0: OpenTelemetry metrics, delivery circuit breaker,
    `@fedify/backfill`, and `fedify bench`
 -  Hollo 0.9.0: Redesigned UI, passkey authentication, FEP-044f quote
    authorization, and major performance improvements

A major release can drop the feature list in favor of the event itself
(“Optique 1.0.0: environment variables, interactive prompts, and 1.0 API
cleanup” still lists features; “Fedify 2.0.0: Modular architecture, debug
dashboard, and relay support” leads with the architecture change). Avoid
filler adjectives in the title.


Opening
-------

Two or three paragraphs, in this order (the first two can swap when a release
is defined by its headline feature):

1.  **The project blurb.** One or two sentences stating what the project is,
    who it is for, and the one thing that distinguishes it. Link the project
    name to its site. Reuse the wording from previous release notes for the
    same project rather than writing a new description each time. Some
    projects italicize the whole sentence; match what the corpus does.
2.  **The release summary.** A paragraph naming the theme of the release and
    its headline additions, so a reader can decide in ten seconds whether to
    keep reading. Often closes with “Here's what changed.” For a major or
    unusually large release, name that fact (“the largest release since
    0.7.0”, “the most significant release in Fedify's history”) and point to
    the breaking-changes or migration section if one follows.
3.  **Optionally, a screenshot or two**, as raw `<img>` tags with descriptive
    `alt` text, when the release changes something visual. Image URLs point at
    GitHub user-attachment URLs.

Link definitions for the blurb sit immediately after it.


Feature sections
----------------

One `##` section per substantial feature, in rough order of importance, not
in changelog order. A section is prose, not bullets. The reliable shape is:

1.  **The problem.** What users could not do, or what broke. Concrete and
    specific (“a remote instance goes down, local followers keep generating
    deliveries to it, and workers spend their retry budget on a host that
    isn't coming back”). This is the part that earns the reader's attention.
2.  **When it is useful.** One or two sentences naming a representative
    situation where the feature pays off, so the reader can tell whether it
    applies to them (“A test helper that wants a buffering sink for one test
    case, or a request handler that wants verbose output for one request”). A
    section has to answer “when would I reach for this?”, not only “what is
    this?”. When the problem paragraph already contains the scenario, fold it
    in there rather than repeating it.
3.  **The solution.** How the release addresses it, naming the API, option, or
    package exactly as it appears in the code.
4.  **A code example.** Short, realistic, runnable-looking, fenced as
    `typescript`, `bash`, `sh`, `text`, or `json`. Show the interesting line,
    not the whole program. Comments in examples are fine and often clarify.
5.  **Caveats and limits.** Which transports/runtimes are supported, which are
    not, what the default is, what the application is still responsible for.
    Saying what a feature does *not* do is a trust signal, not a weakness.
6.  **A documentation link inline**, in the sentence that introduces the
    feature or right after the example (“See the \[MIME composition guide] for
    streaming output, signing, and attachment lifetime rules.”). Never park
    links in a separate list.
7.  **Issue and PR numbers**, bare, worked into the sentence that describes
    the change (“The original proposal and implementation are in #812 and
    #818.”), not appended as a closing parenthetical. See “Referring to issues
    and PRs” below.
8.  **A shoutout** when an external contributor did the work (see below).

Headings are sentence case and unnumbered. They may name a function or
package inline (`deferredValue()` for handler-time fallbacks, `fedify bench`,
`@optique/derived-defaults`: fallbacks computed from earlier options) or state
the capability in plain words (Streaming attachments, Circuit breaker, Compact
help for large command trees). Use title case only where the feature name
itself demands it.

### Example code

For a library, examples are the fastest way for a reader to judge the API.
Each substantial feature gets a short one, and the release as a whole should
include at least one example close to end-to-end: the imports, the setup, the
call, and what comes back. A reader should be able to skim it and understand
the shape of the API without opening the docs.

Examples must be verified, not merely plausible. A wrong example is worse than
no example, because readers copy it and it fails. Before including one:

 -  Run it. Write the snippet to a temporary file outside the repository and
    execute it against the checked-out source, or against a scratch install of
    the published package, using the project's own runtime (Deno, Node.js, or
    Bun).
 -  When it cannot run standalone because it needs a server, credentials, or a
    database, type-check it and confirm every imported symbol and option exists
    in the source at that version by grepping the exports.
 -  Check option names, defaults, and return shapes against the source and the
    docs, not from memory.
 -  Drop any line you could not verify. Do not present a fragment as a runnable
    program when it is not.

### New packages

A new package gets its own section (or `###` subsection under a
“New packages” / “New integrations” heading when there are several) and its
own `### Installation` subsection with per-package-manager commands:

~~~~ bash
npm  add     @upyo/maileroo
pnpm add     @upyo/maileroo
yarn add     @upyo/maileroo
deno add jsr:@upyo/maileroo
bun  add     @upyo/maileroo
~~~~

Align the package managers into columns, and note in prose when a package is
JSR-only or npm-only.


Links
-----

 -  Reference-style links (`[label]: url`) with the definitions collected after
    the paragraph or at the end of the section. hongdown manages placement; you
    just write the reference or an inline link.
 -  Documentation links go to the published site, deep-linked to the relevant
    heading when one exists (`.../smtp#oauth-20-authentication`).
 -  Project blurb links to the project site; the version-specific *CHANGES.md*
    link in the closing points at the tag
    (`https://github.com/OWNER/REPO/blob/1.2.0/CHANGES.md` or `#version-120`).
 -  Issue and PR numbers are bare `#123`, never Markdown links.
 -  Cross-repository references use `owner/repo#123`.
 -  Third-party projects mentioned in prose get a link on first mention
    (`[Lemmy]`, `[Piscina]`, `[Inquirer.js]`).


Referring to issues and PRs
---------------------------

Numbers are bare (`#123`) because GitHub links them automatically, and they
belong inside the sentence that describes the change, not in a bundle tacked
onto the end of it. Prefer the optique 834 shape:

> The original proposal and implementation are in #812 and #818.
> The design rationale and alternative approaches considered are discussed in
> #819. This work was tracked in #472 and implemented in #611. Thanks to
> @narekhovhannisyan for contributing the Mailtrap transport in #32. That
> limitation, raised in #16, is gone in this release. The full design is spread
> across #27 through #33. #12 and #35 cover the rest of it.

Avoid the trailing-bundle shape (logtape 197):

> See the scoped configuration documentation for the full lifetime and
> nesting rules. (#185, #188)
> … keeping any remaining arguments under an `args` property. (#184)

The difference looks small on the page and matters to the reader: “in #812 and
#818” says which number is the proposal and which is the implementation, while
a closing “(#185, #188)” leaves them to guess. When a change spans many
numbers, spread them through a sentence (“landed in #869, #870, and #912”) or
anchor each to the phrase it belongs to (“the SMTP extension requests (#42,
#43, #44, #45, and #46), envelope overrides (#52), and streaming attachments
(#56)”). A parenthetical cluster is acceptable when it sits mid-sentence and
every number is anchored to something; the sentence-final dump is the thing to
avoid. If a sentence cannot absorb the numbers, that is often a sign the
change needs a sentence of its own rather than a citation.

Shoutouts and cross-repository references follow the same rule: put them in
the sentence (“tracked in fedify-dev/fedify#651”, “proposed the feature in
#886 and implemented it in #956”) rather than appending them.

Bulleted lists relax the rule. A bullet in “Other changes”, “Smaller
improvements”, or “Bug fixes” is already a self-contained item, and the
reference is metadata, so it may end with a short parenthetical bundle:

>  -  Fixed `Endpoints.toJsonLd()` emitting invalid `"type": "as:Endpoints"` in
>     JSON-LD output. (#576)
>  -  Public profile pages now include dedicated *Followers* and *Following*
>     pages (#491).

Keep the bundle to the end of the bullet and keep it short. Reserve the
sentence-final bundle for lists; do not use it in prose sections.


Shoutouts
---------

External contributions must be credited; this matters more than any other
convention here. Three forms, in increasing weight:

 -  **Inline, in the feature section the person worked on**, as the last
    sentence: “Thanks to @narekhovhannisyan for contributing the Mailtrap
    transport in #32.” or “Jiwon Kwon (@sij411) contributed the
    `@fedify/backfill` package in #275, #779, and #816.” Use the handle, and add
    the real name when it is known and the project's corpus does so.

 -  **An Acknowledgments section** when several people contributed, listing
    each as `**Name** (@handle): what they did`:

    ~~~~
     -  **ChanHaeng Lee** (@2chanhaeng): `@fedify/uri-template`
     -  **Jiwon Kwon** (@sij411): `@fedify/backfill`
    ~~~~

 -  **A special thank-you** when one person shaped much of the release, at the
    end of the notes: “A special thank-you to @gerardp, whose feature proposals
    shaped much of this release.”

Credit the person who implemented a change, and separately the person who
proposed or reported it when they differ. Do not credit maintainers as if they
were outside contributors.


Supporting sections
-------------------

 -  **Breaking changes at a glance** (major releases): a bulleted list near the
    top, each item naming the removed or changed API, what replaces it, and the
    PR numbers. Bullets are appropriate here because the items are discrete.
 -  **Upgrading**: the exact commands for each install method (npm/Deno/Docker/
    etc.), then either the breaking changes or an explicit “There are no
    breaking changes in X.Y.0.”
 -  **Migration guide**: numbered steps with before/after code for a major
    release, one step per behavior change.
 -  **Other changes** / **Smaller improvements**: prose (or a short bulleted
    list) for items too small for their own section. Keep each to a sentence or
    two.
 -  **Bug fixes**: a bulleted list, each with the symptom and the fix in one
    sentence, with `#NNN` references.
 -  **Performance**: when a release is about speed, give real numbers with
    before/after (“from 2.5s to 1.9s”, “an 85% improvement”) and say what
    changed.


Closing
-------

Match the project's existing convention. The corpus has three shapes:

 -  upyo: a horizontal rule, then “For the complete changelog and technical
    details, see \[*CHANGES.md*].” and “For questions or issues, please visit
    our \[GitHub repository].”
 -  logtape: “There are no breaking changes in 2.2.0. See the \[full changelog]
    for complete details.”
 -  fedify: “See \[*CHANGES.md*] for the complete changelog.”

Some projects end with an invitation to Discussions or Matrix (botkit). If the
corpus does, keep it; if it does not, do not add one.


Tone
----

Aim for understated and matter-of-fact. The changes are the story, and the
prose should not compete with them. This is the hardest thing to get right,
because the default register of release announcements is promotional and the
maintainer does not want that. Plain does not mean dull: the reader should
still finish each section knowing why the change matters, just without being
told how to feel about it.

 -  State what changed and what it enables; let specifics carry the weight.
    “p95 signature verification latency” does more than “significantly faster”.
 -  Do not inflate the release. Cut “we're thrilled/excited/pleased to
    announce”, “the most significant release in X's history”, “a fundamental
    restructuring”, “unprecedented”, “extraordinary”, “critical infrastructure”,
    “major upgrade”, “faster than ever”, “we're happy to deliver”. Several of
    these appear in past notes; do not reuse them.
 -  Do not dramatize the problem either. State the failure mode plainly (“a
    remote instance goes down and workers spend their retry budget on a host
    that isn't coming back”) and skip the scene-setting, the metaphor, and the
    deferred payoff. No “zombie posts”, no “quiet fediverse”, no pager going off
    at 3am.
 -  Avoid unearned adjectives on your own work: powerful, elegant, seamless,
    robust, groundbreaking, transformative. If a quality matters, show it with a
    number, a comparison, or a concrete capability.
 -  Keep emotion out of transitions. “Unfortunately”, “happily”, and “sadly”
    rarely earn their place.
 -  No throat-clearing openers (“It's been a while since our last release…”)
    and no summarizing close. See the `writing` skill.
 -  State limits and tradeoffs plainly; that reads as confidence, not weakness.
    “The package does not write or delete credentials” is a good sentence.
 -  Match the corpus's person. Some projects use “we”, others are impersonal; do
    not switch register mid-note.

For example, instead of:

> We're thrilled to announce that this monumental release fundamentally
> reimagines dependency resolution, finally solving the long-standing
> field-order problem that has plagued users for years.

write:

> Dependency resolution no longer depends on field order. Previously,
> `object({ source, consumer })` and `object({ consumer, source })` accepted
> different inputs; now each source resolves before the parsers that consume
> it.

The second version is not less interesting. It is more specific, and it leaves
the reaction to the reader.


Spelling
--------

American English throughout: behavior, color, authorization, signaling,
canceled, license (noun), center, catalog.


Markdown conventions
--------------------

hongdown enforces these on the final file; write a normal draft and let it
normalize:

 -  Setext headings (`Title` over `===` or `---`).
 -  `~~~~` fences for code blocks, with a language tag.
 -  ` -  ` for list items (space, hyphen, two spaces).
 -  A thematic break renders as `   - - - - - - ...`.
 -  Link reference definitions collected at the end of their section.
 -  Long lines are left long; run hongdown with `--no-line-width`.
 -  GitHub alerts (`> [!NOTE]`) are allowed for upgrade gotchas that must not be
    missed.
 -  Tables are allowed for mappings and matrices of options.
 -  File names, paths, and extensions are italic, not code: *CHANGES.md*,
    *docs/*, *staging/index.ts*, *.eml*, *.env*. Keep backticks for code,
    commands, and identifiers.
