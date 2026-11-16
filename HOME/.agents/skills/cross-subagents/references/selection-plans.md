# Selection flags and multi-step plans

Read this for constrained route selection or independent multi-step workflows.
For scoring, surveys, quotas, recency and reset-credit policy, read
[model-selection.md](model-selection.md).

`prepare.py` accepts the same selection/plan flags, delegates to the existing
selector and adds compact probe/catalog warnings and open defects.
It never dispatches. `--refresh` refreshes discovery only; quota refresh remains
explicit `--refresh-quota` under the selector's existing ownership rules.
One probe/catalog snapshot is reused through selection and reporting, including
stale data and exact probe/refresh errors; failed discovery is not silently
retried by rereading its caches.
`--human` points to a complete preparation JSON receipt; use it for full
candidate/filter/plan details. Direct selector commands below remain supported.

## Selection options

- `--pin-agent`, `--pin-provider`, `--pin-model` restrict named agents/models.
  `--save-pin` persists a constraint; `--run-id` scopes it to a run.
- `--exclude-vendor` requires independence from the host vendor.
  `--include-vendor` (repeatable) restricts the allowed vendors.
- `--max-tier N` permits only vendor-relative ranks 1 through N. Ranking uses
  task-fit evidence independently of quota, cost and speed; scarce quota must
  not silently turn a vendor's third model into its flagship.
- `--max-cost-index FLOAT` (0–1) is a hard billing-adjusted cost cap, unlike
  the selector's soft cost weight. `0.10` is useful for budget-sensitive prose.
  Step specs accept `max_cost_index`.
- `--exclude-route ROUTE` removes an exact route.
- `--require-vision` / `--require-modality image` requires image input.
- `--speed normal|fast|urgent` / `--prefer-fast` weights generation throughput
  and latency. Recency is automatic: six months is neutral, unknown dates
  neutral, and age breaks close calls rather than overriding stronger fit.
- `--min-headroom FLOAT` (default .15 for heavy tasks) and
  `--allow-micro-quota` control absolute token capacity; 100% remaining on a
  small free plan does not imply enough capacity.
- `--min-context TOKENS`, `--require-modality MODALITY` require verified
  capabilities; `--allow-estimated-capabilities` permits family priors.
- `--dry-run` prints selection, rationale and fallbacks without dispatch.
- `--sandbox read-only|workspace-write|bypass` filters incompatible CLIs.
  Pass the same posture to execution. Old Antigravity versions without
  mode/sandbox support cannot honor read-only or workspace-write.

## Quota scope

Quota collection applies to candidate agents after pins/exclusions. Repeatable
`--quota-agent` and `--exclude-quota-agent` restrict quota reads independently
of route eligibility, on both `select_route.py` and `run.py`.
`--quota-cache-only` prohibits live queries; `--quota-state PATH` supplies a
coordinator's quota JSON without collection. Stale quota remains stale and
unavailable quota unknown; capability and eligibility checks still apply.
An explicit `run.py --route` preflights only that agent; fallback planning
honors the same controls. Antigravity discovery does not prove available quota:
quota comes from CodexBar group and model windows.

## Independent steps, not fallback chains

```bash
python3 <skill-dir>/scripts/select_route.py --task code_review --steps 3 \
  --run-id <run-id> \
  --step-spec '{"speed":"fast","quality_floor":0.60,"exclude_vendor":["openai","anthropic","google"]}' \
  --step-spec '{"include_vendor":["openai","anthropic","google"],"max_tier":2}' \
  --step-spec '{"include_vendor":["openai","anthropic","google"],"max_tier":2}' \
  --human
```

`--step-spec` accepts inline JSON or `@path/to/spec.json`; omitted steps inherit
global flags. Global route/vendor/agent exclusions are additive: a step cannot
re-enable globally excluded routes. Earlier routes are excluded from later
steps; default `--step-distinct-vendor` requires different vendors.
`--step-distinct-bucket` additionally separates subscription buckets.
Each step's fallbacks recover that step; they do not replace other planned steps.
Plan mode ignores saved session pins; use explicit pins only for intended
single-route constraints.

Selection does not execute tasks. Pass each step's `selected.route_id` to
`run.py --route` for its own rounds. With `--run-id`, effective per-step
constraints persist to `<run-dir>/selection-plan.json`, preventing fallback from
escaping the plan's filters.

Without a saved plan, explicit routes retain the same canonical model across
providers within the primary vendor. `--max-tier` permits bounded model
substitution; `--include-vendor` widens the vendor list but alone does not allow
a downgrade. `--no-fallback` means the exact required route or nothing.
For exhausted quota, follow [quota-handover.md](quota-handover.md); a failed
attempt does not cancel the task.
