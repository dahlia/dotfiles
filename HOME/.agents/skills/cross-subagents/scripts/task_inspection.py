"""Compact, read-only views of existing task receipts; never executes agents."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from common import SCHEMA_VERSION, ensure_child_path, file_lock, get_cache_dir, validate_safe_identifier


def inspect_task(run_id: str, task_id: str) -> dict[str, Any]:
    validate_safe_identifier(run_id, "run_id")
    validate_safe_identifier(task_id, "task_id")
    run_dir = ensure_child_path(get_cache_dir() / "runs", run_id)
    task_dir = ensure_child_path(run_dir, task_id)
    if not task_dir.is_dir():
        raise FileNotFoundError(f"No task directory: {task_dir}")
    lock_path = ensure_child_path(task_dir, "_task.lock.lock")
    if lock_path.exists() and not lock_path.is_file():
        raise ValueError("task lock must be a regular file")
    # Same companion lock as execute_task/record_native_task. Fail immediately
    # while publication is in progress rather than mixing receipt generations.
    with file_lock(task_dir / "_task.lock", timeout=0.0):
        return _inspect_locked(run_id, task_id, task_dir)


def _inspect_locked(run_id: str, task_id: str, task_dir: Path) -> dict[str, Any]:
    meta_path = ensure_child_path(task_dir, "meta.json")
    if not meta_path.is_file():
        raise ValueError("metadata must be a regular file")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if not isinstance(meta, dict) or not isinstance(meta.get("status"), str):
        raise ValueError("metadata must be an object with a status")
    if meta.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported metadata schema version")
    if meta.get("run_id") != run_id or meta.get("task_id") != task_id:
        raise ValueError("metadata run/task identity does not match requested task")

    issues: list[str] = []

    def readable_result(raw: Any) -> str | None:
        if not isinstance(raw, str) or not raw:
            return None
        path = Path(raw)
        if not path.is_absolute():
            path = task_dir / path
        resolved = path.resolve()
        if not resolved.is_relative_to(task_dir.resolve()):
            issues.append("Result pointer escapes task directory; inspect metadata manually.")
            return None
        try:
            if not resolved.is_file():
                issues.append(f"Result is not a regular file: {resolved}")
                return None
            # Check the file without putting potentially large answer text into output.
            with resolved.open("r", encoding="utf-8") as stream:
                while chunk := stream.read(8192):
                    if chunk.strip():
                        return str(resolved)
        except (OSError, UnicodeError) as exc:
            issues.append(f"Result unavailable: {exc}")
            return None
        issues.append(f"Result is empty: {resolved}")
        return None

    status = meta["status"]
    # A stale task-level result from an earlier success is never a failed turn's answer.
    final_pointer = meta.get("result_file")
    # Native receipts predate result_file and explicitly use task-local text.
    # A null/escaped pointer on a CLI receipt is never replaced by stale text.
    if "result_file" not in meta and meta.get("native_host_execution") is True:
        final_pointer = str(task_dir / "result.md")
    answer = readable_result(final_pointer) if status == "ok" else None
    partial = readable_result(meta.get("attempt_result_file")) if status != "ok" else None
    if status == "ok" and answer is None:
        issues.append("Successful metadata has no readable final answer; do not report completion.")
    if meta.get("model_verified") is not True:
        issues.append("Observed model is not verified; retain this limitation.")
    policy = meta.get("tool_policy") or {}
    if isinstance(policy, dict) and policy.get("no_tools") and meta.get("tools_enforced") is not True:
        issues.append("No-tools policy was not verified as enforced.")
    guard = meta.get("guard")
    if isinstance(guard, dict) and guard.get("status") != "clean":
        issues.append("Protected-input guard did not report clean; do not retry on a changed tree.")

    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "task_id": task_id,
        "status": status,
        "exit_code": meta.get("exit_code"),
        "error_class": meta.get("error_class"),
        "error": meta.get("error"),
        "route_id": meta.get("route_id"),
        "requested_model": meta.get("requested_model"),
        "observed_model": meta.get("observed_model"),
        "model_verified": meta.get("model_verified"),
        "sandbox_mode": meta.get("sandbox_mode"),
        "cwd": meta.get("cwd"),
        "workspace_dirs": meta.get("workspace_dirs"),
        "tool_policy": meta.get("tool_policy"),
        "tools_enforced": meta.get("tools_enforced"),
        "guard": guard,
        "denials_observed": meta.get("denials_observed"),
        "warnings": meta.get("warnings"),
        "maintenance_notice": meta.get("maintenance_notice"),
        "session_cleaned_up": meta.get("session_cleaned_up"),
        "cleanup_note": meta.get("cleanup_note"),
        "cleanup_verified": meta.get("cleanup_verified"),
        "selection_rationale": meta.get("selection_rationale"),
        "meta_file": str(meta_path),
        "result_file": answer,
        "partial_result_file": partial,
        "issues": issues,
        "next_checks": [
            "Read the result or partial result; a partial answer is not completion.",
            "Assess denials/warnings and verify required checks actually ran.",
            "Check git status/diff against write_set; judge findings and significance.",
        ],
    }


def format_inspection(receipt: dict[str, Any]) -> str:
    lines = [
        f"Task {receipt['task_id']}: status={receipt['status']}, exit={receipt['exit_code']}, error_class={receipt['error_class']}",
        f"route: {receipt['route_id']}",
        f"model: requested={receipt['requested_model']}, observed={receipt['observed_model']}, verified={receipt['model_verified']}",
        f"policy: sandbox={receipt['sandbox_mode']}, tools_enforced={receipt['tools_enforced']}, "
        + json.dumps(receipt["tool_policy"], ensure_ascii=False, separators=(",", ":")),
        f"cwd: {receipt['cwd']}; workspace_dirs: " + json.dumps(receipt["workspace_dirs"], ensure_ascii=False),
        f"cleanup: session_cleaned_up={receipt['session_cleaned_up']}, cleanup_verified={receipt['cleanup_verified']}",
    ]
    for key in ("error", "guard", "denials_observed", "warnings", "maintenance_notice", "cleanup_note", "selection_rationale"):
        value = receipt.get(key)
        if value:
            lines.append(f"{key}: " + json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    for key in ("meta_file", "result_file", "partial_result_file"):
        if receipt.get(key):
            lines.append(f"{key}: {receipt[key]}")
    lines.extend(f"ATTENTION: {item}" for item in receipt["issues"])
    lines.extend(f"check: {item}" for item in receipt["next_checks"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default=os.environ.get("CROSS_SUBAGENTS_RUN_ID"),
                        required=not bool(os.environ.get("CROSS_SUBAGENTS_RUN_ID")))
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--human", action="store_true")
    args = parser.parse_args(argv)
    try:
        receipt = inspect_task(args.run_id, args.task_id)
    except (OSError, ValueError, UnicodeError) as exc:
        print(json.dumps({"status": "inspection_failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(format_inspection(receipt) if args.human else json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0 if receipt["status"] == "ok" and receipt["result_file"] else 1
