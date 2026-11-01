Eval cases
==========

Each directory is one test case for the skill, in the layout `skill-creator`
uses: `prompt.md` (the task and what a good result looks like), `case.yaml`
(the name, a description, and whether the case can be graded mechanically), and
`graders/` (one assertion per file).

`hono-bug-form` is marked `manual: true`. Its premise is false, the right
behavior is to stop without drafting, and that reply has to be judged by hand.


Running a case
--------------

1.  Give the prompt from `prompt.md` to an agent with the skill available, in a
    working directory outside the target repository.
2.  Keep the drafts the agent produced in one run directory.
3.  Grade them:

~~~~ bash
python3 scripts/grade_evals.py --list
python3 scripts/grade_evals.py hongdown-feature-simple /path/to/run-dir
~~~~

The grader writes `grading.json` next to the drafts. Run the same case without
the skill as a baseline: the difference between the two pass rates says more
than either score alone.

The label sets in `grade_evals.py` were captured when the cases were written.
Refresh them with `scripts/collect_context.py` before trusting a label failure.
