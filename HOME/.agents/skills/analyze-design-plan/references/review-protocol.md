Stage review protocol
=====================

Select, budget, and prepare
---------------------------

 -  Follow the installed `cross-subagents` skill and its selection, execution,
    session, quota, and reporting instructions. Use its least-authority
    execution policies: read-only reviewers, restricted context, no reviewer
    writes to project files, and only the access the task needs. For visual
    inputs require vision support. Treat vendor as model vendor, not CLI or
    provider.
 -  For each stage, plan at least two independent reviewers from distinct
    vendors, each using a verified latest stable model in an appropriate
    lineage. Increase the number of reviewers or rounds for complex,
    high-impact, or uncertain work. Plan at least two rounds with every regular
    reviewer, then a separate final reviewer on a verified latest frontier-tier
    model (for example, the latest Claude Fable or GPT Astra). The final
    reviewer must not be one of the stage's regular reviewers; a new session of
    the same model does not count. A third vendor for the final reviewer is
    useful but not mandatory.
 -  Discover fresh routes and verify release order, tier, quotas, capabilities,
    and session support as specified by `cross-subagents`. Prefer saved exact
    route plans, using its latest-model options or independently verified
    release metadata with exact pins. If a preferred latest model is absent,
    unverified or unavailable, actively look for another eligible route for
    that exact model or a verified latest stable model in another suitable
    lineage/vendor. Recheck vendor diversity, frontier tier for final review,
    permissions and budget. Disclose the alternative and its rationale before
    use. Never call an older release within a lineage “latest.” If only a
    nonqualifying replacement exists, explain the closest option and ask
    whether the user wants to revise the requirements or wait; remain blocked
    until then. Do not silently introduce metered billing or broaden
    permissions.
 -  Budget the whole workflow early: five stages × (two reviewers × two rounds
    + one final review) = **25 reviewer calls minimum**, before retries,
    reopened gates, or extra review. Use the cross-subagents cost gate before
    any fan-out, pricing selected routes or saved plans and following its
    confirmation and revalidation rules. State the aggregate estimate and the
    assumptions. Follow its orchestration rules for batches of three or more.
    If the estimate rises materially, disclose the change and obtain approval
    where required.
 -  Keep a stage ledger of route, canonical model, vendor, task and session
    IDs, draft revision reviewed, receipts and outcome. Preserve the exact
    reviewed drafts as snapshots or another verifiable revision history. Verify
    model identity, read-only enforcement and resumability from the actual
    evidence, not from the nominal route label. Keep route and receipt details
    out of the draft supplied to other reviewers unless needed.


Run independent rounds
----------------------

1.  Freeze the draft and assign its revision ID. Give each reviewer the same
    draft, relevant evidence and stage-specific questions in a separate
    session. Ask for findings with evidence, severity, and a proposed
    disposition; ask whether anything blocks advancement. Do not give regular
    reviewers each other's findings or responses. Keep their first sessions
    using `--keep-session` or a verified equivalent. With the CLI, use
    `--no-fallback` on review calls so the runner cannot quietly change routes;
    the coordinator handles any alternative explicitly.
2.  Inspect the receipts and answers. Decide which findings to accept, reject
    with reason, or escalate to the user. Revise the draft, or explicitly
    record why it is unchanged. Begin another round only after the current
    round is accounted for; do not combine parallel reviewers into one review
    session.
3.  Resume each original reviewer on the **same agent, exact model/route, and
    session within that stage**, with a new task ID and the current frozen
    revision. Provide the draft and the coordinator's rationale for that
    reviewer's findings, not another reviewer's raw output. Repeat read grants
    and policies on resume. Require at least two completed rounds from each
    regular reviewer and explicit closure of blocking findings. A timeout,
    refusal, missing receipt, or incomplete/mismatched answer is not a
    completed round. If the current draft materially differs from the last
    reviewed version, run another common round on the current revision before
    final review.
4.  Handle mid-stage failures via `cross-subagents` quota or resume procedures,
    but do not switch an active reviewer to a different route, model, or
    session and count earlier rounds. If the original session cannot continue,
    record the blocker and stop that gate. You may propose a new eligible panel
    with a full restart from round one, but only with the user's approval;
    never merge old and new rounds. Resume a timed-out task only if the receipt
    and protected inputs allow it.


Final check and advancement
---------------------------

Send the current reviewed revision, material evidence, and an anonymized
decision and finding log to the separate frontier reviewer. Ask them to verify
the stage's conclusion, evidence, compatibility and scope as applicable, and to
return an explicit pass or blocking findings. Inspect the actual final-review
receipt and answer. This review is in addition to the regular rounds, not one
of them.

Advance only after the final reviewer passes that revision and no blocking
finding or consequential user decision is open. If final review requires a
substantive revision, take it back through the regular reviewers in their
original sessions, then obtain another frontier check of the resulting
revision. Retain the final reviewer and session for follow-up where possible;
if no follow-up is possible, do not count the prior check for the new revision.
Revalidate budget as needed. A veto, missing check, or user preference to skip
reviews is not a pass under this protocol. Record the disposition and point to
the final artifact and receipts in the ledger.
