---
name: writing-code-forge
description: >
  Apply shared writing, review, and Markdown rules when drafting or revising
  issues, pull or merge request descriptions, and announcements on GitHub
  (including Discussions), GitLab, or similar code forges. Load alongside
  the task-specific drafting skill for language, template headings, links,
  cross-vendor style review, and Hongdown formatting without hard wraps.
  This skill does not post anything.
---

Writing for code forges
=======================

Use the target repository's conventions, not the defaults of the forge you
know best. Follow its template and contributing guide where they differ from
the defaults below. The calling skill handles evidence gathering, artifact
metadata, saving drafts, and handover.


Match the repository's language
-------------------------------

An explicit language request from the user overrides the default; mention
any known mismatch with the repository in the handover. Otherwise, use the
calling skill's language default if it gives one. To infer the repository's
language, check recent contributions, especially those by maintainers, then
the README, contributing guide, templates, and the user's recent contributions
there.

If the evidence is mixed or sparse, look for a stated language policy. If that
does not settle it, ask the user. Do not infer the language from the current
conversation, the programming language, or the maintainers' names or
nationality.

Use the chosen language for the title and body, but preserve template labels
and headings, identifiers, error messages, and quoted material as written.
When you infer the repository's language, tell the user what evidence you used.


Keep the template's structure
-----------------------------

Where the forge supports templates for the artifact, look for the repository's
template before choosing a structure. Replace placeholders and remove
instructional HTML comments in Markdown templates.
Fill required sections with known facts and ask for missing details in one
batch. Check a box only if the user confirmed it or you verified it. An
unchecked required acknowledgment means the draft is not ready to post.

Keep the text and levels of template headings, even if the template uses `#`
or the body is short. New peer sections use the template's shallowest level;
new subsections use one level below their parent. Without a template or a
heading convention in the calling skill, start at `##` if headings help. The
forge already displays the title above the body, and a paragraph or two
usually needs no headings.


Apply the writing style
-----------------------

Load `writing` with the skill tool before drafting. If it is not discoverable
by name, read the sibling *writing/SKILL.md* from the same skills directory
when available. For non-English text, apply its guidance on structure, tone,
and review; treat English-specific spelling and punctuation as advisory.

In newly written prose, use sentence case for titles and headings and leave
headings unnumbered unless the template calls for numbering. Italicize
filenames, paths, and extensions in Markdown prose (*src/index.ts*, *.ts*).
Leave them alone inside commands, code blocks, and URLs. Use inline code for
identifiers, functions such as `parseHeader()`, commands, flags, environment
variables, and technical notation as specified by `writing`. If the forge's
title field displays Markdown literally, use plain text when supplying a
title for that field. A title in a Markdown draft can keep its formatting.

Do not put spaces around joining slashes (`CI/CD`, `HTTP/2`, `read/write`).
In English prose, rewrite em dashes and spaced hyphens used as dashes rather
than introducing them. Keep official names and literal text intact. Prefer
concrete, concise language and describe what software does rather than what it
wants or thinks.


Link to the evidence
--------------------

 -  Link relevant documentation in the sentence that explains the claim or
    next step. Prefer the project's published documentation to a repository
    file, and link to the relevant section when it has an anchor. When citing
    a technical specification, link to the section that supports the claim,
    not the document's front page. Use the canonical
    `https://w3id.org/fep/xxxx` URL for a Fediverse Enhancement Proposal, not
    its rendered repository URL.
 -  For issues and changes on the same forge instance, use its native
    references without backticks or manual links. On GitHub, use `#123` or
    `owner/repo#123` for an issue or pull request. On GitLab, use `#123` for an
    issue and `!123` for a merge request, with a project path such as
    `group/project!123` when needed. On other forges, check what the renderer
    supports rather than assuming GitHub syntax. Check the intended posting
    surface, too: issue, pull request, and Discussion bodies need not render
    the same references in the same way.
 -  In running prose, name what each issue or change reference points to:
    “The proposal is in #185 and the implementation is in #188.” Avoid
    unexplained parenthetical bundles dropped at the end of a sentence or
    paragraph. Short lists of discrete changes can end an item with a short
    parenthetical citation. If the renderer already expands a bare reference
    in a list item to show its title and state, don't repeat them.
 -  On GitHub, write a commit ID as a bare full hash so the forge autolinks
    it. On other forges, use a verified native reference or the commit
    permalink if it does not autolink in context.
 -  Use a verified URL for references on a different forge or instance, or
    when the target cannot be expressed as a native reference. To cite a
    specific comment, use its permalink in angle brackets, for example
    `<https://github.com/fedify-dev/fedify/issues/664#issuecomment-4203452333>`.
    The brackets leave trailing punctuation outside the link. Never invent a
    reference number or link target.


Review and format before handover
---------------------------------

Run the cross-vendor style review described by `writing`, even for a short
draft. Follow its selection, validation, and retry procedure and give the
reviewer the intended language, repository style, and template constraints.
If all eligible routes are blocked, follow its pending-review and waiver
procedure; self-review does not count as an external review. If `writing`
itself is unavailable, check the draft against these rules and disclose that
you could not run its review.

Use Hongdown without hard wraps. The forge handles visual wrapping, and
hard-wrapped prose makes a small web edit reflow the whole paragraph.

~~~~ bash
hongdown --write --no-line-width <draft.md>
mise x aqua:dahlia/hongdown -- hongdown --write --no-line-width <draft.md>
~~~~

Use the second command only if `hongdown` is not on `PATH`. If neither is
available, download the appropriate archive from the [Hongdown releases] into
a temporary directory, unpack it, and run the binary there. Use
`gh release view --repo dahlia/hongdown --json tagName,assets` to list assets
and `uname -sm` on Unix-like systems to help identify the platform. Do not
install anything system-wide for one draft. If formatting is unavailable,
keep the prose unwrapped and report that Hongdown did not run.

Before formatting, scan the prose for trailing reference bundles and attach
each reference to the relevant claim. Format after the last edit. Check that
the formatter preserved frontmatter, heading text and levels, code blocks,
and link targets. It can reorder reference definitions and turn `--` in prose
into an em dash; rewrite the sentence and format again if it does. Check the
calling skill's requirements before handing over the draft.

[Hongdown releases]: https://github.com/dahlia/hongdown/releases/latest
