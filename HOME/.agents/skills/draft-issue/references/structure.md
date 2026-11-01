Shaping the body
================

The shape follows the kind of issue and the size of what it has to say. These
patterns are for drafts with no template; a template always wins.


Start from the size
-------------------

A one-paragraph issue is one paragraph. No headings, no bold lead-ins, no
bullet list of two items. The reader is looking at a bug they can reproduce in
thirty seconds, and structure only delays that.

Rough guide:

 -  Up to two short paragraphs: no headings.
 -  One or two ideas that need separating: `##` sections, two or three at most.
 -  A reproduction with code, expected versus actual, and a discussion of the
    fix: `##` sections, and `###` under one of them only if it genuinely nests.

The count comes from the content, so do not pad to reach a pattern. An issue
with one heading and a four-sentence body is fine.


Bugs
----

Most bug reports need three things: the context that makes the bug matter, the
reproduction, and the difference between actual and expected behavior.

~~~~ markdown
Formatting a document twice changes the output. The first pass writes the
reference destination with a literal tab; the second pass re-encodes it, so a
document that should be stable keeps growing.

## Reproduction

Format this document with `hongdown`:

~~~ markdown
 -  Read [tab].

[tab]: <https://example.com/a&#9;b>
~~~

The output contains a raw tab inside the angle brackets, and formatting that
output again produces `&#9;` where the tab used to be.

## Expected behavior

The destination should round-trip unchanged, matching how the formatter already
handles `&#10;` and `&#13;`.
~~~~

Note what this does not do: it does not list the affected versions unless they
matter, does not summarize the formatter's architecture, and does not explain
what a tab is. The reproduction is small enough to paste into a terminal.

When the reporter cannot reproduce reliably, say what they saw and how often
instead of inventing steps. “Crashes roughly once an hour under load, no
pattern found yet” is honest and still actionable; a made-up sequence is not.


Feature requests
----------------

The requirement is a situation. A request that describes the workaround people
use today tells the maintainer far more than a request that describes the API
signature they should add.

~~~~ markdown
Our activity dispatcher has to reject deliveries from domains that publish an
expired key, but the key lookup API only accepts a key ID. To get one, we have
to parse the signature header ourselves, which means every caller reimplements
the same parsing and gets the edge cases slightly different.

A lookup by actor URL would let callers ask the question they actually have
without touching the header format.
~~~~

If the project has a design document or an earlier discussion about the same
area, link it by number. If the feature exists in a related project, say how it
behaves there and what the difference costs.


Refactors and cleanup
---------------------

A refactor issue has to show the cost of not doing the refactor. Name a change
that was harder than it should have been, count the places that had to be
touched, or point at the duplicated logic that drifted. “This module is messy”
is not actionable; “adding a fourth format required editing five switches, and
the fourth one silently fell back to the default” is.

~~~~ markdown
Adding the `markdown` output format meant touching four separate `match`
statements, and the one in `render()` still falls back to plain text, so
`--format markdown` produces the right file name and the wrong contents.
~~~~


Documentation issues
--------------------

Point at the page, quote the sentence that is wrong or missing, and describe
what a reader would conclude from it. A docs issue that says “the docs should
mention X” leaves the writer to guess where.


Design decisions in maintainer-authored issues
----------------------------------------------

When the author can decide, the issue may read as a short design record: the
constraint that forces a choice, the options that were considered, and the
reason the chosen one wins. Keep it in paragraphs. Enumerating the options in a
table, or grading each one, reads like a template rather than a decision, and
the reasoning is what the next reader needs.

It is fine to end with the chosen approach and no call to action; the issue is
then a record of why the change will happen, not a request for input.


Things that do not need their own section
-----------------------------------------

 -  “Summary” at the top that repeats the title.
 -  “Additional context” holding one sentence that belongs in the paragraph
    above it.
 -  “Checklist” items the author does not control, like “I have searched for
    duplicates”; keep them only when the template requires them.
 -  A closing “Thoughts?” unless a decision is genuinely open.
