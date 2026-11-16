"""Discover, check open defects and select routes without dispatching a child.

Selection flags are forwarded verbatim to select_route.py, the single source
of parsing/eligibility/quota/plan behavior. --refresh refreshes discovery only;
use --refresh-quota explicitly for quota, respecting coordinator ownership.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import uuid
from typing import Any

from common import SCHEMA_VERSION, atomic_write_json, enforce_utf8, get_cache_dir, now_iso
from defects import list_reports
from models import collect_all_models
from probe import probe_all
import select_route


def prepare(selection_args: list[str], refresh: bool = False) -> tuple[dict[str, Any], int]:
    # The selector validates flags before discovery; do not duplicate parsing
    # or preflight providers before it has enforced coordinator quota controls.
    discovery: dict[str, Any] = {}

    def discover_models() -> dict[str, Any]:
        probes = probe_all(refresh=refresh)
        catalog = collect_all_models(refresh=refresh, probe_data=probes)
        # Capture diagnostics now, not by rereading stale caches after selection.
        # Only normalized diagnostic fields, never raw CLI initialization/auth data.
        discovery["probes"] = {
            agent: {key: info[key] for key in (
                "version", "usable", "host_usable", "target_usable", "stale",
                "probe_error", "warnings",
            ) if key in info}
            for agent, info in (probes.get("agents") or {}).items()
        }
        discovery["probe_stale"] = probes.get("stale", False)
        discovery["models"] = {
            agent: {**{key: info[key] for key in (
                "usable", "stale", "error", "refresh_error",
            ) if key in info},
                "maintenance_notice": (info.get("meta") or {}).get("maintenance_notice")}
            for agent, info in catalog.items()
        }
        return catalog

    capture = io.StringIO()
    with contextlib.redirect_stdout(capture):
        code = select_route.main(
            selection_args, refresh_discovery=refresh, discover_models=discover_models,
        )
    if code == 2:
        # main() also returns 2 for semantic validation errors; argparse raises
        # SystemExit(2). Neither path may trigger discovery here.
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "invalid_arguments",
            "selection": {"status": "invalid_arguments"},
            "notices": [], "defects": [],
            "note": "Selection rejected arguments; no preparation was performed.",
        }, code
    selection = json.loads(capture.getvalue())
    defects = list_reports()
    notices = []
    for agent, info in discovery.get("probes", {}).items():
        for warning in info.get("warnings") or []:
            notices.append({"agent": agent, "warning": warning})
        if info.get("stale"):
            notices.append({"agent": agent, "warning": "Probe cache is stale."})
        if info.get("probe_error"):
            notices.append({"agent": agent, "probe_error": info["probe_error"]})
    if discovery.get("probe_stale"):
        notices.append({"warning": "Probe cache is stale."})
    for agent, info in discovery.get("models", {}).items():
        if info.get("stale"):
            notices.append({"agent": agent, "warning": "Model catalog is stale."})
        for key in ("error", "refresh_error"):
            if info.get(key):
                notices.append({"agent": agent, key: info[key]})
        maintenance = info.get("maintenance_notice")
        if maintenance:
            notices.append({"agent": agent, "maintenance_notice": maintenance})
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "prepared_at": now_iso(),
        "status": selection.get("status"),
        "selection": selection,
        "notices": notices,
        "defects": defects,
        "discovery": discovery,
        "note": "Preparation does not dispatch. Check defects before using the selected routes.",
    }
    return receipt, code


def format_preparation(receipt: dict[str, Any]) -> str:
    selection = receipt["selection"]
    lines = [f"Preparation: {receipt['status']} (not dispatched)"]
    if "steps" in selection:
        for step in selection["steps"]:
            lines.append(f"step {step['step']}: {step['selected']['route_id']} — {step.get('rationale', '')}")
            for route in step.get("fallbacks") or []:
                lines.append(f"  fallback: {route['route_id']}")
        for step in selection.get("unfilled") or []:
            lines.append(f"UNFILLED step {step['step']}: {step['reason']}")
    else:
        if "candidate_count" in selection:
            lines.append(f"eligible_routes: {selection['candidate_count']}")
        lines.append(str(selection.get("rationale") or "No eligible route."))
        for route in selection.get("fallbacks") or []:
            lines.append(f"fallback: {route['route_id']}")
    for notice in receipt["notices"]:
        lines.append("NOTICE: " + json.dumps(notice, ensure_ascii=False))
    for defect in receipt["defects"]:
        lines.append(f"DEFECT {defect['severity']}: {defect['title']} — {defect['path']}")
    lines.append(receipt["note"])
    lines.append(f"details_file: {receipt['details_file']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    enforce_utf8()
    parser = argparse.ArgumentParser(description=__doc__, add_help=False, allow_abbrev=False)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--human", action="store_true")
    args, forwarded = parser.parse_known_args(argv)
    if "--help" in forwarded or "-h" in forwarded:
        print(__doc__)
        return select_route.main(["--help"])
    try:
        receipt, code = prepare(forwarded, refresh=args.refresh)
        path = get_cache_dir() / "preparations" / f"{uuid.uuid4().hex}.json"
        receipt["details_file"] = str(path)
        atomic_write_json(path, receipt)
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "preparation_failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(format_preparation(receipt) if args.human else json.dumps(receipt, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
