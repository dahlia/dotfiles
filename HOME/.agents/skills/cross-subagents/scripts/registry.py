#!/usr/bin/env python3
"""Live instance lease registry and recursion/fan-out guard.

Tracks active subagent instances under `$SKILL_CACHE_DIR/registry/<run-id>/<instance-id>.json`,
reaps dead PIDs or heartbeats older than 120 seconds, and degrades gracefully from parallel
fan-out to sequential execution when the live instance cap is reached.

Usage:
  python3 registry.py acquire --run-id <run_id> --pid <pid> [--instance-id <id>] [--parent <parent>]
  python3 registry.py heartbeat --run-id <run_id> --instance-id <id>
  python3 registry.py release --run-id <run_id> --instance-id <id>
  python3 registry.py status --run-id <run_id>
  python3 registry.py reap [--run-id <run_id>]
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import sys
from typing import Any

from common import (
    SCHEMA_VERSION,
    atomic_write_json,
    enforce_utf8,
    ensure_child_path,
    file_lock,
    get_cache_dir,
    now_iso,
    parse_iso_age_seconds,
    read_json_file,
    validate_safe_identifier,
)

HEARTBEAT_TIMEOUT_SECONDS = 120.0  # 2 minutes
DEFAULT_MAX_LIVE_INSTANCES = 6
NESTED_MAX_LIVE_INSTANCES = 1


def is_pid_alive(pid: int | None) -> bool:
    """Check whether a process with the given PID is still running."""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32

        open_proc = kernel32.OpenProcess
        open_proc.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_proc.restype = wintypes.HANDLE

        get_exit = kernel32.GetExitCodeProcess
        get_exit.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        get_exit.restype = wintypes.BOOL

        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        handle = open_proc(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            if get_exit(handle, ctypes.byref(exit_code)):
                return exit_code.value == STILL_ACTIVE
            return True
        finally:
            close_handle(handle)
    else:
        try:
            os.kill(int(pid), 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False


def get_run_registry_dir(run_id: str) -> Path:
    validate_safe_identifier(run_id, "run_id")
    reg_root = get_cache_dir() / "registry"
    reg_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    d = ensure_child_path(reg_root, run_id)
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


def _reap_unlocked(reg_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Reap dead PIDs and expired heartbeats (>120s). Caller must hold `_registry.lock`."""
    reaped: list[str] = []
    active: list[dict[str, Any]] = []

    for fpath in sorted(reg_dir.glob("*.json")):
        data = read_json_file(fpath)
        if not data:
            try:
                fpath.unlink()
            except OSError:
                pass
            continue
        inst_id = data.get("instance_id") or fpath.stem
        pid = data.get("pid")
        hb_age = parse_iso_age_seconds(data.get("heartbeat_at") or data.get("started_at"))
        expired = hb_age is not None and hb_age > HEARTBEAT_TIMEOUT_SECONDS
        dead = not is_pid_alive(pid)

        if expired or dead:
            try:
                fpath.unlink()
                reaped.append(inst_id)
            except OSError:
                pass
        else:
            active.append(data)

    return active, reaped


def reap_run_leases(run_id: str) -> dict[str, Any]:
    """Reap dead PIDs and expired heartbeats (>120s) in a run registry under lock."""
    reg_dir = get_run_registry_dir(run_id)
    with file_lock(reg_dir / "_registry.lock"):
        active, reaped = _reap_unlocked(reg_dir)

    return {
        "run_id": run_id,
        "active_count": len(active),
        "active_instances": active,
        "reaped_instances": reaped,
    }


def acquire_lease(
    run_id: str,
    instance_id: str | None = None,
    parent: str = "root",
    pid: int | None = None,
    max_live: int = DEFAULT_MAX_LIVE_INSTANCES,
    host_guard_refused: bool = False,
) -> dict[str, Any]:
    """Atomically reap, check global & per-parent caps, and acquire an instance lease."""
    reg_dir = get_run_registry_dir(run_id)
    inst = instance_id or f"inst-{secrets.token_hex(4)}"
    validate_safe_identifier(inst, "instance_id")
    validate_safe_identifier(parent, "parent")
    lease_path = ensure_child_path(reg_dir, f"{inst}.json")

    with file_lock(reg_dir / "_registry.lock"):
        active, _ = _reap_unlocked(reg_dir)
        active_count = len(active)
        actual_pid = pid if pid is not None else os.getpid()

        # Check for live instance_id collision before capacity checks
        for existing_lease in active:
            if existing_lease.get("instance_id") == inst:
                if existing_lease.get("pid") == actual_pid and existing_lease.get("parent") == parent:
                    ts = now_iso()
                    existing_lease["heartbeat_at"] = ts
                    atomic_write_json(lease_path, existing_lease)
                    is_nested = parent != "root"
                    return {
                        "status": "acquired",
                        "reacquired": True,
                        "mode": "sequential_only" if is_nested else "parallel",
                        "fan_out_allowed": (not is_nested) and (active_count < max_live),
                        "run_id": run_id,
                        "instance_id": inst,
                        "pid": actual_pid,
                        "parent": parent,
                        "active_count": active_count,
                        "max_live": max_live,
                    }
                return {
                    "status": "collision",
                    "fan_out_allowed": False,
                    "reason": (
                        f"Instance ID '{inst}' is already held by live PID {existing_lease.get('pid')} "
                        f"(parent='{existing_lease.get('parent')}')."
                    ),
                    "run_id": run_id,
                    "instance_id": inst,
                    "active_count": active_count,
                    "max_live": max_live,
                }

        is_nested = parent != "root"
        parent_children = sum(1 for a in active if a.get("parent") == parent) if is_nested else 0

        if host_guard_refused:
            return {
                "status": "degraded",
                "mode": "sequential_inline",
                "fan_out_allowed": False,
                "reason": (
                    "Host subagent guard refused spawn; falling back to sequential inline execution "
                    "without incrementing lease count."
                ),
                "run_id": run_id,
                "active_count": active_count,
                "max_live": max_live,
            }

        if active_count >= max_live:
            return {
                "status": "degraded",
                "mode": "sequential_inline",
                "fan_out_allowed": False,
                "reason": (
                    f"Global live instance count ({active_count}) reached cap ({max_live}); "
                    "wait for a slot or execute sequentially."
                ),
                "run_id": run_id,
                "instance_id": inst,
                "active_count": active_count,
                "max_live": max_live,
            }

        if is_nested and parent_children >= NESTED_MAX_LIVE_INSTANCES:
            return {
                "status": "degraded",
                "mode": "sequential_inline",
                "fan_out_allowed": False,
                "reason": (
                    f"Nested parent '{parent}' already has {parent_children} active child instance(s) "
                    f"(cap={NESTED_MAX_LIVE_INSTANCES}); nested fan-out disabled."
                ),
                "run_id": run_id,
                "instance_id": inst,
                "active_count": active_count,
                "parent_active_children": parent_children,
                "max_live": max_live,
            }

        ts = now_iso()
        lease_data = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "instance_id": inst,
            "pid": actual_pid,
            "parent": parent,
            "started_at": ts,
            "heartbeat_at": ts,
        }
        atomic_write_json(lease_path, lease_data)

        return {
            "status": "acquired",
            "mode": "sequential_only" if is_nested else "parallel",
            "fan_out_allowed": (not is_nested) and (active_count + 1 < max_live),
            "run_id": run_id,
            "instance_id": inst,
            "pid": actual_pid,
            "parent": parent,
            "active_count": active_count + 1,
            "max_live": max_live,
        }


def heartbeat_lease(run_id: str, instance_id: str) -> dict[str, Any]:
    reg_dir = get_run_registry_dir(run_id)
    validate_safe_identifier(instance_id, "instance_id")
    fpath = ensure_child_path(reg_dir, f"{instance_id}.json")
    with file_lock(reg_dir / "_registry.lock"):
        data = read_json_file(fpath)
        if not data:
            return {"status": "not_found", "run_id": run_id, "instance_id": instance_id}
        data["heartbeat_at"] = now_iso()
        atomic_write_json(fpath, data)
    return {"status": "ok", "run_id": run_id, "instance_id": instance_id, "heartbeat_at": data["heartbeat_at"]}


def release_lease(run_id: str, instance_id: str) -> dict[str, Any]:
    reg_dir = get_run_registry_dir(run_id)
    validate_safe_identifier(instance_id, "instance_id")
    fpath = ensure_child_path(reg_dir, f"{instance_id}.json")
    removed = False
    with file_lock(reg_dir / "_registry.lock"):
        if fpath.exists():
            try:
                fpath.unlink()
                removed = True
            except OSError:
                pass
        active, _ = _reap_unlocked(reg_dir)
    return {
        "run_id": run_id,
        "instance_id": instance_id,
        "status": "released" if removed else "not_found",
        "active_count": len(active),
    }


def main() -> int:
    enforce_utf8()
    parser = argparse.ArgumentParser(description="Manage live subagent instance leases.")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_acq = sub.add_parser("acquire", help="Acquire a lease for a subagent instance.")
    p_acq.add_argument("--run-id", required=True)
    p_acq.add_argument("--instance-id")
    p_acq.add_argument("--parent", default="root")
    p_acq.add_argument(
        "--pid",
        type=int,
        required=True,
        help="PID of the long-lived caller or worker process holding the lease.",
    )
    p_acq.add_argument("--max-live", type=int, default=DEFAULT_MAX_LIVE_INSTANCES)
    p_acq.add_argument("--host-refused", action="store_true", help="Mark that host guard already refused spawn.")

    p_hb = sub.add_parser("heartbeat", help="Update heartbeat timestamp for an active lease.")
    p_hb.add_argument("--run-id", required=True)
    p_hb.add_argument("--instance-id", required=True)

    p_rel = sub.add_parser("release", help="Release an active lease.")
    p_rel.add_argument("--run-id", required=True)
    p_rel.add_argument("--instance-id", required=True)

    p_st = sub.add_parser("status", help="Reap stale leases and show active count for a run.")
    p_st.add_argument("--run-id", required=True)
    p_st.add_argument("--max-live", type=int, default=DEFAULT_MAX_LIVE_INSTANCES)

    p_reap = sub.add_parser("reap", help="Reap stale leases across one or all runs.")
    p_reap.add_argument("--run-id")

    args = parser.parse_args()

    if args.subcommand == "acquire":
        if not is_pid_alive(args.pid) and not args.host_refused:
            print(
                json.dumps(
                    {
                        "status": "invalid_pid",
                        "reason": f"Supplied --pid {args.pid} is not alive.",
                    },
                    indent=2,
                )
            )
            return 1
        res = acquire_lease(
            run_id=args.run_id,
            instance_id=args.instance_id,
            parent=args.parent,
            pid=args.pid,
            max_live=args.max_live,
            host_guard_refused=args.host_refused,
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("status") == "acquired" else 1

    if args.subcommand == "heartbeat":
        res = heartbeat_lease(args.run_id, args.instance_id)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res["status"] == "ok" else 1

    if args.subcommand == "release":
        res = release_lease(args.run_id, args.instance_id)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    if args.subcommand == "status":
        state = reap_run_leases(args.run_id)
        state["max_live"] = args.max_live
        state["fan_out_allowed"] = state["active_count"] < args.max_live
        print(json.dumps(state, indent=2, ensure_ascii=False))
        return 0

    if args.subcommand == "reap":
        if args.run_id:
            print(json.dumps(reap_run_leases(args.run_id), indent=2, ensure_ascii=False))
        else:
            reg_root = get_cache_dir() / "registry"
            summary = []
            if reg_root.is_dir():
                for d in sorted(reg_root.iterdir()):
                    if d.is_dir():
                        try:
                            summary.append(reap_run_leases(d.name))
                        except ValueError:
                            continue
            print(json.dumps({"runs": summary}, indent=2, ensure_ascii=False))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
