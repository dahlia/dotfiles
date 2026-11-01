Case: sidey-bug-korean
======================

**Repo:** `sidey-app/SIDEY` (no local checkout)
**Tracker language:** Korean
**Template:** `01-bug-report.yml` issue form, rendered in Korean
**Kind:** bug report


Prompt
------

sidey-app/SIDEY 저장소에 버그 리포트 초안 하나 써 줘. 맥북에서 잠자기에서
깨어나면 서버 연결이 자동으로 안 붙고, 앱을 껐다 켜야 다시 붙어. macOS 15.1
(Darwin 24.1.0 arm64)이고 앱 버전은 2.2642.77이야. 재현은 잠자기 걸었다가 10분
뒤에 깨우면 되고, 기대하는 동작은 깨어나면 자동으로 재연결되는 거야. 로그는
아직 없어.


What a good result looks like
-----------------------------

 -  Korean, matching the tracker and the form.
 -  The form's `###` headings with their emoji, in the form's order, including
    the OS, the app version, what happens, how to reproduce, and expected
    behavior.
 -  No `#` or `##` headings.
 -  Frontmatter with `type: Bug` and the labels `bug` and `macOS`, both real.
 -  No invented log file or attachment: the user said there is none.
 -  Formatted with hongdown and not word-wrapped.
