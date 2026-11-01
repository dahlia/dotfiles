Case: hongdown-feature-simple
=============================

**Repo:** `dahlia/hongdown` (checked out at `repo/`)
**Tracker language:** English
**Template:** none
**Kind:** small feature request


Prompt
------

홍다운(hongdown)에 기능 요청 이슈 하나 초안 써줘. 지금 설정 파일에는
`include`/`exclude`가 있는데 CLI에는 그걸 일시적으로 덮어쓸 방법이 없어서,
CI 스크립트마다 설정 파일을 새로 만들고 있어. `--exclude` 같은 옵션으로 한 번만
제외 경로를 줄 수 있으면 좋겠어. 간단하게 써도 돼.


What a good result looks like
-----------------------------

 -  English, because every issue in that tracker is English even though the
    request is Korean.
 -  Two short paragraphs, no headings: the issue is small.
 -  Frontmatter with a title and the `enhancement` label, which exists in the
    repository.
 -  Saved outside `repo/`, under
    `${XDG_DATA_HOME:-~/.local/share}/issue-drafts/dahlia-hongdown/`.
 -  Formatted with hongdown and not word-wrapped.
