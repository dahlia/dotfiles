Quota hand-over
===============

Use this procedure when quota blocks an assigned task at selection or dispatch,
or during execution, including a retained session that can no longer resume.
The coordinator owns recovery. A quota failure ends that attempt, not the
remaining assignment.

Choose a replacement
--------------------

Refresh quota with `quota.py status --refresh --human` after an exhaustion
error. `run.py` records observed failures; native callers must record the same
evidence with
`quota.py record-observation --bucket <bucket> --error <error>` using the
affected route's bucket. Distinguish
an account/group limit from an individual model limit: the former blocks
every route in the affected pool, while the latter blocks only that model.
Exclude exhausted buckets from this task's next attempts; never force an exhausted route or retry
its effort variants to evade a shared limit. Independent OpenCode quota can
remain usable when Antigravity quota is exhausted.

Read the failed attempt's metadata and partial answer, then inspect the actual
working tree. Preserve completed work, evidence paths, unanswered questions,
and the remaining acceptance criteria. Partial answers are unverified input,
not completed reviews or successful results.

First look for the same canonical model on another usable quota pool. Otherwise
reselect with the original task category and model requirements: task fit,
quality/tier, context, vision, tools/sandbox, speed, cost ceiling, and vendor
independence. Use the saved selection plan when present. Check independence
against the routes actually used by the other steps, including replacements.
A cached fallback list is a starting point; refresh selection when it is empty,
stale, or all its routes have failed. Do not treat a route that ran out of quota
as a reason to drop its whole step.

When another route satisfies those requirements and the user's instructions
allow substitution, hand over without waiting for approval. `run.py` may run a
compatible fallback automatically. If its constrained chain cannot continue,
the coordinator must reselect and launch a new task. An implementation pin
introduced by `--route` is not necessarily a user's exact-model requirement;
consult the original request before retaining or relaxing it.

Ask with a 30-minute default
---------------------------

If substitution needs user input, or no sufficiently similar model is
available, prepare the best available alternative and ask the user to choose.
Explain which requested properties it preserves and which would change. Give
the default route, the UTC deadline (30 minutes after delivery), and state
that the default runs if no answer arrives. The default may relax
disclosed model, tier, fit, or speed preferences if the replacement remains
capable of the remaining work; it must preserve hard requirements such as
necessary vision/context, independent review, permissions, and spending limits.
Offer available choices, such as the proposed model, another usable model, or waiting for the original
quota reset. This procedure authorizes the stated default at the deadline;
it does not authorize new billing, broader permissions, or overriding an
explicit instruction such as "only this model" or "do not substitute."

Use a host question mechanism that can receive a reply asynchronously and a
supported timer/deferred continuation that wakes the coordinator at the
deadline. Persist the question, delivery time, UTC deadline, default route,
original constraints, progress/context path, and decision under the run
artifacts so a resumed coordinator can continue. Continue independent work
while the question is pending. Do not turn an early question-tool return,
missing answer, ended turn, or tool timeout into 30 minutes of elapsed time.
Do not poll for replies or use recurring LLM turns to count time.

If no reply arrives by the deadline, refresh availability, use the stated
default (or the next available route under the disclosed replacement policy),
and continue the assignment. Record `decision: timeout` in the saved question
state and pass
`--decision timeout` to `record-handover`. A reply received
before the deadline takes precedence; an explicit request to wait or cancel
must be honored. If the host cannot deliver a delayed continuation, disclose
that limitation and use a supported interruptible wait while the session
remains active. Do not claim that a timer was armed if it was not. If neither
delayed
continuation nor interruptible waiting is available, retain the pending state
and disclose that this host cannot guarantee the 30-minute continuation.

If no runnable alternative exists, explain that specific blocker and the next
known reset instead of inventing availability. Keep the assignment pending;
resume recovery when capacity becomes available. The current host model may
be an alternative when it meets the task requirements, but self-review cannot
satisfy an independent-review requirement.

Transfer and report
-------------------

Start a fresh session when changing route; a provider session ID cannot be
moved to another provider. Pass a hand-over context file with the original
request and constraints, completed changes/tests, current file state, failed
attempt/partial-output paths, remaining work, and the replacement rationale.
Carry forward the original write set, execution permissions, guard, and tool
policy. Check for surviving processes before granting the replacement write
ownership. The replacement must verify inherited work before using it.

For a replacement launched as another task (including a native task registered
with `record-native`), record the link after launching it. Use the route IDs
from the actual task metadata so the report can match its outcome:

~~~~ bash
python3 <skill-dir>/scripts/run.py record-handover --run-id <run-id> \
  --from-task <failed-task> --to-task <replacement-task> \
  --from-route <agent/provider/model> --to-route <agent/provider/model> \
  --reason "Quota exhausted; replacement preserves review tier and vision" \
  --decision automatic --context-file <handover.md>
~~~~

Use `--decision user` for an explicit choice and `--decision timeout` for the
30-minute default. Include changed requirements and the basis for the choice
in `--reason`. `run.py` records its own automatic fallback links. Do not record
a proposed replacement as an executed hand-over. Inspect metadata and results
for the replacement just as for the original task; another exhaustion starts
the same recovery procedure with accumulated failed routes excluded.

At workflow completion, read `run.py report --run-id <run-id> --human` and
report every hand-over: original and replacement routes, quota failure,
automatic/user/timeout choice, changed properties, and final outcome. Pending
or unsuccessful hand-overs must remain visible even when insignificant tasks
are filtered out. Do not describe the workflow as complete while required
replacement work remains unfinished.
