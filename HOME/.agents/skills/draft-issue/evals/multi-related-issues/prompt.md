Case: multi-related-issues
==========================

**Repo:** `dahlia/hongdown` (checked out at `repo/`)
**Tracker language:** English
**Template:** none
**Kind:** two related issues drafted together


Prompt
------

dahlia/hongdown에 관련된 이슈 두 개를 한 번에 초안으로 써 줘. 첫째는
`hongdown --check`가 실패할 때 어떤 파일이 문제인지 stdout으로 알려주지 않고
stderr에만 흘려서, CI 로그에서 실패한 파일을 찾기 어렵다는 것. 둘째는 README에
GitHub Actions CI 예시를 추가해서 `hongdown --check`로 검사하도록 해야 한다는
것. 두 번째는 첫 번째가 먼저 해결돼야 의미가 있어.


What a good result looks like
-----------------------------

 -  Two Markdown drafts in one directory outside `repo/`, under
    `${XDG_DATA_HOME:-~/.local/share}/issue-drafts/dahlia-hongdown/`.
 -  English, and any headings start at `##`; the drafts are short enough
    that a heading is optional.
 -  Frontmatter on both, with titles and real labels.
 -  A relation from the second draft to the first, expressed as a `draft:`
    reference because neither has a number yet.
 -  Formatted with hongdown and not word-wrapped.
