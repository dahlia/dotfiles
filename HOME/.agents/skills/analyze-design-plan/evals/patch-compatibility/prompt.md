---
tags: [skill-creator]
max_turns: 20
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

2.4.x 패치 계획을 짜 주세요. 설정을 새로 고치면 사용자가 명시적으로 넣은
`false`만 기본값 `true`로 돌아갑니다. 2.4.1에서는 테스트로 확인했고, 2.4.0에서
들어온 변경이 기존 병합 경로를 `new_value or default`로 교체했습니다. 같은 병합
함수는 문서에 없는 플러그인 경로에서도 쓰이는데, 몇몇 플러그인은 빈 문자열이
기본값으로 처리되는 데 의존한다고 알려왔습니다. 설정을 전부 타입화하는 제안도
있지만 이번에는 코드에 손대지 말고 원인, 패치 호환성, 작업 순서와 장기 제언만
문서화해 주세요.
