#!/usr/bin/env python3
"""Execute or resume a subagent across Codex, Claude Code, OpenCode, or Antigravity.

Features:
  - Stores prompt files under `$SKILL_CACHE_DIR/runs/<run-id>/prompts/` (`common.md` + `task-<id>.md`)
    and streams them over stdin without shell interpolation.
  - Serializes per-task execution with `_task.lock`, allocates monotonically increasing
    `<task-id>/attempts/attempt-<N>/` directories across reruns, and atomically publishes
    `<task-id>/result.md` only on verified current-attempt success.
  - Enforces the result contract (`result.md`, `meta.json`, `manifest.jsonl`) on all paths,
    including early preparation/validation errors.
  - Supports one-shot and coroutine (`--keep-session`, `--resume`, `--resume-task`) execution
    with effective working-directory persistence and caller override precedence.
  - Integrates versioned CLI recipes (`recipes.json`) with one bounded adapted retry on
    `no_option`/`format` errors, and failure-specific cross-bucket/same-bucket fallback routing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

from common import (
    SCHEMA_VERSION,
    VERIFIED_CANONICAL_ALIASES,
    atomic_write_json,
    atomic_write_text,
    canonical_agent_name,
    enforce_utf8,
    ensure_child_path,
    file_lock,
    get_cache_dir,
    infer_applicable_quota_buckets,
    is_bare_generation_prefix,
    locked_update_json,
    make_bounded_instance_id,
    normalize_model_route,
    normalize_official_model_id,
    now_iso,
    official_agent_name,
    read_json_file,
    validate_official_model_id,
    validate_safe_identifier,
)
from models import collect_all_models, repair_codex_models, resolve_official_model_id
from probe import cache_cli_help_and_recipe, get_agent_recipe, parse_semver, probe_all, strip_ansi
from quota import collect_quota, record_quota_observation
from registry import acquire_lease, heartbeat_lease, is_pid_alive, release_lease
from select_route import (
    check_route_eligibility,
    classify_route_billing,
    evaluate_route_traits,
    load_session_pins,
    select_routes,
)

DEFAULT_TIMEOUT_SECONDS = 600
DEFAULT_QUEUE_TIMEOUT_SECONDS = 300
HEARTBEAT_INTERVAL_SECONDS = 30.0
COST_GATE_MAX_CALLS = 5
COST_GATE_MAX_COST_INDEX = 1.5


def get_run_dir(run_id: str) -> Path:
    validate_safe_identifier(run_id, "run_id")
    runs_root = get_cache_dir() / "runs"
    runs_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    d = ensure_child_path(runs_root, run_id)
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


def append_manifest_event(run_dir: Path, event: dict[str, Any]) -> None:
    manifest_path = run_dir / "manifest.jsonl"
    line = json.dumps(event, ensure_ascii=False) + "\n"
    with file_lock(manifest_path):
        with open(manifest_path, "a", encoding="utf-8", newline="\n") as f:
            f.write(line)
            f.flush()


def atomic_publish_file(src: Path, dst: Path) -> None:
    """Atomically publish `src` to `dst` via a temporary file in `dst.parent`."""
    dst.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    content = src.read_bytes()
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".tmp", dir=str(dst.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, dst)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def allocate_next_attempt_dir(task_dir: Path) -> tuple[int, Path]:
    """Allocate a monotonically increasing, clean `attempts/attempt-<N>` directory for `task_dir`."""
    attempts_root = ensure_child_path(task_dir, "attempts")
    attempts_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    max_num = 0
    for child in attempts_root.iterdir():
        if child.is_dir() and child.name.startswith("attempt-"):
            suffix = child.name[len("attempt-") :]
            if suffix.isdigit():
                max_num = max(max_num, int(suffix))
    next_num = max_num + 1
    attempt_dir = ensure_child_path(attempts_root, f"attempt-{next_num}")
    attempt_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    return next_num, attempt_dir


def classify_failure(exit_code: int, stdout_text: str, stderr_text: str, timed_out: bool = False) -> str:
    """Classify failure into standard error classes for repair and fallback routing."""
    if timed_out or exit_code == 124:
        return "timeout"
    if exit_code == 127:
        return "not_installed"
    combined = f"{stdout_text}\n{stderr_text}".lower()
    if any(
        k in combined
        for k in (
            "rate limit",
            "ratelimit",
            "usage limit",
            "quota",
            "insufficient_quota",
            "429",
            "too many requests",
            "credit balance is too low",
            "hit your limit",
        )
    ):
        return "quota"
    if any(
        k in combined
        for k in (
            "not logged in",
            "authentication",
            "unauthorized",
            "401",
            "403",
            "invalid api key",
            "token expired",
            "login required",
        )
    ):
        return "auth"
    if any(
        k in combined
        for k in (
            "unknown argument",
            "unexpected argument",
            "unknown option",
            "unrecognized option",
            "flag needs an argument",
            "requires --verbose",
            "took \"--",
        )
    ):
        return "no_option"
    if any(
        k in combined
        for k in (
            "model not found",
            "unknown model",
            "invalid model",
            "does not exist or you do not have access",
            "unsupported model",
        )
    ):
        return "model_unavailable"
    if any(k in combined for k in ("permission denied", "approval required", "sandbox", "blocked by policy")):
        return "permission"
    return "failed"


def classify_exception(exc: Exception) -> tuple[str, int]:
    """Map a Python exception during preparation or subprocess launch to `(error_class, exit_code)`."""
    if isinstance(exc, FileNotFoundError):
        msg = str(exc).lower()
        if "prompt" in msg:
            return "failed", 2
        return "not_installed", 127
    if isinstance(exc, PermissionError):
        return "permission", 126
    msg = str(exc).lower()
    if "quota_exhausted" in msg or "rate limit" in msg:
        return "quota", 1
    if "model" in msg and ("not found" in msg or "unavailable" in msg or "eligible" in msg):
        return "model_unavailable", 1
    if "permission" in msg or "sandbox" in msg:
        return "permission", 1
    return "failed", 1


def _strip_date_and_normalize(model_token: str) -> str:
    s = model_token.strip().lower().replace("_", "-")
    s = re.sub(r"-(20\d{6}|\d{2}-20\d{2})$", "", s)
    if s in VERIFIED_CANONICAL_ALIASES:
        s = VERIFIED_CANONICAL_ALIASES[s]
    return s.replace(".", "-")


def _verify_model_match(
    requested_model: str,
    canonical_key: str,
    observed_model: str | None,
    expected_provider: str | None = None,
    expected_variant: str | None = None,
) -> bool:
    """Verify that an observed model string matches the requested model/provider/variant without silent substitution."""
    if not observed_model:
        return True
    obs_raw = observed_model.strip().lower()
    req_raw = requested_model.strip().lower()

    obs_prov: str | None = None
    obs_var: str | None = None
    obs_mod = obs_raw
    if "#" in obs_mod:
        obs_mod, obs_var = obs_mod.split("#", 1)
    if "/" in obs_mod:
        obs_prov, obs_mod = obs_mod.split("/", 1)

    req_prov: str | None = expected_provider.strip().lower() if expected_provider else None
    req_var: str | None = expected_variant.strip().lower() if expected_variant else None
    req_mod = req_raw
    if "#" in req_mod:
        req_mod, parsed_req_var = req_mod.split("#", 1)
        req_var = req_var or parsed_req_var
    if "/" in req_mod:
        parsed_req_prov, req_mod = req_mod.split("/", 1)
        req_prov = req_prov or parsed_req_prov

    if obs_prov and req_prov and obs_prov != req_prov:
        return False
    if obs_var and req_var and obs_var != req_var:
        return False

    if obs_mod == req_mod or obs_mod.replace(".", "-") == req_mod.replace(".", "-"):
        return True

    norm_obs = _strip_date_and_normalize(obs_mod)
    norm_req = _strip_date_and_normalize(req_mod)
    norm_canon = _strip_date_and_normalize(canonical_key)
    return norm_obs == norm_req or (norm_req == norm_canon and norm_obs == norm_canon)


def run_process_with_timeout(
    cmd: list[str],
    stdin_text: str,
    cwd: str | None,
    timeout_sec: int,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str, bool, bool]:
    """Run a subprocess with stdin_text and wall-clock timeout, killing and verifying the process group on timeout.

    Returns `(exit_code, stdout, stderr, timed_out, cleanup_verified)`.
    """
    kwargs: dict[str, Any] = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "cwd": cwd,
        "env": env,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **kwargs)
    timed_out = False
    cleanup_verified = True
    try:
        stdout_data, stderr_data = proc.communicate(input=stdin_text, timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        timed_out = True
        if os.name != "nt":
            pgid = proc.pid
            try:
                os.killpg(pgid, signal.SIGTERM)
            except OSError:
                pass
            time.sleep(1.0)
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass
            time.sleep(0.2)
            # Verify entire process group is gone
            try:
                os.killpg(pgid, 0)
                cleanup_verified = False
            except ProcessLookupError:
                cleanup_verified = True
            except PermissionError:
                cleanup_verified = False
        else:
            tk_ok = False
            try:
                tk_proc = subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
                tk_ok = tk_proc.returncode == 0
            except Exception:
                tk_ok = False
            if not tk_ok:
                try:
                    proc.kill()
                except Exception:
                    pass
            time.sleep(0.2)
            cleanup_verified = tk_ok and not is_pid_alive(proc.pid)

        try:
            stdout_data, stderr_data = proc.communicate(timeout=3)
        except Exception:
            # If final pipe drain times out or fails, a descendant process may still hold stdio pipes open
            stdout_data, stderr_data = "", ""
            cleanup_verified = False

        if os.name != "nt":
            try:
                os.killpg(proc.pid, 0)
                cleanup_verified = False
            except ProcessLookupError:
                pass
            except PermissionError:
                cleanup_verified = False
        elif is_pid_alive(proc.pid):
            cleanup_verified = False

    return (
        proc.returncode if proc.returncode is not None else -1,
        stdout_data or "",
        stderr_data or "",
        timed_out,
        cleanup_verified,
    )


def invoke_codex(
    binary: str,
    route: dict[str, Any],
    prompt_text: str,
    attempt_dir: Path,
    effort: str | None,
    keep_session: bool,
    resume_session_id: str | None,
    sandbox_mode: str,
    cwd: str | None,
    timeout_sec: int,
    child_env: dict[str, str],
    recipe_flags: dict[str, Any] | None = None,
    allow_gh: bool = False,
    allow_network: bool = False,
) -> dict[str, Any]:
    result_file = attempt_dir / "result.md"
    if result_file.exists():
        result_file.unlink()
    events_file = attempt_dir / "events.jsonl"
    stderr_file = attempt_dir / "stderr.log"

    flags = recipe_flags if recipe_flags is not None else get_agent_recipe("codex")
    model_id = route.get("resolved_model_id") or route["model_id"]
    eff = effort or route.get("variant")
    cmd: list[str] = [binary]
    if flags.get("has_no_daemon", True):
        cmd.append("--no-daemon")

    if resume_session_id:
        cmd.extend(["exec", "resume"])
    else:
        cmd.append("exec")
        if not keep_session and flags.get("has_ephemeral", True):
            cmd.append("--ephemeral")

    if flags.get("has_skip_git_repo_check", True):
        cmd.append("--skip-git-repo-check")
    if flags.get("has_json", True):
        cmd.append("--json")
    cmd.extend(["-m", model_id])
    if eff:
        cmd.extend(["-c", f'model_reasoning_effort="{eff}"'])

    if sandbox_mode == "bypass":
        if flags.get("has_dangerously_bypass", True):
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            cmd.extend(["-c", 'sandbox_mode="danger-full-access"', "-c", "approval_policy=never"])
    else:
        cmd.extend(["-c", f'sandbox_mode="{sandbox_mode}"', "-c", "approval_policy=never"])
        if sandbox_mode == "workspace-write" and (allow_gh or allow_network):
            cmd.extend(["-c", "sandbox_workspace_write.network_access=true"])
        elif sandbox_mode == "read-only" and (allow_gh or allow_network):
            print(
                "WARNING: Codex read-only sandbox strictly isolates network access. "
                "Reference sources should be pre-fetched into local files and passed via --context-file, "
                "or run in --sandbox workspace-write if live fetching is required.",
                file=sys.stderr,
            )

    cmd.extend(["-o", str(result_file)])
    if resume_session_id:
        cmd.extend([resume_session_id, "-"])
    else:
        cmd.append("-")

    code, out, err, timed_out, cleanup_verified = run_process_with_timeout(
        cmd, prompt_text, cwd, timeout_sec, env=child_env
    )
    events_file.write_text(out, encoding="utf-8")
    stderr_file.write_text(err, encoding="utf-8")

    session_id = resume_session_id
    usage = {"input_tokens": 0, "output_tokens": 0}
    last_agent_msg = ""
    stream_errors: list[str] = []
    saw_turn_completed = False
    observed_model: str | None = None

    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        ev_type = ev.get("type")
        if ev.get("model") and not observed_model:
            observed_model = str(ev["model"])
        if ev_type == "thread.started" and ev.get("thread_id"):
            session_id = ev["thread_id"]
        elif ev_type == "turn.completed":
            saw_turn_completed = True
            if isinstance(ev.get("usage"), dict):
                u = ev["usage"]
                usage["input_tokens"] += int(u.get("input_tokens", 0))
                usage["output_tokens"] += int(u.get("output_tokens", 0))
        elif ev_type == "item.completed":
            item = ev.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                last_agent_msg = item["text"]
        elif ev_type in ("error", "turn.failed"):
            stream_errors.append(json.dumps(ev))

    if saw_turn_completed and (not result_file.is_file() or not result_file.read_text(encoding="utf-8").strip()) and last_agent_msg:
        result_file.write_text(last_agent_msg.strip() + "\n", encoding="utf-8")

    final_text = result_file.read_text(encoding="utf-8").strip() if (saw_turn_completed and result_file.is_file()) else ""
    err_combined = err + ("\n" + "\n".join(stream_errors) if stream_errors else "")
    ephemeral_used = not keep_session and not resume_session_id and flags.get("has_ephemeral", True)

    if timed_out:
        return {
            "status": "timeout",
            "exit_code": code,
            "session_id": session_id,
            "usage": usage,
            "requested_model": route.get("requested_model") or model_id,
            "resolved_model": model_id,
            "observed_model": observed_model,
            "session_cleaned_up": ephemeral_used,
            "cleanup_verified": cleanup_verified,
            "error_class": "timeout",
            "error": "Execution timed out.",
        }

    if observed_model and not _verify_model_match(model_id, route["canonical_key"], observed_model):
        return {
            "status": "failed",
            "exit_code": code or 1,
            "session_id": session_id,
            "usage": usage,
            "requested_model": route.get("requested_model") or model_id,
            "resolved_model": model_id,
            "observed_model": observed_model,
            "session_cleaned_up": ephemeral_used,
            "cleanup_verified": cleanup_verified,
            "error_class": "model_unavailable",
            "error": f"Model substitution detected: requested '{model_id}', observed '{observed_model}'.",
        }

    if code != 0 or stream_errors or not saw_turn_completed or not final_text:
        err_class = classify_failure(code, out, err_combined, timed_out=False)
        if code == 0 and not stream_errors and (not saw_turn_completed or not final_text):
            err_class = "format"
        return {
            "status": "quota" if err_class == "quota" else "failed",
            "exit_code": code,
            "session_id": session_id,
            "usage": usage,
            "requested_model": route.get("requested_model") or model_id,
            "resolved_model": model_id,
            "observed_model": observed_model,
            "session_cleaned_up": ephemeral_used,
            "cleanup_verified": cleanup_verified,
            "error_class": err_class,
            "error": err_combined.strip()[:500]
            or ("Codex stream ended before turn.completed." if not saw_turn_completed else "Empty result from Codex."),
        }

    return {
        "status": "ok",
        "exit_code": 0,
        "session_id": session_id,
        "usage": usage,
        "requested_model": route.get("requested_model") or model_id,
        "resolved_model": model_id,
        "observed_model": observed_model,
        "session_cleaned_up": ephemeral_used,
        "cleanup_verified": cleanup_verified,
        "error_class": None,
    }


def invoke_claude_code(
    binary: str,
    route: dict[str, Any],
    prompt_text: str,
    attempt_dir: Path,
    effort: str | None,
    keep_session: bool,
    resume_session_id: str | None,
    sandbox_mode: str,
    cwd: str | None,
    timeout_sec: int,
    child_env: dict[str, str],
    recipe_flags: dict[str, Any] | None = None,
    allow_gh: bool = False,
    allow_network: bool = False,
) -> dict[str, Any]:
    result_file = attempt_dir / "result.md"
    if result_file.exists():
        result_file.unlink()
    raw_json_file = attempt_dir / "claude_output.json"
    stderr_file = attempt_dir / "stderr.log"

    flags = recipe_flags if recipe_flags is not None else get_agent_recipe("claude-code")
    # Pass the exact resolved model_id rather than a moving alias so model pinning is deterministic
    resolved_model = route.get("resolved_model_id") or route["model_id"]
    requested_model = route.get("requested_model") or resolved_model
    eff = effort or route.get("variant")
    cmd: list[str] = [binary, "-p", "--model", resolved_model]

    if eff and flags.get("has_effort", True):
        cmd.extend(["--effort", eff])

    if resume_session_id:
        cmd.extend(["--resume", resume_session_id])
    elif not keep_session and flags.get("has_no_session_persistence", True):
        cmd.append("--no-session-persistence")

    if sandbox_mode == "read-only":
        if not (flags.get("has_tools", True) and flags.get("has_disallowed_tools", True)):
            return {
                "status": "failed",
                "exit_code": 2,
                "session_id": resume_session_id,
                "usage": {"input_tokens": 0, "output_tokens": 0},
                "requested_model": requested_model,
                "resolved_model": resolved_model,
                "observed_model": None,
                "session_cleaned_up": True,
                "cleanup_verified": True,
                "error_class": "permission",
                "error": "Installed Claude Code CLI does not support --tools/--disallowedTools required for read-only enforcement.",
            }
        if flags.get("has_permission_mode", True):
            cmd.extend(["--permission-mode", "dontAsk"])
        if flags.get("has_permission_prompts", True):
            cmd.extend(["--permission-prompts", "none"])
        ro_tools = ["Read", "Grep", "Glob"]
        if allow_network:
            ro_tools.extend(["WebFetch", "WebSearch"])
        if allow_gh:
            ro_tools.append("Bash")
        disallowed = ["Edit", "Write", "NotebookEdit"]
        if not allow_gh:
            disallowed.append("Bash")
        cmd.extend(["--tools", ",".join(ro_tools), "--disallowedTools", ",".join(disallowed)])
        if (allow_gh or allow_network) and flags.get("has_allowed_tools", True):
            allowed_specs = ["Read", "Grep", "Glob"]
            if allow_network:
                allowed_specs.extend(["WebFetch", "WebSearch"])
            if allow_gh:
                allowed_specs.extend([
                    "Bash(gh issue view:*)",
                    "Bash(gh issue list:*)",
                    "Bash(gh pr view:*)",
                    "Bash(gh pr diff:*)",
                    "Bash(gh pr list:*)",
                    "Bash(gh search:*)",
                    "Bash(gh repo view:*)",
                ])
            cmd.extend(["--allowedTools", ",".join(allowed_specs)])
    elif sandbox_mode == "workspace-write":
        if flags.get("has_permission_mode", True):
            cmd.extend(["--permission-mode", "acceptEdits"])
        if flags.get("has_permission_prompts", True):
            cmd.extend(["--permission-prompts", "none"])
        if flags.get("has_allowed_tools", True):
            cmd.extend(["--allowedTools", "Read,Grep,Glob,Edit,Write,NotebookEdit,Bash"])
    else:
        cmd.append("--dangerously-skip-permissions")

    cmd.extend(["--output-format", "json"])

    code, out, err, timed_out, cleanup_verified = run_process_with_timeout(
        cmd, prompt_text, cwd, timeout_sec, env=child_env
    )
    raw_json_file.write_text(out, encoding="utf-8")
    stderr_file.write_text(err, encoding="utf-8")

    session_id = resume_session_id
    usage = {"input_tokens": 0, "output_tokens": 0}
    result_text = ""
    is_error = False
    subtype: str | None = None
    permission_denials: list[Any] = []
    observed_models: list[str] = []

    cleaned_out = strip_ansi(out).strip()
    start = cleaned_out.find("{")
    end = cleaned_out.rfind("}")
    if start != -1 and end != -1 and end >= start:
        try:
            payload = json.loads(cleaned_out[start : end + 1])
            session_id = payload.get("session_id") or session_id
            is_error = bool(payload.get("is_error", False))
            subtype = payload.get("subtype")
            permission_denials = payload.get("permission_denials") or []
            result_text = (payload.get("result") or "").strip()
            u = payload.get("usage") or {}
            usage["input_tokens"] = int(u.get("input_tokens", 0))
            usage["output_tokens"] = int(u.get("output_tokens", 0))
            m_usage = payload.get("modelUsage")
            if isinstance(m_usage, dict):
                observed_models = [str(k) for k in m_usage.keys()]
        except Exception:
            pass

    no_persist = not keep_session and not resume_session_id and flags.get("has_no_session_persistence", True)

    # Filter out background Haiku bookkeeping entries when a non-Haiku model was requested
    route_key = route.get("canonical_key") or route.get("model_id") or ""
    if "haiku" not in route_key:
        non_haiku = [m for m in observed_models if "haiku" not in m.lower()]
        observed_primary = non_haiku[0] if non_haiku else (observed_models[0] if observed_models else None)
    else:
        observed_primary = observed_models[0] if observed_models else None

    if timed_out:
        return {
            "status": "timeout",
            "exit_code": code,
            "session_id": session_id,
            "usage": usage,
            "requested_model": requested_model,
            "resolved_model": resolved_model,
            "observed_model": observed_primary,
            "observed_models": observed_models,
            "session_cleaned_up": no_persist,
            "cleanup_verified": cleanup_verified,
            "error_class": "timeout",
            "error": "Execution timed out.",
        }

    if observed_primary and not _verify_model_match(resolved_model, route["canonical_key"], observed_primary):
        return {
            "status": "failed",
            "exit_code": code or 1,
            "session_id": session_id,
            "usage": usage,
            "requested_model": requested_model,
            "resolved_model": resolved_model,
            "observed_model": observed_primary,
            "observed_models": observed_models,
            "session_cleaned_up": no_persist,
            "cleanup_verified": cleanup_verified,
            "error_class": "model_unavailable",
            "error": f"Claude model substitution detected: requested '{resolved_model}', observed '{observed_primary}'.",
        }

    # Require explicit terminal subtype == "success" and zero permission denials
    subtype_failed = subtype != "success"
    has_denials = len(permission_denials) > 0
    if code != 0 or is_error or subtype_failed or has_denials or not result_text:
        if result_text:
            (attempt_dir / "partial_result.md").write_text(result_text + "\n", encoding="utf-8")
        err_class = classify_failure(code, out, err, timed_out=False)
        if has_denials:
            err_class = "permission"
        elif code == 0 and not is_error and (subtype_failed or not result_text):
            err_class = "format"
        return {
            "status": "quota" if err_class == "quota" else "failed",
            "exit_code": code,
            "session_id": session_id,
            "usage": usage,
            "requested_model": requested_model,
            "resolved_model": resolved_model,
            "observed_model": observed_primary,
            "observed_models": observed_models,
            "permission_denials": permission_denials,
            "session_cleaned_up": no_persist,
            "cleanup_verified": cleanup_verified,
            "error_class": err_class,
            "error": (result_text if is_error else err.strip())[:500]
            or (
                f"Permission denied for {len(permission_denials)} tool call(s)."
                if has_denials
                else f"Claude terminal subtype={subtype!r} (expected 'success')."
            ),
        }

    result_file.write_text(result_text + "\n", encoding="utf-8")
    return {
        "status": "ok",
        "exit_code": 0,
        "session_id": session_id,
        "usage": usage,
        "requested_model": requested_model,
        "resolved_model": resolved_model,
        "observed_model": observed_primary,
        "observed_models": observed_models,
        "session_cleaned_up": no_persist,
        "cleanup_verified": cleanup_verified,
        "error_class": None,
    }


def invoke_opencode(
    binary: str,
    route: dict[str, Any],
    prompt_text: str,
    attempt_dir: Path,
    effort: str | None,
    keep_session: bool,
    resume_session_id: str | None,
    sandbox_mode: str,
    cwd: str | None,
    timeout_sec: int,
    child_env: dict[str, str],
    recipe_flags: dict[str, Any] | None = None,
    allow_gh: bool = False,
    allow_network: bool = False,
) -> dict[str, Any]:
    result_file = attempt_dir / "result.md"
    if result_file.exists():
        result_file.unlink()
    events_file = attempt_dir / "events.jsonl"
    stderr_file = attempt_dir / "stderr.log"

    flags = recipe_flags if recipe_flags is not None else get_agent_recipe("opencode")
    has_standalone = flags.get("has_standalone", True)
    has_auto = flags.get("has_auto", True)
    has_agent_flag = flags.get("has_agent_flag", True)
    has_session_cmd = flags.get("has_session_cmd", True)

    resolved_model = route.get("resolved_model_id") or route["model_id"]
    prov = route.get("provider")
    model_spec = f"{prov}/{resolved_model}" if prov else resolved_model
    variant = effort or route.get("variant")
    if variant:
        model_spec = f"{model_spec}#{variant}"

    env_for_proc = dict(child_env)
    cmd: list[str] = [binary, "run"]
    if has_standalone:
        cmd.append("--standalone")
    cmd.extend(["-m", model_spec])
    if flags.get("has_format_json", True):
        cmd.extend(["--format", "json"])

    if sandbox_mode == "read-only":
        ver_str = str(flags.get("version") or "")
        ver_tuple = parse_semver(ver_str)
        major_ver = ver_tuple[0] if ver_tuple is not None else 2

        if major_ver >= 2 and not has_agent_flag and flags.get("read_only_supported") is False:
            return {
                "status": "failed",
                "exit_code": 2,
                "session_id": resume_session_id,
                "usage": {"input_tokens": 0, "output_tokens": 0},
                "requested_model": route.get("requested_model") or model_spec,
                "resolved_model": model_spec,
                "observed_model": None,
                "session_cleaned_up": True,
                "cleanup_verified": True,
                "error_class": "permission",
                "error": "Installed OpenCode CLI does not support verified read-only permission restrictions.",
            }

        existing_cfg: dict[str, Any] = {}
        if env_for_proc.get("OPENCODE_CONFIG_CONTENT"):
            try:
                existing_cfg = json.loads(env_for_proc["OPENCODE_CONFIG_CONTENT"])
            except Exception:
                existing_cfg = {}

        if allow_gh:
            print(
                "WARNING: OpenCode does not support granular read-only command whitelisting. "
                "Shell execution remains strictly denied in read-only sandbox for safety; "
                "pre-fetch GitHub issues/PRs into --context-file instead.",
                file=sys.stderr,
            )

        if major_ver == 1:
            existing_cfg.pop("permissions", None)
            existing_cfg["permission"] = {
                "edit": "deny",
                "bash": "deny",
                "write": "deny",
                "patch": "deny",
                "webfetch": "allow" if allow_network else "deny",
            }
            verified_ro = isinstance(existing_cfg.get("permission"), dict) and all(
                existing_cfg["permission"].get(k) == "deny" for k in ("edit", "write", "patch", "bash")
            )
        else:
            existing_cfg.pop("permission", None)
            perm_rules: list[dict[str, str]] = [
                {"action": "edit", "resource": "*", "effect": "deny"},
                {"action": "write", "resource": "*", "effect": "deny"},
                {"action": "patch", "resource": "*", "effect": "deny"},
                {"action": "shell", "resource": "*", "effect": "deny"},
                {"action": "bash", "resource": "*", "effect": "deny"},
            ]
            if allow_network:
                perm_rules.append({"action": "webfetch", "resource": "*", "effect": "allow"})
            existing_cfg["permissions"] = perm_rules
            verified_ro = isinstance(existing_cfg.get("permissions"), list) and len(existing_cfg["permissions"]) >= 5

        if not verified_ro:
            return {
                "status": "failed",
                "exit_code": 2,
                "session_id": resume_session_id,
                "usage": {"input_tokens": 0, "output_tokens": 0},
                "requested_model": route.get("requested_model") or model_spec,
                "resolved_model": model_spec,
                "observed_model": None,
                "session_cleaned_up": True,
                "cleanup_verified": True,
                "error_class": "permission",
                "error": "Failed to verify OpenCode read-only permission configuration before execution.",
            }

        env_for_proc["OPENCODE_CONFIG_CONTENT"] = json.dumps(existing_cfg)
        if has_agent_flag:
            cmd.extend(["--agent", "plan"])
    elif has_auto:
        cmd.append("--auto")

    if resume_session_id:
        cmd.extend(["-s", resume_session_id])

    code, out, err, timed_out, cleanup_verified = run_process_with_timeout(
        cmd, prompt_text, cwd, timeout_sec, env=env_for_proc
    )
    events_file.write_text(out, encoding="utf-8")
    stderr_file.write_text(err, encoding="utf-8")

    session_id = resume_session_id
    stream_text_parts: list[str] = []
    stream_errors: list[str] = []
    usage = {"input_tokens": 0, "output_tokens": 0}
    observed_model: str | None = None
    saw_stream_finish = False

    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("sessionID") and not session_id:
            session_id = ev["sessionID"]
        ev_type = ev.get("type")
        if ev_type == "text":
            part = ev.get("part") or {}
            if part.get("text"):
                stream_text_parts.append(part["text"])
        elif ev_type in ("step_finish", "step-finish", "finish", "message.completed"):
            part = ev.get("part") or ev
            reason = str(part.get("reason") or part.get("finish") or "").lower()
            if reason not in ("error", "failed", "cancelled", "canceled", "aborted"):
                saw_stream_finish = True
        elif ev_type == "error":
            stream_errors.append(json.dumps(ev.get("error") or ev))

    export_verified = False
    export_text_parts: list[str] = []
    session_cleaned_up = False
    cleanup_warning: str | None = None

    if session_id and has_session_cmd:
        if not timed_out:
            try:
                exp_cmd = [binary, "session", "export"]
                if has_standalone:
                    exp_cmd.append("--standalone")
                exp_cmd.append(session_id)
                exp_proc = subprocess.run(
                    exp_cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    cwd=cwd,
                    env=env_for_proc,
                    timeout=20,
                )
                if exp_proc.returncode == 0 and exp_proc.stdout.strip():
                    (attempt_dir / "session_export.json").write_text(exp_proc.stdout, encoding="utf-8")
                    exp_json = json.loads(exp_proc.stdout)
                    data_root = exp_json.get("data") if isinstance(exp_json.get("data"), dict) else exp_json
                    info = data_root.get("info") or {}
                    tokens = info.get("tokens") or {}
                    usage["input_tokens"] = int(tokens.get("input", 0))
                    usage["output_tokens"] = int(tokens.get("output", 0))

                    msgs = data_root.get("messages") or []
                    # Require at least one user message in the exported session and inspect only the current turn's assistant messages
                    last_user_idx = -1
                    for idx, m in enumerate(msgs):
                        if not isinstance(m, dict):
                            continue
                        m_info = m.get("info") if isinstance(m.get("info"), dict) else m
                        if m_info.get("role") == "user" or m.get("type") == "user":
                            last_user_idx = idx

                    turn_msgs = msgs[last_user_idx + 1 :] if last_user_idx >= 0 else []
                    assistant_finishes: list[tuple[Any, str]] = []
                    for m in turn_msgs:
                        if not isinstance(m, dict):
                            continue
                        m_info = m.get("info") if isinstance(m.get("info"), dict) else m
                        if m_info.get("role") == "assistant" or m.get("type") == "assistant":
                            nested_model = m_info.get("model") if isinstance(m_info.get("model"), dict) else (
                                m.get("model") if isinstance(m.get("model"), dict) else {}
                            )
                            nested_prov = m_info.get("provider") if isinstance(m_info.get("provider"), dict) else (
                                m.get("provider") if isinstance(m.get("provider"), dict) else {}
                            )
                            raw_mid = (
                                m_info.get("modelID")
                                or nested_model.get("modelID")
                                or nested_model.get("id")
                                or nested_model.get("model")
                                or (m_info.get("model") if isinstance(m_info.get("model"), str) else None)
                            )
                            raw_pid = (
                                m_info.get("providerID")
                                or nested_model.get("providerID")
                                or nested_model.get("provider")
                                or nested_prov.get("id")
                                or (m_info.get("provider") if isinstance(m_info.get("provider"), str) else None)
                                or route["provider"]
                            )
                            raw_var = (
                                m_info.get("variant")
                                or nested_model.get("variant")
                                or nested_model.get("effort")
                                or m.get("variant")
                            )
                            if raw_mid and not observed_model:
                                obs_str = str(raw_mid)
                                if "/" not in obs_str and raw_pid:
                                    obs_str = f"{raw_pid}/{obs_str}"
                                if raw_var and "#" not in obs_str:
                                    obs_str = f"{obs_str}#{raw_var}"
                                observed_model = obs_str
                            if m_info.get("error") or m.get("error"):
                                stream_errors.append(json.dumps(m_info.get("error") or m.get("error")))
                            completed_ts = (m_info.get("time") or {}).get("completed")
                            if completed_ts is None and isinstance(m.get("time"), dict):
                                completed_ts = m["time"].get("completed")
                            finish_val = str(m_info.get("finish") or m.get("finish") or "").lower()
                            assistant_finishes.append((completed_ts, finish_val))
                            for c in m.get("parts") or m.get("content") or []:
                                if isinstance(c, dict) and c.get("type") == "text" and c.get("text"):
                                    export_text_parts.append(c["text"])

                    if last_user_idx >= 0 and assistant_finishes:
                        final_completed_ts, final_finish_val = assistant_finishes[-1]
                        if final_completed_ts is not None and final_finish_val in ("stop", "end_turn", "completed"):
                            export_verified = True
            except Exception as exp_exc:
                cleanup_warning = f"session export error: {exp_exc}"[:200]

        # Always attempt session delete for one-shot invocations, even after timeout or failure
        if not keep_session and not resume_session_id:
            try:
                del_cmd = [binary, "session", "delete"]
                if has_standalone:
                    del_cmd.append("--standalone")
                del_cmd.append(session_id)
                del_proc = subprocess.run(
                    del_cmd,
                    capture_output=True,
                    text=True,
                    cwd=cwd,
                    env=env_for_proc,
                    timeout=15,
                )
                session_cleaned_up = del_proc.returncode == 0
                if not session_cleaned_up:
                    cleanup_warning = (del_proc.stderr or del_proc.stdout or "session delete returned non-zero").strip()[:200]
            except Exception as exc:
                cleanup_warning = str(exc)[:200]

    # Retain session_id in metadata whenever --keep-session, --resume, or cleanup did not succeed
    retained_session_id = session_id if (keep_session or resume_session_id or not session_cleaned_up) else None
    final_text = "".join(stream_text_parts or export_text_parts).strip()
    saw_completion = export_verified if has_session_cmd else saw_stream_finish

    err_combined = err + ("\n" + "\n".join(stream_errors) if stream_errors else "")
    if timed_out:
        return {
            "status": "timeout",
            "exit_code": code,
            "session_id": retained_session_id,
            "usage": usage,
            "requested_model": route.get("requested_model") or model_spec,
            "resolved_model": model_spec,
            "observed_model": observed_model,
            "session_cleaned_up": session_cleaned_up,
            "cleanup_verified": cleanup_verified,
            "cleanup_warning": cleanup_warning,
            "error_class": "timeout",
            "error": "Execution timed out.",
        }

    if observed_model and not _verify_model_match(
        resolved_model,
        route["canonical_key"],
        observed_model,
        expected_provider=route["provider"],
        expected_variant=variant,
    ):
        return {
            "status": "failed",
            "exit_code": code or 1,
            "session_id": retained_session_id,
            "usage": usage,
            "requested_model": route.get("requested_model") or model_spec,
            "resolved_model": model_spec,
            "observed_model": observed_model,
            "session_cleaned_up": session_cleaned_up,
            "cleanup_verified": cleanup_verified,
            "cleanup_warning": cleanup_warning,
            "error_class": "model_unavailable",
            "error": f"OpenCode model substitution detected: requested '{model_spec}', observed '{observed_model}'.",
        }

    if code != 0 or stream_errors or not saw_completion or not final_text:
        if final_text:
            (attempt_dir / "partial_result.md").write_text(final_text + "\n", encoding="utf-8")
        err_class = classify_failure(code, out, err_combined, timed_out=False)
        if code == 0 and not stream_errors and (not saw_completion or not final_text):
            err_class = "format"
        return {
            "status": "quota" if err_class == "quota" else "failed",
            "exit_code": code,
            "session_id": retained_session_id,
            "usage": usage,
            "requested_model": route.get("requested_model") or model_spec,
            "resolved_model": model_spec,
            "observed_model": observed_model,
            "session_cleaned_up": session_cleaned_up,
            "cleanup_verified": cleanup_verified,
            "cleanup_warning": cleanup_warning,
            "error_class": err_class,
            "error": err_combined.strip()[:500] or "OpenCode produced no verified completed output.",
        }

    result_file.write_text(final_text + "\n", encoding="utf-8")
    return {
        "status": "ok",
        "exit_code": 0,
        "session_id": retained_session_id,
        "usage": usage,
        "requested_model": route.get("requested_model") or model_spec,
        "resolved_model": model_spec,
        "observed_model": observed_model,
        "session_cleaned_up": session_cleaned_up,
        "cleanup_verified": cleanup_verified,
        "cleanup_warning": cleanup_warning,
        "error_class": None,
    }


def invoke_antigravity(
    binary: str,
    route: dict[str, Any],
    prompt_text: str,
    attempt_dir: Path,
    effort: str | None,
    keep_session: bool,
    resume_session_id: str | None,
    sandbox_mode: str,
    cwd: str | None,
    timeout_sec: int,
    child_env: dict[str, str],
    recipe_flags: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Invoke Antigravity (`agy`) using stream-json stdin so arbitrary prompt lengths work safely."""
    result_file = attempt_dir / "result.md"
    if result_file.exists():
        result_file.unlink()
    events_file = attempt_dir / "events.jsonl"
    stderr_file = attempt_dir / "stderr.log"

    flags = recipe_flags if recipe_flags is not None else get_agent_recipe("antigravity")
    resolved_model = route.get("resolved_model_id") or route["model_id"]
    requested_model = route.get("requested_model") or resolved_model

    if sandbox_mode in ("read-only", "workspace-write") and not (
        flags.get("has_mode", True) and flags.get("has_sandbox", True)
    ):
        return {
            "status": "failed",
            "exit_code": 2,
            "session_id": resume_session_id,
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "requested_model": requested_model,
            "resolved_model": resolved_model,
            "observed_model": None,
            "session_cleaned_up": False,
            "cleanup_verified": True,
            "error_class": "permission",
            "error": f"Installed Antigravity CLI does not support --mode and --sandbox required for {sandbox_mode} mode.",
        }

    eff = effort or route.get("variant")
    cmd: list[str] = [
        binary,
        "--model",
        resolved_model,
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
    ]
    if flags.get("has_disable_slash_commands", True):
        cmd.append("--disable-slash-commands")
    if eff and eff in ("low", "medium", "high") and flags.get("has_effort", True):
        cmd.extend(["--effort", eff])
    if resume_session_id:
        cmd.extend(["--conversation", resume_session_id])

    if sandbox_mode == "read-only":
        cmd.extend(["--mode", "plan", "--sandbox"])
    elif sandbox_mode == "workspace-write":
        cmd.extend(["--mode", "accept-edits", "--sandbox"])
    else:
        if flags.get("has_dangerously_skip_permissions", True):
            cmd.append("--dangerously-skip-permissions")

    if flags.get("has_print_timeout", True):
        cmd.extend(["--print-timeout", f"{timeout_sec}s"])
    cmd.extend(["--print", ""])

    ndjson_input = json.dumps({"event": "user", "message": {"content": prompt_text}}, ensure_ascii=False) + "\n"
    code, out, err, timed_out, cleanup_verified = run_process_with_timeout(
        cmd, ndjson_input, cwd, timeout_sec + 10, env=child_env
    )
    events_file.write_text(out, encoding="utf-8")
    stderr_file.write_text(err, encoding="utf-8")

    session_id = resume_session_id
    usage = {"input_tokens": 0, "output_tokens": 0}
    response_text = ""
    result_status = None
    result_error = None
    observed_model: str | None = None

    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("conversation_id") and not session_id:
            session_id = ev["conversation_id"]
        if ev.get("model") and not observed_model:
            observed_model = str(ev["model"])
        if ev.get("event") == "result" and isinstance(ev.get("result"), dict):
            res_obj = ev["result"]
            session_id = res_obj.get("conversation_id") or session_id
            result_status = res_obj.get("status")
            result_error = res_obj.get("error")
            if res_obj.get("model") and not observed_model:
                observed_model = str(res_obj["model"])
            response_text = (res_obj.get("response") or "").strip()
            u = res_obj.get("usage") or {}
            usage["input_tokens"] = int(u.get("input_tokens", 0))
            usage["output_tokens"] = int(u.get("output_tokens", 0))

    cleanup_note = (
        None
        if (keep_session or resume_session_id)
        else "Antigravity CLI v0.2.2 persists conversation state under ~/.gemini/antigravity-cli and does not expose a session delete subcommand."
    )

    err_combined = f"{err}\n{result_error or ''}".strip()
    if timed_out:
        return {
            "status": "timeout",
            "exit_code": code,
            "session_id": session_id,
            "usage": usage,
            "requested_model": requested_model,
            "resolved_model": resolved_model,
            "observed_model": observed_model,
            "session_cleaned_up": False,
            "cleanup_verified": cleanup_verified,
            "cleanup_note": cleanup_note,
            "error_class": "timeout",
            "error": "Execution timed out.",
        }
    if observed_model and not _verify_model_match(resolved_model, route["canonical_key"], observed_model):
        return {
            "status": "failed",
            "exit_code": code or 1,
            "session_id": session_id,
            "usage": usage,
            "requested_model": requested_model,
            "resolved_model": resolved_model,
            "observed_model": observed_model,
            "session_cleaned_up": False,
            "cleanup_verified": cleanup_verified,
            "cleanup_note": cleanup_note,
            "error_class": "model_unavailable",
            "error": f"Antigravity model substitution detected: requested '{resolved_model}', observed '{observed_model}'.",
        }
    if code != 0 or result_status != "SUCCESS" or not response_text:
        if response_text:
            (attempt_dir / "partial_result.md").write_text(response_text + "\n", encoding="utf-8")
        err_class = classify_failure(code, out, err_combined, timed_out=False)
        if code == 0 and result_status == "SUCCESS" and not response_text:
            err_class = "format"
        return {
            "status": "quota" if err_class == "quota" else "failed",
            "exit_code": code,
            "session_id": session_id,
            "usage": usage,
            "requested_model": requested_model,
            "resolved_model": resolved_model,
            "observed_model": observed_model,
            "session_cleaned_up": False,
            "cleanup_verified": cleanup_verified,
            "cleanup_note": cleanup_note,
            "error_class": err_class,
            "error": err_combined[:500] or "Antigravity invocation failed.",
        }

    result_file.write_text(response_text + "\n", encoding="utf-8")
    return {
        "status": "ok",
        "exit_code": 0,
        "session_id": session_id,
        "usage": usage,
        "requested_model": requested_model,
        "resolved_model": resolved_model,
        "observed_model": observed_model,
        "session_cleaned_up": False,
        "cleanup_verified": cleanup_verified,
        "cleanup_note": cleanup_note,
        "error_class": None,
    }


def resolve_explicit_route(route_str: str, force_route: bool = False) -> dict[str, Any]:
    """Parse an explicit route string `agent/provider/model[#variant]` and verify against discovered catalog."""
    cleaned = route_str.strip()
    parts = [p.strip() for p in cleaned.split("/") if p.strip()]
    if len(parts) == 2:
        agent = canonical_agent_name(parts[0])
        default_prov = {
            "codex": "openai",
            "claude-code": "anthropic",
            "antigravity": "antigravity",
            "opencode": "opencode",
        }[agent]
        raw_spec = parts[1]
        variant = None
        if "#" in raw_spec:
            raw_spec, variant = raw_spec.split("#", 1)
        norm = normalize_model_route(agent, default_prov, raw_spec, variant=variant)
    elif len(parts) >= 3:
        agent = canonical_agent_name(parts[0])
        prov = parts[1]
        model_spec = "/".join(parts[2:])
        variant = None
        if "#" in model_spec:
            model_spec, variant = model_spec.split("#", 1)
        norm = normalize_model_route(agent, prov, model_spec, variant=variant)
    else:
        raise ValueError(
            f"Invalid route format '{route_str}'. Expected '<agent>/<provider>/<model>' or '<agent>/<model>'."
        )

    if is_bare_generation_prefix(norm["official_model_id"]) or is_bare_generation_prefix(norm["model_id"]):
        raise ValueError(
            f"Explicit route '{route_str}' uses bare generation prefix '{norm['official_model_id']}', "
            "not a concrete model identifier. Specify a concrete model (e.g. 'gpt-6.1-sol', 'gpt-6-astra', 'claude-fable-5-1')."
        )

    catalog = collect_all_models(agents=[norm["agent"]], refresh=False)
    agent_cat = catalog.get(norm["agent"]) or {}
    if not agent_cat.get("usable") and not force_route:
        raise ValueError(f"Agent '{norm['agent']}' for explicit route '{route_str}' is not usable.")

    for cand in agent_cat.get("routes") or []:
        if cand["route_id"].lower() == norm["route_id"].lower():
            matched = dict(cand)
            matched["requested_model"] = norm["model_id"]
            matched["in_catalog"] = True
            return matched
        if (
            cand["provider"].lower() == norm["provider"].lower()
            and (
                cand["model_id"].lower() == norm["model_id"].lower()
                or (cand.get("alias") or "").lower() == norm["model_id"].lower()
                or cand["canonical_key"].lower() == norm["canonical_key"].lower()
            )
            and (norm.get("variant") is None or cand.get("variant") == norm.get("variant"))
        ):
            matched = dict(cand)
            if norm.get("variant") and not matched.get("variant"):
                matched["variant"] = norm["variant"]
            matched["requested_model"] = norm["model_id"]
            matched["in_catalog"] = True
            return matched

    if not force_route:
        raise ValueError(
            f"Explicit route '{route_str}' was not found in discovered catalog for '{norm['agent']}'. "
            "Pass --force-route to invoke an unlisted model ID."
        )
    norm["requested_model"] = norm["model_id"]
    norm["in_catalog"] = False
    return norm


PROMPT_BLOAT_WARN_CHARS = 8000


def prepare_prompts(
    run_dir: Path,
    task_id: str,
    prompt_text: str | None,
    prompt_file: str | None,
    common_prompt_file: str | None,
    context_files: list[str] | None = None,
    references: list[str] | None = None,
    recommend_inspect: bool = False,
) -> tuple[str, Path, Path | None, str | None]:
    """Save prompts into `$SKILL_CACHE_DIR/runs/<run-id>/prompts/` and return `(combined, task_path, common_path, size_warning)`."""
    validate_safe_identifier(task_id, "task_id")
    prompts_dir = ensure_child_path(run_dir, "prompts")
    prompts_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    saved_common: Path | None = None
    common_content = ""
    if common_prompt_file:
        src_common = Path(common_prompt_file)
        common_content = src_common.read_text(encoding="utf-8")
        saved_common = prompts_dir / "common.md"
        if not saved_common.exists() or saved_common.read_text(encoding="utf-8") != common_content:
            saved_common.write_text(common_content, encoding="utf-8")

    saved_task = ensure_child_path(prompts_dir, f"task-{task_id}.md")
    if prompt_file:
        task_content = Path(prompt_file).read_text(encoding="utf-8")
    elif prompt_text is not None:
        task_content = prompt_text
    elif saved_task.is_file():
        task_content = saved_task.read_text(encoding="utf-8")
    elif not sys.stdin.isatty():
        task_content = sys.stdin.read()
    else:
        raise ValueError("No prompt supplied. Provide --prompt-file, --prompt, or pipe stdin.")

    size_warning: str | None = None
    if len(task_content) > PROMPT_BLOAT_WARN_CHARS:
        size_warning = (
            f"Task prompt is {len(task_content)} chars (>{PROMPT_BLOAT_WARN_CHARS}). "
            "Prefer writing large code blocks, logs, or reference documents to separate files "
            "and passing their paths via --context-file so the subagent can read them on demand."
        )
        print(f"WARNING: {size_warning}", file=sys.stderr)

    blocks_to_append: list[str] = []
    if context_files and "## Context File Pointers" not in task_content:
        ctx_lines = ["## Context File Pointers", "Read these local files directly for detailed task inputs or snapshots:"]
        for cf in context_files:
            if cf and str(cf).strip():
                ctx_lines.append(f"- `{str(cf).strip()}`")
        blocks_to_append.append("\n".join(ctx_lines))

    if references and "## Primary Source Pointers" not in task_content:
        ref_lines = ["## Primary Source Pointers (Issues, PRs, Discussions, Web Pages)"]
        if recommend_inspect:
            ref_lines.append(
                "**Coordinator recommendation**: Inspecting the following primary sources will materially improve "
                "your understanding of the problem and edge cases. Actively read/query them (via `gh`, web fetch, "
                "or pre-saved snapshot files) before finalizing your output:"
            )
        else:
            ref_lines.append(
                "If you need additional upstream context or design rationale, inspect these primary sources:"
            )
        for ref in references:
            if ref and str(ref).strip():
                ref_lines.append(f"- {str(ref).strip()}")
        blocks_to_append.append("\n".join(ref_lines))

    if blocks_to_append:
        task_content = f"{task_content.rstrip()}\n\n" + "\n\n".join(blocks_to_append) + "\n"

    saved_task.write_text(task_content, encoding="utf-8")
    combined = f"{common_content.rstrip()}\n\n{task_content}" if common_content.strip() else task_content
    return combined, saved_task, saved_common, size_warning


def _summarize_result_excerpt(result_file: Path | None) -> str | None:
    """Extract a concise one-line summary from a published `result.md` when `--outcome-summary` was not given."""
    if not result_file or not result_file.is_file():
        return None
    try:
        text = result_file.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return None
    if not text:
        return None
    for line in text.splitlines():
        s = line.strip().lstrip("#*- ").strip()
        if len(s) >= 8 and not s.startswith("```"):
            return (s[:197] + "...") if len(s) > 200 else s
    return None


def load_coroutine_session(run_dir: Path, resume_task_id: str) -> dict[str, Any]:
    validate_safe_identifier(resume_task_id, "resume_task_id")
    sessions_file = run_dir / "sessions.json"
    sessions = read_json_file(sessions_file) or {}
    entry = (sessions.get("tasks") or {}).get(resume_task_id)
    if not entry:
        meta_path = ensure_child_path(run_dir, resume_task_id) / "meta.json"
        meta = read_json_file(meta_path)
        if meta and meta.get("session_id"):
            if meta.get("resumable") is False:
                raise ValueError(
                    f"Task '{resume_task_id}' was executed in one-shot mode (resumable=False) and cannot be resumed."
                )
            return {
                "session_id": meta["session_id"],
                "route_id": meta.get("route_id") or f"{meta['agent']}/{meta['provider']}/{meta['model']}",
                "execution_identity": meta.get("execution_identity"),
                "agent": meta["agent"],
                "provider": meta["provider"],
                "model": meta["model"],
                "variant": meta.get("variant"),
                "effort": meta.get("effort"),
                "sandbox_mode": meta.get("sandbox_mode"),
                "cwd": meta.get("cwd"),
                "resumable": True,
            }
        raise ValueError(f"No saved session_id found for task '{resume_task_id}' in run '{run_dir.name}'.")

    if entry.get("resumable") is False:
        raise ValueError(
            f"Task '{resume_task_id}' was not saved as a resumable coroutine (resumable=False)."
        )
    return entry


def save_coroutine_session(
    run_dir: Path,
    task_id: str,
    route: dict[str, Any],
    session_id: str,
    effort: str | None,
    sandbox_mode: str,
    effective_cwd: str,
    resumable: bool,
) -> None:
    validate_safe_identifier(task_id, "task_id")
    sessions_file = run_dir / "sessions.json"
    record = {
        "task_id": task_id,
        "session_id": session_id,
        "route_id": route["route_id"],
        "execution_identity": route.get("execution_identity"),
        "agent": route["agent"],
        "provider": route["provider"],
        "model": route["model_id"],
        "resolved_model": route.get("resolved_model_id") or route["model_id"],
        "variant": route.get("variant"),
        "effort": effort or route.get("variant"),
        "sandbox_mode": sandbox_mode,
        "cwd": effective_cwd,
        "resumable": resumable,
        "updated_at": now_iso(),
    }

    def _update(current: dict[str, Any] | None) -> dict[str, Any]:
        base = current if isinstance(current, dict) else {"schema_version": SCHEMA_VERSION, "tasks": {}}
        base["schema_version"] = SCHEMA_VERSION
        base.setdefault("tasks", {})[task_id] = record
        return base

    locked_update_json(sessions_file, _update)


def _adapt_recipe_from_error(
    agent: str,
    current_flags: dict[str, Any],
    error_class: str,
    error_text: str,
) -> tuple[dict[str, Any], list[str]]:
    """Disable unsupported CLI flags detected in an unknown-option or format error message."""
    updated = dict(current_flags)
    disabled: list[str] = []
    lower_err = error_text.lower()
    flag_map = {
        "--no-daemon": "has_no_daemon",
        "--ephemeral": "has_ephemeral",
        "--skip-git-repo-check": "has_skip_git_repo_check",
        "--no-session-persistence": "has_no_session_persistence",
        "--effort": "has_effort",
        "--permission-prompts": "has_permission_prompts",
        "--standalone": "has_standalone",
        "--auto": "has_auto",
        "--disable-slash-commands": "has_disable_slash_commands",
        "--print-timeout": "has_print_timeout",
        "--json": "has_json",
        "--format": "has_format_json",
    }
    for cli_flag, key in flag_map.items():
        if cli_flag in lower_err and updated.get(key, True):
            updated[key] = False
            disabled.append(cli_flag)
    return updated, disabled


def _perform_self_repair(
    agent: str,
    binary: str | None,
    version: str | None,
    error_class: str,
    error_text: str,
    prev_flags: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run bounded self-repair on `no_option` or `format` failures and return `(adapted_flags, maintenance_notice)`."""
    repaired_probe = False
    repaired_models = False
    fresh_flags: dict[str, Any] = get_agent_recipe(agent, version)
    try:
        if binary:
            fresh_flags = cache_cli_help_and_recipe(agent, binary, version)
            repaired_probe = True
    except Exception:
        pass

    adapted_flags, disabled_flags = _adapt_recipe_from_error(agent, fresh_flags, error_class, error_text)
    recipe_changed = bool(disabled_flags) or (prev_flags is not None and adapted_flags != prev_flags)

    if agent == "codex" and binary:
        try:
            repair_codex_models(binary)
            repaired_models = True
        except Exception:
            pass

    recipe_file = f"references/agents/{agent}.md"
    notice = {
        "error_class": error_class,
        "agent": agent,
        "root_cause_excerpt": error_text[:240],
        "disabled_flags": disabled_flags,
        "recipe_changed": recipe_changed,
        "repaired_cli_help_cache": repaired_probe,
        "repaired_model_schema_cache": repaired_models,
        "recommended_skill_file_to_update": recipe_file,
        "message": (
            f"Self-repair refreshed cached --help/recipe for '{agent}' after '{error_class}' "
            f"(disabled flags: {disabled_flags or 'none'}, recipe_changed={recipe_changed}). "
            f"If the CLI permanently changed its flags or JSON output, update {recipe_file}."
        ),
    }
    return adapted_flags, notice


def _write_early_failure_contract(
    run_dir: Path,
    task_dir: Path,
    run_id: str,
    task_id: str,
    route_str: str | None,
    sandbox_mode: str,
    effective_cwd: str,
    exc: Exception,
    parent_instance: str = "root",
    parent_task_id: str | None = None,
    task_summary: str | None = None,
) -> dict[str, Any]:
    """Guarantee that `<task_dir>/meta.json` and `manifest.jsonl` are written even on early preparation failures."""
    ts_now = now_iso()
    err_class, exit_code = classify_exception(exc)
    agent_guess = "codex"
    prov_guess = "unknown"
    model_guess = route_str or "auto"
    if route_str and "/" in route_str:
        parts = [p.strip() for p in route_str.split("/") if p.strip()]
        try:
            agent_guess = canonical_agent_name(parts[0])
        except Exception:
            agent_guess = "codex"
        if len(parts) >= 3:
            prov_guess = parts[1]
            model_guess = "/".join(parts[2:])
        elif len(parts) == 2:
            model_guess = parts[1]

    off_agent = official_agent_name(agent_guess)
    off_model = resolve_official_model_id(model_guess) if model_guess != "auto" else "auto"

    meta_payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "task_id": task_id,
        "parent_instance": parent_instance,
        "parent_task_id": parent_task_id,
        "attempt": 0,
        "route_id": route_str or "auto",
        "agent": agent_guess,
        "official_agent": off_agent,
        "provider": prov_guess,
        "model": model_guess,
        "official_model_id": off_model,
        "variant": None,
        "effort": None,
        "sandbox_mode": sandbox_mode,
        "cwd": effective_cwd,
        "requested_model": model_guess,
        "resolved_model": None,
        "observed_model": None,
        "status": "quota" if err_class == "quota" else "failed",
        "exit_code": exit_code,
        "started_at": ts_now,
        "ended_at": ts_now,
        "duration_ms": 0,
        "session_id": None,
        "resumable": False,
        "session_cleaned_up": True,
        "cleanup_note": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "error_class": err_class,
        "error": str(exc)[:500],
        "maintenance_notice": None,
        "selection_rationale": "failed during task preparation/validation",
        "task_summary": task_summary,
        "outcome_summary": f"Failed during task preparation ({err_class}): {str(exc)[:180]}",
        "significant": False,
        "attempt_dir": None,
        "result_file": None,
        "lease_mode": None,
    }
    atomic_write_json(task_dir / "meta.json", meta_payload)
    append_manifest_event(
        run_dir,
        {
            "event": "task_finished",
            "timestamp": ts_now,
            "run_id": run_id,
            "task_id": task_id,
            "parent_instance": parent_instance,
            "parent_task_id": parent_task_id,
            "attempt": 0,
            "route_id": meta_payload["route_id"],
            "official_agent": off_agent,
            "official_model_id": off_model,
            "prompt_sha256": None,
            "started_at": ts_now,
            "ended_at": ts_now,
            "duration_ms": 0,
            "status": meta_payload["status"],
            "exit_code": exit_code,
            "error_class": err_class,
            "error": meta_payload["error"],
            "usage": meta_payload["usage"],
            "task_summary": task_summary,
            "outcome_summary": meta_payload["outcome_summary"],
            "significant": False,
            "selection_rationale": meta_payload["selection_rationale"],
        },
    )
    return meta_payload


def execute_task(
    run_id: str,
    task_id: str,
    route_str: str | None = None,
    task_category: str = "general",
    prompt_text: str | None = None,
    prompt_file: str | None = None,
    common_prompt_file: str | None = None,
    effort: str | None = None,
    keep_session: bool = False,
    resume_session_id: str | None = None,
    resume_task_id: str | None = None,
    sandbox_mode: str | None = None,
    cwd: str | None = None,
    timeout_sec: int = DEFAULT_TIMEOUT_SECONDS,
    queue_timeout_sec: int = DEFAULT_QUEUE_TIMEOUT_SECONDS,
    parent_instance: str | None = None,
    parent_task_id: str | None = None,
    min_context: int | None = None,
    require_modalities: list[str] | None = None,
    require_vision: bool = False,
    speed: str = "normal",
    allow_estimated_capabilities: bool = False,
    allow_micro_quota: bool = False,
    min_headroom: float | None = None,
    exclude_vendors: list[str] | None = None,
    exclude_agents: list[str] | None = None,
    no_fallback: bool = False,
    force_route: bool = False,
    dry_run: bool = False,
    context_files: list[str] | None = None,
    references: list[str] | None = None,
    recommend_inspect: bool = False,
    allow_gh: bool = False,
    allow_network: bool = False,
    task_summary: str | None = None,
    outcome_summary: str | None = None,
    significant: bool | None = None,
) -> dict[str, Any]:
    eff_run_id = validate_safe_identifier(
        run_id or os.environ.get("CROSS_SUBAGENTS_RUN_ID") or f"run-{int(time.time())}",
        "run_id",
    )
    eff_task_id = validate_safe_identifier(task_id, "task_id")
    eff_parent = validate_safe_identifier(
        parent_instance or os.environ.get("CROSS_SUBAGENTS_PARENT_INSTANCE") or "root",
        "parent",
    )
    raw_parent_task = parent_task_id or os.environ.get("CROSS_SUBAGENTS_PARENT_TASK_ID")
    eff_parent_task = (
        validate_safe_identifier(raw_parent_task, "parent_task_id") if raw_parent_task else None
    )

    run_dir = get_run_dir(eff_run_id)
    task_dir = ensure_child_path(run_dir, eff_task_id)
    task_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    published_result_file = task_dir / "result.md"

    caller_supplied_sandbox = sandbox_mode
    effective_sandbox = sandbox_mode or "workspace-write"
    effective_cwd = str(Path(cwd).resolve()) if cwd else str(Path.cwd().resolve())

    eff_req_mods = [m.strip().lower() for m in (require_modalities or []) if m.strip()]
    if require_vision and "image" not in eff_req_mods:
        eff_req_mods.append("image")

    # Serialize concurrent invocations of the same (run_id, task_id)
    with file_lock(task_dir / "_task.lock", timeout=30.0):
        if published_result_file.exists() and not dry_run:
            try:
                published_result_file.unlink()
            except OSError:
                pass

        try:
            if resume_task_id:
                prev = load_coroutine_session(run_dir, resume_task_id)
                resume_session_id = prev["session_id"]
                route_str = prev["route_id"]
                effort = effort or prev.get("effort")
                # Honor explicit caller --sandbox override; only inherit saved sandbox_mode when caller omitted --sandbox
                if caller_supplied_sandbox is not None:
                    effective_sandbox = caller_supplied_sandbox
                elif prev.get("sandbox_mode"):
                    effective_sandbox = prev["sandbox_mode"]
                if cwd is None and prev.get("cwd"):
                    effective_cwd = str(Path(prev["cwd"]).resolve())
                keep_session = True
            elif resume_session_id and (not route_str or route_str.lower() == "auto"):
                raise ValueError(
                    "When resuming a raw --resume <session_id> without --resume-task, you must specify --route <agent>/<provider>/<model>."
                )

            eff_prompt_text = prompt_text
            if dry_run and eff_prompt_text is None and prompt_file is None and sys.stdin.isatty():
                eff_prompt_text = "(dry-run)"

            combined_prompt, saved_task_path, saved_common_path, prompt_size_warning = prepare_prompts(
                run_dir,
                eff_task_id,
                eff_prompt_text,
                prompt_file,
                common_prompt_file,
                context_files=context_files,
                references=references,
                recommend_inspect=recommend_inspect,
            )
            prompt_sha256 = hashlib.sha256(combined_prompt.encode("utf-8")).hexdigest()

            task_norm = task_category.strip().lower().replace("-", "_")
            route_chain: list[dict[str, Any]] = []
            selection_rationale = ""

            if route_str and route_str.lower() != "auto":
                explicit_route = resolve_explicit_route(route_str, force_route=force_route)
                if not force_route:
                    pins = load_session_pins(run_id=eff_run_id)
                    eff_pin_agent = pins.get("pin_agent")
                    eff_pin_provider = (pins.get("pin_provider") or "").strip().lower() or None
                    eff_pin_model = (pins.get("pin_model") or "").strip().lower() or None
                    excl_agents_set = {canonical_agent_name(a) for a in (exclude_agents or [])}
                    excl_vendors_set = {v.strip().lower() for v in (exclude_vendors or [])}
                    req_mods_set = set(eff_req_mods)
                    quota_state = collect_quota(refresh=False)

                    ok, reason, q_eval, traits = check_route_eligibility(
                        route=explicit_route,
                        task_norm=task_norm,
                        quota_state=quota_state,
                        min_context=min_context,
                        require_modalities=req_mods_set,
                        allow_estimated_capabilities=allow_estimated_capabilities,
                        allow_micro_quota=allow_micro_quota,
                        min_headroom=min_headroom,
                        eff_pin_agent=eff_pin_agent,
                        eff_pin_provider=eff_pin_provider,
                        eff_pin_model=eff_pin_model,
                        excl_agents=excl_agents_set,
                        excl_vendors=excl_vendors_set,
                        quality_floor=None,
                        is_explicit_route=True,
                    )
                    if not ok:
                        raise ValueError(
                            f"Explicit route '{explicit_route['route_id']}' failed eligibility check ({reason}). "
                            "Pass --force-route to bypass."
                        )
                    explicit_route = {
                        **explicit_route,
                        "cost_index": traits["cost_index"],
                        "billing_mode": traits["billing_mode"],
                        "cost_estimated": traits["cost_estimated"],
                        "tokens_per_sec": traits["tokens_per_sec"],
                        "supports_vision": traits["supports_vision"],
                        "quota": q_eval,
                    }
                else:
                    traits = evaluate_route_traits(explicit_route, task_norm)
                    explicit_route = {
                        **explicit_route,
                        "cost_index": traits["cost_index"],
                        "billing_mode": traits["billing_mode"],
                        "cost_estimated": traits["cost_estimated"],
                        "tokens_per_sec": traits["tokens_per_sec"],
                        "supports_vision": traits["supports_vision"],
                    }

                selection_rationale = f"{explicit_route['route_id']} selected explicitly by caller"
                route_chain.append(explicit_route)
                if not no_fallback and not resume_session_id:
                    sel = select_routes(
                        task=task_category,
                        min_context=min_context,
                        require_modalities=eff_req_mods,
                        require_vision=require_vision,
                        speed=speed,
                        allow_estimated_capabilities=allow_estimated_capabilities,
                        allow_micro_quota=allow_micro_quota,
                        min_headroom=min_headroom,
                        exclude_vendors=exclude_vendors,
                        exclude_agents=exclude_agents,
                        run_id=eff_run_id,
                    )
                    exp_buckets = set(explicit_route.get("applicable_buckets") or [explicit_route["quota_bucket"]])
                    for fb in [sel.get("selected")] + (sel.get("fallbacks") or []):
                        if not fb or fb["route_id"] == explicit_route["route_id"]:
                            continue
                        fb_copy = dict(fb)
                        fb_buckets = set(fb_copy.get("applicable_buckets") or [fb_copy["quota_bucket"]])
                        fb_copy["same_bucket_as_primary"] = bool(fb_buckets & exp_buckets)
                        route_chain.append(fb_copy)
            else:
                sel = select_routes(
                    task=task_category,
                    min_context=min_context,
                    require_modalities=eff_req_mods,
                    require_vision=require_vision,
                    speed=speed,
                    allow_estimated_capabilities=allow_estimated_capabilities,
                    allow_micro_quota=allow_micro_quota,
                    min_headroom=min_headroom,
                    exclude_vendors=exclude_vendors,
                    exclude_agents=exclude_agents,
                    run_id=eff_run_id,
                )
                if sel["status"] != "ok" or not sel.get("selected"):
                    raise RuntimeError(f"No eligible route found for task '{task_category}'.")
                selection_rationale = sel["rationale"]
                route_chain.append(sel["selected"])
                if not no_fallback and not resume_session_id:
                    route_chain.extend(sel.get("fallbacks") or [])
        except Exception as prep_exc:
            return _write_early_failure_contract(
                run_dir=run_dir,
                task_dir=task_dir,
                run_id=eff_run_id,
                task_id=eff_task_id,
                route_str=route_str,
                sandbox_mode=effective_sandbox,
                effective_cwd=effective_cwd,
                exc=prep_exc,
                parent_instance=eff_parent,
                parent_task_id=eff_parent_task,
                task_summary=task_summary,
            )

        if dry_run:
            planned = route_chain[0]
            off_agent_dry = planned.get("official_agent") or official_agent_name(planned["agent"])
            off_model_dry = planned.get("official_model_id") or resolve_official_model_id(
                planned.get("resolved_model_id") or planned["model_id"]
            )
            return {
                "status": "dry_run",
                "run_id": eff_run_id,
                "task_id": eff_task_id,
                "parent_instance": eff_parent,
                "parent_task_id": eff_parent_task,
                "selected_route": planned["route_id"],
                "official_agent": off_agent_dry,
                "official_model_id": off_model_dry,
                "execution_identity": planned.get("execution_identity"),
                "billing_mode": planned.get("billing_mode") or classify_route_billing(planned),
                "estimated_cost_index": planned.get("cost_index", 0.20),
                "cost_estimated": planned.get("cost_estimated", True),
                "tokens_per_sec": planned.get("tokens_per_sec"),
                "supports_vision": planned.get("supports_vision"),
                "rationale": selection_rationale,
                "fallbacks": [r["route_id"] for r in route_chain[1:]],
                "prompt_files": {
                    "common": str(saved_common_path) if saved_common_path else None,
                    "task": str(saved_task_path),
                    "sha256": prompt_sha256,
                },
                "prompt_size_warning": prompt_size_warning,
                "sandbox_mode": effective_sandbox,
                "cwd": effective_cwd,
                "keep_session": keep_session,
                "resume_session_id": resume_session_id,
            }

        inst_id = make_bounded_instance_id("task", eff_task_id)
        lease = acquire_lease(run_id=eff_run_id, instance_id=inst_id, parent=eff_parent, pid=os.getpid())
        if lease.get("status") != "acquired" and queue_timeout_sec > 0 and eff_parent == "root":
            wait_deadline = time.monotonic() + min(queue_timeout_sec, timeout_sec)
            while time.monotonic() < wait_deadline and lease.get("status") != "acquired":
                time.sleep(2.0)
                lease = acquire_lease(run_id=eff_run_id, instance_id=inst_id, parent=eff_parent, pid=os.getpid())

        if lease.get("status") != "acquired":
            ts_now = now_iso()
            planned = route_chain[0]
            off_agent_def = planned.get("official_agent") or official_agent_name(planned["agent"])
            off_model_def = planned.get("official_model_id") or resolve_official_model_id(
                planned.get("resolved_model_id") or planned["model_id"]
            )
            deferred_meta = {
                "schema_version": SCHEMA_VERSION,
                "run_id": eff_run_id,
                "task_id": eff_task_id,
                "parent_instance": eff_parent,
                "parent_task_id": eff_parent_task,
                "attempt": 0,
                "route_id": planned["route_id"],
                "execution_identity": planned.get("execution_identity"),
                "agent": planned["agent"],
                "official_agent": off_agent_def,
                "provider": planned["provider"],
                "model": planned["model_id"],
                "official_model_id": off_model_def,
                "variant": planned.get("variant"),
                "effort": effort or planned.get("variant"),
                "sandbox_mode": effective_sandbox,
                "cwd": effective_cwd,
                "requested_model": planned.get("requested_model") or planned["model_id"],
                "resolved_model": planned.get("resolved_model_id") or planned["model_id"],
                "observed_model": None,
                "status": "deferred_capacity",
                "exit_code": 75,
                "started_at": ts_now,
                "ended_at": ts_now,
                "duration_ms": 0,
                "session_id": resume_session_id,
                "resumable": False,
                "session_cleaned_up": True,
                "cleanup_note": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
                "error_class": "capacity",
                "error": lease.get("reason") or "Live instance capacity reached; deferring to sequential execution.",
                "maintenance_notice": None,
                "selection_rationale": selection_rationale,
                "task_summary": task_summary or f"{task_category} ({eff_task_id})",
                "outcome_summary": "Deferred due to live instance capacity cap.",
                "significant": False,
                "prompt_size_warning": prompt_size_warning,
                "attempt_dir": None,
                "result_file": None,
                "lease_mode": lease.get("mode", "sequential_inline"),
            }
            atomic_write_json(task_dir / "meta.json", deferred_meta)
            append_manifest_event(
                run_dir,
                {
                    "event": "task_deferred_capacity",
                    "timestamp": ts_now,
                    "run_id": eff_run_id,
                    "task_id": eff_task_id,
                    "parent_instance": eff_parent,
                    "parent_task_id": eff_parent_task,
                    "route_id": planned["route_id"],
                    "official_agent": off_agent_def,
                    "official_model_id": off_model_def,
                    "prompt_sha256": prompt_sha256,
                    "status": "deferred_capacity",
                    "reason": deferred_meta["error"],
                },
            )
            return deferred_meta

        hb_stop = threading.Event()

        def _heartbeat_loop() -> None:
            while not hb_stop.wait(HEARTBEAT_INTERVAL_SECONDS):
                try:
                    heartbeat_lease(eff_run_id, lease["instance_id"])
                except Exception:
                    pass

        hb_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
        hb_thread.start()

        try:
            probe_data = probe_all(refresh=False)
            attempts_history: list[dict[str, Any]] = []
            exhausted_buckets: set[str] = set()

            child_env = dict(os.environ)
            child_env["CROSS_SUBAGENTS_RUN_ID"] = eff_run_id
            child_env["CROSS_SUBAGENTS_PARENT_INSTANCE"] = lease["instance_id"]
            child_env["CROSS_SUBAGENTS_PARENT_TASK_ID"] = eff_task_id

            for route_idx, route in enumerate(route_chain):
                route_buckets = set(
                    route.get("applicable_buckets")
                    or infer_applicable_quota_buckets(
                        route["agent"], route["provider"], route["canonical_key"], route["model_id"]
                    )
                )
                if exhausted_buckets and (route_buckets & exhausted_buckets):
                    continue

                agent = route["agent"]
                agent_info = (probe_data.get("agents") or {}).get(agent) or {}
                binary = agent_info.get("binary")
                agent_version = agent_info.get("version")
                current_recipe_flags = agent_info.get("recipe_flags") or get_agent_recipe(agent, agent_version)

                # Each route in route_chain may run at most 1 initial attempt + 1 adapted recipe retry on `no_option`
                for recipe_retry_idx in range(2):
                    attempt_idx, attempt_dir = allocate_next_attempt_dir(task_dir)

                    if published_result_file.exists():
                        try:
                            published_result_file.unlink()
                        except OSError:
                            pass

                    started_at = now_iso()
                    t0 = time.monotonic()

                    if route_idx == 0 and recipe_retry_idx == 0:
                        rationale_for_attempt = selection_rationale
                    elif recipe_retry_idx > 0:
                        rationale_for_attempt = (
                            f"adapted recipe retry on {route['route_id']} after {attempts_history[-1]['error_class']}"
                        )
                    else:
                        rationale_for_attempt = (
                            f"fallback after {attempts_history[-1]['route_id']} ({attempts_history[-1]['error_class']})"
                        )

                    append_manifest_event(
                        run_dir,
                        {
                            "event": "task_started",
                            "timestamp": started_at,
                            "run_id": eff_run_id,
                            "task_id": eff_task_id,
                            "parent_instance": eff_parent,
                            "parent_task_id": eff_parent_task,
                            "attempt": attempt_idx,
                            "route_id": route["route_id"],
                            "prompt_sha256": prompt_sha256,
                            "started_at": started_at,
                            "selection_rationale": rationale_for_attempt,
                        },
                    )

                    try:
                        if not agent_info.get("usable") or not binary:
                            res: dict[str, Any] = {
                                "status": "failed",
                                "exit_code": 127,
                                "session_id": None,
                                "usage": {"input_tokens": 0, "output_tokens": 0},
                                "requested_model": route.get("requested_model") or route["model_id"],
                                "resolved_model": route.get("resolved_model_id") or route["model_id"],
                                "observed_model": None,
                                "session_cleaned_up": True,
                                "cleanup_verified": True,
                                "error_class": "not_installed",
                                "error": f"Agent '{agent}' is not installed or not usable.",
                            }
                        elif agent == "codex":
                            res = invoke_codex(
                                binary,
                                route,
                                combined_prompt,
                                attempt_dir,
                                effort,
                                keep_session,
                                resume_session_id,
                                effective_sandbox,
                                effective_cwd,
                                timeout_sec,
                                child_env,
                                recipe_flags=current_recipe_flags,
                                allow_gh=allow_gh,
                                allow_network=allow_network,
                            )
                        elif agent == "claude-code":
                            res = invoke_claude_code(
                                binary,
                                route,
                                combined_prompt,
                                attempt_dir,
                                effort,
                                keep_session,
                                resume_session_id,
                                effective_sandbox,
                                effective_cwd,
                                timeout_sec,
                                child_env,
                                recipe_flags=current_recipe_flags,
                                allow_gh=allow_gh,
                                allow_network=allow_network,
                            )
                        elif agent == "opencode":
                            res = invoke_opencode(
                                binary,
                                route,
                                combined_prompt,
                                attempt_dir,
                                effort,
                                keep_session,
                                resume_session_id,
                                effective_sandbox,
                                effective_cwd,
                                timeout_sec,
                                child_env,
                                recipe_flags=current_recipe_flags,
                                allow_gh=allow_gh,
                                allow_network=allow_network,
                            )
                        elif agent == "antigravity":
                            res = invoke_antigravity(
                                binary,
                                route,
                                combined_prompt,
                                attempt_dir,
                                effort,
                                keep_session,
                                resume_session_id,
                                effective_sandbox,
                                effective_cwd,
                                timeout_sec,
                                child_env,
                                recipe_flags=current_recipe_flags,
                            )
                        else:
                            res = {
                                "status": "failed",
                                "exit_code": 2,
                                "session_id": None,
                                "usage": {"input_tokens": 0, "output_tokens": 0},
                                "requested_model": route["model_id"],
                                "resolved_model": route["model_id"],
                                "observed_model": None,
                                "session_cleaned_up": True,
                                "cleanup_verified": True,
                                "error_class": "not_installed",
                                "error": f"Unsupported agent '{agent}'",
                            }
                    except Exception as invoke_exc:
                        err_cls, ex_code = classify_exception(invoke_exc)
                        res = {
                            "status": "failed",
                            "exit_code": ex_code,
                            "session_id": resume_session_id,
                            "usage": {"input_tokens": 0, "output_tokens": 0},
                            "requested_model": route.get("requested_model") or route["model_id"],
                            "resolved_model": route.get("resolved_model_id") or route["model_id"],
                            "observed_model": None,
                            "session_cleaned_up": False,
                            "cleanup_verified": True,
                            "error_class": err_cls,
                            "error": f"Subprocess invocation error: {invoke_exc}",
                        }

                    ended_at = now_iso()
                    duration_ms = int((time.monotonic() - t0) * 1000)

                    maintenance_notice: dict[str, Any] | None = None
                    if res.get("error_class") in ("quota", "auth"):
                        if res.get("error_class") == "quota":
                            record_quota_observation(route["quota_bucket"], res.get("error") or "Rate limit reached")
                        exhausted_buckets.add(route["quota_bucket"])
                    elif res.get("error_class") in ("no_option", "format"):
                        prev_recipe_flags = dict(current_recipe_flags)
                        current_recipe_flags, maintenance_notice = _perform_self_repair(
                            agent,
                            binary,
                            agent_version,
                            str(res.get("error_class")),
                            str(res.get("error") or ""),
                            prev_flags=prev_recipe_flags,
                        )

                    attempt_result_file = attempt_dir / "result.md"
                    if res["status"] == "ok" and attempt_result_file.is_file():
                        atomic_publish_file(attempt_result_file, published_result_file)

                    resumable_flag = bool(
                        res["status"] == "ok" and res.get("session_id") and (keep_session or resume_session_id)
                    )
                    if resumable_flag and res.get("session_id"):
                        save_coroutine_session(
                            run_dir,
                            eff_task_id,
                            route,
                            str(res["session_id"]),
                            effort=effort,
                            sandbox_mode=effective_sandbox,
                            effective_cwd=effective_cwd,
                            resumable=True,
                        )

                    off_agent = route.get("official_agent") or official_agent_name(agent)
                    raw_model_for_norm = (
                        res.get("observed_model")
                        or res.get("resolved_model")
                        or route.get("official_model_id")
                        or route.get("resolved_model_id")
                        or route["model_id"]
                    )
                    off_model_id = resolve_official_model_id(str(raw_model_for_norm))

                    eff_task_summary = task_summary or f"{task_category} ({eff_task_id})"
                    if outcome_summary:
                        eff_outcome_summary = outcome_summary
                    elif res["status"] == "ok":
                        eff_outcome_summary = (
                            _summarize_result_excerpt(published_result_file)
                            or f"Completed {task_category} task successfully."
                        )
                    else:
                        eff_outcome_summary = f"Failed ({res.get('error_class')}): {(res.get('error') or '')[:160]}"

                    eff_significant = (
                        bool(significant)
                        if significant is not None
                        else (res["status"] == "ok")
                    )

                    meta_payload: dict[str, Any] = {
                        "schema_version": SCHEMA_VERSION,
                        "run_id": eff_run_id,
                        "task_id": eff_task_id,
                        "parent_instance": eff_parent,
                        "parent_task_id": eff_parent_task,
                        "attempt": attempt_idx,
                        "route_id": route["route_id"],
                        "execution_identity": route.get("execution_identity"),
                        "agent": agent,
                        "official_agent": off_agent,
                        "provider": route["provider"],
                        "model": route["model_id"],
                        "official_model_id": off_model_id,
                        "variant": route.get("variant"),
                        "effort": effort or route.get("variant"),
                        "sandbox_mode": effective_sandbox,
                        "cwd": effective_cwd,
                        "requested_model": res.get("requested_model") or route.get("requested_model") or route["model_id"],
                        "resolved_model": res.get("resolved_model") or route.get("resolved_model_id") or route["model_id"],
                        "observed_model": res.get("observed_model"),
                        "status": res["status"],
                        "exit_code": res["exit_code"],
                        "started_at": started_at,
                        "ended_at": ended_at,
                        "duration_ms": duration_ms,
                        "session_id": res.get("session_id"),
                        "resumable": resumable_flag,
                        "session_cleaned_up": res.get("session_cleaned_up"),
                        "cleanup_verified": res.get("cleanup_verified", True),
                        "cleanup_note": res.get("cleanup_note") or res.get("cleanup_warning"),
                        "usage": res.get("usage") or {"input_tokens": 0, "output_tokens": 0},
                        "error_class": res.get("error_class"),
                        "error": res.get("error"),
                        "maintenance_notice": maintenance_notice,
                        "selection_rationale": rationale_for_attempt,
                        "task_summary": eff_task_summary,
                        "outcome_summary": eff_outcome_summary,
                        "significant": eff_significant,
                        "prompt_size_warning": prompt_size_warning,
                        "attempt_dir": str(attempt_dir),
                        "result_file": str(published_result_file)
                        if (res["status"] == "ok" and published_result_file.is_file())
                        else None,
                        "lease_mode": lease.get("mode"),
                    }
                    atomic_write_json(task_dir / "meta.json", meta_payload)

                    manifest_event = {
                        "event": "task_finished",
                        "timestamp": ended_at,
                        "run_id": eff_run_id,
                        "task_id": eff_task_id,
                        "parent_instance": eff_parent,
                        "parent_task_id": eff_parent_task,
                        "attempt": attempt_idx,
                        "route_id": route["route_id"],
                        "official_agent": off_agent,
                        "official_model_id": off_model_id,
                        "prompt_sha256": prompt_sha256,
                        "started_at": started_at,
                        "ended_at": ended_at,
                        "duration_ms": duration_ms,
                        "status": res["status"],
                        "exit_code": res["exit_code"],
                        "error_class": res.get("error_class"),
                        "usage": meta_payload["usage"],
                        "task_summary": eff_task_summary,
                        "outcome_summary": eff_outcome_summary,
                        "significant": eff_significant,
                        "maintenance_notice": maintenance_notice,
                        "selection_rationale": rationale_for_attempt,
                    }
                    append_manifest_event(run_dir, manifest_event)
                    attempts_history.append(meta_payload)

                    if res["status"] == "ok":
                        if len(attempts_history) > 1:
                            meta_payload["prior_failed_attempts"] = [
                                {"route_id": a["route_id"], "error_class": a["error_class"]}
                                for a in attempts_history[:-1]
                            ]
                            atomic_write_json(task_dir / "meta.json", meta_payload)
                        return meta_payload

                    # If a timed-out writer process could not be confirmed dead, refuse to start another writer
                    if res.get("error_class") == "timeout" and not res.get("cleanup_verified", True) and effective_sandbox != "read-only":
                        return meta_payload

                    # Retry the same route once if self-repair adapted recipe flags on `no_option` or `format`
                    if (
                        recipe_retry_idx == 0
                        and res.get("error_class") in ("no_option", "format")
                        and maintenance_notice
                        and maintenance_notice.get("recipe_changed")
                    ):
                        continue
                    break

                # Never switch models mid-coroutine when resuming an existing session
                if resume_session_id or no_fallback:
                    return attempts_history[-1]

                if attempts_history[-1].get("error_class") in (
                    "quota",
                    "auth",
                    "model_unavailable",
                    "not_installed",
                    "no_option",
                    "timeout",
                    "format",
                ):
                    continue
                break

            return attempts_history[-1]
        except Exception as exec_exc:
            return _write_early_failure_contract(
                run_dir=run_dir,
                task_dir=task_dir,
                run_id=eff_run_id,
                task_id=eff_task_id,
                route_str=route_chain[0]["route_id"] if route_chain else route_str,
                sandbox_mode=effective_sandbox,
                effective_cwd=effective_cwd,
                exc=exec_exc,
                parent_instance=eff_parent,
                parent_task_id=eff_parent_task,
                task_summary=task_summary,
            )
        finally:
            hb_stop.set()
            if lease.get("instance_id"):
                release_lease(eff_run_id, lease["instance_id"])


def mark_task_outcome(
    run_id: str,
    task_id: str,
    significant: bool | None = None,
    task_summary: str | None = None,
    outcome_summary: str | None = None,
) -> dict[str, Any]:
    """Update a completed task's significance flag (`Assisted-by` eligibility) and task/outcome summaries."""
    run_dir = get_run_dir(run_id)
    eff_task_id = validate_safe_identifier(task_id, "task_id")
    task_dir = ensure_child_path(run_dir, eff_task_id)
    meta_path = task_dir / "meta.json"
    if not meta_path.is_file():
        raise FileNotFoundError(f"Task metadata not found at {meta_path}")

    def _update(cur: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(cur, dict):
            raise ValueError(f"Corrupt meta.json at {meta_path}")
        if significant is not None:
            cur["significant"] = bool(significant)
        if task_summary is not None:
            cur["task_summary"] = task_summary
        if outcome_summary is not None:
            cur["outcome_summary"] = outcome_summary
        cur["updated_at"] = now_iso()
        return cur

    updated = locked_update_json(meta_path, _update)
    append_manifest_event(
        run_dir,
        {
            "event": "task_outcome_marked",
            "timestamp": now_iso(),
            "run_id": run_id,
            "task_id": eff_task_id,
            "official_agent": updated.get("official_agent"),
            "official_model_id": updated.get("official_model_id"),
            "significant": updated.get("significant"),
            "task_summary": updated.get("task_summary"),
            "outcome_summary": updated.get("outcome_summary"),
        },
    )
    return updated


def record_native_task(
    run_id: str,
    task_id: str,
    agent: str,
    model: str,
    parent_task_id: str | None = None,
    significant: bool = True,
    task_summary: str | None = None,
    outcome_summary: str | None = None,
    result_text: str | None = None,
    result_file: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Register a native host subagent execution into the run directory for call tree reporting."""
    run_dir = get_run_dir(run_id)
    eff_task_id = validate_safe_identifier(task_id, "task_id")
    eff_parent_task = validate_safe_identifier(parent_task_id, "parent_task_id") if parent_task_id else None

    canon_agent = canonical_agent_name(agent)
    off_agent = official_agent_name(canon_agent)
    off_model, eff_effort, _ = normalize_official_model_id(model)

    if not off_model or not re.match(r"^[a-z0-9][a-z0-9.-]*$", off_model):
        raise ValueError(
            f"Invalid normalized official model identifier '{off_model}' from input '{model}'. "
            "Must be a non-empty identifier starting with alphanumeric and containing only lowercase letters, digits, '.', and '-'."
        )

    if is_bare_generation_prefix(model) or is_bare_generation_prefix(off_model):
        raise ValueError(
            f"Cannot record native task with bare generation prefix '{model}'. "
            "Specify a concrete official model identifier (e.g. 'gpt-6.1-sol', 'gemini-2.5-pro')."
        )

    if result_file:
        rf_path = Path(result_file)
        if not rf_path.is_file():
            raise FileNotFoundError(f"Specified result file does not exist: {result_file}")
        result_content = rf_path.read_text(encoding="utf-8", errors="replace")
    elif result_text is not None:
        result_content = result_text
    else:
        result_content = (
            f"# Native Host Subagent Execution: {eff_task_id}\n\n"
            f"- **Agent**: {off_agent}\n"
            f"- **Model**: `{off_model}`\n\n"
            f"### Task Summary\n{task_summary or 'Native subagent execution.'}\n\n"
            f"### Outcome Summary\n{outcome_summary or 'Completed successfully.'}\n"
        )

    task_dir = ensure_child_path(run_dir, eff_task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    lock_file = task_dir / "_task.lock"

    with file_lock(lock_file):
        meta_path = task_dir / "meta.json"
        result_path = task_dir / "result.md"
        if (meta_path.is_file() or result_path.is_file()) and not overwrite:
            raise FileExistsError(
                f"Task '{eff_task_id}' already exists in run '{run_id}'. Pass --overwrite to update."
            )

        now_str = now_iso()
        meta: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "task_id": eff_task_id,
            "parent_instance": None,
            "parent_task_id": eff_parent_task,
            "attempt": 1,
            "route_id": f"{canon_agent}/{canon_agent}/{off_model}",
            "agent": canon_agent,
            "official_agent": off_agent,
            "provider": canon_agent,
            "model": off_model,
            "requested_model": model,
            "resolved_model": off_model,
            "official_model_id": off_model,
            "variant": None,
            "effort": eff_effort,
            "status": "ok",
            "exit_code": 0,
            "started_at": now_str,
            "ended_at": now_str,
            "duration_ms": 0,
            "session_id": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "task_summary": task_summary or f"Native execution via {off_agent}",
            "outcome_summary": outcome_summary or (result_content[:120].strip()),
            "significant": bool(significant),
            "error_class": None,
            "native_host_execution": True,
            "created_at": now_str,
            "completed_at": now_str,
        }

        # Stage result text to temporary file, atomically commit meta.json, then commit result.md
        # If result rename fails, roll back meta.json to its prior state or unlink it if newly created.
        old_meta_content: str | None = meta_path.read_text(encoding="utf-8") if meta_path.is_file() else None
        tmp_result = task_dir / "result.md.tmp"
        tmp_result.write_text(result_content, encoding="utf-8")
        try:
            atomic_write_json(meta_path, meta)
            try:
                tmp_result.replace(result_path)
            except Exception as replace_err:
                if old_meta_content is not None:
                    try:
                        atomic_write_text(meta_path, old_meta_content)
                    except Exception as rb_err:
                        raise replace_err from rb_err
                elif meta_path.exists():
                    try:
                        meta_path.unlink()
                    except OSError as unlink_err:
                        raise replace_err from unlink_err
                raise replace_err
        finally:
            if tmp_result.exists():
                try:
                    tmp_result.unlink()
                except OSError:
                    pass

    append_manifest_event(
        run_dir,
        {
            "event": "native_task_recorded",
            "timestamp": now_iso(),
            "run_id": run_id,
            "task_id": eff_task_id,
            "parent_task_id": eff_parent_task,
            "official_agent": off_agent,
            "official_model_id": off_model,
            "significant": bool(significant),
            "task_summary": meta["task_summary"],
            "outcome_summary": meta["outcome_summary"],
        },
    )
    return meta


def _infer_parent_task_from_instance(parent_instance: str | None, known_tasks: set[str]) -> str | None:
    """Recover parent_task_id from a bounded instance ID (`task-<slug>-<hash>`) when not explicitly set."""
    if not parent_instance or parent_instance == "root":
        return None
    for tid in sorted(known_tasks, key=len, reverse=True):
        expected_inst = make_bounded_instance_id("task", tid)
        if parent_instance == expected_inst:
            return tid
    return None


def build_run_report(run_id: str, only_significant: bool = False) -> dict[str, Any]:
    """Reconstruct the full recursive subagent invocation tree and normalized participant list for `run_id`."""
    run_dir = get_run_dir(run_id)
    tasks_by_id: dict[str, dict[str, Any]] = {}

    for child in sorted(run_dir.iterdir()):
        if not child.is_dir() or child.name in ("prompts",):
            continue
        meta_path = child / "meta.json"
        if not meta_path.is_file():
            continue
        meta = read_json_file(meta_path)
        if not isinstance(meta, dict):
            continue
        tid = str(meta.get("task_id") or child.name)
        agent_canon = str(meta.get("agent") or "codex")
        off_agent = meta.get("official_agent") or official_agent_name(agent_canon)
        raw_mod = (
            meta.get("official_model_id")
            or meta.get("observed_model")
            or meta.get("resolved_model")
            or meta.get("model")
            or "unknown"
        )
        off_model = resolve_official_model_id(str(raw_mod)) if raw_mod != "auto" else "auto"
        sig = bool(meta.get("significant", meta.get("status") == "ok"))

        tasks_by_id[tid] = {
            "task_id": tid,
            "parent_task_id": meta.get("parent_task_id"),
            "parent_instance": meta.get("parent_instance") or "root",
            "official_agent": off_agent,
            "official_model_id": off_model,
            "agent": agent_canon,
            "provider": meta.get("provider"),
            "route_id": meta.get("route_id"),
            "status": meta.get("status"),
            "significant": sig,
            "task_summary": meta.get("task_summary") or tid,
            "outcome_summary": meta.get("outcome_summary") or "",
            "started_at": meta.get("started_at"),
            "ended_at": meta.get("ended_at"),
            "duration_ms": meta.get("duration_ms", 0),
            "usage": meta.get("usage") or {"input_tokens": 0, "output_tokens": 0},
            "children": [],
        }

    known_task_ids = set(tasks_by_id.keys())
    for tid, node in tasks_by_id.items():
        if not node["parent_task_id"]:
            inferred = _infer_parent_task_from_instance(node.get("parent_instance"), known_task_ids)
            if inferred and inferred != tid:
                node["parent_task_id"] = inferred

    roots: list[dict[str, Any]] = []
    for tid, node in sorted(tasks_by_id.items(), key=lambda kv: str(kv[1].get("started_at") or kv[0])):
        ptid = node.get("parent_task_id")
        if ptid and ptid in tasks_by_id and ptid != tid:
            tasks_by_id[ptid]["children"].append(node)
        else:
            roots.append(node)

    # Aggregate unique (official_agent, official_model_id) pairs
    pair_map: dict[tuple[str, str], dict[str, Any]] = {}
    for tid, node in sorted(tasks_by_id.items(), key=lambda kv: str(kv[1].get("started_at") or kv[0])):
        if only_significant and not node["significant"]:
            continue
        pair_key = (node["official_agent"], node["official_model_id"])
        entry = pair_map.setdefault(
            pair_key,
            {
                "official_agent": node["official_agent"],
                "official_model_id": node["official_model_id"],
                "significant": False,
                "assisted_by": f"Assisted-by: {node['official_agent']} ({node['official_model_id']})",
                "tasks": [],
            },
        )
        if node["significant"]:
            entry["significant"] = True
        entry["tasks"].append(
            {
                "task_id": tid,
                "parent_task_id": node["parent_task_id"],
                "status": node["status"],
                "significant": node["significant"],
                "task_summary": node["task_summary"],
                "outcome_summary": node["outcome_summary"],
            }
        )

    participants = list(pair_map.values())
    all_trailers = [p["assisted_by"] for p in participants]
    sig_trailers = [p["assisted_by"] for p in participants if p["significant"]]

    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "generated_at": now_iso(),
        "only_significant": only_significant,
        "task_count": len(tasks_by_id),
        "tree": roots,
        "participants": participants,
        "assisted_by_trailers": sig_trailers if only_significant else all_trailers,
        "significant_assisted_by_trailers": sig_trailers,
    }


def format_human_run_report(report: dict[str, Any]) -> str:
    """Format the recursive subagent invocation tree and `Assisted-by` trailers for user reporting."""
    lines: list[str] = [f"Subagent Invocation Tree (run: {report['run_id']})"]
    lines.append("└── root")

    def _render_node(node: dict[str, Any], prefix: str, is_last: bool) -> None:
        connector = "└── " if is_last else "├── "
        sig_tag = "significant" if node.get("significant") else "non-significant"
        lines.append(
            f"{prefix}{connector}[{node['task_id']}] {node['official_agent']} ({node['official_model_id']}) "
            f"— status={node['status']}, {sig_tag}"
        )
        child_prefix = prefix + ("    " if is_last else "│   ")
        if node.get("task_summary"):
            lines.append(f"{child_prefix}task: {node['task_summary']}")
        if node.get("outcome_summary"):
            lines.append(f"{child_prefix}outcome: {node['outcome_summary']}")
        children = node.get("children") or []
        for idx, ch in enumerate(children):
            _render_node(ch, child_prefix, idx == len(children) - 1)

    roots = report.get("tree") or []
    for idx, rnode in enumerate(roots):
        _render_node(rnode, "    ", idx == len(roots) - 1)

    lines.append("")
    lines.append("Participating (Coding Agent, Official Model ID) Pairs:")
    for p in report.get("participants") or []:
        sig_label = "significant" if p.get("significant") else "filtered/no-op"
        t_ids = ", ".join(t["task_id"] for t in (p.get("tasks") or []))
        lines.append(f"  - {p['official_agent']} ({p['official_model_id']}) [{sig_label}] — tasks: {t_ids}")

    sig_trailers = report.get("significant_assisted_by_trailers") or []
    if sig_trailers:
        lines.append("")
        lines.append("Recommended Commit Trailers (significant contributions only):")
        for tr in sig_trailers:
            lines.append(f"  {tr}")
    return "\n".join(lines)


def evaluate_cost_gate(
    num_calls: int,
    task_category: str = "general",
    run_id: str | None = None,
    include_fallback_allowance: bool = True,
) -> dict[str, Any]:
    """Check whether a planned fan-out exceeds the cost/call confirmation gate."""
    sel = select_routes(task=task_category, run_id=run_id)
    top = sel.get("selected") or {}
    fallbacks = sel.get("fallbacks") or []
    unit_cost = float(top.get("cost_index", 0.20))
    billing_mode = top.get("billing_mode") or "unknown"
    cost_estimated = bool(top.get("cost_estimated", True))

    requires_orchestrator = num_calls >= 3
    orchestrator_calls = 1 if requires_orchestrator else 0
    effective_calls = num_calls + orchestrator_calls

    fallback_buffer = 0.0
    if include_fallback_allowance and fallbacks:
        fb_cost = float(fallbacks[0].get("cost_index", unit_cost))
        fallback_buffer = round(0.15 * fb_cost * num_calls, 3)

    total_cost_idx = round(effective_calls * unit_cost + fallback_buffer, 3)
    requires_confirmation = (
        effective_calls >= COST_GATE_MAX_CALLS
        or total_cost_idx >= COST_GATE_MAX_COST_INDEX
        or billing_mode == "unknown"
    )
    est_tag = "estimated" if cost_estimated else "verified"
    summary = (
        f"Planned fan-out: {num_calls} worker call(s)"
        f"{' + 1 orchestrator' if requires_orchestrator else ''} on {top.get('route_id', 'auto')} "
        f"(billing={billing_mode}, {est_tag} cost index {total_cost_idx:.2f}) — "
        f"{'CONFIRMATION REQUIRED' if requires_confirmation else 'within auto gate'}; "
        f"{'spawn dedicated orchestrator subagent' if requires_orchestrator else 'direct invocation allowed'}."
    )
    return {
        "planned_worker_calls": num_calls,
        "orchestrator_calls": orchestrator_calls,
        "total_planned_calls": effective_calls,
        "primary_route": top.get("route_id"),
        "billing_mode": billing_mode,
        "cost_estimated": cost_estimated,
        "unit_cost_index": unit_cost,
        "fallback_allowance_index": fallback_buffer,
        "estimated_cost_index": total_cost_idx,
        "requires_user_confirmation": requires_confirmation,
        "requires_orchestrator_subagent": requires_orchestrator,
        "summary": summary,
    }


def main() -> int:
    enforce_utf8()
    if len(sys.argv) > 1 and sys.argv[1] in ("report", "mark", "record-native"):
        mode = sys.argv[1]
        sub_parser = argparse.ArgumentParser(prog=f"run.py {mode}")
        sub_parser.add_argument(
            "--run-id",
            default=os.environ.get("CROSS_SUBAGENTS_RUN_ID"),
            required=not bool(os.environ.get("CROSS_SUBAGENTS_RUN_ID")),
            help="Run identifier.",
        )
        if mode == "report":
            sub_parser.add_argument(
                "--only-significant",
                action="store_true",
                help="Filter participants and Assisted-by trailers to significant contributions only.",
            )
            sub_parser.add_argument("--human", action="store_true", help="Print human-readable call tree.")
            s_args = sub_parser.parse_args(sys.argv[2:])
            rep = build_run_report(s_args.run_id, only_significant=s_args.only_significant)
            if s_args.human:
                print(format_human_run_report(rep))
            else:
                print(json.dumps(rep, indent=2, ensure_ascii=False))
            return 0
        elif mode == "record-native":
            sub_parser.add_argument("--task-id", required=True, help="Task identifier to record.")
            sub_parser.add_argument(
                "--parent-task",
                default=os.environ.get("CROSS_SUBAGENTS_PARENT_TASK_ID"),
                help="Parent task identifier.",
            )
            sub_parser.add_argument(
                "--agent",
                default="antigravity",
                help="Host agent name (e.g. antigravity, codex, claude-code).",
            )
            sub_parser.add_argument(
                "--model",
                required=True,
                help="Host model identifier (e.g. gemini-2.5-pro, gpt-6.1-sol).",
            )
            sub_parser.add_argument("--summary", help="Concise summary of the task performed.")
            sub_parser.add_argument("--outcome-summary", help="Concise summary of the outcome / findings.")
            sub_parser.add_argument("--result-file", help="Path to markdown result file.")
            sub_parser.add_argument("--result-text", help="Inline markdown result text.")
            sub_parser.add_argument("--overwrite", action="store_true", help="Overwrite existing task record if present.")
            sig_group = sub_parser.add_mutually_exclusive_group()
            sig_group.add_argument(
                "--significant",
                action="store_true",
                default=True,
                help="Mark task contribution as significant (default: True).",
            )
            sig_group.add_argument(
                "--insignificant",
                action="store_true",
                help="Mark task contribution as non-significant.",
            )
            s_args = sub_parser.parse_args(sys.argv[2:])
            sig_val = not s_args.insignificant
            rec = record_native_task(
                run_id=s_args.run_id,
                task_id=s_args.task_id,
                agent=s_args.agent,
                model=s_args.model,
                parent_task_id=s_args.parent_task,
                significant=sig_val,
                task_summary=s_args.summary,
                outcome_summary=s_args.outcome_summary,
                result_text=s_args.result_text,
                result_file=s_args.result_file,
                overwrite=s_args.overwrite,
            )
            print(json.dumps(rec, indent=2, ensure_ascii=False))
            return 0
        else:
            sub_parser.add_argument("--task-id", required=True, help="Task identifier to update.")
            sig_group = sub_parser.add_mutually_exclusive_group()
            sig_group.add_argument("--significant", action="store_true", help="Mark task output as significant.")
            sig_group.add_argument("--insignificant", action="store_true", help="Mark task output as non-significant.")
            sub_parser.add_argument("--summary", help="Updated task description summary.")
            sub_parser.add_argument("--outcome-summary", help="Updated outcome/findings summary.")
            s_args = sub_parser.parse_args(sys.argv[2:])
            sig_val: bool | None = True if s_args.significant else (False if s_args.insignificant else None)
            updated = mark_task_outcome(
                run_id=s_args.run_id,
                task_id=s_args.task_id,
                significant=sig_val,
                task_summary=s_args.summary,
                outcome_summary=s_args.outcome_summary,
            )
            print(json.dumps(updated, indent=2, ensure_ascii=False))
            return 0

    parser = argparse.ArgumentParser(description="Execute, resume, mark, or report cross-subagent tasks.")
    parser.add_argument(
        "--run-id",
        default=os.environ.get("CROSS_SUBAGENTS_RUN_ID") or f"run-{int(time.time())}",
        help="Run identifier.",
    )
    parser.add_argument("--task-id", default="task-1", help="Task identifier within the run.")
    parser.add_argument("--route", help="Explicit route `<agent>/<provider>/<model>` or `auto`.")
    parser.add_argument("--task", default="general", help="Task category for automatic route selection.")
    parser.add_argument("--prompt", help="Task prompt string (prefer --prompt-file or stdin).")
    parser.add_argument("--prompt-file", help="Path to task prompt Markdown/text file.")
    parser.add_argument("--common-prompt", help="Path to shared common prompt file prepended to stdin.")
    parser.add_argument(
        "--context-file",
        action="append",
        help="Local file path pointer to include in the prompt so the subagent can read large inputs on demand.",
    )
    parser.add_argument(
        "--reference",
        action="append",
        help="Primary source pointer (GitHub issue/PR, discussion, URL, or saved snapshot path).",
    )
    parser.add_argument(
        "--recommend-inspect",
        action="store_true",
        help="Actively recommend in the prompt that the subagent inspect the provided --reference sources.",
    )
    parser.add_argument(
        "--allow-gh",
        action="store_true",
        help="Allow read-only subagents to run `gh` CLI commands to inspect issues, PRs, and discussions.",
    )
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help="Allow read-only subagents to use web fetch/search tools to inspect referenced URLs.",
    )
    parser.add_argument("--summary", help="Concise summary of the task delegated to this subagent.")
    parser.add_argument("--outcome-summary", help="Concise summary of the subagent's outcome.")
    sig_grp = parser.add_mutually_exclusive_group()
    sig_grp.add_argument("--significant", action="store_true", help="Mark task contribution as significant.")
    sig_grp.add_argument("--insignificant", action="store_true", help="Mark task contribution as non-significant.")
    parser.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], help="Reasoning effort.")
    parser.add_argument("--keep-session", action="store_true", help="Preserve session ID for coroutine resumption.")
    parser.add_argument("--resume", help="Explicit session/thread/conversation ID to resume (requires --route).")
    parser.add_argument("--resume-task", help="Task ID in the same run whose coroutine session should be resumed.")
    parser.add_argument(
        "--sandbox",
        default=None,
        choices=["workspace-write", "read-only", "bypass"],
        help="Execution permission mode (defaults to workspace-write, or inherits coroutine mode unless explicitly set).",
    )
    parser.add_argument("--cwd", help="Working directory for the subagent process.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="Wall-clock timeout in seconds.")
    parser.add_argument(
        "--queue-timeout",
        type=int,
        default=DEFAULT_QUEUE_TIMEOUT_SECONDS,
        help="Max seconds to wait for a live instance lease slot before returning deferred_capacity.",
    )
    parser.add_argument(
        "--parent",
        default=os.environ.get("CROSS_SUBAGENTS_PARENT_INSTANCE") or "root",
        help="Parent instance ID for lease tracking ('root' or parent ID).",
    )
    parser.add_argument(
        "--parent-task",
        default=os.environ.get("CROSS_SUBAGENTS_PARENT_TASK_ID"),
        help="Parent task ID for recursive call tree tracking.",
    )
    parser.add_argument("--min-context", type=int, help="Minimum required verified context window in tokens.")
    parser.add_argument("--require-modality", action="append", help="Required input modality (e.g. text, image).")
    parser.add_argument(
        "--require-vision",
        action="store_true",
        help="Require image/vision input modality support.",
    )
    parser.add_argument(
        "--speed",
        default="normal",
        choices=["normal", "fast", "urgent"],
        help="Token generation speed preference ('normal', 'fast', or 'urgent').",
    )
    parser.add_argument("--prefer-fast", action="store_true", help="Shorthand for --speed fast.")
    parser.add_argument(
        "--allow-estimated-capabilities",
        action="store_true",
        help="Allow family-prior estimates to satisfy --min-context and --require-modality/--require-vision filters.",
    )
    parser.add_argument(
        "--allow-micro-quota",
        action="store_true",
        help="Allow routes on low-tier/free accounts with small absolute quota headroom for heavy tasks.",
    )
    parser.add_argument(
        "--min-headroom",
        type=float,
        help="Minimum required effective quota headroom (default: 0.15 for heavy tasks).",
    )
    parser.add_argument("--force-route", action="store_true", help="Bypass eligibility checks on an explicit --route.")
    parser.add_argument("--exclude-vendor", action="append", help="Exclude model vendor(s) during selection.")
    parser.add_argument("--exclude-agent", action="append", help="Exclude agent(s) during selection.")
    parser.add_argument("--no-fallback", action="store_true", help="Disable automatic fallback chain on failure.")
    parser.add_argument("--dry-run", action="store_true", help="Print planned route and prompt paths without running.")
    parser.add_argument("--cost-gate", type=int, metavar="NUM_CALLS", help="Evaluate cost/call gate for NUM_CALLS fan-out.")
    parser.add_argument("--report", action="store_true", help="Print the recursive subagent tree report for --run-id.")
    parser.add_argument("--only-significant", action="store_true", help="Filter --report to significant tasks only.")
    parser.add_argument("--human", action="store_true", help="Print human-readable output when used with --report.")

    args = parser.parse_args()

    if args.report:
        rep = build_run_report(args.run_id, only_significant=args.only_significant)
        if args.human:
            print(format_human_run_report(rep))
        else:
            print(json.dumps(rep, indent=2, ensure_ascii=False))
        return 0

    if args.cost_gate is not None:
        gate = evaluate_cost_gate(args.cost_gate, task_category=args.task, run_id=args.run_id)
        print(json.dumps(gate, indent=2, ensure_ascii=False))
        return 0

    eff_speed = "fast" if (args.prefer_fast and args.speed == "normal") else args.speed
    sig_flag: bool | None = True if args.significant else (False if args.insignificant else None)

    res = execute_task(
        run_id=args.run_id,
        task_id=args.task_id,
        route_str=args.route,
        task_category=args.task,
        prompt_text=args.prompt,
        prompt_file=args.prompt_file,
        common_prompt_file=args.common_prompt,
        effort=args.effort,
        keep_session=args.keep_session,
        resume_session_id=args.resume,
        resume_task_id=args.resume_task,
        sandbox_mode=args.sandbox,
        cwd=args.cwd,
        timeout_sec=args.timeout,
        queue_timeout_sec=args.queue_timeout,
        parent_instance=args.parent,
        parent_task_id=args.parent_task,
        min_context=args.min_context,
        require_modalities=args.require_modality,
        require_vision=args.require_vision,
        speed=eff_speed,
        allow_estimated_capabilities=args.allow_estimated_capabilities,
        allow_micro_quota=args.allow_micro_quota,
        min_headroom=args.min_headroom,
        exclude_vendors=args.exclude_vendor,
        exclude_agents=args.exclude_agent,
        no_fallback=args.no_fallback,
        force_route=args.force_route,
        dry_run=args.dry_run,
        context_files=args.context_file,
        references=args.reference,
        recommend_inspect=args.recommend_inspect,
        allow_gh=args.allow_gh,
        allow_network=args.allow_network,
        task_summary=args.summary,
        outcome_summary=args.outcome_summary,
        significant=sig_flag,
    )
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0 if res.get("status") in ("ok", "dry_run") else 1


if __name__ == "__main__":
    sys.exit(main())
