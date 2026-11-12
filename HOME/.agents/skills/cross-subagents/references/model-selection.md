Model survey and selection reference
====================================

Open-ended model support
------------------------

The skill supports a fixed set of coding agent CLIs (Codex, Claude Code,
OpenCode, Antigravity), but it never hardcodes which models may run through
them. Every authenticated model that a CLI exposes becomes a route candidate,
including models from providers such as OpenCode Go (GLM, Kimi, MiniMax, MiMo,
Qwen) and any model released after this skill was written.

`scripts/select_route.py` ships family priors for well-known families so they
rank sensibly before a survey exists. A newly discovered model with no prior
falls back to a generic prior (`task_fit` 0.75, text-only, 75 tokens per
second) that clears the default quality floors. Run a survey for that model as
soon as practical so selection relies on evidence instead of the generic
prior.


Canonical survey storage
------------------------

Store model surveys by canonical model family and key rather than by agent
binary so identical models exposed across multiple agents or providers share a
single survey record:

~~~~ text
$SKILL_CACHE_DIR/model-survey/<family>/<canonical_key>.json
$SKILL_CACHE_DIR/model-survey/index.json
~~~~

`index.json` maps every `(agent, provider, model_id)` route across all
discovered agents to its `canonical_key`. For example,
`claude-code/anthropic/claude-opus-5-5`, `opencode/anthropic/claude-opus-5-5`,
and `antigravity/anthropic/claude-opus-5-5-high` all resolve to
`claude-opus/claude-opus-5-5.json`.


Survey procedure and schema
---------------------------

Survey current-generation models by default (selected dynamically by highest
version tuple per model lineage), plus any older or specialized model the user
explicitly asks to evaluate. Because surveying multiple model families takes
significant web search volume, do not run surveys inside the main conversation
context. Spawn one orchestrator subagent that fans out parallel research
subagents per model family and writes the resulting JSON files via
`scripts/models.py survey-put <canonical_key> --file <path>`.

Check which models lack a fresh survey with:

~~~~ bash
python3 <skill-dir>/scripts/models.py survey-missing
~~~~

Each survey gathers evidence from at least two independent sources (combining
official documentation or release notes with independent benchmarks and
developer reports from forums such as Reddit or Hacker News). Providers
sometimes adjust serving quantization, routing, or tool-call harnesses over
time, so prefer recent reports over launch-day announcements.

Illustrative schema example for `<family>/<canonical_key>.json`:

~~~~ json
{
  "schema_version": 2,
  "canonical_key": "claude-opus-5-5",
  "family": "claude-opus",
  "surveyed_at": "2026-10-28T12:00:00Z",
  "released_at": "2026-09-22",
  "confidence": "high",
  "sources": [
    {"url": "https://docs.anthropic.com/...", "published": "2026-10-20", "kind": "official"},
    {"url": "https://news.ycombinator.com/...", "published": "2026-10-24", "kind": "community"}
  ],
  "strengths": ["Long-horizon refactoring", "Low false-positive rate in code review"],
  "weaknesses": ["Higher weekly quota consumption than Sonnet"],
  "task_fit": {
    "code_review": 0.91,
    "security_audit": 0.90,
    "architecture": 0.91,
    "debugging": 0.90,
    "code_generation": 0.92,
    "refactoring": 0.91,
    "testing": 0.89,
    "prose": 0.93,
    "documentation": 0.91,
    "survey": 0.89,
    "general": 0.91
  },
  "context_window": 400000,
  "input_modalities": ["text", "image"],
  "output_modalities": ["text"],
  "cost": {"input_per_mtok": 15.0, "output_per_mtok": 75.0},
  "latency_tier": 2,
  "tokens_per_sec": 62.0,
  "knowledge_cutoff": null,
  "tool_use_reliability": "high",
  "instruction_adherence": "high",
  "effort_differences": "High effort materially improves multi-file debugging; medium suffices for routine edits.",
  "notes": "Illustrative example payload; leave unverified fields null.",
  "unknowns": ["Exact training cutoff month not published in official release notes"]
}
~~~~

Do not fabricate benchmark numbers or cutoff dates. Leave unverified fields
`null` and list unanswered questions in `unknowns`. When a newly released model
has almost no public track record, set `confidence: "insufficient"` so
`scripts/select_route.py` falls back to family priors until real data is
available.

Model capability surveys describe the model (`task_fit`, `context_window`,
`input_modalities`, `tokens_per_sec`, and public API per-MToken reference
pricing in `cost`). Record `tokens_per_sec` as the median output throughput
from independent measurements; providers may serve the same model at different
speeds, so put provider-specific throughput under `provider_overrides`.
Record `released_at` as the model's public release date (ISO `YYYY-MM-DD`) when
release notes state it; it feeds recency scoring and overrides the local registry.

Whether a given route is covered by a flat subscription or billed per API token
depends on the route's own `(agent, provider, auth_plan)` tuple, which
`select_route.py` evaluates per route at selection time.


Vision and multimodal requirements
----------------------------------

Most code generation does not need image input, but some tasks cannot be done
without it: implementing a UI from a design mockup, comparing a rendered page
against a reference, or reading Playwright screenshots while debugging a
frontend. For these tasks, pass `--require-vision` (equivalent to
`--require-modality image`) and usually `--task ui_debugging` or
`--task frontend`.

A text-only model handed a screenshot path will either ignore it or guess, so
treat vision as a hard filter, not a soft preference. By default the filter
accepts only modalities verified by the CLI catalog or a sourced survey. When
neither source reports modalities for a route, the filter rejects it with
`unverified_modalities`. Pass `--allow-estimated-capabilities` to accept family
priors and name-based hints (such as `vl`, `vision`, or `omni` in the model ID)
instead; the rationale then reports `vision=ok` but `verified_vision: false`.


Token generation speed
----------------------

When the user asks for a quick turnaround, pass `--speed fast` (or
`--prefer-fast`), or `--speed urgent` when latency matters more than a small
quality loss. The selector computes a `speed_score` in `[0, 1]` from
`tokens_per_sec` (normalized against 160 tokens per second) and the latency
tier, then adds it to the score with a mode-dependent weight:

| `--speed` | Weight |
| --------- | ------ |
| `normal`  | 0.03   |
| `fast`    | 0.15   |
| `urgent`  | 0.30   |

Speed never overrides the quality floor. It only reorders routes that already
qualify, so an urgent request still gets a model fit for the task. Throughput
comes from the survey when available and otherwise from the family prior or a
latency-tier default; `tps_estimated: true` in the candidate output marks the
estimated case.


Model recency
-------------

Within one model family the newer release is usually the better one, so a
route's release date contributes a small, bounded term to the soft score:

~~~~ text
age <= 30 days        recency = +1.0
age == 180 days       recency =  0.0   (six months: neither fresh nor stale)
age >= 540 days       recency = -1.0
~~~~

Between those anchors the value interpolates linearly. With the `0.05` weight
the whole range spans `0.10`, comparable to the cost and headroom terms: enough
to break a close call in favor of the fresher model, not enough to promote a
newer mid-tier model over a clearly stronger one. Release dates are resolved in
this order:

1. `released_at` in the model's survey, when the survey records an ISO date
   (add the field to the survey JSON; it is evidence and overrides the rest).
2. The local model registry that OpenCode maintains at
   `$XDG_CACHE_HOME/opencode/models.json` (a models.dev snapshot with
   `release_date` per provider and model). It covers every provider in that
   snapshot, not just OpenCode routes. The date is resolved per model rather
   than per provider, so the same model scores the same no matter which CLI or
   provider serves it. Set `CROSS_SUBAGENTS_MODEL_REGISTRY=/path/to/models.json`
   when the snapshot lives elsewhere.
3. `released_at` on the matching family prior, if one is ever added there.

When none of these knows the date, `recency` is `0.0`, so routes without release
metadata rank exactly as before. Each candidate reports `release_date`,
`model_age_days`, `recency_score`, and `recency_source`, and the one-line
rationale includes the release age, so a freshness-driven pick is auditable
rather than silent. Recency never bypasses the hard filters, the per-task
quality floor, or `--max-tier`, which stays task-fit-only by design.


Hierarchical cache TTLs
-----------------------

Different metadata ages at different rates:

 -  Model availability (`models/<agent>.json`) uses a 24-hour TTL and also
    invalidates early when the agent's configuration fingerprint changes.
 -  Account quota and reset credits (`quota/status.json`) use a 15-minute
    per-agent TTL and overlay active 429 observations (`observations.json`) on
    every read.
 -  Survey cost metadata uses a 7-day TTL (`cost_stale: true` after 7 days).
 -  Survey benchmarks and task fit use a 30-day TTL (`survey_stale: true` after
    30 days).

When a survey exceeds its TTL, `models.py` retains the existing file, marks the
corresponding staleness flag, and steps `effective_confidence` down by one
level (`high` to `medium`, `medium` to `low`, and `low` to `insufficient` after
30 days).


Five-step route selection
-------------------------

`scripts/select_route.py` selects routes through five stages:

1.  Exclude routes whose agent is uninstalled or unauthenticated, routes where
    any applicable quota bucket (`applicable_buckets`, such as
    `claude-code:session`, `claude-code:all-models`, or `claude-code:fable`) is
    confirmed exhausted on a non-stale reading, routes that lack a verified
    `context_window` or `input_modalities` matching `--min-context` or
    `--require-modality` (unless `--allow-estimated-capabilities` is passed),
    routes excluded by user pins (`--pin-agent`, `--pin-model`,
    `--exclude-vendor`), routes outside `--include-vendor`, routes whose
    vendor-internal tier rank exceeds `--max-tier`, and routes whose adjusted
    cost index exceeds `--max-cost-index`. Never use a stale proactive quota
    reading (`stale: true`) as a hard filter.

2.  Enforce a minimum `task_fit` quality floor per task category (for example,
    `0.75` for `security_audit`, `0.72` for `code_review` and `debugging`,
    `0.70` for `ui_debugging`, `0.68` for `frontend`, and `0.65` for
    `code_generation`, `refactoring`, and `prose`). Low cost, ample quota, or
    high speed never overrides an unsuitable model.

3.  Compute the soft trade-off score for every route above the quality floor:

    ~~~~ text
    score = task_fit
          - 0.22 * scarcity
          + 0.18 * credit_expiry_pressure
          + 0.14 * reset_imminence
          - 0.08 * cost_index
          + speed_weight * speed_score
          + 0.05 * recency
    ~~~~

    Here `scarcity` is the maximum scarcity across all quota buckets that apply
    to the route, reflecting both `used_percent` and burn pace relative to the
    elapsed fraction of the quota window, scaled by the subscription plan's
    estimated capacity factor. `speed_weight` depends on `--speed` (see
    [Token generation speed](#token-generation-speed)). `recency` is explained
    in [Model recency](#model-recency).

4.  When scores are within `0.01`, prefer the route with greater effective quota
    headroom, then lower latency tier, then a fresher model, then higher
    `tokens_per_sec`, then lower cost.

5.  Return the primary route alongside 2–3 fallback routes. The selector first
    picks routes with both a distinct quota bucket and a distinct canonical
    model, then includes routes that serve the same canonical model on a
    different quota bucket (for example, failing over from
    `opencode/deepseek/deepseek-flash` to
    `opencode/opencode-go/deepseek-v4.1-flash` or from `claude-code` to
    `opencode/anthropic`).


Vendor tiers
------------

`--max-tier N` keeps only models ranked at or above N within their own vendor.
Tier ranking is computed from task-fit evidence only: the latest validated
survey for the canonical model, otherwise the family prior. It deliberately
ignores quota scarcity, reset timing, cost, speed, and the agent-specific
native bonus, so a vendor's flagship does not change identity because an
account is tight or because the same model is served through a different CLI.

Ranking is per canonical model, with ties sharing a rank. The same model served
through several providers therefore receives one rank, and `--max-tier 2` does
not arbitrarily drop one of two equally strong models. Use `--max-tier 2` for
"top or second tier" and `--max-tier 1` when only the flagship is acceptable.
Combined with `--include-vendor`, it expresses constraints like "the two best
models from OpenAI, Anthropic, or Google" without enumerating model IDs.


Cost ceiling
------------

The scoring formula treats cost as a soft signal (`-0.08 * cost_index`), which
is deliberately small: a model with a meaningfully higher `task_fit` wins even
when it costs several times more. That is the right default for code review or
debugging, where the strongest model is worth the quota, but it is the wrong
default for copy-editing. Raising `--max-tier` does not help, because the cap is
inclusive: `--max-tier 2` still allows the tier-1 flagship.

`--max-cost-index <float>` adds a hard ceiling instead. It drops every route
whose adjusted `cost_index` exceeds the value, then selects normally among the
survivors. The index already reflects billing mode: free routes sit near `0.01`,
subscription routes are discounted by half because they consume quota rather
than per-token money, and metered routes use per-MToken pricing when a survey
provides it (otherwise the family prior). A cap of `0.10` is a useful starting
point for prose review; loosen it to `0.15` when no eligible reviewer survives.

The ceiling applies to the primary route and its fallbacks alike, and it can be
set per step with `max_cost_index` in `--step-spec`. When the cap leaves no
route, the selector returns `no_route_available` rather than silently spending
more; treat that as a signal to self-review or to relax the cap deliberately.


Multi-step planning
-------------------

A workflow that needs several independent reviewers is not a fallback chain:
fallbacks substitute for a failed route, while steps are separate rounds that
must not repeat a model. `select_route.py --steps N` returns a `steps` array,
each entry containing its own `selected` route, `fallbacks`, and `rationale`.

Each step runs a normal selection with accumulated exclusions: every route
already chosen is added to `exclude_route_ids`, and with
`--step-distinct-vendor` (the default) the used vendors are added to the hard
filters. `--step-distinct-bucket` additionally excludes the quota buckets used
by earlier steps. Per-step constraints come from `--step-spec`, a JSON object
(inline or `@file`) that may override `task`, `speed`, `max_tier`,
`max_cost_index`, `quality_floor`, `min_context`, `min_headroom`,
`allow_micro_quota`, `allow_estimated_capabilities`, `include_vendor`,
`exclude_vendor`, `exclude_agent`, `exclude_route`, `require_modality`,
`require_vision`, and the `pin_*` fields. Steps without a spec inherit the
global flags. Global `--exclude-route`, `--exclude-vendor` and
`--exclude-agent` entries are additive: a step spec can add exclusions but
cannot remove a global one. A
canonical model chosen by one step is excluded from every later step even when
`--no-step-distinct-vendor` allows the same vendor.

Plan mode ignores saved session pins, because a pin is a single-route
constraint that conflicts with multi-step independence; pass `--pin-*`
explicitly only when a single route is intended. A step that cannot satisfy its
constraints (for example two steps both restricted to one vendor with
`--step-distinct-vendor`) is reported under `unfilled`, and the plan status
becomes `partial` rather than silently repeating a model.

When plan mode runs with `--run-id`, the effective constraints for each step
(including accumulated vendor, agent, route, capability, and sandbox filters)
are saved to `<run-dir>/selection-plan.json`. An explicit `run.py --route`
whose route matches a saved step inherits those constraints when it builds its
fallback chain, so a quota failure cannot escape the plan's filtered vendor
set. If no plan was saved, an explicitly pinned route restricts its fallbacks
to the primary route's own vendor and canonical model, allowing another
provider to serve the same model without silently downgrading to a weaker
variant. Pass `--max-tier <N>` to permit substitution within an explicit tier
bound, `--include-vendor` to widen the vendor allow-list, or `--no-fallback`
to disable fallback entirely. Family fit estimates alone do not establish that
a flagship and a flash variant are interchangeable.

When quota prevents this chain from continuing, follow
[quota hand-over](quota-handover.md). The coordinator must preserve the assigned
work, reselect under its original requirements, or ask with a disclosed
30-minute default when those requirements need to change. This recovery may
start a new task; `run.py` does not conduct the user conversation or arm the
host timer. Record the new task link with `run.py record-handover` so the final
report retains the transition.


Absolute quota headroom vs. deceptive percentage usage
------------------------------------------------------

Account usage percentages can be deceptive. A free, trial, or hobby plan might
report `0% used` (100% remaining), but its absolute capacity may be limited to
a few requests per day or 20,000 tokens—barely enough for a single code
generation turn. Conversely, a high-tier account (Pro, Max, Enterprise, or
pay-as-you-go metered API) at 85% usage still possesses abundant absolute token
headroom.

To prevent assigning heavy tasks to accounts that will run out of quota
mid-task:

 -  `scripts/quota.py` assigns realistic plan capacity multipliers:
    `enterprise: 3.0`, `team/promax: 2.5`, `pro: 1.5`, `max: 1.2`, `plus: 0.6`,
    `starter/hobby: 0.25`, and `free/trial: 0.1`.
 -  Route selection computes
    `effective_headroom = (remaining_pct / 100.0) * capacity_factor`.
 -  Heavy tasks (`code_review`, `debugging`, `architecture`, `code_generation`,
    `security_audit`, `ui_debugging`, `refactoring`, `testing`) require a
    minimum absolute headroom floor (`--min-headroom`, default `0.15`).
 -  Low-capacity accounts (`free`, `trial`, `starter`, `hobby`) with
    insufficient absolute headroom are automatically filtered out for heavy
    tasks unless explicitly pinned or overridden with `--allow-micro-quota`.
 -  In the scoring formula, higher absolute headroom earns a modest score bonus,
    ensuring well-provisioned accounts are naturally favored over micro-quota
    plans even when the micro-quota plan is currently at 0% usage.


Consuming quota before weekly reset and expiring reset credits
--------------------------------------------------------------

Waiting for an exact tie between two models before considering reset timing
makes the rule inert in practice, because no two model families have identical
`task_fit` profiles. Instead, `reset_imminence` and `credit_expiry_pressure`
act as soft trade-offs above the quality floor:

 -  When a weekly quota window is within 72 hours of resetting and still has
    substantial unused headroom, `reset_imminence` boosts higher-tier routes in
    that bucket (such as preferring `gpt-6-astra` or `gpt-6.1-sol` over a
    smaller model) so paid subscription capacity is put to work before it
    expires.
 -  When an account holds a free rate-limit reset credit expiring within 7
    days, `credit_expiry_pressure` prioritizes routes in that bucket so the
    remaining allowance is used and the reset credit can be redeemed before
    expiration.
 -  If a run becomes blocked on a confirmed exhausted Codex quota bucket
    (explicit limit reached or fractional usage `>= 99.95%`) and an unexpired
    free reset credit expiring within 72 hours is available, record the user
    notification via `scripts/quota.py notify-reset-credit --run-id <run-id>`
    and notify the user asynchronously without blocking execution on other
    routes. If the run remains blocked with no user objection after 30 minutes
    (or immediately if the user approves with `--user-approved`),
    `scripts/quota.py consume-reset-credit --run-id <run-id>` redeems at most
    one free credit per run using an attempt-scoped `idempotencyKey`, verifies
    the RPC outcome, and refreshes `quota/status.json`.
