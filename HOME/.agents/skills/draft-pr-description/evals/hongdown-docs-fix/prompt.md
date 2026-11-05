Case: hongdown-docs-fix
=======================

**Fixture:** `/tmp/pr-eval/hongdown` (`dahlia/hongdown`, depth-30 clone)
**Branch:** `test/pr-eval-docs`, one commit ahead of `main`
**Change:** a paragraph added to *STYLE.md* about longer tilde fences
**Tracker language:** English
**Template:** none

Recreate the fixture if it is gone:

~~~~ bash
git clone --depth 30 https://github.com/dahlia/hongdown.git /tmp/pr-eval/hongdown
cd /tmp/pr-eval/hongdown
git checkout -b test/pr-eval-docs
# Add the paragraph from the "What a good result looks like" section to STYLE.md
# under "Fenced code blocks with tildes", then commit it.
~~~~


Prompt
------

이제 PR 올릴 차례인데, PR 제목이랑 설명 초안을 영어로 써 줘. 이번 브랜치는
*STYLE.md*에 “코드 블록 안에 네 개짜리 물결 펜스가 들어 있으면 더 긴 펜스를
쓰라“는 설명을 한 단락 보강한 거야. 무엇을 했는지보다 왜 필요하고 어떻게
동작하는지를 드러내 줘. 장황하지 않게 짧고 간결하게. PR 템플릿은 없어.

초안은 저장소 밖에 두고, hongdown으로 포매팅하고, 본문은 클립보드에도 넣어 줘.
클립보드는 실제 클립보드를 건드리지 않도록 환경에 설정된 `PR_CLIPBOARD_CMD`를
그대로 써 줘 (평가 하네스가 스텁을 넣어 둡니다).


What a good result looks like
-----------------------------

The diff adds this paragraph under “Fenced code blocks with tildes”:

~~~~~~ text
When the code block itself contains a run of four tildes, use a longer fence.
A five-tilde fence keeps the inner run as content instead of closing the block
early:

~~~~~ markdown
~~~~~~

~~~~~
~~~~

A good draft:

 -  opens with why the rule matters: a four-tilde run inside a block would
    close it early, so the fence has to be longer;
 -  says how the example and the paragraph make that visible, without listing
    the file as a change log;
 -  is two short paragraphs at most, with no headings or a single `##` section;
 -  writes *STYLE.md* in italics, uses no em dash, and leaves no space around
    slashes;
 -  has a frontmatter title in imperative sentence case, e.g. “Document longer
    fences for nested tildes”, with no trailing period or `[Docs]` prefix;
 -  is saved under `~/.local/share/pr-drafts/dahlia-hongdown/`, outside the
    checkout, and is formatted with `hongdown --write --no-line-width`;
 -  leaves the clipboard file with the body only, no frontmatter;
 -  omits an AI disclosure. *AI_POLICY.md* requires one from outside
    contributors but exempts maintainers, and the authenticated account is
    `dahlia`, an admin on this repository. Adding “Drafted with …” here would
    ignore the exemption.

A draft that merely says “Updated STYLE.md to describe longer fences” misses the
point, even if the formatting is correct.
~~~~~
