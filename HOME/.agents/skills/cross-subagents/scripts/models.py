#!/usr/bin/env python3
"""Collect, normalize, cache, and index model routes and capability surveys.

Usage:
  python3 models.py [--agent <name>] [--refresh] [--human]
  python3 models.py survey-index [--refresh]
  python3 models.py survey-missing [--all]
  python3 models.py survey-get <canonical_key>
  python3 models.py survey-put <canonical_key> --file <json_file>
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import sys
import tempfile
from typing import Any
from urllib.parse import urlparse

from common import (
    AGENT_BINARIES,
    ROUTE_NORMALIZER_VERSION,
    SCHEMA_VERSION,
    atomic_write_json,
    canonical_agent_name,
    codex_app_server_rpc,
    compute_agent_config_fingerprint,
    enforce_utf8,
    ensure_child_path,
    get_cache_dir,
    infer_model_family,
    normalize_model_route,
    normalize_official_model_id,
    now_iso,
    official_agent_name,
    parse_iso_age_seconds,
    read_json_file,
    validate_safe_identifier,
)
from probe import probe_all, run_cmd, strip_ansi

MODELS_TTL_SECONDS = 86400  # 24 hours
SURVEY_COST_TTL_SECONDS = 7 * 86400  # 7 days
SURVEY_BENCHMARK_TTL_SECONDS = 30 * 86400  # 30 days

VALID_CONFIDENCE_LEVELS = ("high", "medium", "low", "insufficient")


def collect_codex_models(
    binary: str,
    agent_info: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Query Codex models via `codex app-server` stdio JSON-RPC (`model/list`) with pagination."""
    meta: dict[str, Any] = {
        "method": "app-server:model/list",
        "plan_type": agent_info.get("plan_type"),
    }
    raw_models: list[dict[str, Any]] = []
    rpc_error: str | None = None

    try:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        pagination_complete = False
        req_id = 2
        for _page in range(25):
            params: dict[str, Any] = {"cursor": cursor} if cursor else {}
            rpc_out = codex_app_server_rpc(
                binary,
                [{"jsonrpc": "2.0", "id": req_id, "method": "model/list", "params": params}],
                timeout=15.0,
            )
            for note in rpc_out.get("notifications") or []:
                if note.get("method") == "account/updated":
                    p = note.get("params") or {}
                    if p.get("planType"):
                        meta["plan_type"] = p.get("planType")
                    if p.get("authMode"):
                        meta["auth_mode"] = p.get("authMode")

            msg = (rpc_out.get("responses") or {}).get(req_id)
            if not msg:
                rpc_error = f"model/list pagination interrupted on page {_page + 1} (no response for cursor={cursor})"
                raw_models = []
                break
            if "error" in msg:
                rpc_error = f"model/list RPC error on page {_page + 1}: {msg['error']}"
                raw_models = []
                break
            res = msg.get("result") or {}
            page_data = res.get("data") or []
            if isinstance(page_data, list):
                raw_models.extend(page_data)
            next_cursor = res.get("nextCursor") if "nextCursor" in res else res.get("next_cursor")
            if not next_cursor:
                pagination_complete = True
                break
            next_cursor_str = str(next_cursor)
            if next_cursor_str == cursor or next_cursor_str in seen_cursors:
                rpc_error = f"model/list pagination cycle detected (repeated cursor='{next_cursor_str}')"
                raw_models = []
                break
            seen_cursors.add(next_cursor_str)
            cursor = next_cursor_str
            req_id += 1
        if not pagination_complete and raw_models:
            rpc_error = rpc_error or "model/list pagination did not reach a terminal page"
            raw_models = []
    except Exception as exc:
        rpc_error = str(exc)
        raw_models = []

    if not raw_models:
        raw_models, fallback_meta = repair_codex_models(binary, prior_error=rpc_error)
        meta.update(fallback_meta)

    auth_plan = meta.get("plan_type") or meta.get("auth_mode") or agent_info.get("auth_mode") or "chatgpt"
    routes: list[dict[str, Any]] = []
    for item in raw_models:
        model_id = item.get("id") or item.get("slug")
        if not model_id:
            continue
        efforts_raw = item.get("supportedReasoningEfforts") or []
        efforts = [
            e.get("reasoningEffort")
            for e in efforts_raw
            if isinstance(e, dict) and e.get("reasoningEffort")
        ]
        route = normalize_model_route(
            agent="codex",
            provider="openai",
            raw_model_id=model_id,
            display_name=item.get("displayName") or model_id,
            efforts=efforts,
            efforts_verified=bool(efforts),
            hidden=bool(item.get("hidden", False)),
            quota_bucket="codex:codex",
            auth_plan=auth_plan,
        )
        route["description"] = item.get("description")
        in_mod = item.get("inputModalities") or item.get("input_modalities")
        out_mod = item.get("outputModalities") or item.get("output_modalities")
        if isinstance(in_mod, list):
            route["input_modalities"] = [str(m).lower() for m in in_mod]
        if isinstance(out_mod, list):
            route["output_modalities"] = [str(m).lower() for m in out_mod]
        if isinstance(item.get("contextWindow"), int):
            route["context_window"] = item["contextWindow"]
        routes.append(route)

    return routes, meta


def repair_codex_models(
    binary: str,
    prior_error: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Self-healing fallback for Codex model discovery via schema rediscovery or `codex debug models`."""
    meta: dict[str, Any] = {
        "repaired": True,
        "prior_error": prior_error,
        "maintenance_notice": (
            "Codex `model/list` RPC required fallback repair. "
            "Consider updating `~/.agents/skills/cross-subagents/`."
        ),
    }

    # 1. Inspect generated JSON schemas to discover renamed model-list RPC methods
    discovered_methods: list[str] = []
    with tempfile.TemporaryDirectory(prefix="codex-schema-") as tmpdir:
        s_proc = run_cmd([binary, "app-server", "generate-json-schema", "-o", tmpdir], timeout=15)
        if s_proc.returncode == 0:
            schema_files = list(Path(tmpdir).rglob("*.json"))
            meta["schema_files_found"] = len(schema_files)
            for sf in schema_files:
                try:
                    text = sf.read_text(encoding="utf-8", errors="replace")
                    for m in re.findall(r'"(model/[A-Za-z0-9_/-]+|models/[A-Za-z0-9_/-]+)"', text):
                        if m not in discovered_methods and m != "model/list":
                            discovered_methods.append(m)
                except Exception:
                    continue

    for alt_method in discovered_methods[:3]:
        try:
            rpc_out = codex_app_server_rpc(
                binary,
                [{"jsonrpc": "2.0", "id": 2, "method": alt_method, "params": {}}],
                timeout=10.0,
            )
            msg = (rpc_out.get("responses") or {}).get(2) or {}
            res = msg.get("result") or {}
            data = res.get("data") or res.get("models")
            if isinstance(data, list) and data:
                meta["method"] = f"app-server:{alt_method}"
                return data, meta
        except Exception:
            continue

    # 2. Try `codex debug models` CLI fallback
    proc = run_cmd([binary, "debug", "models"], timeout=15)
    if proc.returncode == 0 and proc.stdout.strip():
        try:
            payload = json.loads(proc.stdout)
            items = payload.get("models") if isinstance(payload, dict) else payload
            if isinstance(items, list) and items:
                meta["method"] = "codex-debug-models"
                converted = [
                    {
                        "id": m.get("slug") or m.get("id"),
                        "displayName": m.get("display_name") or m.get("slug") or m.get("id"),
                        "hidden": m.get("visibility") not in (None, "list"),
                    }
                    for m in items
                    if isinstance(m, dict) and (m.get("slug") or m.get("id"))
                ]
                return converted, meta
        except Exception:
            pass

    raise RuntimeError(
        f"Codex model discovery failed on both `model/list` ({prior_error}) and fallback paths."
    )


def map_claude_provider(api_provider: str | None) -> str:
    raw = (api_provider or "firstParty").strip().lower()
    if raw in ("firstparty", "anthropic", "claude.ai"):
        return "anthropic"
    if "bedrock" in raw:
        return "bedrock"
    if "vertex" in raw:
        return "vertex"
    return raw


def collect_claude_models(
    binary: str,
    agent_info: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Query Claude Code models via NDJSON control_request initialize (zero tokens)."""
    init_req = json.dumps(
        {
            "type": "control_request",
            "request_id": "init",
            "request": {"subtype": "initialize"},
        }
    )
    proc = run_cmd(
        [
            binary,
            "--print",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--no-session-persistence",
        ],
        timeout=20,
        stdin_text=init_req + "\n",
    )

    resp_payload: dict[str, Any] | None = None
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") == "control_response":
            resp_payload = (obj.get("response") or {}).get("response") or obj.get("response")
            break

    if not resp_payload or not isinstance(resp_payload.get("models"), list):
        raise RuntimeError(
            f"Claude Code initialize control_request did not return models (exit={proc.returncode})."
        )

    account = resp_payload.get("account") or {}
    plan_type = account.get("subscriptionType") or agent_info.get("plan_type") or "subscription"
    api_provider = account.get("apiProvider") or agent_info.get("api_provider") or "firstParty"
    provider_slug = map_claude_provider(api_provider)
    meta = {
        "method": "control_request:initialize",
        "plan_type": plan_type,
        "api_provider": api_provider,
        "provider_slug": provider_slug,
        "claude_code_version": resp_payload.get("claude_code_version"),
    }

    by_resolved: dict[str, dict[str, Any]] = {}
    for m in resp_payload["models"]:
        if not isinstance(m, dict):
            continue
        val = m.get("value")
        resolved = m.get("resolvedModel") or val
        if not resolved:
            continue
        alias = val if (val and val != resolved) else None
        existing = by_resolved.get(resolved)
        if existing is None:
            efforts = m.get("supportedEffortLevels") or []
            route = normalize_model_route(
                agent="claude-code",
                provider=provider_slug,
                raw_model_id=resolved,
                display_name=m.get("displayName") or resolved,
                efforts=efforts,
                efforts_verified=bool(m.get("supportsEffort", False)),
                alias=alias if alias != "default" else None,
                auth_plan=plan_type,
            )
            route["aliases"] = [alias] if alias else []
            route["description"] = m.get("description")
            route["supports_fast_mode"] = bool(m.get("supportsFastMode", False))
            route["supports_adaptive_thinking"] = bool(m.get("supportsAdaptiveThinking", False))
            by_resolved[resolved] = route
        else:
            if alias and alias not in existing["aliases"]:
                existing["aliases"].append(alias)
            if alias and alias != "default" and not existing.get("alias"):
                existing["alias"] = alias
                existing["display_name"] = m.get("displayName") or existing["display_name"]

    return list(by_resolved.values()), meta


def collect_opencode_models(
    binary: str,
    agent_info: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Query OpenCode models via `opencode models` and filter to authenticated providers."""
    if not agent_info.get("target_usable", agent_info.get("usable")):
        raise RuntimeError("OpenCode is not usable or has no authenticated providers.")

    proc = run_cmd([binary, "models"], timeout=20)
    if proc.returncode != 0:
        raise RuntimeError(f"`opencode models` failed with exit code {proc.returncode}: {proc.stderr}")

    provider_methods: dict[str, str] = {}
    for p in agent_info.get("authenticated_providers") or []:
        if isinstance(p, dict) and p.get("provider"):
            raw_method = str(p.get("auth_method") or p.get("auth_type") or "configured").strip().lower()
            provider_methods[p["provider"]] = raw_method

    routes: list[dict[str, Any]] = []
    for raw_line in strip_ansi(proc.stdout or "").splitlines():
        line = raw_line.strip()
        if not line or "/" not in line:
            continue
        provider, model_part = line.split("/", 1)
        provider_slug = provider.strip().lower()
        is_free_tagged = "free" in model_part.lower()
        if provider_slug not in provider_methods and not (provider_slug == "opencode" and is_free_tagged):
            continue

        auth_method = provider_methods.get(provider_slug, "free" if is_free_tagged else "unknown")
        if provider_slug == "opencode":
            auth_plan = "free" if is_free_tagged else ("api_key" if "key" in auth_method else "unknown")
        elif provider_slug in ("opencode-go", "github-copilot") or "oauth" in auth_method:
            auth_plan = "subscription"
        elif any(k in auth_method for k in ("key", "api", "token")):
            auth_plan = "api_key"
        else:
            auth_plan = "unknown"

        route = normalize_model_route(
            agent="opencode",
            provider=provider_slug,
            raw_model_id=model_part.strip(),
            display_name=line,
            efforts=[],
            efforts_verified=False,
            auth_plan=auth_plan,
        )
        route["cli_model_arg"] = (
            f"{provider_slug}/{route['model_id']}#{route['variant']}"
            if route.get("variant")
            else f"{provider_slug}/{route['model_id']}"
        )
        routes.append(route)

    meta = {
        "method": "opencode-models",
        "authenticated_providers": sorted(provider_methods.keys()),
    }
    return routes, meta


def collect_antigravity_models(
    binary: str,
    agent_info: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Query Antigravity models via `agy models` (TSV id<TAB>label)."""
    proc = run_cmd([binary, "models"], timeout=25)
    if proc.returncode != 0:
        raise RuntimeError(f"`agy models` failed with exit code {proc.returncode}: {proc.stderr}")

    auth_plan = agent_info.get("plan_type") or "subscription"
    routes: list[dict[str, Any]] = []
    for raw_line in strip_ansi(proc.stdout or "").splitlines():
        line = raw_line.strip()
        if not line or "\t" not in line:
            continue
        model_id, label = line.split("\t", 1)
        route = normalize_model_route(
            agent="antigravity",
            provider="antigravity",
            raw_model_id=model_id.strip(),
            display_name=label.strip(),
            efforts_verified=True,
            auth_plan=auth_plan,
        )
        routes.append(route)

    if not routes:
        raise RuntimeError("`agy models` returned no model entries.")

    return routes, {"method": "agy-models"}


def collect_agent_models(
    agent: str,
    probe_data: dict[str, Any] | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """Collect or load cached models for a single agent, validating version and config fingerprint."""
    canon = canonical_agent_name(agent)
    models_dir = get_cache_dir() / "models"
    models_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    cache_file = models_dir / f"{canon}.json"

    if probe_data is None:
        probe_data = probe_all(agents=[canon], refresh=False)
    agent_info = (probe_data.get("agents") or {}).get(canon) or {}
    current_ver = agent_info.get("version")
    current_fp = agent_info.get("config_fingerprint") or compute_agent_config_fingerprint(
        canon, agent_info.get("binary"), current_ver
    )

    existing = read_json_file(cache_file)
    if (
        not refresh
        and existing
        and not existing.get("stale")
        and existing.get("agent_version") == current_ver
        and existing.get("normalizer_version") == ROUTE_NORMALIZER_VERSION
        and existing.get("config_fingerprint") == current_fp
    ):
        age = parse_iso_age_seconds(existing.get("collected_at"))
        if age is not None and age < MODELS_TTL_SECONDS:
            return existing

    if not agent_info.get("usable") or not agent_info.get("binary"):
        reason = "; ".join(agent_info.get("warnings") or [f"{canon} is not usable."])
        if existing and existing.get("routes") and agent_info.get("stale"):
            existing["stale"] = True
            existing["refresh_error"] = reason
            atomic_write_json(cache_file, existing)
            return existing
        empty_payload = {
            "schema_version": SCHEMA_VERSION,
            "agent": canon,
            "agent_version": current_ver,
            "config_fingerprint": current_fp,
            "collected_at": now_iso(),
            "stale": False,
            "usable": False,
            "error": reason,
            "routes": [],
        }
        atomic_write_json(cache_file, empty_payload)
        return empty_payload

    binary = agent_info["binary"]
    try:
        if canon == "codex":
            routes, meta = collect_codex_models(binary, agent_info)
        elif canon == "claude-code":
            routes, meta = collect_claude_models(binary, agent_info)
        elif canon == "opencode":
            routes, meta = collect_opencode_models(binary, agent_info)
        elif canon == "antigravity":
            routes, meta = collect_antigravity_models(binary, agent_info)
        else:
            raise ValueError(f"Unsupported agent: {canon}")

        if meta.get("maintenance_notice"):
            print(f"WARNING: {meta['maintenance_notice']}", file=sys.stderr)

        payload = {
            "schema_version": SCHEMA_VERSION,
            "normalizer_version": ROUTE_NORMALIZER_VERSION,
            "agent": canon,
            "agent_version": current_ver,
            "config_fingerprint": current_fp,
            "collected_at": now_iso(),
            "stale": False,
            "usable": True,
            "meta": meta,
            "routes": routes,
        }
        atomic_write_json(cache_file, payload)
        return payload
    except Exception as exc:
        if (
            existing
            and existing.get("routes")
            and existing.get("config_fingerprint") == current_fp
        ):
            existing["stale"] = True
            existing["refresh_error"] = str(exc)
            existing["last_error_at"] = now_iso()
            atomic_write_json(cache_file, existing)
            return existing
        err_payload = {
            "schema_version": SCHEMA_VERSION,
            "agent": canon,
            "agent_version": current_ver,
            "config_fingerprint": current_fp,
            "collected_at": now_iso(),
            "stale": False,
            "usable": False,
            "error": str(exc),
            "routes": [],
        }
        atomic_write_json(cache_file, err_payload)
        return err_payload


def load_all_cached_models() -> dict[str, Any]:
    """Load all cached `models/<agent>.json` files on disk so partial refreshes do not drop agents."""
    models_dir = get_cache_dir() / "models"
    out: dict[str, Any] = {}
    if not models_dir.is_dir():
        return out
    for agent in AGENT_BINARIES:
        fpath = models_dir / f"{agent}.json"
        doc = read_json_file(fpath)
        if doc:
            out[agent] = doc
    return out


def build_catalog_alias_map(all_models: dict[str, Any] | None = None) -> dict[str, str]:
    """Build a mapping from discovered aliases and route IDs to official model identifiers."""
    if all_models is None:
        all_models = load_all_cached_models()
    alias_map: dict[str, str] = {}
    for _agent, data in (all_models or {}).items():
        for route in data.get("routes") or []:
            off_id = route.get("official_model_id") or normalize_official_model_id(
                route.get("resolved_model_id") or route.get("model_id") or ""
            )[0]
            if not off_id:
                continue
            for key in (
                route.get("model_id"),
                route.get("resolved_model_id"),
                route.get("canonical_key"),
                route.get("alias"),
                *(route.get("aliases") or []),
            ):
                if isinstance(key, str) and key.strip():
                    alias_map[key.strip().lower()] = off_id
    return alias_map


def resolve_official_model_id(
    raw_model_id: str,
    all_models: dict[str, Any] | None = None,
) -> str:
    """Resolve any model identifier, alias, or provider-prefixed spec to its official model identifier."""
    alias_map = build_catalog_alias_map(all_models)
    return normalize_official_model_id(raw_model_id, alias_lookup=alias_map)[0]


def collect_all_models(agents: list[str] | None = None, refresh: bool = False) -> dict[str, Any]:
    target_agents = [canonical_agent_name(a) for a in agents] if agents else list(AGENT_BINARIES.keys())
    probe_data = probe_all(agents=target_agents, refresh=refresh)
    results: dict[str, Any] = {}
    for agent in target_agents:
        results[agent] = collect_agent_models(agent, probe_data=probe_data, refresh=refresh)
    # Merge with full cached catalog before rebuilding the global survey index
    full_catalog = load_all_cached_models()
    full_catalog.update(results)
    rebuild_survey_index(full_catalog)
    return results


def get_survey_dir() -> Path:
    d = get_cache_dir() / "model-survey"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


def evaluate_survey_freshness(s_data: dict[str, Any] | None) -> dict[str, Any]:
    """Evaluate 30-day benchmark freshness and 7-day cost freshness on a survey record."""
    if not isinstance(s_data, dict):
        return {
            "has_survey": False,
            "survey_stale": True,
            "cost_stale": True,
            "confidence": None,
            "effective_confidence": "insufficient",
        }
    bench_age = parse_iso_age_seconds(s_data.get("surveyed_at"))
    cost_age = parse_iso_age_seconds(s_data.get("cost_surveyed_at") or s_data.get("surveyed_at"))
    raw_conf = s_data.get("confidence", "medium")
    if raw_conf not in VALID_CONFIDENCE_LEVELS:
        raw_conf = "medium"

    bench_stale = bench_age is None or bench_age > SURVEY_BENCHMARK_TTL_SECONDS
    cost_stale = cost_age is None or cost_age > SURVEY_COST_TTL_SECONDS

    eff_conf = raw_conf
    if bench_stale and raw_conf in VALID_CONFIDENCE_LEVELS:
        idx = min(len(VALID_CONFIDENCE_LEVELS) - 1, VALID_CONFIDENCE_LEVELS.index(raw_conf) + 1)
        eff_conf = VALID_CONFIDENCE_LEVELS[idx]

    return {
        "has_survey": True,
        "survey_stale": bench_stale,
        "cost_stale": cost_stale,
        "confidence": raw_conf,
        "effective_confidence": eff_conf,
        "surveyed_at": s_data.get("surveyed_at"),
        "cost_surveyed_at": s_data.get("cost_surveyed_at") or s_data.get("surveyed_at"),
    }


def rebuild_survey_index(all_models: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build `$SKILL_CACHE_DIR/model-survey/index.json` mapping routes to canonical survey files."""
    if all_models is None:
        all_models = load_all_cached_models()
        if not all_models:
            all_models = collect_all_models(refresh=False)

    survey_dir = get_survey_dir()
    route_to_canonical: dict[str, str] = {}
    canonical_to_info: dict[str, dict[str, Any]] = {}

    for _agent, data in all_models.items():
        for route in data.get("routes") or []:
            if route.get("hidden"):
                continue
            r_id = route.get("route_id", "")
            raw_c_key = route.get("canonical_key", "")
            raw_family = route.get("family", "")
            try:
                c_key = validate_safe_identifier(raw_c_key, "canonical_key")
                family = validate_safe_identifier(raw_family, "family")
            except ValueError as ex:
                print(f"WARNING: Skipping route '{r_id}' with invalid key '{raw_c_key}': {ex}", file=sys.stderr)
                continue
            rel_path = f"{family}/{c_key}.json"
            full_path = ensure_child_path(survey_dir, survey_dir / rel_path)
            route_to_canonical[r_id] = c_key

            entry = canonical_to_info.setdefault(
                c_key,
                {
                    "canonical_key": c_key,
                    "family": family,
                    "survey_path": rel_path,
                    "routes": [],
                    "has_survey": False,
                    "survey_stale": False,
                    "cost_stale": False,
                    "confidence": None,
                    "effective_confidence": None,
                },
            )
            if r_id not in entry["routes"]:
                entry["routes"].append(r_id)

            if full_path.is_file():
                s_data = read_json_file(full_path)
                fresh = evaluate_survey_freshness(s_data)
                entry.update(fresh)

    index_payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": now_iso(),
        "route_to_canonical": route_to_canonical,
        "canonical_models": canonical_to_info,
    }
    atomic_write_json(survey_dir / "index.json", index_payload)
    return index_payload


def extract_version_tuple(canonical_key: str) -> tuple[int, ...]:
    """Extract numeric version components from a canonical model key (e.g. 'claude-opus-5-5' -> (5, 5))."""
    m = re.search(r"(\d+)(?:[.-](\d+))?", canonical_key)
    if not m:
        return (0,)
    major = int(m.group(1))
    minor = int(m.group(2)) if m.group(2) is not None else 0
    return (major, minor)


def select_latest_generation_candidates(canonical_models: dict[str, dict[str, Any]]) -> set[str]:
    """Dynamically determine latest-generation canonical models from the discovered catalog."""
    non_chat_tags = ("preview", "computer-use", "embed", "tts", "whisper", "moderation", "audio", "realtime")
    by_lineage: dict[str, list[tuple[tuple[int, ...], str]]] = {}

    for c_key, info in canonical_models.items():
        k = c_key.lower()
        if any(t in k for t in non_chat_tags):
            continue
        # Group by base lineage (e.g., claude-opus, claude-fable, gpt-astra, gpt-sol, gemini-argon, deepseek-flash)
        lineage = re.sub(r"-v?\d+(?:[.-]\d+)*", "", k)
        ver = extract_version_tuple(k)
        by_lineage.setdefault(lineage, []).append((ver, c_key))

    selected: set[str] = set()
    for _lineage, items in by_lineage.items():
        if not items:
            continue
        max_ver = max(v for v, _ in items)
        for ver, c_key in items:
            if ver == max_ver:
                selected.add(c_key)
    return selected


def get_survey(canonical_key: str) -> dict[str, Any] | None:
    c_key = validate_safe_identifier(canonical_key.strip().lower(), "canonical_key")
    family = validate_safe_identifier(infer_model_family(c_key), "family")
    survey_dir = get_survey_dir()
    path = ensure_child_path(survey_dir, survey_dir / family / f"{c_key}.json")
    data = read_json_file(path)
    if not data:
        return None
    fresh = evaluate_survey_freshness(data)
    data["stale"] = fresh["survey_stale"]
    data["cost_stale"] = fresh["cost_stale"]
    data["effective_confidence"] = fresh["effective_confidence"]
    return data


def _is_valid_http_url(url: Any) -> bool:
    if not isinstance(url, str):
        return False
    cleaned = url.strip()
    try:
        parsed = urlparse(cleaned)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc) and "." in parsed.netloc
    except Exception:
        return False


def _validate_capability_and_price_fields(obj: dict[str, Any], prefix: str = "") -> None:
    ctx = obj.get("context_window")
    if ctx is not None:
        if isinstance(ctx, bool) or not isinstance(ctx, int) or not (1024 <= ctx <= 100_000_000):
            raise ValueError(
                f"Invalid {prefix}context_window '{ctx}'. Must be an integer in [1024, 100000000] or null."
            )

    for mod_field in ("input_modalities", "output_modalities", "modalities"):
        mods = obj.get(mod_field)
        if mods is not None:
            if not isinstance(mods, list) or not all(isinstance(m, str) and m.strip() for m in mods):
                raise ValueError(f"'{prefix}{mod_field}' must be a list of non-empty strings or null.")

    lat_tier = obj.get("latency_tier")
    if lat_tier is not None:
        if isinstance(lat_tier, bool) or not isinstance(lat_tier, int) or lat_tier not in (1, 2, 3):
            raise ValueError(f"Invalid {prefix}latency_tier '{lat_tier}'. Must be 1, 2, 3, or null.")

    tps = obj.get("tokens_per_sec")
    if tps is not None:
        if (
            isinstance(tps, bool)
            or not isinstance(tps, (int, float))
            or not math.isfinite(float(tps))
            or float(tps) <= 0.0
        ):
            raise ValueError(
                f"Invalid {prefix}tokens_per_sec '{tps}'. Must be a finite positive number or null."
            )

    lat_obj = obj.get("latency")
    if lat_obj is not None:
        if not isinstance(lat_obj, dict):
            raise ValueError(f"'{prefix}latency' must be a dictionary (e.g. {{'tier': 2}}) or null.")
        t_val = lat_obj.get("tier")
        if t_val is not None and (isinstance(t_val, bool) or not isinstance(t_val, int) or t_val not in (1, 2, 3)):
            raise ValueError(f"Invalid {prefix}latency.tier '{t_val}'. Must be 1, 2, 3, or null.")
        lat_tps = lat_obj.get("tokens_per_sec")
        if lat_tps is not None and (
            isinstance(lat_tps, bool)
            or not isinstance(lat_tps, (int, float))
            or not math.isfinite(float(lat_tps))
            or float(lat_tps) <= 0.0
        ):
            raise ValueError(
                f"Invalid {prefix}latency.tokens_per_sec '{lat_tps}'. Must be a finite positive number or null."
            )

    cost_obj = obj.get("cost")
    if cost_obj is not None:
        if not isinstance(cost_obj, dict):
            raise ValueError(f"'{prefix}cost' must be a dictionary or null.")
        for price_key in ("input_per_mtok", "output_per_mtok"):
            p_val = cost_obj.get(price_key)
            if p_val is not None:
                if (
                    isinstance(p_val, bool)
                    or not isinstance(p_val, (int, float))
                    or not math.isfinite(float(p_val))
                    or float(p_val) < 0.0
                ):
                    raise ValueError(
                        f"Invalid {prefix}cost.{price_key} '{p_val}'. Must be a finite non-negative number or null."
                    )

    task_fit = obj.get("task_fit")
    if task_fit is not None:
        if not isinstance(task_fit, dict):
            raise ValueError(
                f"'{prefix}task_fit' must be a dictionary mapping task names to floats in [0, 1] or null."
            )
        for k, v in task_fit.items():
            if v is not None:
                if (
                    isinstance(v, bool)
                    or not isinstance(v, (int, float))
                    or not math.isfinite(float(v))
                    or not (0.0 <= float(v) <= 1.0)
                ):
                    raise ValueError(
                        f"Invalid {prefix}task_fit score for '{k}': {v}. Must be a finite float in [0.0, 1.0] or null."
                    )


def validate_survey_payload(payload: dict[str, Any]) -> None:
    """Validate survey payload fields before writing to cache."""
    if not isinstance(payload, dict):
        raise ValueError("Survey payload must be a JSON object.")
    conf = payload.get("confidence", "medium")
    if conf not in VALID_CONFIDENCE_LEVELS:
        raise ValueError(f"Invalid confidence '{conf}'. Must be one of {VALID_CONFIDENCE_LEVELS}.")

    sources = payload.get("sources")
    valid_url_count = 0
    if sources is not None:
        if not isinstance(sources, list):
            raise ValueError("'sources' must be a list of source objects.")
        for s in sources:
            if not isinstance(s, dict) or not isinstance(s.get("url"), str):
                raise ValueError("Each entry in 'sources' must be an object with a string 'url'.")
            url = s["url"].strip()
            if not _is_valid_http_url(url):
                raise ValueError(f"Source URL '{url}' must start with http:// or https:// and include a valid host.")
            valid_url_count += 1

    if conf in ("high", "medium") and valid_url_count < 2:
        raise ValueError(
            f"Surveys with confidence='{conf}' require at least 2 valid http(s):// sources (found {valid_url_count})."
        )

    _validate_capability_and_price_fields(payload, prefix="")

    prov_overrides = payload.get("provider_overrides")
    if prov_overrides is not None:
        if not isinstance(prov_overrides, dict) or not all(
            isinstance(k, str) and isinstance(v, dict) for k, v in prov_overrides.items()
        ):
            raise ValueError("'provider_overrides' must be a dictionary mapping provider IDs to override objects.")
        for prov_key, prov_obj in prov_overrides.items():
            _validate_capability_and_price_fields(prov_obj, prefix=f"provider_overrides.{prov_key}.")

    for ts_field in ("surveyed_at", "cost_surveyed_at"):
        if payload.get(ts_field) is not None and parse_iso_age_seconds(payload.get(ts_field)) is None:
            raise ValueError(f"Invalid ISO 8601 timestamp in '{ts_field}': {payload.get(ts_field)}")


def put_survey(canonical_key: str, payload: dict[str, Any]) -> Path:
    c_key = validate_safe_identifier(canonical_key.strip().lower(), "canonical_key")
    family = validate_safe_identifier(infer_model_family(c_key), "family")
    validate_survey_payload(payload)

    survey_dir = get_survey_dir()
    path = ensure_child_path(survey_dir, survey_dir / family / f"{c_key}.json")
    now = now_iso()

    # Preserve all caller-supplied extension fields (benchmarks, stability, language_support,
    # provider_overrides, latency, etc.) while enforcing required core metadata.
    record: dict[str, Any] = dict(payload)
    record.update(
        {
            "schema_version": SCHEMA_VERSION,
            "canonical_key": c_key,
            "family": family,
            "surveyed_at": payload.get("surveyed_at") or now,
            "cost_surveyed_at": payload.get("cost_surveyed_at") or payload.get("surveyed_at") or now,
            "stale": False,
            "confidence": payload.get("confidence", "medium"),
            "sources": payload.get("sources", []),
            "strengths": payload.get("strengths", []),
            "weaknesses": payload.get("weaknesses", []),
            "task_fit": payload.get("task_fit", {}),
            "context_window": payload.get("context_window"),
            "cost": payload.get("cost", {}),
            "knowledge_cutoff": payload.get("knowledge_cutoff"),
            "tool_use_reliability": payload.get("tool_use_reliability"),
            "instruction_adherence": payload.get("instruction_adherence"),
            "effort_differences": payload.get("effort_differences"),
            "notes": payload.get("notes", ""),
            "unknowns": payload.get("unknowns", []),
        }
    )
    atomic_write_json(path, record)
    rebuild_survey_index()
    return path


def format_human_models(all_models: dict[str, Any]) -> str:
    lines: list[str] = []
    for agent, data in all_models.items():
        routes = data.get("routes") or []
        stale_str = " [STALE]" if data.get("stale") else ""
        lines.append(
            f"=== {agent} (v{data.get('agent_version') or '?'}) : {len(routes)} routes{stale_str} ==="
        )
        if data.get("error"):
            lines.append(f"  error: {data['error']}")
        for r in routes[:20]:
            eff = f" efforts={','.join(r['efforts'])}" if r.get("efforts") else ""
            alias = f" alias={r['alias']}" if r.get("alias") else ""
            lines.append(
                f"  - {r['route_id']} (canonical={r['canonical_key']}, bucket={r['quota_bucket']}{alias}{eff})"
            )
        if len(routes) > 20:
            lines.append(f"  ... ({len(routes) - 20} more routes)")
    return "\n".join(lines)


def main() -> int:
    enforce_utf8()
    parser = argparse.ArgumentParser(description="Collect and query available model routes and surveys.")
    sub = parser.add_subparsers(dest="subcommand")

    p_col = sub.add_parser("collect", help="Collect model lists from installed agents.")
    p_col.add_argument("--agent", action="append", help="Collect only for specified agent(s).")
    p_col.add_argument("--refresh", action="store_true", help="Ignore TTL and force refresh.")
    p_col.add_argument("--human", action="store_true", help="Print human-readable summary.")

    p_idx = sub.add_parser("survey-index", help="Rebuild and print the model survey index.")
    p_idx.add_argument("--refresh", action="store_true", help="Refresh model lists before indexing.")

    p_mis = sub.add_parser("survey-missing", help="List canonical models missing a fresh survey or cost check.")
    p_mis.add_argument(
        "--all",
        action="store_true",
        help="Include older/legacy models instead of filtering to latest-generation models.",
    )

    p_get = sub.add_parser("survey-get", help="Read the cached survey for a canonical model key.")
    p_get.add_argument("canonical_key", help="Canonical model key (e.g. claude-opus-5-5, gpt-6-astra).")

    p_put = sub.add_parser("survey-put", help="Write or update a model survey JSON entry.")
    p_put.add_argument("canonical_key", help="Canonical model key.")
    p_put.add_argument("--file", required=True, help="Path to JSON file (or '-' for stdin).")

    parser.add_argument("--agent", action="append", help="Collect only for specified agent(s).")
    parser.add_argument("--refresh", action="store_true", help="Ignore TTL and force refresh.")
    parser.add_argument("--human", action="store_true", help="Print human-readable summary.")

    args = parser.parse_args()
    cmd = args.subcommand or "collect"

    if cmd == "collect":
        res = collect_all_models(agents=args.agent, refresh=args.refresh)
        if args.human:
            print(format_human_models(res))
        else:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    if cmd == "survey-index":
        if args.refresh:
            collect_all_models(refresh=True)
        idx = rebuild_survey_index()
        print(json.dumps(idx, indent=2, ensure_ascii=False))
        return 0

    if cmd == "survey-missing":
        idx = rebuild_survey_index()
        canon_map = idx.get("canonical_models") or {}
        latest_set = select_latest_generation_candidates(canon_map)
        missing: list[dict[str, Any]] = []
        for c_key, info in sorted(canon_map.items()):
            if not args.all and c_key not in latest_set:
                continue
            reasons: list[str] = []
            if not info.get("has_survey"):
                reasons.append("missing")
            else:
                if info.get("survey_stale"):
                    reasons.append("benchmark_stale")
                if info.get("cost_stale"):
                    reasons.append("cost_stale")
            if reasons:
                missing.append({**info, "refresh_reasons": reasons})
        print(json.dumps({"missing_count": len(missing), "models": missing}, indent=2, ensure_ascii=False))
        return 0

    if cmd == "survey-get":
        data = get_survey(args.canonical_key)
        if not data:
            print(json.dumps({"error": f"No survey found for {args.canonical_key}"}), file=sys.stderr)
            return 1
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0

    if cmd == "survey-put":
        if args.file == "-":
            raw = sys.stdin.read()
        else:
            raw = Path(args.file).read_text(encoding="utf-8")
        payload = json.loads(raw)
        saved_path = put_survey(args.canonical_key, payload)
        print(json.dumps({"status": "ok", "canonical_key": args.canonical_key, "path": str(saved_path)}))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
