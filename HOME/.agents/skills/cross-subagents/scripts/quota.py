#!/usr/bin/env python3
"""Query and normalize account usage limits, quota buckets, and reset credits.

Usage:
  python3 quota.py [status] [--agent <name>] [--refresh] [--human]
  python3 quota.py record-observation --bucket <bucket_id> --error <text> [--reset-seconds <sec>]
  python3 quota.py notify-reset-credit --run-id <run_id> [--credit-id <id>] [--user-objected]
  python3 quota.py consume-reset-credit --agent codex --run-id <run_id> [--credit-id <id>] [--user-approved]
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
import shutil
import sys
import time
from typing import Any
from zoneinfo import ZoneInfo

from common import (
    AGENT_BINARIES,
    SCHEMA_VERSION,
    canonical_agent_name,
    codex_app_server_rpc,
    compute_agent_config_fingerprint,
    enforce_utf8,
    file_lock,
    get_cache_dir,
    locked_update_json,
    now_iso,
    normalize_official_model_id,
    parse_iso_age_seconds,
    read_json_file,
    validate_safe_identifier,
)
from probe import probe_all, run_cmd, strip_ansi

QUOTA_TTL_SECONDS = 900  # 15 minutes
RESET_CREDIT_WAIT_SECONDS = 1800  # 30 minutes notification window
RESET_CREDIT_EXPIRY_WINDOW_SECONDS = 72 * 3600  # 72 hours for automatic reset credit redemption
RESET_CREDIT_PRESSURE_WINDOW_SECONDS = 7 * 86400  # 7 days for soft route-selection pressure

# Estimated relative weekly capacity multiplier across subscription tiers.
# Used for soft ranking and minimum headroom protection for heavy subagent tasks.
PLAN_CAPACITY_FACTORS: dict[str, float] = {
    "enterprise": 3.0,
    "team": 2.5,
    "promax": 2.5,
    "chatgpt_pro_500": 2.5,
    "pro": 1.5,
    "claude max": 1.2,
    "max": 1.2,
    "plus": 0.6,
    "subscription": 1.0,
    "opencode-go": 0.9,
    "api-or-oauth": 1.0,
    "starter": 0.25,
    "hobby": 0.25,
    "free": 0.1,
    "trial": 0.1,
}

# Only fixed-offset non-DST timezones are included in the fallback table so DST zones
# never compute a 1-hour-off timestamp when system tzdata is missing.
FALLBACK_TZ_OFFSETS: dict[str, timezone] = {
    "UTC": timezone.utc,
    "ETC/UTC": timezone.utc,
    "GMT": timezone.utc,
    "ASIA/SEOUL": timezone(timedelta(hours=9)),
    "KST": timezone(timedelta(hours=9)),
    "ASIA/TOKYO": timezone(timedelta(hours=9)),
    "JST": timezone(timedelta(hours=9)),
}

CLAUDE_USAGE_LINE_RE = re.compile(
    r"^Current\s+(?P<scope>session|week(?:\s*\((?P<group>[^)]+)\))?):\s*"
    r"(?P<used>\d+(?:\.\d+)?)%\s*used\s*[·•-]\s*resets\s+(?P<reset_full>.+?)\s*$",
    re.IGNORECASE,
)


def estimate_plan_capacity(plan_type: str | None) -> dict[str, Any]:
    key = (plan_type or "").strip().lower()
    factor = PLAN_CAPACITY_FACTORS.get(key, 1.0)
    return {
        "plan_type": plan_type or "unknown",
        "capacity_factor": factor,
        "estimated": True,
    }


def compute_window_metrics(
    used_percent: float | None,
    window_mins: int | None,
    resets_at_epoch: int | None,
    capacity_factor: float = 1.0,
    available_credits: list[dict[str, Any]] | None = None,
    reached_limit: bool | None = None,
    used_is_fractional: bool = False,
) -> dict[str, Any]:
    """Compute scarcity, reset imminence, and credit expiry pressure for a quota window."""
    now_ts = int(time.time())
    if used_percent is None and not reached_limit:
        return {
            "status": "unknown",
            "exhausted": False,
            "tight": False,
            "near_exhaustion_rounded": False,
            "elapsed_percent": None,
            "hours_until_reset": None,
            "effective_headroom": None,
            "scarcity": 0.25,
            "reset_imminence": 0.0,
            "credit_expiry_pressure": 0.0,
        }

    eff_used = 100.0 if (used_percent is None and reached_limit) else float(used_percent or 0.0)
    # Explicit exhaustion (reached_limit=True) or fractional usedPercent >= 99.95% is confirmed exhausted;
    # integer 100% without explicit exhaustion (reached_limit=False) is treated as rounded near-exhaustion.
    if reached_limit is True:
        exhausted = True
        near_rounded = False
    elif used_is_fractional and eff_used >= 99.95:
        exhausted = True
        near_rounded = False
    elif reached_limit is False:
        exhausted = eff_used > 100.0
        near_rounded = eff_used >= 100.0 and not exhausted
    else:
        exhausted = eff_used >= 100.0
        near_rounded = False

    remaining_pct = 0.0 if exhausted else (0.5 if near_rounded else max(0.0, 100.0 - eff_used))
    effective_headroom = (remaining_pct / 100.0) * capacity_factor

    elapsed_pct: float | None = None
    hours_until_reset: float | None = None
    if resets_at_epoch is not None and resets_at_epoch > now_ts:
        secs_left = max(0, resets_at_epoch - now_ts)
        hours_until_reset = secs_left / 3600.0
        if window_mins and window_mins > 0:
            total_secs = window_mins * 60
            clamped_left = min(total_secs, secs_left)
            elapsed_pct = ((total_secs - clamped_left) * 100.0) / total_secs

    tight = exhausted or near_rounded or (eff_used >= 80.0)
    if not tight and elapsed_pct is not None and eff_used >= 50.0 and eff_used > elapsed_pct:
        tight = True

    is_micro = capacity_factor <= 0.25
    raw_scarcity = (eff_used / 100.0) / max(0.1, capacity_factor)
    if is_micro:
        # Micro-tier accounts (free, trial, hobby) have high absolute scarcity even at low percentage usage
        raw_scarcity = max(raw_scarcity, 0.40 + 0.50 * (eff_used / 100.0))
    if elapsed_pct is not None and eff_used > elapsed_pct:
        pace_penalty = ((eff_used - elapsed_pct) / 100.0) * 0.35
        raw_scarcity += pace_penalty
    if near_rounded:
        raw_scarcity = max(raw_scarcity, 0.99)
    scarcity = round(min(1.0, max(0.0, raw_scarcity)), 4)

    reset_imminence = 0.0
    if (
        not exhausted
        and not near_rounded
        and hours_until_reset is not None
        and (window_mins is None or window_mins >= 1440)
        and hours_until_reset <= 72.0
    ):
        time_urgency = max(0.0, (72.0 - hours_until_reset) / 72.0)
        headroom_ratio = remaining_pct / 100.0
        reset_imminence = round(min(1.0, time_urgency * headroom_ratio), 4)

    credit_expiry_pressure = 0.0
    for cred in available_credits or []:
        if cred.get("status") != "available":
            continue
        if cred.get("free_grant") is not True:
            continue
        exp = cred.get("expires_at")
        if isinstance(exp, (int, float)) and exp > now_ts:
            days_left = (exp - now_ts) / 86400.0
            if days_left <= 7.0:
                pressure = max(0.2, (7.0 - days_left) / 7.0)
                credit_expiry_pressure = max(credit_expiry_pressure, round(min(1.0, pressure), 4))

    return {
        "status": "exhausted" if exhausted else ("tight" if tight else "ample"),
        "exhausted": exhausted,
        "tight": tight,
        "near_exhaustion_rounded": near_rounded,
        "elapsed_percent": round(elapsed_pct, 1) if elapsed_pct is not None else None,
        "hours_until_reset": round(hours_until_reset, 2) if hours_until_reset is not None else None,
        "effective_headroom": round(effective_headroom, 4),
        "is_micro_capacity": is_micro,
        "insufficient_headroom": effective_headroom < 0.15,
        "capacity_factor": capacity_factor,
        "scarcity": scarcity,
        "reset_imminence": reset_imminence,
        "credit_expiry_pressure": credit_expiry_pressure,
    }


def _classify_free_grant(credit_obj: dict[str, Any]) -> bool | None:
    """Determine whether a Codex reset credit is a confirmed free promotional/subscription grant."""
    if credit_obj.get("paid") is True or credit_obj.get("isPaid") is True:
        return False
    if credit_obj.get("price") or credit_obj.get("cost"):
        return False
    if credit_obj.get("paid") is False or credit_obj.get("isPaid") is False:
        return True
    grant_type = str(credit_obj.get("grantType") or credit_obj.get("source") or "").strip().lower()
    if grant_type in ("free", "promo", "promotional", "included", "subscription", "grant"):
        return True
    if "paid" not in credit_obj and "isPaid" not in credit_obj and credit_obj.get("resetType"):
        return True
    return None


def query_codex_quota(binary: str) -> dict[str, Any]:
    """Query Codex rate limits and reset credits via `codex app-server` JSON-RPC."""
    rpc_out = codex_app_server_rpc(
        binary,
        [
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "account/rateLimits/read",
                "params": {},
            }
        ],
        timeout=15.0,
    )
    responses = rpc_out["responses"]
    notifications = rpc_out["notifications"]
    msg = responses.get(3)
    if not msg:
        raise RuntimeError("Codex `account/rateLimits/read` returned no response.")
    if "error" in msg:
        raise RuntimeError(f"account/rateLimits/read error: {msg['error']}")

    rpc_result = msg.get("result")
    if not rpc_result or not isinstance(rpc_result, dict):
        raise RuntimeError("Codex `account/rateLimits/read` returned empty result.")

    plan_from_notify: str | None = None
    for notif in notifications:
        if notif.get("method") == "account/updated":
            p = notif.get("params") or {}
            if p.get("planType"):
                plan_from_notify = p.get("planType")

    rl_main = rpc_result.get("rateLimits") or {}
    plan_type = rl_main.get("planType") or plan_from_notify or "chatgpt"
    plan_norm = estimate_plan_capacity(plan_type)
    cap_factor = plan_norm["capacity_factor"]

    rc_container = rpc_result.get("rateLimitResetCredits")
    raw_reset_credits = (rc_container or {}).get("credits") if isinstance(rc_container, dict) else None
    avail_count = (rc_container or {}).get("availableCount") if isinstance(rc_container, dict) else None

    reset_credits: list[dict[str, Any]] = []
    for c in raw_reset_credits or []:
        if not isinstance(c, dict):
            continue
        reset_credits.append(
            {
                "id": c.get("id"),
                "status": c.get("status", "unknown"),
                "granted_at": c.get("grantedAt"),
                "expires_at": c.get("expiresAt"),
                "reset_type": c.get("resetType"),
                "free_grant": _classify_free_grant(c),
            }
        )

    ordinary_allowed = rpc_result.get("ordinaryUsageAllowed")  # May be bool or None

    by_id = rpc_result.get("rateLimitsByLimitId") or {}
    if not by_id and rl_main:
        by_id = {rl_main.get("limitId") or "codex": rl_main}

    buckets: dict[str, dict[str, Any]] = {}
    for limit_id, entry in by_id.items():
        if not isinstance(entry, dict):
            continue
        bucket_key = f"codex:{limit_id}"
        primary = entry.get("primary") or {}
        secondary = entry.get("secondary") or {}

        reached_type = entry.get("rateLimitReachedType")
        explicit_reached = (reached_type is not None) or (ordinary_allowed is False)

        p_used = primary.get("usedPercent")
        p_is_frac = isinstance(p_used, float) or (isinstance(p_used, str) and "." in p_used)
        p_mins = primary.get("windowDurationMins")
        p_reset = primary.get("resetsAt")
        p_reached = bool(explicit_reached and (not secondary or str(reached_type or "").lower() != "secondary"))

        p_metrics = compute_window_metrics(
            used_percent=float(p_used) if p_used is not None else None,
            window_mins=int(p_mins) if p_mins is not None else None,
            resets_at_epoch=int(p_reset) if p_reset is not None else None,
            capacity_factor=cap_factor,
            available_credits=reset_credits,
            reached_limit=p_reached,
            used_is_fractional=p_is_frac,
        )

        s_metrics: dict[str, Any] | None = None
        if isinstance(secondary, dict) and secondary:
            s_used = secondary.get("usedPercent")
            s_is_frac = isinstance(s_used, float) or (isinstance(s_used, str) and "." in s_used)
            s_mins = secondary.get("windowDurationMins")
            s_reset = secondary.get("resetsAt")
            s_reached = bool(explicit_reached and (str(reached_type or "").lower() == "secondary" or not p_metrics["exhausted"]))
            s_metrics = compute_window_metrics(
                used_percent=float(s_used) if s_used is not None else None,
                window_mins=int(s_mins) if s_mins is not None else None,
                resets_at_epoch=int(s_reset) if s_reset is not None else None,
                capacity_factor=cap_factor,
                available_credits=reset_credits,
                reached_limit=s_reached,
                used_is_fractional=s_is_frac,
            )

        combined_metrics = dict(p_metrics)
        effective_used = p_used
        effective_mins = p_mins
        effective_reset = p_reset

        if s_metrics:
            if s_metrics["exhausted"] or (
                not p_metrics["exhausted"] and s_metrics["scarcity"] > p_metrics["scarcity"]
            ):
                combined_metrics["status"] = s_metrics["status"]
                combined_metrics["exhausted"] = p_metrics["exhausted"] or s_metrics["exhausted"]
                combined_metrics["tight"] = p_metrics["tight"] or s_metrics["tight"]
                combined_metrics["near_exhaustion_rounded"] = (
                    p_metrics["near_exhaustion_rounded"] or s_metrics["near_exhaustion_rounded"]
                )
                combined_metrics["scarcity"] = max(p_metrics["scarcity"], s_metrics["scarcity"])
                if (
                    p_metrics["effective_headroom"] is not None
                    and s_metrics["effective_headroom"] is not None
                ):
                    combined_metrics["effective_headroom"] = min(
                        p_metrics["effective_headroom"], s_metrics["effective_headroom"]
                    )
                elif s_metrics["effective_headroom"] is not None:
                    combined_metrics["effective_headroom"] = s_metrics["effective_headroom"]
                effective_used = secondary.get("usedPercent", p_used)
                effective_mins = secondary.get("windowDurationMins", p_mins)
                effective_reset = secondary.get("resetsAt", p_reset)
                combined_metrics["hours_until_reset"] = s_metrics["hours_until_reset"]
                combined_metrics["elapsed_percent"] = s_metrics["elapsed_percent"]
            else:
                combined_metrics["exhausted"] = p_metrics["exhausted"] or s_metrics["exhausted"]
                combined_metrics["tight"] = p_metrics["tight"] or s_metrics["tight"]
                combined_metrics["scarcity"] = max(p_metrics["scarcity"], s_metrics["scarcity"])
            combined_metrics["reset_imminence"] = max(
                p_metrics["reset_imminence"], s_metrics["reset_imminence"]
            )
            combined_metrics["credit_expiry_pressure"] = max(
                p_metrics["credit_expiry_pressure"], s_metrics["credit_expiry_pressure"]
            )

        credits_info = entry.get("credits") or {}
        buckets[bucket_key] = {
            "bucket_id": bucket_key,
            "agent": "codex",
            "limit_id": limit_id,
            "used_percent": effective_used,
            "rate_limit_reached_type": reached_type,
            "window_duration_mins": effective_mins,
            "resets_at_epoch": effective_reset,
            "windows": {
                "primary": {
                    "used_percent": p_used,
                    "window_duration_mins": p_mins,
                    "resets_at_epoch": p_reset,
                    **p_metrics,
                },
                "secondary": (
                    {
                        "used_percent": secondary.get("usedPercent"),
                        "window_duration_mins": secondary.get("windowDurationMins"),
                        "resets_at_epoch": secondary.get("resetsAt"),
                        **s_metrics,
                    }
                    if s_metrics
                    else None
                ),
            },
            "monetary_credits": {
                "has_credits": credits_info.get("hasCredits") if "hasCredits" in credits_info else None,
                "balance": credits_info.get("balance"),
            },
            **combined_metrics,
        }

    return {
        "agent": "codex",
        "method": "app-server:account/rateLimits/read",
        "plan": plan_norm,
        "ordinary_usage_allowed": ordinary_allowed,
        "reset_credits": {
            "available_count": avail_count,
            "credits": reset_credits,
        },
        "buckets": buckets,
    }


def resolve_timezone(tz_name: str | None) -> Any:
    """Resolve an IANA timezone name with a fixed-offset non-DST fallback table; return None if unresolvable."""
    if not tz_name:
        return datetime.now().astimezone().tzinfo
    cleaned = tz_name.strip()
    try:
        return ZoneInfo(cleaned)
    except Exception:
        return FALLBACK_TZ_OFFSETS.get(cleaned.upper())


def parse_claude_reset_time(reset_text: str, tz_name: str | None) -> int | None:
    """Parse human reset strings like 'Oct 31, 10pm' or 'Oct 28, 11:50pm' in the given timezone."""
    tz = resolve_timezone(tz_name)
    if tz is None:
        return None

    now = datetime.now(tz)
    cleaned = reset_text.strip()
    cleaned_norm = re.sub(
        r"(\b\d{1,2})(?::(\d{2}))?\s*(am|pm)\b",
        lambda m: f"{int(m.group(1)):02d}:{m.group(2) or '00'}{m.group(3).upper()}",
        cleaned,
        flags=re.IGNORECASE,
    )

    for fmt in ("%Y %b %d, %I:%M%p", "%Y %B %d, %I:%M%p", "%I:%M%p"):
        try:
            if fmt.startswith("%Y"):
                parsed = datetime.strptime(f"{now.year} {cleaned_norm}", fmt)
                candidate = parsed.replace(tzinfo=tz)
                if candidate < now - timedelta(days=30):
                    candidate = candidate.replace(year=now.year + 1)
            else:
                parsed = datetime.strptime(cleaned_norm, fmt)
                candidate = now.replace(
                    hour=parsed.hour,
                    minute=parsed.minute,
                    second=0,
                    microsecond=0,
                )
                if candidate <= now:
                    candidate += timedelta(days=1)
            return int(candidate.timestamp())
        except ValueError:
            continue
    return None


def query_claude_quota(binary: str, plan_type: str | None = None) -> dict[str, Any]:
    """Query Claude Code usage via `claude -p /usage --output-format json --no-session-persistence`."""
    proc = run_cmd(
        [binary, "-p", "/usage", "--output-format", "json", "--no-session-persistence"],
        timeout=20,
    )
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        raise RuntimeError(f"Claude `/usage` command failed (exit={proc.returncode}): {proc.stderr}")

    raw_stdout = strip_ansi(proc.stdout).strip()
    start = raw_stdout.find("{")
    end = raw_stdout.rfind("}")
    if start == -1 or end == -1:
        raise RuntimeError("Claude `/usage` did not return valid JSON.")

    payload = json.loads(raw_stdout[start : end + 1])
    result_text = payload.get("result") or ""
    plan_norm = estimate_plan_capacity(plan_type or "Claude Max")
    cap_factor = plan_norm["capacity_factor"]

    buckets: dict[str, dict[str, Any]] = {}
    for raw_line in result_text.splitlines():
        line = raw_line.strip()
        m = CLAUDE_USAGE_LINE_RE.match(line)
        if not m:
            continue
        scope = m.group("scope").lower()
        group = (m.group("group") or "").strip().lower()
        used_raw_str = m.group("used")
        used_val = float(used_raw_str)
        reset_full = m.group("reset_full").strip()
        tz_match = re.search(r"\(([^)]+)\)\s*$", reset_full)
        if tz_match:
            tz_name = tz_match.group(1).strip()
            reset_text = reset_full[: tz_match.start()].strip()
        else:
            tz_name = None
            reset_text = reset_full

        if scope.startswith("session"):
            bucket_id = "claude-code:session"
            win_mins = 300  # 5-hour session window
        elif not group or group == "all models":
            bucket_id = "claude-code:all-models"
            win_mins = 10080  # 7-day weekly window
        else:
            slug = re.sub(r"[^a-z0-9]+", "-", group).strip("-")
            bucket_id = f"claude-code:{slug}"
            win_mins = 10080

        resets_epoch = parse_claude_reset_time(reset_text, tz_name)
        metrics = compute_window_metrics(
            used_percent=used_val,
            window_mins=win_mins,
            resets_at_epoch=resets_epoch,
            capacity_factor=cap_factor,
            reached_limit=None,
        )
        buckets[bucket_id] = {
            "bucket_id": bucket_id,
            "agent": "claude-code",
            "scope": scope,
            "group": group or ("session" if scope.startswith("session") else "all models"),
            "used_percent": used_val,
            "used_percent_raw": used_raw_str,
            "window_duration_mins": win_mins,
            "resets_at_epoch": resets_epoch,
            "resets_at_raw": f"{reset_text} ({tz_name})" if tz_name else reset_text,
            **metrics,
        }

    if not buckets:
        unknown_metrics = compute_window_metrics(None, 10080, None, cap_factor)
        buckets["claude-code:all-models"] = {
            "bucket_id": "claude-code:all-models",
            "agent": "claude-code",
            "used_percent": None,
            "parse_warning": "Could not parse usage lines from `/usage` output; treating as unknown.",
            "raw_excerpt": result_text[:400],
            **unknown_metrics,
        }

    return {
        "agent": "claude-code",
        "method": "slash-command:/usage",
        "plan": plan_norm,
        "reset_credits": {"available_count": None, "credits": []},
        "buckets": buckets,
    }


def _antigravity_window_scope(title: str) -> str | None:
    """Map CodexBar window labels to shared groups or a canonical model scope."""
    label = re.sub(r"\s+(?:weekly|\d+(?:-hour|h))$", "", title.strip(), flags=re.I)
    if label.lower() in ("gemini", "gemini models"):
        return "antigravity:group:gemini"
    if label.lower() in ("claude/gpt", "claude and gpt"):
        return "antigravity:group:third-party"
    model = normalize_official_model_id(label)[0]
    if model.startswith(("gemini-", "claude-", "gpt-")) and re.search(r"\d", model):
        return f"antigravity:model:{model}"
    return None


def parse_antigravity_quota(records: Any) -> dict[str, Any]:
    """Normalize CodexBar's Antigravity summary without assuming one shared pool."""
    matches = [r for r in records if isinstance(r, dict) and r.get("provider") == "antigravity"] if isinstance(records, list) else []
    if len(matches) != 1 or matches[0].get("error"):
        raise RuntimeError("Antigravity quota query did not return one unambiguous account.")
    record = matches[0]
    usage = record.get("usage")
    if not isinstance(usage, dict):
        raise RuntimeError("Antigravity quota response has no structured usage data.")
    source_age = parse_iso_age_seconds(usage.get("updatedAt"))
    if source_age is None or source_age >= QUOTA_TTL_SECONDS or source_age < -60:
        raise RuntimeError("Antigravity quota source timestamp is missing or stale.")
    labels = record.get("rateWindowLabels")
    labels = labels if isinstance(labels, dict) else {}
    windows = [
        {"title": labels.get(key, ""), "window": usage[key], "id": key}
        for key in ("primary", "secondary", "tertiary")
        if isinstance(usage.get(key), dict)
    ]
    extra = usage.get("extraRateWindows")
    if isinstance(extra, list):
        windows.extend(w for w in extra if isinstance(w, dict))
    buckets: dict[str, dict[str, Any]] = {}
    for item in windows:
        scope = _antigravity_window_scope(str(item.get("title") or ""))
        window = item.get("window")
        if not scope or not isinstance(window, dict) or item.get("usageKnown") is False:
            continue
        raw_used = window.get("usedPercent")
        used = float(raw_used) if isinstance(raw_used, (int, float)) and not isinstance(raw_used, bool) and math.isfinite(raw_used) else None
        reset = window.get("resetsAt")
        try:
            reset_epoch = int(datetime.fromisoformat(reset.replace("Z", "+00:00")).timestamp()) if isinstance(reset, str) else None
        except ValueError:
            reset_epoch = None
        # A passed reset cannot certify current availability or exhaustion.
        if reset_epoch is not None and reset_epoch <= time.time():
            used = None
        metrics = compute_window_metrics(used, window.get("windowMinutes"), reset_epoch)
        normalized = {
            "bucket_id": scope, "agent": "antigravity", "used_percent": used,
            "resets_at_epoch": reset_epoch, "source_updated_at": usage["updatedAt"],
            **metrics,
        }
        bucket = buckets.setdefault(scope, {**normalized, "windows": {}})
        bucket["windows"][str(item.get("id") or item.get("title"))] = normalized
        # Never let an ample group or another window overwrite an exhausted window.
        if (metrics["exhausted"], metrics["scarcity"], reset_epoch or 0) >= (bucket["exhausted"], bucket["scarcity"], bucket.get("resets_at_epoch") or 0):
            bucket.update(normalized)
    if not buckets:
        raise RuntimeError("Antigravity quota response contained no recognized usage windows.")
    return {
        "agent": "antigravity", "method": "codexbar:antigravity",
        "source_updated_at": usage["updatedAt"],
        "plan": estimate_plan_capacity("subscription"),
        "reset_credits": {"available_count": None, "credits": []}, "buckets": buckets,
    }


def query_antigravity_quota() -> dict[str, Any]:
    binary = shutil.which("codexbar")
    if not binary:
        raise RuntimeError("codexbar is unavailable; Antigravity quota is unknown.")
    proc = run_cmd([binary, "usage", "--provider", "antigravity", "--json"], timeout=30)
    if proc.returncode != 0:
        raise RuntimeError("Antigravity quota query failed; retaining previous readings as stale.")
    return parse_antigravity_quota(json.loads(proc.stdout))


def get_observations_file() -> Path:
    d = get_cache_dir() / "quota"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d / "observations.json"


def quota_observation_bucket(route: dict[str, Any], error_text: str) -> str:
    """Only an explicit individual limit narrows Antigravity's shared scope."""
    if route["agent"] == "antigravity":
        if re.search(r"\bindividual\s+(?:model\s+)?quota\b", error_text, re.IGNORECASE):
            key = normalize_official_model_id(route["model_id"])[0]
            return f"antigravity:model:{key}"
        return "antigravity:default"
    return route["quota_bucket"]


def _structured_retry_after_seconds(error_text: str) -> int | None:
    """Read provider retry delays from the JSON error records retained by adapters."""
    delays: list[int] = []
    for line in error_text.splitlines():
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            continue
        while isinstance(record, dict):
            milliseconds = record.get("retryAfterMs")
            if (
                isinstance(milliseconds, (int, float))
                and not isinstance(milliseconds, bool)
                and math.isfinite(milliseconds)
                and milliseconds >= 0
            ):
                delays.append(max(1, math.ceil(milliseconds / 1000)))
            record = record.get("error")
    return max(delays) if delays else None


def record_quota_observation(bucket_id: str, error_text: str, reset_seconds: int | None = None) -> dict[str, Any]:
    """Record an observed rate-limit / 429 event under an exclusive file lock."""
    obs_file = get_observations_file()
    now_ts = int(time.time())

    provider_resets_at: int | None = None
    if reset_seconds is None:
        reset_seconds = _structured_retry_after_seconds(error_text)
    if reset_seconds is not None:
        backoff_seconds = max(1, int(reset_seconds))
        provider_resets_at = now_ts + backoff_seconds
    else:
        m_sec = re.search(r"retry\s+after\s+(\d+)\s*s", error_text, re.IGNORECASE)
        m_min = re.search(r"(\d+)\s*minutes?", error_text, re.IGNORECASE)
        if m_sec:
            backoff_seconds = int(m_sec.group(1))
            provider_resets_at = now_ts + backoff_seconds
        elif m_min:
            backoff_seconds = int(m_min.group(1)) * 60
            provider_resets_at = now_ts + backoff_seconds
        else:
            backoff_seconds = 900  # Default 15-minute synthetic cooldown

    backoff_until = now_ts + backoff_seconds
    entry = {
        "bucket_id": bucket_id,
        "observed_at": now_iso(),
        "observed_epoch": now_ts,
        "backoff_until_epoch": backoff_until,
        "provider_resets_at_epoch": provider_resets_at,
        "resets_at_epoch": provider_resets_at,
        "error_excerpt": error_text[:300],
    }

    def _update(current: dict[str, Any] | None) -> dict[str, Any]:
        base = current if isinstance(current, dict) else {"schema_version": SCHEMA_VERSION, "buckets": {}}
        base["schema_version"] = SCHEMA_VERSION
        base.setdefault("buckets", {})[bucket_id] = entry
        return base

    locked_update_json(obs_file, _update)
    return entry


def _strip_expired_overlay_fields(bucket: dict[str, Any]) -> dict[str, Any]:
    """Restore a bucket that had a persisted `observed_limited` overlay back to unknown baseline."""
    if bucket.get("status") != "observed_limited":
        return bucket
    agent = bucket.get("agent") or bucket.get("bucket_id", "").split(":", 1)[0]
    cleaned = {
        "bucket_id": bucket.get("bucket_id"),
        "agent": agent,
        "used_percent": None,
        "resets_at_epoch": None,
        **compute_window_metrics(None, None, None, 1.0),
    }
    if bucket.get("provider"):
        cleaned["provider"] = bucket["provider"]
    return cleaned


def _apply_observation_to_bucket(bucket: dict[str, Any], obs: dict[str, Any], now_ts: int) -> dict[str, Any]:
    backoff_until = obs.get("backoff_until_epoch") or obs.get("resets_at_epoch")
    if not isinstance(backoff_until, int) or backoff_until <= now_ts:
        return _strip_expired_overlay_fields(bucket)
    provider_resets = obs.get("provider_resets_at_epoch")
    baseline_reset = bucket.get("resets_at_epoch")
    if bucket.get("exhausted") and isinstance(baseline_reset, (int, float)) and baseline_reset > now_ts:
        # A synthetic retry cooldown must not erase a known account reset.
        provider_resets = max(provider_resets or 0, baseline_reset)
    updated = dict(bucket)
    updated["status"] = "observed_limited"
    updated["exhausted"] = True
    updated["tight"] = True
    updated["scarcity"] = 1.0
    updated["effective_headroom"] = 0.0
    updated["backoff_until_epoch"] = backoff_until
    if provider_resets is not None:
        updated["resets_at_epoch"] = provider_resets
        updated["hours_until_reset"] = round(max(0, provider_resets - now_ts) / 3600.0, 2)
    else:
        updated["resets_at_epoch"] = None
        updated["hours_until_reset"] = None
    updated["observed_error"] = obs.get("error_excerpt")
    updated["observed_at"] = obs.get("observed_at")
    return updated


def overlay_observations(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of `payload` with active rate-limit observations overlaid and expired overlays cleared."""
    out = copy.deepcopy(payload)
    obs_data = read_json_file(get_observations_file()) or {}
    obs_buckets = obs_data.get("buckets") or {}

    now_ts = int(time.time())
    agents_map = out.setdefault("agents", {})
    all_buckets = out.setdefault("buckets", {})

    # First ensure any expired persisted overlay in out is stripped
    for b_id, b_val in list(all_buckets.items()):
        if isinstance(b_val, dict) and b_val.get("status") == "observed_limited":
            all_buckets[b_id] = _strip_expired_overlay_fields(b_val)
            ag = b_id.split(":", 1)[0]
            if ag in agents_map and isinstance(agents_map[ag], dict):
                agents_map[ag].setdefault("buckets", {})[b_id] = all_buckets[b_id]

    for b_id, obs in obs_buckets.items():
        if not isinstance(obs, dict):
            continue
        # Older versions attributed individual errors to the shared pool without
        # recording a model. That observation cannot identify an affected route;
        # discard its overbroad veto and let the next attempt learn its scope.
        if b_id == "antigravity:default" and re.search(
            r"\bindividual\s+(?:model\s+)?quota\b", str(obs.get("error_excerpt") or ""), re.IGNORECASE
        ):
            continue
        backoff_until = obs.get("backoff_until_epoch") or obs.get("resets_at_epoch")
        if not isinstance(backoff_until, int) or backoff_until <= now_ts:
            continue
        agent_prefix = b_id.split(":", 1)[0]
        base_bucket = all_buckets.get(b_id) or {
            "bucket_id": b_id,
            "agent": agent_prefix,
            "used_percent": None,
            **compute_window_metrics(None, None, None, 1.0),
        }
        overlaid = _apply_observation_to_bucket(base_bucket, obs, now_ts)
        all_buckets[b_id] = overlaid
        if agent_prefix in agents_map and isinstance(agents_map[agent_prefix], dict):
            agents_map[agent_prefix].setdefault("buckets", {})[b_id] = overlaid

    return out


def build_observed_quota(agent: str, agent_info: dict[str, Any]) -> dict[str, Any]:
    """Build clean baseline quota state for OpenCode or Antigravity."""
    buckets: dict[str, dict[str, Any]] = {}
    if agent == "opencode":
        providers = [
            p["provider"]
            for p in (agent_info.get("authenticated_providers") or [])
            if isinstance(p, dict) and p.get("provider")
        ]
        if "opencode" not in providers:
            providers.append("opencode")
        # The subscription catalog shares the OpenCode Console credential but
        # bills against its own quota bucket.
        if "opencode" in providers and "opencode-go" not in providers:
            providers.append("opencode-go")
        for prov in providers:
            b_id = f"opencode:{prov}"
            plan = estimate_plan_capacity(
                "opencode-go"
                if prov == "opencode-go"
                else agent_info.get("plan_type") or "subscription"
            )
            metrics = compute_window_metrics(None, None, None, plan["capacity_factor"])
            buckets[b_id] = {
                "bucket_id": b_id,
                "agent": "opencode",
                "provider": prov,
                "plan": plan,
                "used_percent": None,
                "resets_at_epoch": None,
                **metrics,
            }
    else:
        b_id = f"{agent}:default"
        metrics = compute_window_metrics(None, None, None, 1.0)
        buckets[b_id] = {
            "bucket_id": b_id,
            "agent": agent,
            "used_percent": None,
            "resets_at_epoch": None,
            **metrics,
        }

    return {
        "agent": agent,
        "method": "observation-fallback",
        "plan": estimate_plan_capacity(agent_info.get("plan_type") or "subscription"),
        "reset_credits": {"available_count": None, "credits": []},
        "buckets": buckets,
    }


def _agent_quota_is_fresh(agent: str, entry: dict[str, Any] | None) -> bool:
    if not isinstance(entry, dict) or entry.get("stale"):
        return False
    if agent == "antigravity":
        if entry.get("method") != "codexbar:antigravity":
            return False
        source_age = parse_iso_age_seconds(entry.get("source_updated_at"))
        if source_age is None or source_age >= QUOTA_TTL_SECONDS or source_age < -60:
            return False
    age = parse_iso_age_seconds(entry.get("collected_at"))
    if age is None or age >= QUOTA_TTL_SECONDS:
        return False
    expected_fp = compute_agent_config_fingerprint(agent, None, entry.get("agent_version"))
    if entry.get("config_fingerprint") and entry.get("config_fingerprint") != expected_fp:
        return False
    return True


def collect_quota(agents: list[str] | None = None, refresh: bool = False) -> dict[str, Any]:
    """Collect quota status across agents with per-agent 15-minute TTL and non-persisted observation overlay."""
    quota_dir = get_cache_dir() / "quota"
    quota_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    cache_path = quota_dir / "status.json"
    target_agents = [canonical_agent_name(a) for a in agents] if agents else list(AGENT_BINARIES.keys())

    existing = read_json_file(cache_path)
    existing_agents = (
        existing.get("agents")
        if isinstance(existing, dict)
        and existing.get("schema_version") == SCHEMA_VERSION
        and isinstance(existing.get("agents"), dict)
        else {}
    )

    agents_to_query: list[str] = []
    for agent in target_agents:
        if refresh or not _agent_quota_is_fresh(agent, existing_agents.get(agent)):
            agents_to_query.append(agent)

    if not agents_to_query and existing:
        return overlay_observations(existing)

    probe_data = probe_all(agents=agents_to_query, refresh=False)
    fresh_results: dict[str, dict[str, Any]] = {}

    for agent in agents_to_query:
        a_info = (probe_data.get("agents") or {}).get(agent) or {}
        binary = a_info.get("binary")
        ver = a_info.get("version")
        fp = compute_agent_config_fingerprint(agent, binary, ver)
        if not a_info.get("usable") or not binary:
            fresh_results[agent] = {
                "agent": agent,
                "binary": binary,
                "agent_version": ver,
                "config_fingerprint": fp,
                "usable": False,
                "stale": False,
                "collected_at": now_iso(),
                "buckets": {},
            }
            continue

        try:
            if agent == "codex":
                q_res = query_codex_quota(binary)
            elif agent == "claude-code":
                q_res = query_claude_quota(binary, plan_type=a_info.get("plan_type"))
            elif agent == "antigravity":
                q_res = query_antigravity_quota()
            else:
                q_res = build_observed_quota(agent, a_info)
            fp = compute_agent_config_fingerprint(agent, binary, ver)
            q_res["binary"] = binary
            q_res["agent_version"] = ver
            q_res["config_fingerprint"] = fp
            q_res["usable"] = True
            q_res["stale"] = False
            q_res["collected_at"] = now_iso()
            fresh_results[agent] = q_res
        except Exception as exc:
            prev = existing_agents.get(agent)
            if isinstance(prev, dict) and isinstance(prev.get("buckets"), dict) and prev["buckets"]:
                stale_copy = copy.deepcopy(prev)
                stale_copy["stale"] = True
                stale_copy["refresh_error"] = str(exc)
                fresh_results[agent] = stale_copy
            else:
                fallback = build_observed_quota(agent, a_info)
                fallback["binary"] = binary
                fallback["agent_version"] = ver
                fallback["config_fingerprint"] = fp
                fallback["usable"] = True
                fallback["stale"] = True
                fallback["collected_at"] = now_iso()
                fallback["refresh_error"] = str(exc)
                fresh_results[agent] = fallback

    def _merge(current: dict[str, Any] | None) -> dict[str, Any]:
        merged_agents: dict[str, Any] = {}
        if (
            isinstance(current, dict)
            and current.get("schema_version") == SCHEMA_VERSION
            and isinstance(current.get("agents"), dict)
        ):
            for ag_k, ag_v in current["agents"].items():
                if isinstance(ag_v, dict) and isinstance(ag_v.get("buckets"), dict):
                    ag_copy = copy.deepcopy(ag_v)
                    for b_k, b_v in list(ag_copy["buckets"].items()):
                        if isinstance(b_v, dict):
                            ag_copy["buckets"][b_k] = _strip_expired_overlay_fields(b_v)
                    merged_agents[ag_k] = ag_copy
                else:
                    merged_agents[ag_k] = ag_v
        merged_agents.update(fresh_results)

        all_buckets: dict[str, dict[str, Any]] = {}
        for a_data in merged_agents.values():
            if isinstance(a_data, dict) and isinstance(a_data.get("buckets"), dict):
                for b_id, b_val in a_data["buckets"].items():
                    all_buckets[b_id] = b_val

        return {
            "schema_version": SCHEMA_VERSION,
            "collected_at": now_iso(),
            "stale": any(
                not _agent_quota_is_fresh(a_name, a_val)
                for a_name, a_val in merged_agents.items()
                if isinstance(a_val, dict) and a_val.get("usable")
            ),
            "agents": merged_agents,
            "buckets": all_buckets,
        }

    baseline_saved = locked_update_json(cache_path, _merge)
    return overlay_observations(baseline_saved)


def _get_reset_credit_lock_path() -> Path:
    d = get_cache_dir() / "quota"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d / "reset_credit_tx.json"


def notify_codex_reset_credit(
    run_id: str,
    credit_id: str | None = None,
    useful_work_blocked: bool = True,
    user_objected: bool = False,
) -> dict[str, Any]:
    """Record user notification or objection state for a Codex reset credit under account-wide lock."""
    validate_safe_identifier(run_id, "run_id")
    redeem_log_path = get_cache_dir() / "quota" / "redeemed_credits.json"
    now_ts = int(time.time())
    ts_iso = now_iso()

    with file_lock(_get_reset_credit_lock_path(), timeout=30.0):

        def _update(current: dict[str, Any] | None) -> dict[str, Any]:
            base = current if isinstance(current, dict) else {"schema_version": SCHEMA_VERSION, "runs": {}}
            base["schema_version"] = SCHEMA_VERSION
            runs = base.setdefault("runs", {})
            entry = runs.get(run_id) or {}
            if not entry.get("notified_epoch"):
                entry["notified_at"] = ts_iso
                entry["notified_epoch"] = now_ts
            if credit_id and not entry.get("credit_id"):
                entry["credit_id"] = credit_id
                entry["idempotency_key"] = f"cross-subagents-{run_id}-{credit_id}"
            entry["run_id"] = run_id
            entry["useful_work_blocked"] = bool(useful_work_blocked)
            if user_objected:
                entry["user_objected"] = True
                entry["status"] = "objected"
            else:
                entry.setdefault("user_objected", False)
                entry.setdefault("status", "notified")
            entry["updated_at"] = ts_iso
            runs[run_id] = entry
            return base

        updated = locked_update_json(redeem_log_path, _update)
        return updated["runs"][run_id]


def consume_codex_reset_credit(
    run_id: str,
    credit_id: str | None = None,
    allow_paid: bool = False,
    user_approved: bool = False,
    useful_work_blocked: bool = True,
    force_unexhausted: bool = False,
) -> dict[str, Any]:
    """Serialize and consume at most one eligible free reset credit in Codex with strict idempotency."""
    validate_safe_identifier(run_id, "run_id")
    redeem_log_path = get_cache_dir() / "quota" / "redeemed_credits.json"

    # Hold account-wide transaction lock across the entire refresh -> check -> RPC -> commit sequence
    with file_lock(_get_reset_credit_lock_path(), timeout=30.0):
        redeem_log = read_json_file(redeem_log_path) or {"schema_version": SCHEMA_VERSION, "runs": {}}
        runs_map = redeem_log.setdefault("runs", {})
        existing_run = runs_map.get(run_id) or {}

        if existing_run.get("user_objected"):
            return {
                "status": "skipped",
                "reason": f"User objected to reset credit redemption in run '{run_id}'.",
            }

        if existing_run.get("status") in ("redeemed", "alreadyRedeemed", "noCredit"):
            return {
                "status": "skipped",
                "reason": f"Run '{run_id}' already reached terminal reset-credit status '{existing_run.get('status')}'.",
                "previous": existing_run,
            }

        # Block if any OTHER run has an in-flight or uncertain redemption on this account
        for other_run_id, other_entry in runs_map.items():
            if other_run_id != run_id and isinstance(other_entry, dict):
                if other_entry.get("status") in ("redeeming", "uncertain"):
                    return {
                        "status": "skipped",
                        "reason": (
                            f"Another run ('{other_run_id}') has an in-flight or uncertain redemption "
                            f"(credit_id={other_entry.get('credit_id')}); refusing concurrent redemption."
                        ),
                    }

        if not useful_work_blocked and not user_approved:
            return {
                "status": "skipped",
                "reason": "No blocked Codex work remains; skipping automatic reset credit redemption.",
            }

        quota_data = collect_quota(agents=["codex"], refresh=True)
        codex_q = (quota_data.get("agents") or {}).get("codex") or {}
        if codex_q.get("stale"):
            return {
                "status": "skipped",
                "reason": "Codex quota data is stale; refusing to consume reset credit without a fresh limit read.",
            }

        buckets = codex_q.get("buckets") or {}
        is_exhausted = (codex_q.get("ordinary_usage_allowed") is False) or any(
            isinstance(b, dict) and b.get("exhausted") for b in buckets.values()
        )
        if not is_exhausted and not force_unexhausted:
            return {
                "status": "nothing_to_reset",
                "reason": "Codex rate limits are not confirmed exhausted.",
            }

        credits = ((codex_q.get("reset_credits") or {}).get("credits")) or []
        in_flight_attempt = existing_run.get("status") in ("notified", "redeeming", "uncertain")
        if any(c.get("status") == "redeeming" for c in credits) and not in_flight_attempt:
            return {
                "status": "skipped",
                "reason": "A reset credit is currently in 'redeeming' state; skipping to prevent duplicate redemption.",
            }

        now_ts = int(time.time())
        # Preserve bound credit_id and idempotency_key ONLY for retries of the same active/uncertain attempt
        locked_credit_id = existing_run.get("credit_id") if in_flight_attempt else None
        locked_idempotency_key = existing_run.get("idempotency_key") if in_flight_attempt else None
        attempt_seq = int(existing_run.get("attempt_seq") or 1)

        if locked_credit_id and credit_id and credit_id != locked_credit_id:
            return {
                "status": "conflict",
                "reason": (
                    f"Run '{run_id}' is already bound to credit_id '{locked_credit_id}' "
                    f"(idempotency_key={locked_idempotency_key}); cannot switch to '{credit_id}'."
                ),
            }

        target_credit_id = locked_credit_id or credit_id

        candidates: list[dict[str, Any]] = []
        for c in credits:
            if not isinstance(c, dict):
                continue
            c_id = c.get("id")
            if not c_id:
                continue
            if target_credit_id is not None and c_id != target_credit_id:
                continue
            c_status = c.get("status")
            if c_status not in ("available", "redeeming") or (
                c_status == "redeeming" and c_id != locked_credit_id
            ):
                continue
            exp = c.get("expires_at")
            # Require a known future expiration timestamp
            if not isinstance(exp, (int, float)) or exp <= now_ts:
                continue
            # Automatic redemption (without explicit user approval) requires expiry within 72 hours
            if not user_approved and (exp - now_ts) > RESET_CREDIT_EXPIRY_WINDOW_SECONDS:
                continue
            if not (allow_paid or c.get("free_grant") is True):
                continue
            candidates.append(c)

        if not candidates:
            return {
                "status": "unavailable",
                "reason": (
                    "No eligible unexpired free reset credit found "
                    "(automatic redemption requires confirmed free_grant=True and expiry within 72 hours)."
                ),
            }

        candidates.sort(key=lambda c: float(c["expires_at"]))
        chosen = candidates[0]
        chosen_id = str(chosen["id"])
        idempotency_key = (
            locked_idempotency_key
            or (
                f"cross-subagents-{run_id}-{chosen_id}"
                if attempt_seq == 1
                else f"cross-subagents-{run_id}-{chosen_id}-attempt-{attempt_seq}"
            )
        )

        notified_epoch = existing_run.get("notified_epoch")
        if not user_approved:
            if not isinstance(notified_epoch, int):
                existing_run.update(
                    {
                        "run_id": run_id,
                        "agent": "codex",
                        "credit_id": chosen_id,
                        "idempotency_key": idempotency_key,
                        "attempt_seq": attempt_seq,
                        "expires_at": chosen.get("expires_at"),
                        "useful_work_blocked": bool(useful_work_blocked),
                        "user_objected": False,
                        "notified_at": now_iso(),
                        "notified_epoch": now_ts,
                        "status": "notified",
                        "updated_at": now_iso(),
                    }
                )
                runs_map[run_id] = existing_run
                locked_update_json(redeem_log_path, lambda _cur: redeem_log)
                return {
                    "status": "awaiting_user_window",
                    "reason": (
                        "Recorded reset-credit notification and bound credit_id/idempotency_key. "
                        "Wait 30 minutes without user objection or pass --user-approved when the user confirms."
                    ),
                    "credit_id": chosen_id,
                    "idempotency_key": idempotency_key,
                    "notified_at": existing_run["notified_at"],
                    "eligible_auto_redeem_epoch": now_ts + RESET_CREDIT_WAIT_SECONDS,
                }
            elapsed = now_ts - notified_epoch
            if elapsed < RESET_CREDIT_WAIT_SECONDS:
                return {
                    "status": "awaiting_user_window",
                    "reason": (
                        f"Only {elapsed}s elapsed since notification (< {RESET_CREDIT_WAIT_SECONDS}s). "
                        "Pass --user-approved if user confirmed."
                    ),
                    "credit_id": chosen_id,
                    "idempotency_key": idempotency_key,
                    "remaining_seconds": RESET_CREDIT_WAIT_SECONDS - elapsed,
                }

        existing_run.update(
            {
                "run_id": run_id,
                "agent": "codex",
                "credit_id": chosen_id,
                "idempotency_key": idempotency_key,
                "attempt_seq": attempt_seq,
                "expires_at": chosen.get("expires_at"),
                "useful_work_blocked": bool(useful_work_blocked),
                "status": "redeeming",
                "updated_at": now_iso(),
            }
        )
        runs_map[run_id] = existing_run
        locked_update_json(redeem_log_path, lambda _cur: redeem_log)

        probe_data = probe_all(agents=["codex"], refresh=False)
        binary = ((probe_data.get("agents") or {}).get("codex") or {}).get("binary")
        if not binary:
            raise RuntimeError("Codex binary not found.")

        params: dict[str, Any] = {
            "creditId": chosen_id,
            "idempotencyKey": idempotency_key,
        }
        rpc_out = codex_app_server_rpc(
            binary,
            [
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "account/rateLimitResetCredit/consume",
                    "params": params,
                }
            ],
            timeout=15.0,
        )
        rpc_resp = rpc_out["responses"].get(4)

        if not rpc_resp or "error" in rpc_resp:
            err_detail = (rpc_resp or {}).get("error") or "No response from account/rateLimitResetCredit/consume"
            # Preserve status="uncertain" on transport timeout so the same credit_id + idempotency_key is retried
            if not rpc_resp:
                existing_run["status"] = "uncertain"
            else:
                existing_run["status"] = "failed"
                existing_run["credit_id"] = None
                existing_run["idempotency_key"] = None
                existing_run["attempt_seq"] = attempt_seq + 1
            existing_run["error"] = err_detail
            existing_run["updated_at"] = now_iso()
            runs_map[run_id] = existing_run
            locked_update_json(redeem_log_path, lambda _cur: redeem_log)
            return {
                "status": existing_run["status"],
                "credit_id": chosen_id,
                "idempotency_key": idempotency_key,
                "error": err_detail,
            }

        result_obj = rpc_resp.get("result")
        raw_outcome = (
            (result_obj.get("outcome") or result_obj.get("status"))
            if isinstance(result_obj, dict)
            else None
        )

        if raw_outcome in ("reset", "alreadyRedeemed"):
            final_status = "redeemed"
        elif raw_outcome in ("nothingToReset", "noCredit"):
            final_status = str(raw_outcome)
        elif raw_outcome == "redeeming":
            final_status = "redeeming"
        else:
            # Never assume an empty `{}` or unknown outcome is `redeemed`
            final_status = "uncertain"

        existing_run.update(
            {
                "status": final_status,
                "outcome": raw_outcome,
                "redeemed_at": now_iso() if final_status == "redeemed" else None,
                "result": result_obj,
                "updated_at": now_iso(),
            }
        )
        # If the attempt completed with `nothingToReset`, retire this attempt's idempotency key so a future
        # attempt after actual exhaustion gets a fresh logical attempt key.
        if final_status == "nothingToReset":
            existing_run["last_completed_credit_id"] = chosen_id
            existing_run["last_completed_idempotency_key"] = idempotency_key
            existing_run["credit_id"] = None
            existing_run["idempotency_key"] = None
            existing_run["attempt_seq"] = attempt_seq + 1

        runs_map[run_id] = existing_run
        locked_update_json(redeem_log_path, lambda _cur: redeem_log)

        if final_status == "redeemed":
            try:
                collect_quota(agents=["codex"], refresh=True)
            except Exception:
                pass
        return existing_run


def format_human_quota(data: dict[str, Any]) -> str:
    lines = [f"Quota snapshot (collected: {data.get('collected_at')}, stale={data.get('stale')})"]
    for b_id, b in sorted((data.get("buckets") or {}).items()):
        raw_u = b.get("used_percent_raw")
        used = (
            f"{raw_u}%"
            if raw_u is not None
            else (f"{b['used_percent']}%" if b.get("used_percent") is not None else "unknown")
        )
        st = b.get("status", "unknown")
        hrs = f", resets in {b['hours_until_reset']}h" if b.get("hours_until_reset") is not None else ""
        scarcity = b.get("scarcity")
        imminence = b.get("reset_imminence")
        lines.append(
            f"- {b_id}: status={st} | used={used}{hrs} | scarcity={scarcity} | reset_imminence={imminence}"
        )
    return "\n".join(lines)


def main() -> int:
    enforce_utf8()
    parser = argparse.ArgumentParser(description="Inspect and manage agent quota buckets and reset credits.")
    sub = parser.add_subparsers(dest="subcommand")

    p_st = sub.add_parser("status", help="Query quota status across agents.")
    p_st.add_argument("--agent", action="append", help="Query only specified agent(s).")
    p_st.add_argument("--refresh", action="store_true", help="Force fresh query.")
    p_st.add_argument("--human", action="store_true", help="Print human-readable summary.")

    p_obs = sub.add_parser("record-observation", help="Record an observed 429 or rate-limit error for a bucket.")
    p_obs.add_argument("--bucket", required=True, help="Bucket ID (e.g. opencode:deepseek, antigravity:default).")
    p_obs.add_argument("--error", required=True, help="Observed error message.")
    p_obs.add_argument("--reset-seconds", type=int, help="Cooldown seconds until reset.")

    p_not = sub.add_parser("notify-reset-credit", help="Record user notification or objection for a Codex reset credit.")
    p_not.add_argument("--run-id", required=True, help="Current run ID.")
    p_not.add_argument("--credit-id", help="Specific credit ID.")
    p_not.add_argument("--user-objected", action="store_true", help="Record that the user objected to consuming the credit.")

    p_con = sub.add_parser("consume-reset-credit", help="Consume an available free rate-limit reset credit.")
    p_con.add_argument("--agent", default="codex", help="Agent name (currently codex).")
    p_con.add_argument("--run-id", required=True, help="Current run ID (enforces 1 redemption per run).")
    p_con.add_argument("--credit-id", help="Specific credit ID to consume.")
    p_con.add_argument("--user-approved", action="store_true", help="User explicitly approved immediate redemption.")
    p_con.add_argument("--force-unexhausted", action="store_true", help="Allow consuming even if quota is not marked exhausted.")

    parser.add_argument("--agent", action="append", help="Query only specified agent(s).")
    parser.add_argument("--refresh", action="store_true", help="Force fresh query.")
    parser.add_argument("--human", action="store_true", help="Print human-readable summary.")

    args = parser.parse_args()
    cmd = args.subcommand or "status"

    if cmd == "status":
        res = collect_quota(agents=args.agent, refresh=args.refresh)
        if args.human:
            print(format_human_quota(res))
        else:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    if cmd == "record-observation":
        rec = record_quota_observation(args.bucket, args.error, args.reset_seconds)
        print(json.dumps(rec, indent=2, ensure_ascii=False))
        return 0

    if cmd == "notify-reset-credit":
        rec = notify_codex_reset_credit(
            args.run_id,
            args.credit_id,
            user_objected=args.user_objected,
        )
        print(json.dumps(rec, indent=2, ensure_ascii=False))
        return 0

    if cmd == "consume-reset-credit":
        if canonical_agent_name(args.agent) != "codex":
            print(json.dumps({"status": "unsupported", "reason": "Programmatic reset credit consumption is only supported on Codex."}))
            return 1
        res = consume_codex_reset_credit(
            run_id=args.run_id,
            credit_id=args.credit_id,
            user_approved=args.user_approved,
            force_unexhausted=args.force_unexhausted,
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("status") in ("redeemed", "skipped") else 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
