---
name: release-notes
description: >
  Draft release notes and release announcements for an open source project,
  in a maintainer's established house style, ready to post to GitHub
  Discussions. Use this skill whenever the user asks to write release notes,
  a release announcement, a "what's new in X.Y" post, or a changelog summary;
  asks to draft release notes from CHANGES.md; mentions a RELEASE-x.y.md file;
  or asks to turn a changelog into something users would actually read. Also
  trigger on Korean phrasings like "릴리스 노트", "릴리즈 노트", "릴리스 노트
  작성해 줘", or "새 버전 공지 써 줘". The skill reads the changelog section,
  every linked issue and pull request with its discussion, and the new
  documentation, then writes the announcement, formats it with hongdown, and
  saves it to RELEASE-x.y.md.
---

Release notes
=============

Write the post a maintainer puts on GitHub Discussions after tagging a
release. The audience splits in two. Someone already using an older version
is deciding whether upgrading is worth their afternoon. Someone who has never
heard of the project is deciding whether it is worth five minutes. Both of
them know how to read a changelog, so a release note that merely reformats
*CHANGES.md* gives them nothing. What makes the note worth reading is the
*why* behind each change: the problem users hit, the shape of the fix, the
tradeoffs the maintainer chose, and what it feels like to use.

This means the writing is downstream of the research. Go read the actual
discussion before you write a sentence. A release note that misstates a
default or invents a motivation is worse than no note at all.

Load `writing-code-forge` with the skill tool before drafting; if it is not
discoverable by name, read the sibling *writing-code-forge/SKILL.md* in the
same skills directory. Follow it for writing style, links, review, and
formatting.

Detailed conventions recovered from the maintainer's previous release notes
live in *references/house-style.md*. Read that file before drafting; it is the
difference between a note that matches the existing corpus and one that merely
follows the generic advice below.


Pin down the target
-------------------

Establish three things before doing anything else:

 -  **Repository.** If the working directory is a git checkout, use it. Run
    `gh repo view --json nameWithOwner,url,description,homepageUrl` to get the
    canonical slug, URL, and one-line description.
 -  **Version.** Usually the newest section at the top of *CHANGES.md* /
    *CHANGELOG.md*. If the user names a version, use theirs. Confirm with the
    user if the changelog's top section looks unreleased or ambiguous.
 -  **Output file.** *RELEASE-x.y.md* at the repository root, where x.y is the
    major and minor version, so 2.3.0 or 2.3.1 both write to *RELEASE-2.3.md*,
    and 1.2.0 writes to *RELEASE-1.2.md*. If that file already exists, you are
    revising it, not starting from scratch: read it first.

If the user gave only a project name and no checkout, ask for the repository
or clone it. Do not guess at a version from release tags alone; the changelog
is the source of truth because it is what the discussion links into.


Read the changelog section
--------------------------

Open *CHANGES.md* and read the whole section for the target version, not just
the summary line at the top. Note every `#NNN` reference, and note which
package or subsystem each entry touches. The changelog is terse on purpose;
it is an index, not the story.


Read every linked issue and pull request, including the discussion
------------------------------------------------------------------

This is the step that separates a real release note from a changelog
rewrite, and it is the step most likely to be skipped. For each `#NNN`:

~~~~ bash
gh issue view 123 --comments
gh pr view 456 --comments
gh pr diff 456
gh pr view 456 --json files,author,additions,deletions
~~~~

The `--comments` flag matters. The reasoning you need is usually in the
thread: why the obvious approach did not work, which edge cases the reviewer
raised, what the default should be. Also pull review comments and reviews,
which `gh pr view` does not surface:

~~~~ bash
gh api repos/OWNER/REPO/pulls/456/comments --paginate
gh api repos/OWNER/REPO/pulls/456/reviews --paginate
~~~~

While reading, record three things per feature:

1.  **The motivation.** What were users unable to do? What broke? The first
    paragraph of a feature section almost always states this.
2.  **The precise behavior.** Option names, defaults, error behavior, which
    transports or runtimes are supported, and what is explicitly *not*
    supported. Getting these details right is what makes the note trustworthy.
3.  **The people.** Who authored or contributed the change, and who filed or
    shaped the request. A PR's author and reviewers are visible in
    `gh pr view`; a maintainer is not an external contributor, so check whether
    the author is a regular maintainer before writing a shoutout.

If a change links to another repository (`owner/repo#NNN`), fetch that too.


Read the new documentation
--------------------------

New features almost always ship with documentation, usually under *docs/*.
Find the pages for the features in this release and read them. They carry the
details the changelog omits: full option lists, defaults, runtime caveats,
and the recommended usage pattern. If you do not know the public docs URL,
look for the site's base URL in the repository or a previous release note.


Recover the project's voice
---------------------------

Each project has a canonical way of introducing itself and of signing off, and
reusing it keeps the corpus coherent. Before drafting, fetch one or two
previous release notes for the same project and read them:

~~~~ bash
gh discussion list -R OWNER/REPO --limit 10
gh discussion view -R OWNER/REPO 75
~~~~

`gh discussion` is a preview command; if it is unavailable, the same body is
reachable through `gh api graphql` on `repository.discussion`. If an older
*RELEASE-x.y.md* exists in the repository, read that instead; it is faster and
already in the working tree.

From it, lift the project blurb verbatim (the one- or two-sentence
description with the link to the project site) and match the closing
convention. Do not invent a new description of the project for each release.


Draft
-----

Follow *references/house-style.md* for the full conventions. The short
version:

 -  Open with the project blurb, then a paragraph that names the theme of the
    release and its headline features. A reader should finish the second
    paragraph knowing whether this release matters to them.
 -  Give each substantial feature its own `##` section, written as prose. Say
    briefly when the feature is useful, state the problem before the solution,
    show a short realistic code example, then cover the caveats and limits.
 -  For a library, include at least one example that shows the API in use at a
    glance, and verify every example runs or at least type-checks against the
    checked-out source before you include it. See “Example code” in the
    reference.
 -  Shout out external contributors inside the feature section they worked on,
    usually as the last sentence, e.g. “Thanks to @name for contributing this in
    #123.” For releases with several contributors, also add a dedicated
    Acknowledgments section.
 -  Put minor items in an “Other changes” section and smaller corrections in
    “Bug fixes” rather than giving each one a heading.
 -  Add an “Upgrading” section with the commands a user runs, and call out
    breaking changes plainly. When there are none, say so.
 -  Keep the tone understated and matter-of-fact. State what changed and what it
    enables, and let numbers and concrete capabilities carry the weight. Do not
    inflate the release (“thrilled to announce”, “the most significant release
    in…”, “unprecedented”) or dramatize the problem (scene-setting, metaphors,
    “zombie posts”). See the Tone section of the reference.
 -  Close with the project's usual sign-off and a link to *CHANGES.md*.

Write in English unless the user asks otherwise, with American spelling
(“behavior”, “color”, “authorization”, “signaling”).

Then step back and reread the draft against these questions:

 -  Would someone on the previous version want to upgrade after reading this?
 -  Would someone who has never heard of the project want to try it?
 -  Is every factual claim traceable to the changelog, a discussion, or the
    docs? Delete or fix anything that is not.
 -  Does the tone stay plain and matter-of-fact, or has it slid into promotion
    (“thrilled to announce”) or melodrama (a dramatized problem, a grandiose
    claim about the release)?
 -  Does it read like the previous release notes for this project?


Review and save
---------------

Save the note as ordinary Markdown in *RELEASE-x.y.md*. Follow the review and
formatting steps in `writing-code-forge` before handing it over.

Leave the file on disk. Do not post to GitHub Discussions unless the user
explicitly asks; posting is a separate, deliberate action. When the note is
ready, tell the user the path and offer to post it.


Reference
---------

 -  *references/house-style.md*: title, section, linking, shoutout, tone, and
    formatting conventions recovered from the maintainer's previous release
    notes. Read it before drafting.
