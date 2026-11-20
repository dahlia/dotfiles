---
name: writing-code-forge
description: >
  Apply shared writing, review, and Markdown rules when drafting or revising
  issues and pull or merge request descriptions on GitHub, GitLab, or similar
  code forges. Load alongside the task-specific drafting skill for repository
  language, template headings, links, cross-vendor style review, and Hongdown
  formatting without hard wraps. This skill does not post anything.
---

Writing for code forges
=======================

Use the target repository's conventions, not the defaults of the forge you
know best. Follow its template and contributing guide where they differ from
the defaults below. The calling skill handles evidence gathering, artifact
metadata, saving drafts, and handover.


Match the repository's language
-------------------------------

An explicit language request from the user overrides the repository's
default; mention the mismatch in the handover. Otherwise, look at recent
contributions of the same kind, especially those by maintainers, then the
README, contributing guide, templates, and the user's recent contributions
there. Use those samples to match the project's register and level of detail
as well as its language.

If the evidence is mixed or sparse, look for a stated language policy. If that
does not settle it, ask the user. Do not infer the language from the current
conversation, the programming language, or the maintainers' names or
nationality.

Use the chosen language for the title and body, but preserve template labels
and headings, identifiers, error messages, and quoted material as written.
Tell the user what evidence you used to choose the language.


Keep the template's structure
-----------------------------

Look for the repository's template before choosing a structure. Replace
placeholders and remove instructional HTML comments in Markdown templates.
Fill required sections with known facts and ask for missing details in one
batch. Check a box only if the user confirmed it or you verified it. An
unchecked required acknowledgment means the draft is not ready to post.

Keep the text and levels of template headings, even if the template uses `#`
or the body is short. New peer sections use the template's shallowest level;
new subsections use one level below their parent. Without a template, start at
`##` if headings help. The forge already displays the title above the body,
and a paragraph or two usually needs no headings.


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
title field displays Markdown literally, use plain text there.

Do not put spaces around joining slashes (`CI/CD`, `HTTP/2`, `read/write`).
In English prose, rewrite em dashes and spaced hyphens used as dashes rather
than introducing them. Keep official names and literal text intact. Prefer
concrete, concise language and describe what software does rather than what it
wants or thinks.


Link to the evidence
--------------------

 -  Link relevant documentation where it helps explain a claim or a next step.
    When citing a technical specification, link to the section that supports
    the claim, not the document's front page. Use the canonical
    `https://w3id.org/fep/xxxx` URL for a Fediverse Enhancement Proposal, not
    its rendered repository URL.
 -  For issues and changes on the same forge instance, use its native
    references without backticks or manual links. On GitHub, use `#123` or
    `owner/repo#123` for an issue or pull request. On GitLab, use `#123` for an
    issue and `!123` for a merge request, with a project path such as
    `group/project!123` when needed. On other forges, check what the renderer
    supports rather than assuming GitHub syntax.
 -  On GitHub a bare reference in a list item expands to show the title and
    state; don't repeat them there. In running prose or where the forge does
    not expand the reference, add context if the reader needs it.
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

Format after the last edit. Check that the formatter preserved frontmatter,
heading text and levels, code blocks, and link targets. It can reorder
reference definitions and turn `--` in prose into an em dash; rewrite the
sentence and format again if it does. Check the calling skill's requirements
before handing over the draft.

[Hongdown releases]: https://github.com/dahlia/hongdown/releases/latest
