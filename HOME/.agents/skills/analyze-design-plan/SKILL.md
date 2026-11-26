---
name: analyze-design-plan
description: Analyze software problems, design solutions, and produce an implementation plan before coding, with independent multi-vendor review at every stage. Use when the user requests pre-implementation analysis, feature design, API or UX design through planning, or root-cause investigation and a patch plan for a bug or security fix (e.g., “구현 전에 분석·설계·계획을 세워 주세요”, “원인부터 조사하고 수정 계획을 짜 주세요”, “design this before coding”). Do not impose it on an ordinary implementation request with no such planning intent, or use it for a review of already-written code.
compatibility: Requires the installed cross-subagents skill, access to its supported review routes, and a writable documentation workspace. Do not start without cross-subagents.
---

Analyze, design, plan
=====================

Leave an evidence-based handoff for a later implementation. Do not edit
production code or tests, commit, publish, or begin implementation, even when
the plan is approved. Use only authorized sources and investigative actions.
This skill is finished when the final plan clears its review gate and you have
handed it to the user, or when a blocker prevents further progress.


Setup
-----

1.  Load `cross-subagents` before doing project analysis. If it is missing or
    cannot be loaded, stop and tell the user. Do not substitute a single-agent
    workflow.
2.  Identify the repository, relevant project instructions, issue or request,
    intended release, and user constraints. Check the evidence rather than
    treating the issue's diagnosis or preferred solution as established. Ask
    only about missing information that prevents meaningful or safe progress.
3.  Use the user's requested output directory. Otherwise create a private,
    uniquely named run directory outside the project source tree (for example,
    under `${XDG_STATE_HOME:-$HOME/.local/state}/analyze-design-plan/`). Keep
    project files untouched except for explicitly requested documentation in a
    user-chosen location. Record the workspace, language, scope, stages,
    planned reviewers and rounds, and starting revision or other source
    baseline in `00-run.md`. Write all user-facing artifacts in the language of
    the request unless the user specifies another.
4.  Read [the review protocol](references/review-protocol.md) before selecting
    routes, estimating calls, or starting the first stage. Preflight viable
    routes and obtain any required approval before dispatch. Never send
    confidential issue material or project data to another service without
    authorization.


Choose the workflow
-------------------

Use one row per stage, in order. A short task can have short documents; it
cannot skip stages or reduce the review minimum. For a mixed request,
distinguish restoring the existing contract from adding behavior; agree on the
sequence if that distinction changes scope.

### Feature

| Stage                 | Establish and document                                                                                                                                                                                                                                                                                                      |
| --------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 01 Problem analysis   | Identify users, current behavior, underlying need, constraints, and evidence. Look beyond the requested solution to nearby use cases or a more general remedy; distinguish useful generality from speculative scope.                                                                                                        |
| 02 Solution direction | Compare credible options, including narrower or no-change options when relevant. Select a direction with success criteria, trade-offs, non-goals, and unresolved decisions.                                                                                                                                                 |
| 03 Sketch             | Make the direction concrete. For libraries and frameworks, show proposed API calls, results and failures; for end-user products, sketch user flows, important states, and accessibility where relevant. For other systems, sketch the comparable consumer-facing boundary. Sketches are documentation, not implementations. |
| 04 Design             | Define API and module boundaries, responsibilities, contracts and invariants, errors, compatibility, and key algorithm or data choices where they matter. Explain why the chosen design is sufficient without specifying every implementation detail.                                                                       |
| 05 Work plan          | Sequence the work and dependencies, verification, docs, changelog, and logical commit boundaries. Include rollout, migrations, rollback, or monitoring where applicable.                                                                                                                                                    |

### Bug or security fix

| Stage                      | Establish and document                                                                                                                                                                                                                                                                                                                          |
| -------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 01 Triage                  | Check intended behavior against specs, project goals and actual usage. Decide whether this is a bug, contract ambiguity, or a symptom of a larger issue. Identify affected versions and the earliest known affected version; distinguish confirmed, inferred, and unknown versions.                                                             |
| 02 Root cause              | Trace the causal chain from trigger to symptom. Test competing explanations against available evidence and separate the immediate fault from its mechanism. For ordinary bugs, use safe, scoped checks where authorized; record what you could not verify.                                                                                      |
| 03 Contributing conditions | Examine technical structure, tests and tooling, and human or process factors where evidence supports them. Avoid blame and speculation about people. Separate structural improvements into recommendations rather than folding them into the patch.                                                                                             |
| 04 Patch design            | Prefer the smallest patch that restores the supported contract and addresses the cause. Compare API compatibility and actual user expectations, including less obvious consumers. If no compatible fix exists, explain the trade-off and ask the user to decide on the release or migration strategy rather than quietly breaking the contract. |
| 05 Work plan               | Sequence the fix and regression checks, version coverage, docs, changelog, and logical commits. Include backports, release coordination, and disclosure handling as applicable. Keep longer-term recommendations separate.                                                                                                                      |

For security issues, keep investigation and delegated work defensive: use
authorized static inspection, existing non-offensive evidence, affected-version
assessment, and remediation planning. Do not develop exploits, reproduce
vulnerability attacks, or delegate offensive workflows. State uncertainty
rather than seeking disallowed evidence. Keep embargoed details private and do
not assume authorization to disclose them in a public changelog or to external
reviewers.


Run the stages
--------------

For each stage, write its numbered Markdown artifact before review. Include
evidence with stable references (paths and symbols, revisions, issue links, or
authorized notes), facts versus assumptions, options and rationale, and open
questions or verification limits. Link to the previous approved artifact rather
than silently superseding it. Keep precise records of which revision reviewers
saw.

Run the entire [review protocol](references/review-protocol.md) for the stage
before entering the next one. Record reviewer findings and their disposition,
the exact reviewed revision, and whether the gate passed. Do not mark a draft
approved when any required review is missing.

Tell the user promptly when evidence overturns an assumption, materially
changes direction, or changes release or compatibility expectations. Explain
the evidence and impact, not just the fact of a change. If an approved earlier
stage is invalidated, reopen it and review the replacement before relying on
it; mark dependent artifacts stale and re-review affected stages in order. Do
not broaden scope or make a consequential product or release decision without
the user's authorization.

End by linking the documents, summarizing the selected direction, why it was
chosen, any decisions still needed, and whether implementation can proceed in a
separate task. If blocked, identify the last approved stage, link the current
draft, and explain what would allow work to resume. Include the delegation
report required by `cross-subagents`.
