Case: widget-template
=====================

**Fixture:** `/tmp/pr-eval/widget` (synthetic TypeScript project)
**Branch:** `fix/empty-label`, one commit ahead of `main`
**Change:** `formatLabel()` now throws on an empty label instead of returning an
empty string; a test was added
**Tracker language:** English
**Template:** `.github/PULL_REQUEST_TEMPLATE.md` with `##` sections and
checkboxes

Recreate the fixture if it is gone: a `widget` repo with the template shown
below, a `formatLabel()` that trims whitespace, a `fix/empty-label` branch that
rejects an empty label, and a test for it.


Prompt
------

PR 설명 초안 좀 써 줘. 이번 브랜치는 빈 라벨이 들어오면 `formatLabel()`이 빈
문자열을 돌려주던 걸 에러로 바꾼 거야. 대시보드에서 빈 버튼이 렌더링되던
문제를 막으려고. 테스트는 추가했고 문서는 안 건드렸어. 이슈는 따로 없어.

리포에 PR 템플릿이 있으니 그대로 따라 줘. 체크박스는 실제로 한 것만 체크하고,
템플릿의 HTML 주석은 본문에서 지워 줘. 초안은 저장소 밖에 두고 hongdown으로
포매팅해 줘. 클립보드는 실제 클립보드를 건드리지 않도록 환경에 설정된
`PR_CLIPBOARD_CMD`를 그대로 써 줘 (평가 하네스가 스텁을 넣어 둡니다).


What a good result looks like
-----------------------------

The template is:

~~~~ markdown
## Summary

<!-- Describe the change and why it is needed. -->

## Related issues

<!-- Link the issue this closes, if any. -->

## Checklist

- [ ] I added or updated tests.
- [ ] I updated the documentation.

## Screenshots

<!-- Add screenshots for UI changes. -->
~~~~

A good draft:

 -  keeps the template's four `##` headings in order, with no extra heading
    levels and no `#`;
 -  removes the three HTML comments;
 -  fills `Summary` with the empty-button symptom and the new rejection, not a
    file list;
 -  leaves `Related issues` empty or says “None” because no issue exists, and
    does not invent `Fixes #12`;
 -  checks the tests box and leaves the documentation box unchecked;
 -  removes or empties `Screenshots` because there is no visual surface, rather
    than inventing an image;
 -  has an English frontmatter title in imperative sentence case, e.g. “Reject
    empty labels in formatLabel()”;
 -  is saved outside the checkout and formatted with hongdown.
