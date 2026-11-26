---
tags: [skill-creator]
max_turns: 20
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

라이브러리의 `read(path)`는 UTF-8 파일만 읽고, 호출자는 여러 줄 텍스트를
테스트하려면 임시 파일을 만들어야 합니다. 이슈는 `read_string(text)`를
요청하는데, 다른 사용자는 이미 열린 텍스트 스트림도 지원해 달라고 합니다. 기존
`read(path)`의 입력과 예외 동작은 그대로 유지해야 해요. 이번에는 구현하지 말고,
문제부터 분석해서 API 설계와 작업 계획까지 문서로 남겨 주세요.
