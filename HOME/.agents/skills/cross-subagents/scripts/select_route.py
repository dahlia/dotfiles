#!/usr/bin/env python3
"""Quota- and capability-aware route selector for cross-subagents.

Implements the 5-step selection procedure:
  1. Hard filters (availability, non-stale multi-bucket quota exhaustion, verified context window & modalities, pins/exclusions)
  2. Task-specific quality floor (`min_task_fit`)
  3. Soft trade-off scoring:
       score = task_fit - lambda*scarcity + mu*credit_expiry_pressure + nu*reset_imminence - omicron*cost
  4. Tie-breaking (effective headroom -> latency tier -> cost)
  5. Fallback chain construction (prioritizing distinct quota buckets, plus same-bucket model alternatives tagged for non-quota failover)

Usage:
  python3 select_route.py --task code_review [--run-id <id>] [--exclude-vendor google] [--pin-agent codex] [--dry-run] [--human]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from common import (
    SCHEMA_VERSION,
    canonical_agent_name,
    enforce_utf8,
    ensure_child_path,
    get_cache_dir,
    infer_applicable_quota_buckets,
    infer_model_vendor as infer_vendor_from_common,
    infer_model_family,
    is_bare_generation_prefix,
    locked_update_json,
    normalize_official_model_id,
    now_iso,
    official_agent_name,
    read_json_file,
    validate_safe_identifier,
)
from models import collect_all_models, get_survey, resolve_official_model_id
from quota import collect_quota

TASK_QUALITY_FLOORS: dict[str, float] = {
    "code_review": 0.72,
    "security_audit": 0.75,
    "architecture": 0.74,
    "debugging": 0.72,
    "ui_debugging": 0.70,
    "frontend": 0.68,
    "code_generation": 0.65,
    "refactoring": 0.65,
    "testing": 0.62,
    "prose": 0.65,
    "documentation": 0.58,
    "survey": 0.55,
    "general": 0.60,
}

HEAVY_TASKS: set[str] = {
    "code_review",
    "debugging",
    "architecture",
    "code_generation",
    "security_audit",
    "ui_debugging",
    "refactoring",
    "testing",
}

# Family prior estimates used for soft ranking when a model survey is missing or has `confidence: "insufficient"`.
# Never used to satisfy hard `--min-context` or `--require-modality` / `--require-vision` filters unless `--allow-estimated-capabilities` is set.
FAMILY_PRIORS: dict[str, dict[str, Any]] = {
    "gpt-6-astra": {
        "task_fit": {
            "code_review": 0.95,
            "security_audit": 0.94,
            "architecture": 0.94,
            "debugging": 0.94,
            "ui_debugging": 0.92,
            "frontend": 0.91,
            "code_generation": 0.92,
            "refactoring": 0.91,
            "testing": 0.90,
            "prose": 0.88,
            "documentation": 0.88,
            "survey": 0.89,
            "general": 0.93,
        },
        "context_window": 400000,
        "input_modalities": ["text", "image"],
        "cost_index": 0.35,
        "latency_tier": 3,
        "tokens_per_sec": 45.0,
    },
    "gpt-6.1-sol": {
        "task_fit": {
            "code_review": 0.91,
            "security_audit": 0.90,
            "architecture": 0.90,
            "debugging": 0.92,
            "ui_debugging": 0.91,
            "frontend": 0.92,
            "code_generation": 0.93,
            "refactoring": 0.92,
            "testing": 0.91,
            "prose": 0.86,
            "documentation": 0.87,
            "survey": 0.87,
            "general": 0.91,
        },
        "context_window": 400000,
        "input_modalities": ["text", "image"],
        "cost_index": 0.25,
        "latency_tier": 2,
        "tokens_per_sec": 78.0,
    },
    "gpt-6-sol": {
        "task_fit": {
            "code_review": 0.89,
            "security_audit": 0.88,
            "architecture": 0.88,
            "debugging": 0.90,
            "ui_debugging": 0.89,
            "frontend": 0.90,
            "code_generation": 0.91,
            "refactoring": 0.90,
            "testing": 0.89,
            "prose": 0.84,
            "documentation": 0.85,
            "survey": 0.85,
            "general": 0.89,
        },
        "context_window": 400000,
        "input_modalities": ["text", "image"],
        "cost_index": 0.22,
        "latency_tier": 2,
        "tokens_per_sec": 75.0,
    },
    "gpt-6.1-terra": {
        "task_fit": {
            "code_review": 0.82,
            "security_audit": 0.80,
            "architecture": 0.80,
            "debugging": 0.84,
            "ui_debugging": 0.84,
            "frontend": 0.86,
            "code_generation": 0.86,
            "refactoring": 0.85,
            "testing": 0.86,
            "prose": 0.80,
            "documentation": 0.82,
            "survey": 0.82,
            "general": 0.83,
        },
        "context_window": 256000,
        "input_modalities": ["text", "image"],
        "cost_index": 0.12,
        "latency_tier": 1,
        "tokens_per_sec": 135.0,
    },
    "gpt-6-luna": {
        "task_fit": {
            "code_review": 0.78,
            "security_audit": 0.75,
            "architecture": 0.76,
            "debugging": 0.80,
            "ui_debugging": 0.81,
            "frontend": 0.83,
            "code_generation": 0.83,
            "refactoring": 0.82,
            "testing": 0.83,
            "prose": 0.78,
            "documentation": 0.80,
            "survey": 0.80,
            "general": 0.80,
        },
        "context_window": 256000,
        "input_modalities": ["text", "image"],
        "cost_index": 0.08,
        "latency_tier": 1,
        "tokens_per_sec": 150.0,
    },
    "claude-fable": {
        "task_fit": {
            "code_review": 0.95,
            "security_audit": 0.94,
            "architecture": 0.95,
            "debugging": 0.93,
            "ui_debugging": 0.93,
            "frontend": 0.94,
            "code_generation": 0.93,
            "refactoring": 0.93,
            "testing": 0.90,
            "prose": 0.95,
            "documentation": 0.93,
            "survey": 0.92,
            "general": 0.94,
        },
        "context_window": 400000,
        "input_modalities": ["text", "image", "pdf"],
        "cost_index": 0.40,
        "latency_tier": 3,
        "tokens_per_sec": 42.0,
    },
    "claude-opus": {
        "task_fit": {
            "code_review": 0.91,
            "security_audit": 0.90,
            "architecture": 0.91,
            "debugging": 0.90,
            "ui_debugging": 0.90,
            "frontend": 0.91,
            "code_generation": 0.92,
            "refactoring": 0.91,
            "testing": 0.89,
            "prose": 0.93,
            "documentation": 0.91,
            "survey": 0.89,
            "general": 0.91,
        },
        "context_window": 400000,
        "input_modalities": ["text", "image", "pdf"],
        "cost_index": 0.28,
        "latency_tier": 2,
        "tokens_per_sec": 62.0,
    },
    "claude-sonnet": {
        "task_fit": {
            "code_review": 0.83,
            "security_audit": 0.81,
            "architecture": 0.82,
            "debugging": 0.84,
            "ui_debugging": 0.86,
            "frontend": 0.88,
            "code_generation": 0.87,
            "refactoring": 0.86,
            "testing": 0.86,
            "prose": 0.86,
            "documentation": 0.86,
            "survey": 0.84,
            "general": 0.85,
        },
        "context_window": 200000,
        "input_modalities": ["text", "image", "pdf"],
        "cost_index": 0.14,
        "latency_tier": 1,
        "tokens_per_sec": 95.0,
    },
    "claude-haiku": {
        "task_fit": {
            "code_review": 0.68,
            "security_audit": 0.65,
            "architecture": 0.65,
            "debugging": 0.70,
            "ui_debugging": 0.73,
            "frontend": 0.75,
            "code_generation": 0.74,
            "refactoring": 0.73,
            "testing": 0.75,
            "prose": 0.74,
            "documentation": 0.76,
            "survey": 0.75,
            "general": 0.72,
        },
        "context_window": 200000,
        "input_modalities": ["text", "image", "pdf"],
        "cost_index": 0.05,
        "latency_tier": 1,
        "tokens_per_sec": 145.0,
    },
    "gemini-4": {
        "task_fit": {
            "code_review": 0.89,
            "security_audit": 0.88,
            "architecture": 0.90,
            "debugging": 0.89,
            "ui_debugging": 0.92,
            "frontend": 0.91,
            "code_generation": 0.90,
            "refactoring": 0.89,
            "testing": 0.88,
            "prose": 0.87,
            "documentation": 0.89,
            "survey": 0.91,
            "general": 0.89,
        },
        "context_window": 1000000,
        "input_modalities": ["text", "image", "audio", "video", "pdf"],
        "cost_index": 0.22,
        "latency_tier": 2,
        "tokens_per_sec": 85.0,
    },
    "gemini-3.8": {
        "task_fit": {
            "code_review": 0.78,
            "security_audit": 0.76,
            "architecture": 0.77,
            "debugging": 0.79,
            "ui_debugging": 0.85,
            "frontend": 0.84,
            "code_generation": 0.82,
            "refactoring": 0.81,
            "testing": 0.82,
            "prose": 0.79,
            "documentation": 0.82,
            "survey": 0.84,
            "general": 0.80,
        },
        "context_window": 1000000,
        "input_modalities": ["text", "image", "audio", "video", "pdf"],
        "cost_index": 0.08,
        "latency_tier": 1,
        "tokens_per_sec": 155.0,
    },
    "deepseek": {
        "task_fit": {
            "code_review": 0.76,
            "security_audit": 0.74,
            "architecture": 0.75,
            "debugging": 0.78,
            "ui_debugging": 0.70,
            "frontend": 0.78,
            "code_generation": 0.81,
            "refactoring": 0.80,
            "testing": 0.80,
            "prose": 0.75,
            "documentation": 0.78,
            "survey": 0.78,
            "general": 0.78,
        },
        "context_window": 128000,
        "input_modalities": ["text"],
        "cost_index": 0.04,
        "latency_tier": 1,
        "tokens_per_sec": 125.0,
    },
    "qwen": {
        "task_fit": {
            "code_review": 0.81,
            "security_audit": 0.79,
            "architecture": 0.80,
            "debugging": 0.83,
            "ui_debugging": 0.82,
            "frontend": 0.84,
            "code_generation": 0.86,
            "refactoring": 0.85,
            "testing": 0.84,
            "prose": 0.79,
            "documentation": 0.81,
            "survey": 0.80,
            "general": 0.82,
        },
        "context_window": 256000,
        "input_modalities": ["text", "image"],
        "cost_index": 0.06,
        "latency_tier": 1,
        "tokens_per_sec": 115.0,
    },
    "glm": {
        "task_fit": {
            "code_review": 0.80,
            "security_audit": 0.78,
            "architecture": 0.79,
            "debugging": 0.81,
            "ui_debugging": 0.79,
            "frontend": 0.82,
            "code_generation": 0.84,
            "refactoring": 0.83,
            "testing": 0.82,
            "prose": 0.80,
            "documentation": 0.81,
            "survey": 0.80,
            "general": 0.81,
        },
        "context_window": 200000,
        "input_modalities": ["text", "image"],
        "cost_index": 0.06,
        "latency_tier": 1,
        "tokens_per_sec": 110.0,
    },
    "kimi": {
        "task_fit": {
            "code_review": 0.83,
            "security_audit": 0.80,
            "architecture": 0.82,
            "debugging": 0.84,
            "ui_debugging": 0.81,
            "frontend": 0.84,
            "code_generation": 0.86,
            "refactoring": 0.85,
            "testing": 0.84,
            "prose": 0.83,
            "documentation": 0.84,
            "survey": 0.84,
            "general": 0.84,
        },
        "context_window": 256000,
        "input_modalities": ["text", "image"],
        "cost_index": 0.07,
        "latency_tier": 1,
        "tokens_per_sec": 105.0,
    },
    "minimax": {
        "task_fit": {
            "code_review": 0.79,
            "security_audit": 0.76,
            "architecture": 0.78,
            "debugging": 0.80,
            "ui_debugging": 0.78,
            "frontend": 0.82,
            "code_generation": 0.83,
            "refactoring": 0.82,
            "testing": 0.81,
            "prose": 0.81,
            "documentation": 0.81,
            "survey": 0.80,
            "general": 0.80,
        },
        "context_window": 256000,
        "input_modalities": ["text", "image"],
        "cost_index": 0.05,
        "latency_tier": 1,
        "tokens_per_sec": 120.0,
    },
    "mimo": {
        "task_fit": {
            "code_review": 0.78,
            "security_audit": 0.75,
            "architecture": 0.77,
            "debugging": 0.80,
            "ui_debugging": 0.82,
            "frontend": 0.81,
            "code_generation": 0.82,
            "refactoring": 0.81,
            "testing": 0.80,
            "prose": 0.77,
            "documentation": 0.79,
            "survey": 0.79,
            "general": 0.79,
        },
        "context_window": 128000,
        "input_modalities": ["text", "image"],
        "cost_index": 0.05,
        "latency_tier": 1,
        "tokens_per_sec": 125.0,
    },
}

# Scoring weights
WEIGHT_SCARCITY = 0.22  # lambda
WEIGHT_CREDIT_EXPIRY = 0.18  # mu
WEIGHT_RESET_IMMINENCE = 0.14  # nu
WEIGHT_COST = 0.08  # omicron
SPEED_WEIGHTS: dict[str, float] = {
    "normal": 0.03,
    "fast": 0.15,
    "urgent": 0.30,
}
TIER_DEFAULT_TPS: dict[int, float] = {
    1: 120.0,
    2: 65.0,
    3: 38.0,
}


def resolve_prior_for_route(route: dict[str, Any]) -> dict[str, Any]:
    c_key = route["canonical_key"]
    # Re-derive the family from the canonical key so stale cached `family` values never hide a prior
    family = infer_model_family(c_key)
    if c_key in FAMILY_PRIORS:
        return FAMILY_PRIORS[c_key]
    for prefix in ("gpt-6-astra", "gpt-6.1-sol", "gpt-6-sol", "gpt-6.1-terra", "gpt-6-luna"):
        if c_key.startswith(prefix):
            return FAMILY_PRIORS[prefix]
    if family in FAMILY_PRIORS:
        return FAMILY_PRIORS[family]
    # Open-ended fallback for newly discovered models so they are eligible across standard tasks
    return {
        "task_fit": {"general": 0.75},
        "context_window": 128000,
        "input_modalities": ["text"],
        "cost_index": 0.12,
        "latency_tier": 2,
        "tokens_per_sec": 75.0,
    }


def classify_route_billing(route: dict[str, Any]) -> str:
    """Classify a route's billing mode as `subscription`, `free`, `api_metered`, or `unknown`."""
    agent = route.get("agent")
    provider = str(route.get("provider") or "").lower()
    model_id = str(route.get("model_id") or "").lower()
    auth_plan = str(route.get("auth_plan") or "").lower().strip()

    if auth_plan == "free" or (agent == "opencode" and provider == "opencode" and "free" in model_id and auth_plan in ("free", "unknown", "")):
        return "free"

    if (
        provider in ("bedrock", "vertex", "foundry")
        or auth_plan in ("api", "api_key", "apikey", "api key", "key", "token", "bedrock", "vertex", "foundry")
        or auth_plan.startswith("api-key:")
        or auth_plan.startswith("api_key:")
    ):
        return "api_metered"

    if agent == "codex" and (
        auth_plan in ("promax", "pro", "plus", "team", "chatgpt", "chatgpt_pro_500", "subscription")
        or "chatgpt" in auth_plan
    ):
        return "subscription"

    if (
        agent == "claude-code"
        and provider == "anthropic"
        and auth_plan in ("max", "pro", "claude max", "claude pro", "team", "enterprise", "claude.ai", "subscription")
    ):
        return "subscription"

    if agent == "antigravity" and auth_plan in ("subscription", "pro", "max"):
        return "subscription"

    if agent == "opencode" and (
        provider in ("opencode-go", "github-copilot")
        or auth_plan in ("oauth", "subscription")
        or auth_plan.startswith("subscription:")
    ):
        return "subscription"

    return "unknown"


def _survey_has_valid_sources(survey: dict[str, Any] | None) -> bool:
    if not isinstance(survey, dict):
        return False
    sources = survey.get("sources")
    if not isinstance(sources, list):
        return False
    from models import _is_valid_http_url

    valid_urls = [
        s
        for s in sources
        if isinstance(s, dict) and _is_valid_http_url(s.get("url"))
    ]
    return len(valid_urls) >= 2


def _safe_latency_tier(val: Any, default: int = 2) -> int:
    if not isinstance(val, bool) and isinstance(val, int) and val in (1, 2, 3):
        return val
    return default


def evaluate_route_traits(route: dict[str, Any], task: str) -> dict[str, Any]:
    """Combine route catalog metadata, validated survey data (with provider overrides), and family priors."""
    c_key = route["canonical_key"]
    survey = get_survey(c_key)
    prior = resolve_prior_for_route(route)
    prior_fits = prior.get("task_fit") or {}
    default_fit = prior_fits.get(task, prior_fits.get("general", 0.70))

    effort_adj = 0.0
    if route.get("agent") == "antigravity":
        if route.get("variant") == "low":
            effort_adj = -0.04
        elif route.get("variant") == "high":
            effort_adj = +0.02

    native_bonus = 0.0
    agent = route.get("agent")
    provider = route.get("provider")
    if (agent == "codex" and provider == "openai") or (
        agent == "claude-code" and provider == "anthropic"
    ):
        native_bonus = 0.02
    elif agent == "opencode" and provider == "deepseek" and c_key.startswith("deepseek"):
        native_bonus = 0.02

    eff_conf = (survey or {}).get("effective_confidence") or (survey or {}).get("confidence")
    has_valid_survey = bool(survey and eff_conf not in (None, "insufficient"))
    has_sourced_evidence = has_valid_survey and _survey_has_valid_sources(survey) and eff_conf in ("high", "medium")

    verified_ctx = (
        route.get("context_window")
        if (not isinstance(route.get("context_window"), bool) and isinstance(route.get("context_window"), int))
        else None
    )
    verified_modalities: list[str] | None = (
        [str(m).lower() for m in route["input_modalities"]]
        if isinstance(route.get("input_modalities"), list) and route.get("input_modalities")
        else None
    )

    prov_override: dict[str, Any] = {}
    if has_valid_survey and survey and isinstance(survey.get("provider_overrides"), dict):
        po_map = survey["provider_overrides"]
        cand_ov = po_map.get(f"{agent}/{provider}") or po_map.get(str(provider))
        if isinstance(cand_ov, dict):
            prov_override = cand_ov

    if has_valid_survey and survey:
        s_fits = dict(survey.get("task_fit") if isinstance(survey.get("task_fit"), dict) else {})
        if isinstance(prov_override.get("task_fit"), dict):
            s_fits.update(prov_override["task_fit"])

        raw_fit = s_fits.get(task)
        if raw_fit is None:
            raw_fit = s_fits.get("general")
        if not isinstance(raw_fit, bool) and isinstance(raw_fit, (int, float)) and 0.0 <= float(raw_fit) <= 1.0:
            fit = float(raw_fit)
        else:
            fit = float(default_fit)

        if has_sourced_evidence:
            ov_ctx = prov_override.get("context_window", survey.get("context_window"))
            if (
                verified_ctx is None
                and not isinstance(ov_ctx, bool)
                and isinstance(ov_ctx, int)
                and 1024 <= ov_ctx <= 100_000_000
            ):
                verified_ctx = ov_ctx
            if verified_modalities is None:
                s_mods = (
                    prov_override.get("input_modalities")
                    or survey.get("input_modalities")
                    or survey.get("modalities")
                )
                if isinstance(s_mods, list) and all(isinstance(m, str) for m in s_mods):
                    verified_modalities = [m.lower() for m in s_mods]

        lat_candidate = prov_override.get("latency_tier") or survey.get("latency_tier")
        if lat_candidate is None and isinstance(survey.get("latency"), dict):
            lat_candidate = survey["latency"].get("tier")
        latency_tier = _safe_latency_tier(lat_candidate, prior.get("latency_tier", 2))

        po_latency = prov_override.get("latency") if isinstance(prov_override.get("latency"), dict) else {}
        tps_candidate = prov_override.get("tokens_per_sec") or po_latency.get("tokens_per_sec")
        if tps_candidate is None:
            tps_candidate = survey.get("tokens_per_sec")
        if tps_candidate is None and isinstance(survey.get("latency"), dict):
            tps_candidate = survey["latency"].get("tokens_per_sec")
        if (
            not isinstance(tps_candidate, bool)
            and isinstance(tps_candidate, (int, float))
            and float(tps_candidate) > 0.0
        ):
            tokens_per_sec = float(tps_candidate)
            tps_estimated = False
        else:
            tokens_per_sec = float(prior.get("tokens_per_sec") or TIER_DEFAULT_TPS.get(latency_tier, 65.0))
            tps_estimated = True
        source_kind = f"survey:{eff_conf}"
    else:
        fit = float(default_fit)
        latency_tier = _safe_latency_tier(prior.get("latency_tier"), 2)
        tokens_per_sec = float(prior.get("tokens_per_sec") or TIER_DEFAULT_TPS.get(latency_tier, 65.0))
        tps_estimated = True
        source_kind = "family-prior"

    if route.get("agent") == "antigravity" and route.get("variant") == "low":
        tokens_per_sec = round(tokens_per_sec * 1.15, 1)
    elif route.get("agent") == "antigravity" and route.get("variant") == "high":
        tokens_per_sec = round(tokens_per_sec * 0.85, 1)

    billing_mode = classify_route_billing(route)
    base_cost_idx = float(prior.get("cost_index", 0.20))
    cost_obj: dict[str, Any] = {}
    if has_valid_survey and survey and not survey.get("cost_stale"):
        if isinstance(survey.get("cost"), dict):
            cost_obj.update(survey["cost"])
        if isinstance(prov_override.get("cost"), dict):
            cost_obj.update(prov_override["cost"])

    cost_estimated = True
    if billing_mode == "free":
        cost_idx = 0.01
        cost_estimated = False
    elif billing_mode == "subscription":
        cost_idx = base_cost_idx * 0.5
    elif billing_mode == "api_metered":
        in_mtok = cost_obj.get("input_per_mtok")
        out_mtok = cost_obj.get("output_per_mtok")
        if (
            not isinstance(in_mtok, bool)
            and isinstance(in_mtok, (int, float))
            and not isinstance(out_mtok, bool)
            and isinstance(out_mtok, (int, float))
        ):
            cost_idx = min(1.0, max(0.01, (float(in_mtok) + 3.0 * float(out_mtok)) / 240.0))
            cost_estimated = False
        else:
            cost_idx = base_cost_idx
    else:
        cost_idx = base_cost_idx

    estimated_ctx = verified_ctx or prior.get("context_window", 128000)
    if verified_modalities is not None:
        estimated_mods = [str(m).lower() for m in verified_modalities]
    else:
        estimated_mods = [str(m).lower() for m in (prior.get("input_modalities") or ["text"])]
        # Heuristic multimodal inference for known vision-capable model tags only when verified modalities are absent
        if "image" not in estimated_mods and any(
            tag in c_key for tag in ("vl", "vision", "omni", "gemini", "claude", "gpt-6", "kimi-k2", "glm-5", "qwen3.5", "qwen3.6")
        ):
            estimated_mods = sorted(set(estimated_mods) | {"image"})

    fit = round(min(1.0, max(0.0, fit + effort_adj + native_bonus)), 4)
    tps_norm = min(1.0, max(0.05, tokens_per_sec / 160.0))
    tier_norm = (4 - int(latency_tier)) / 3.0
    speed_score = round(0.65 * tps_norm + 0.35 * tier_norm, 4)

    return {
        "task_fit": fit,
        "context_window": estimated_ctx,
        "verified_context_window": verified_ctx,
        "input_modalities": estimated_mods,
        "verified_modalities": [m.lower() for m in verified_modalities] if verified_modalities is not None else None,
        "supports_vision": "image" in (verified_modalities if verified_modalities is not None else estimated_mods),
        "verified_vision": bool(verified_modalities is not None and "image" in verified_modalities),
        "billing_mode": billing_mode,
        "cost_index": round(cost_idx, 4),
        "cost_estimated": cost_estimated,
        "latency_tier": int(latency_tier),
        "tokens_per_sec": round(tokens_per_sec, 1),
        "tps_estimated": tps_estimated,
        "speed_score": speed_score,
        "trait_source": source_kind,
    }


def infer_model_vendor(route: dict[str, Any]) -> str:
    return infer_vendor_from_common(route["canonical_key"], route.get("provider"))


def get_pins_path(run_id: str | None = None) -> Path:
    if run_id:
        validate_safe_identifier(run_id, "run_id")
        runs_root = get_cache_dir() / "runs"
        runs_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        d = ensure_child_path(runs_root, run_id)
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        return d / "pins.json"
    return get_cache_dir() / "pins.json"


def load_session_pins(run_id: str | None = None) -> dict[str, Any]:
    """Load session pins. When `run_id` is supplied, read strictly from that run's pins file without leaking global pins."""
    if run_id:
        return read_json_file(get_pins_path(run_id)) or {}
    return read_json_file(get_pins_path(None)) or {}


def save_session_pins(pins: dict[str, Any], run_id: str | None = None) -> None:
    path = get_pins_path(run_id)

    def _update(_cur: dict[str, Any] | None) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, "updated_at": now_iso(), **pins}

    locked_update_json(path, _update)


def evaluate_route_quota(
    route: dict[str, Any],
    buckets: dict[str, dict[str, Any]],
    agent_stale: bool,
) -> dict[str, Any]:
    """Evaluate all applicable quota buckets for a route (e.g. session + weekly + model-specific)."""
    app_buckets = route.get("applicable_buckets") or infer_applicable_quota_buckets(
        route["agent"], route["provider"], route["canonical_key"], route["model_id"]
    )
    exhausted_buckets: list[str] = []
    max_scarcity = 0.0
    max_imminence = 0.0
    max_credit_pressure = 0.0
    min_headroom: float | None = None
    limiting_bucket_id = route["quota_bucket"]
    limiting_bucket: dict[str, Any] = buckets.get(limiting_bucket_id) or {}

    for b_id in app_buckets:
        b = buckets.get(b_id) or {}
        if not b:
            continue
        is_obs_limited = b.get("status") == "observed_limited"
        b_stale = False if is_obs_limited else agent_stale

        if b.get("exhausted") and not b_stale:
            exhausted_buckets.append(b_id)

        sc = float(b.get("scarcity", 0.25))
        if sc >= max_scarcity:
            max_scarcity = sc
            limiting_bucket_id = b_id
            limiting_bucket = b

        max_imminence = max(max_imminence, float(b.get("reset_imminence", 0.0)))
        max_credit_pressure = max(max_credit_pressure, float(b.get("credit_expiry_pressure", 0.0)))
        hr = b.get("effective_headroom")
        if hr is not None:
            hr_val = float(hr)
            min_headroom = hr_val if min_headroom is None else min(min_headroom, hr_val)

    if not limiting_bucket:
        max_scarcity = 0.25

    return {
        "bucket_id": limiting_bucket_id,
        "primary_bucket_id": route["quota_bucket"],
        "applicable_buckets": app_buckets,
        "exhausted_buckets": exhausted_buckets,
        "exhausted": len(exhausted_buckets) > 0,
        "status": "exhausted" if exhausted_buckets else limiting_bucket.get("status", "unknown"),
        "used_percent": limiting_bucket.get("used_percent"),
        "hours_until_reset": limiting_bucket.get("hours_until_reset"),
        "scarcity": round(max_scarcity, 4),
        "reset_imminence": round(max_imminence, 4),
        "credit_expiry_pressure": round(max_credit_pressure, 4),
        "effective_headroom": min_headroom,
        "stale": agent_stale,
    }


def check_route_eligibility(
    route: dict[str, Any],
    task_norm: str,
    quota_state: dict[str, Any],
    min_context: int | None = None,
    require_modalities: set[str] | None = None,
    allow_estimated_capabilities: bool = False,
    allow_micro_quota: bool = False,
    min_headroom: float | None = None,
    eff_pin_agent: str | None = None,
    eff_pin_provider: str | None = None,
    eff_pin_model: str | None = None,
    excl_agents: set[str] | None = None,
    excl_vendors: set[str] | None = None,
    quality_floor: float | None = None,
    is_explicit_route: bool = False,
) -> tuple[bool, str | None, dict[str, Any], dict[str, Any]]:
    """Shared hard-eligibility evaluator for both automatic selection and explicit `--route` requests."""
    agent = route["agent"]
    buckets = quota_state.get("buckets") or {}
    agent_q = (quota_state.get("agents") or {}).get(agent) or {}
    agent_stale = bool(agent_q.get("stale", False))

    q_eval = evaluate_route_quota(route, buckets, agent_stale)
    traits = evaluate_route_traits(route, task_norm)
    vendor = infer_model_vendor(route)

    # Reject bare unversioned family prefixes across all execution and selection paths
    if (
        route.get("is_bare_prefix")
        or is_bare_generation_prefix(route.get("official_model_id") or "")
        or is_bare_generation_prefix(route.get("model_id") or "")
    ):
        return (
            False,
            f"bare_generation_prefix:{route.get('official_model_id') or route.get('model_id')} (specify concrete model, e.g. gpt-6.1-sol)",
            q_eval,
            traits,
        )

    if excl_agents and agent in excl_agents:
        return False, f"excluded_agent:{agent}", q_eval, traits
    if excl_vendors and vendor in excl_vendors:
        return False, f"excluded_vendor:{vendor}", q_eval, traits
    if eff_pin_agent and agent != eff_pin_agent:
        return False, f"pinned_agent_mismatch:{agent}!={eff_pin_agent}", q_eval, traits
    if eff_pin_provider and route["provider"] != eff_pin_provider:
        return False, f"pinned_provider_mismatch:{route['provider']}!={eff_pin_provider}", q_eval, traits

    if eff_pin_model:
        if is_bare_generation_prefix(eff_pin_model):
            return (
                False,
                f"bare_generation_prefix:{eff_pin_model} (specify concrete model, e.g. gpt-6.1-sol)",
                q_eval,
                traits,
            )
        matches_pin = eff_pin_model in (
            route["model_id"].lower(),
            route["canonical_key"].lower(),
            (route.get("alias") or "").lower(),
            route["route_id"].lower(),
            (route.get("official_model_id") or "").lower(),
        )
        if not matches_pin:
            return False, f"pinned_model_mismatch:{route['route_id']}", q_eval, traits

    if q_eval["exhausted"]:
        return False, f"quota_exhausted:{','.join(q_eval['exhausted_buckets'])}", q_eval, traits

    # Absolute headroom protection: prevent assigning heavy coding tasks to micro-quota or near-empty accounts
    eff_min_hr = min_headroom if min_headroom is not None else 0.15
    if (
        not allow_micro_quota
        and task_norm in HEAVY_TASKS
        and q_eval.get("effective_headroom") is not None
        and q_eval["effective_headroom"] < eff_min_hr
    ):
        return (
            False,
            f"insufficient_absolute_quota:headroom={q_eval['effective_headroom']:.3f}<{eff_min_hr} "
            f"(account plan tier or remaining capacity too small for heavy task '{task_norm}'; pass --allow-micro-quota to force)",
            q_eval,
            traits,
        )

    if min_context:
        ctx_to_check = (
            traits["context_window"]
            if allow_estimated_capabilities
            else traits["verified_context_window"]
        )
        if ctx_to_check is None:
            return False, f"unverified_context_window:need>={min_context}", q_eval, traits
        if ctx_to_check < min_context:
            return False, f"context_window:{ctx_to_check}<{min_context}", q_eval, traits

    if require_modalities:
        mods_to_check = (
            traits["input_modalities"]
            if allow_estimated_capabilities
            else traits["verified_modalities"]
        )
        if mods_to_check is None:
            return False, f"unverified_modalities:need={','.join(sorted(require_modalities))}", q_eval, traits
        if not require_modalities.issubset(set(mods_to_check)):
            missing = ",".join(sorted(require_modalities - set(mods_to_check)))
            return False, f"missing_modality:{missing}", q_eval, traits

    if quality_floor is not None and not eff_pin_model and not is_explicit_route:
        if traits["task_fit"] < quality_floor:
            return False, f"below_quality_floor:{traits['task_fit']}<{quality_floor}", q_eval, traits

    return True, None, q_eval, traits


def select_routes(
    task: str = "general",
    min_context: int | None = None,
    require_modalities: list[str] | None = None,
    require_vision: bool = False,
    speed: str = "normal",
    allow_estimated_capabilities: bool = False,
    allow_micro_quota: bool = False,
    min_headroom: float | None = None,
    pin_agent: str | None = None,
    pin_provider: str | None = None,
    pin_model: str | None = None,
    exclude_agents: list[str] | None = None,
    exclude_vendors: list[str] | None = None,
    quality_floor: float | None = None,
    refresh_quota: bool = False,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Run the 5-step route selection algorithm and return top route + fallback chain."""
    task_norm = task.strip().lower().replace("-", "_")
    floor = quality_floor if quality_floor is not None else TASK_QUALITY_FLOORS.get(task_norm, 0.60)
    speed_norm = (speed or "normal").strip().lower()
    if speed_norm not in SPEED_WEIGHTS:
        speed_norm = "normal"
    speed_weight = SPEED_WEIGHTS[speed_norm]

    pins = load_session_pins(run_id=run_id)
    eff_pin_agent = canonical_agent_name(pin_agent) if pin_agent else pins.get("pin_agent")
    eff_pin_provider = (pin_provider or pins.get("pin_provider") or "").strip().lower() or None
    eff_pin_model = (pin_model or pins.get("pin_model") or "").strip().lower() or None

    excl_agents = {canonical_agent_name(a) for a in (exclude_agents or [])}
    excl_vendors = {v.strip().lower() for v in (exclude_vendors or [])}
    req_mods = {m.strip().lower() for m in (require_modalities or []) if m.strip()}
    if require_vision:
        req_mods.add("image")

    all_models = collect_all_models(refresh=False)
    quota_state = collect_quota(refresh=refresh_quota)

    candidates: list[dict[str, Any]] = []
    filtered_out: list[dict[str, str]] = []

    for agent, a_data in all_models.items():
        if not a_data.get("usable"):
            continue
        if eff_pin_agent and agent != eff_pin_agent:
            continue
        if agent in excl_agents:
            continue

        for route in a_data.get("routes") or []:
            r_id = route["route_id"]
            if route.get("hidden") and eff_pin_model not in (
                route["model_id"].lower(),
                route["canonical_key"].lower(),
                r_id.lower(),
                (route.get("official_model_id") or "").lower(),
            ):
                continue

            ok, reason, q_eval, traits = check_route_eligibility(
                route=route,
                task_norm=task_norm,
                quota_state=quota_state,
                min_context=min_context,
                require_modalities=req_mods,
                allow_estimated_capabilities=allow_estimated_capabilities,
                allow_micro_quota=allow_micro_quota,
                min_headroom=min_headroom,
                eff_pin_agent=eff_pin_agent,
                eff_pin_provider=eff_pin_provider,
                eff_pin_model=eff_pin_model,
                excl_agents=excl_agents,
                excl_vendors=excl_vendors,
                quality_floor=floor,
                is_explicit_route=False,
            )
            if not ok:
                filtered_out.append({"route_id": r_id, "reason": reason or "filtered"})
                continue

            vendor = infer_model_vendor(route)
            scarcity = q_eval["scarcity"]
            reset_imminence = q_eval["reset_imminence"]
            credit_pressure = q_eval["credit_expiry_pressure"]
            cost_idx = traits["cost_index"]
            speed_score = traits["speed_score"]

            headroom = q_eval.get("effective_headroom")
            headroom_sort = float(headroom) if headroom is not None else 0.75
            headroom_bonus = round(0.04 * min(1.0, max(0.0, (headroom_sort - 0.20) / 0.80)), 4)

            score = (
                traits["task_fit"]
                - WEIGHT_SCARCITY * scarcity
                + WEIGHT_CREDIT_EXPIRY * credit_pressure
                + WEIGHT_RESET_IMMINENCE * reset_imminence
                - WEIGHT_COST * cost_idx
                + speed_weight * speed_score
                + headroom_bonus
            )
            score = round(score, 4)

            used_str = (
                f"{q_eval['used_percent']}% used"
                if q_eval.get("used_percent") is not None
                else f"quota {q_eval.get('status', 'unknown')}"
            )
            reset_str = (
                f", resets in {q_eval['hours_until_reset']}h"
                if q_eval.get("hours_until_reset") is not None
                else ""
            )
            speed_str = (
                f", ~{traits['tokens_per_sec']:.0f} tok/s (tier {traits['latency_tier']})"
                if speed_norm in ("fast", "urgent")
                else ""
            )
            vision_str = ", vision=ok" if "image" in req_mods else ""
            off_agent = route.get("official_agent") or official_agent_name(agent)
            off_model = route.get("official_model_id") or resolve_official_model_id(
                route.get("resolved_model_id") or route["model_id"], all_models=all_models
            )
            rationale = (
                f"{r_id} [{off_agent} / {off_model}] selected — {task_norm} fit {traits['task_fit']:.2f} "
                f"({traits['trait_source']}){vision_str}{speed_str}, {q_eval['bucket_id']} {used_str}{reset_str}, score {score:.3f}"
            )

            candidates.append(
                {
                    **route,
                    "official_agent": off_agent,
                    "official_model_id": off_model,
                    "vendor": vendor,
                    "task": task_norm,
                    "speed_mode": speed_norm,
                    "score": score,
                    "task_fit": traits["task_fit"],
                    "trait_source": traits["trait_source"],
                    "context_window": traits["context_window"],
                    "verified_context_window": traits["verified_context_window"],
                    "input_modalities": traits["input_modalities"],
                    "verified_modalities": traits["verified_modalities"],
                    "supports_vision": traits["supports_vision"],
                    "verified_vision": traits["verified_vision"],
                    "billing_mode": traits["billing_mode"],
                    "cost_index": cost_idx,
                    "cost_estimated": traits["cost_estimated"],
                    "latency_tier": traits["latency_tier"],
                    "tokens_per_sec": traits["tokens_per_sec"],
                    "speed_score": speed_score,
                    "quota": q_eval,
                    "_headroom_sort": headroom_sort,
                    "rationale": rationale,
                }
            )

    candidates.sort(
        key=lambda c: (
            round(c["score"], 2),
            c["_headroom_sort"],
            -c["latency_tier"],
            c["tokens_per_sec"],
            -c["cost_index"],
            c["score"],
        ),
        reverse=True,
    )

    for c in candidates:
        c.pop("_headroom_sort", None)

    if not candidates:
        return {
            "status": "no_route_available",
            "task": task_norm,
            "quality_floor": floor,
            "selected": None,
            "fallbacks": [],
            "filtered_count": len(filtered_out),
            "filtered_sample": filtered_out[:15],
        }

    selected = candidates[0]

    fallbacks: list[dict[str, Any]] = []
    primary_buckets = set(selected["quota"].get("applicable_buckets") or [selected["quota_bucket"]])
    used_buckets = set(primary_buckets)
    used_canonicals = {selected["canonical_key"]}

    # Phase 1: Distinct quota bucket AND distinct canonical model
    for cand in candidates[1:]:
        if len(fallbacks) >= 3:
            break
        cand_buckets = set(cand["quota"].get("applicable_buckets") or [cand["quota_bucket"]])
        if not (cand_buckets & used_buckets) and cand["canonical_key"] not in used_canonicals:
            fb_copy = dict(cand)
            fb_copy["same_bucket_as_primary"] = False
            fallbacks.append(fb_copy)
            used_buckets.update(cand_buckets)
            used_canonicals.add(cand["canonical_key"])

    # Phase 2: Same canonical model on a DIFFERENT quota bucket (for cross-provider failover of a specific model)
    if len(fallbacks) < 3:
        for cand in candidates[1:]:
            if len(fallbacks) >= 3:
                break
            cand_buckets = set(cand["quota"].get("applicable_buckets") or [cand["quota_bucket"]])
            if not any(f["route_id"] == cand["route_id"] for f in fallbacks) and not (cand_buckets & used_buckets):
                fb_copy = dict(cand)
                fb_copy["same_bucket_as_primary"] = False
                fallbacks.append(fb_copy)
                used_buckets.update(cand_buckets)

    # Phase 3: Alternative model in an already-used bucket (used only for non-quota model_unavailable/format failures)
    if len(fallbacks) < 3:
        for cand in candidates[1:]:
            if len(fallbacks) >= 3:
                break
            if (
                not any(f["route_id"] == cand["route_id"] for f in fallbacks)
                and cand["canonical_key"] not in used_canonicals
            ):
                fb_copy = dict(cand)
                fb_copy["same_bucket_as_primary"] = True
                fallbacks.append(fb_copy)
                used_canonicals.add(cand["canonical_key"])

    return {
        "status": "ok",
        "selected_at": now_iso(),
        "task": task_norm,
        "quality_floor": floor,
        "rationale": selected["rationale"],
        "selected": selected,
        "fallbacks": fallbacks,
        "candidate_count": len(candidates),
    }


def main() -> int:
    enforce_utf8()
    parser = argparse.ArgumentParser(description="Select optimal model route and fallback chain.")
    parser.add_argument("--task", default="general", help="Task category (e.g. code_review, debugging, prose).")
    parser.add_argument("--run-id", help="Optional run ID for run-scoped pins.")
    parser.add_argument("--min-context", type=int, help="Minimum required verified context window in tokens.")
    parser.add_argument("--require-modality", action="append", help="Required input modality (e.g. text, image).")
    parser.add_argument(
        "--require-vision",
        action="store_true",
        help="Require image/vision input support (for UI mockups, screenshots, or Playwright inspection).",
    )
    parser.add_argument(
        "--speed",
        default="normal",
        choices=["normal", "fast", "urgent"],
        help="Token generation speed preference ('normal', 'fast', or 'urgent').",
    )
    parser.add_argument(
        "--prefer-fast",
        action="store_true",
        help="Shorthand for --speed fast.",
    )
    parser.add_argument(
        "--allow-estimated-capabilities",
        action="store_true",
        help="Allow family-prior estimates to satisfy --min-context and --require-modality/--require-vision filters.",
    )
    parser.add_argument(
        "--allow-micro-quota",
        action="store_true",
        help="Allow selection of low-tier/free accounts with micro-quota headroom for heavy tasks.",
    )
    parser.add_argument(
        "--min-headroom",
        type=float,
        help="Minimum required effective quota headroom (default: 0.15 for heavy coding tasks).",
    )
    parser.add_argument("--pin-agent", help="Restrict selection to a specific agent.")
    parser.add_argument("--pin-provider", help="Restrict selection to a specific provider.")
    parser.add_argument("--pin-model", help="Restrict selection to a specific model ID or canonical key.")
    parser.add_argument("--exclude-agent", action="append", help="Exclude agent(s) from selection.")
    parser.add_argument("--exclude-vendor", action="append", help="Exclude model vendor(s) (e.g. openai, anthropic, google).")
    parser.add_argument("--quality-floor", type=float, help="Override minimum task_fit quality floor.")
    parser.add_argument("--save-pin", action="store_true", help="Persist --pin-* flags as session/run pins.")
    parser.add_argument("--clear-pins", action="store_true", help="Clear persisted session/run pins.")
    parser.add_argument("--refresh-quota", action="store_true", help="Force fresh quota query before selecting.")
    parser.add_argument("--dry-run", action="store_true", help="Show selection rationale and fallback chain without executing.")
    parser.add_argument("--human", action="store_true", help="Print concise human-readable summary.")

    args = parser.parse_args()

    if args.clear_pins:
        save_session_pins({"pin_agent": None, "pin_provider": None, "pin_model": None}, run_id=args.run_id)

    if args.save_pin:
        save_session_pins(
            {
                "pin_agent": canonical_agent_name(args.pin_agent) if args.pin_agent else None,
                "pin_provider": args.pin_provider,
                "pin_model": args.pin_model,
            },
            run_id=args.run_id,
        )

    eff_speed = "fast" if (args.prefer_fast and args.speed == "normal") else args.speed
    res = select_routes(
        task=args.task,
        min_context=args.min_context,
        require_modalities=args.require_modality,
        require_vision=args.require_vision,
        speed=eff_speed,
        allow_estimated_capabilities=args.allow_estimated_capabilities,
        allow_micro_quota=args.allow_micro_quota,
        min_headroom=args.min_headroom,
        pin_agent=args.pin_agent,
        pin_provider=args.pin_provider,
        pin_model=args.pin_model,
        exclude_agents=args.exclude_agent,
        exclude_vendors=args.exclude_vendor,
        quality_floor=args.quality_floor,
        refresh_quota=args.refresh_quota,
        run_id=args.run_id,
    )

    if args.human:
        if res["status"] != "ok" or not res.get("selected"):
            print(f"No route available for task={args.task} (floor={res.get('quality_floor')}).")
            return 1
        print(res["rationale"])
        for idx, fb in enumerate(res.get("fallbacks") or [], 1):
            same_b = " [same-bucket]" if fb.get("same_bucket_as_primary") else ""
            print(
                f"  fallback {idx}: {fb['route_id']} (score={fb['score']:.3f}, "
                f"fit={fb['task_fit']:.2f}, bucket={fb['quota']['bucket_id']}{same_b})"
            )
        return 0

    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0 if res["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
