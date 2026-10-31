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
  - Applies explicit execution postures: `--no-tools` (tool-free, enforced by Claude Code and
    OpenCode), `--allow-shell` (read-only plus shell for tests), and `--isolate` (no ambient
    hooks, instructions, MCP, or execpolicy rules). `meta.json` records `tool_policy` and
    `tools_enforced`.
  - Offers an opt-in protected-input guard (`--guard`) that hashes the working tree and Git
    state before the attempt and fails the task when they changed.
  - Treats structured tool denials as warnings (`denials_observed`) rather than failing a
    completed turn, and always exposes the attempt's own answer text through
    `attempt_result_file` in `meta.json`.
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
import uuid
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
from guard import snapshot as guard_snapshot, verify as guard_verify
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
# A terminal finish reason that means the final assistant message is complete.
# Anything else (`length`, `tool-calls`, `content-filter`, unknown) is truncated.
OPENCODE_SUCCESS_FINISH = frozenset({"stop", "end_turn", "completed", "finish", "done"})


def _epoch_ms(value: Any) -> int | None:
    """Normalize a second- or millisecond-epoch timestamp to milliseconds."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return int(number * 1000) if number < 10**12 else int(number)


def _message_text(message: dict[str, Any]) -> str:
    """Extract visible text from a session-export message in either v1 or v2 shape."""
    chunks: list[str] = []
    for item in message.get("parts") or message.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
            chunks.append(str(item["text"]))
    if not chunks:
        for key in ("text", "content"):
            value = message.get(key)
            if isinstance(value, str) and value:
                chunks.append(value)
    return "".join(chunks)


# A denied call is only recognised from a structured status, an explicit
# denial field, or a short error emitted by the tool runner itself. Command
# output is never evidence: a failing test that prints "permission" is not a
# sandbox denial, and a command may legitimately read files whose contents
# mention the word.
DENIAL_STATUSES = frozenset({"denied", "declined", "rejected", "not_allowed"})
DENIAL_ERROR_MARKERS = (
    "operation not permitted",
    "permission denied",
    "denied by sandbox",
    "sandbox denied",
    "approval denied",
    "approval required",
    "not permitted by policy",
    "eperm",
    "eacces",
)
# Tool-runner errors are single messages. Anything longer is a transcript of
# the command's own stderr/stdout and must not be keyword-scanned.
DENIAL_ERROR_MAX_CHARS = 400


def _denied_flag(container: Any) -> bool:
    """Return True when a mapping carries an explicit denial flag."""
    if not isinstance(container, dict):
        return False
    for key in ("denied", "is_denied", "permission_denied"):
        if container.get(key) is True:
            return True
    for key in ("permission", "approval", "decision"):
        if str(container.get(key) or "").lower() in ("denied", "declined", "rejected", "deny"):
            return True
    return False


def _find_last_json_object(text: str) -> dict[str, Any] | None:
    """Return the last JSON object in `text`, tolerating surrounding noise.

    Some CLIs prepend warnings (or emit more than one JSON document) on
    stdout. Slicing from the first `{` to the last `}` then fails to parse a
    perfectly good payload, which used to turn a completed run into a
    `format` failure with a misleading "subtype=None" message.
    """
    decoder = json.JSONDecoder()
    found: dict[str, Any] | None = None
    index = text.find("{")
    while index != -1:
        try:
            obj, end = decoder.raw_decode(text[index:])
        except ValueError:
            index = text.find("{", index + 1)
            continue
        if isinstance(obj, dict):
            found = obj
        index = text.find("{", index + max(end, 1))
    return found


def _int_or_zero(value: Any) -> int:
    """Coerce a usage field to int without discarding an otherwise valid payload."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _tool_denial(part: dict[str, Any]) -> str | None:
    """Return a denial description when a tool part reports a structured refusal.

    Only signals that actually mean "the runner refused this call" count: an
    explicit denied/declined status, a denial flag or approval decision, or a
    short error from the tool runner naming an OS/sandbox denial. Ordinary
    command output (`output`, `aggregated_output`) is never scanned, because
    unrelated output may contain words such as "permission" or "denied".
    """
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    state_meta = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
    status = str(state.get("status") or part.get("status") or "").lower()
    tool = part.get("tool") or part.get("name") or part.get("command") or "tool"

    if status in DENIAL_STATUSES:
        return f"{tool}: denied (status={status})"
    for container in (part, state, state_meta):
        if _denied_flag(container):
            return f"{tool}: denied"

    error_detail = ""
    for value in (part.get("error"), state.get("error"), state_meta.get("error")):
        if value:
            error_detail = str(value)
            break
    if not error_detail or len(error_detail) > DENIAL_ERROR_MAX_CHARS:
        return None
    text = error_detail.lower()
    if any(marker in text for marker in DENIAL_ERROR_MARKERS):
        return f"{tool}: {error_detail[:160]}"
    return None


def _denial_error(denials: list[Any]) -> str | None:
    """Render observed denials as a failure message instead of an empty result."""
    cleaned = [str(item).strip() for item in denials if str(item).strip()]
    if not cleaned:
        return None
    return ("Tool call(s) denied: " + "; ".join(cleaned))[:500]


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
    """Classify failure into standard error classes for repair and fallback routing.

    `stdout_text` and `stderr_text` must contain only structured CLI error
    output (stderr, error events, or the CLI's own error payload). Never pass
    the agent's transcript or tool output: a test that prints "429" or
    "quota" would otherwise mark a quota bucket exhausted.
    """
    if timed_out or exit_code == 124:
        return "timeout"
    if exit_code == 127:
        return "not_installed"
    combined = f"{stdout_text}\n{stderr_text}".lower()
    rate_limited = any(
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
    )
    # Explicit exhaustion markers win over retry hints: a daily quota reset
    # (`retry-after: 86400`) is exhausted, not a temporary throttle.
    if rate_limited and any(
        k in combined
        for k in (
            "insufficient_quota",
            "exceeded your current quota",
            "usage limit reached",
            "credit balance is too low",
            "hit your limit",
            "quota exceeded",
        )
    ):
        return "quota"
    # Request/token throttling is temporary even when the provider does not
    # spell out "retry after". Exhaustion markers above still win.
    if any(
        k in combined
        for k in (
            "rate_limit_exceeded",
            "tokens per min",
            "requests per min",
            "tokens/min",
            "requests/min",
            "tpm limit",
            "rpm limit",
        )
    ) or re.search(r"\b(tpm|rpm)\b", combined):
        return "transient"
    # A temporary throttle carries a retry hint; account exhaustion does not.
    if rate_limited and any(
        k in combined
        for k in ("retry-after", "retry after", "try again", "temporarily", "overloaded", "please wait")
    ):
        return "transient"
    if (
        any(
            k in combined
            for k in ("overloaded", "service unavailable", "bad gateway", "gateway timeout")
        )
        or re.search(r"\b(502|503|504)\b", combined)
    ):
        return "transient"
    if rate_limited:
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
            "model_not_supported",
            "model is not supported",
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
    if "sandbox_unsupported" in msg:
        # The route's CLI cannot honour the requested posture; this is an
        # option/capability mismatch, not a permission failure.
        return "no_option", 1
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
    if req_prov is None and "/" in req_mod:
        # Without an explicit provider, a leading segment is provider syntax.
        # With one, a slash is part of a namespaced model ID (e.g. OpenRouter).
        parsed_req_prov, req_mod = req_mod.split("/", 1)
        req_prov = parsed_req_prov

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


def _model_identity(model: str) -> tuple[str | None, str, str | None]:
    """Split `provider/model#variant` into comparable components."""
    raw = model.strip().lower()
    variant: str | None = None
    provider: str | None = None
    if "#" in raw:
        raw, variant = raw.split("#", 1)
    if "/" in raw:
        provider, raw = raw.split("/", 1)
    return provider, raw, variant


def _models_compatible(models: list[str]) -> bool:
    """True when every observation names the same model/provider/variant.

    A missing provider or variant does not conflict with an otherwise identical
    observation; two different explicit values do.
    """
    identities = [_model_identity(model) for model in models if model]
    names = {name for _provider, name, _variant in identities}
    providers = {provider for provider, _name, _variant in identities if provider}
    variants = {variant for _provider, _name, variant in identities if variant}
    return len(names) <= 1 and len(providers) <= 1 and len(variants) <= 1


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
    pipes_ok = True
    try:
        stdout_data, stderr_data = proc.communicate(input=stdin_text, timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        timed_out = True
        killed = True
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
            killed = tk_ok

        try:
            stdout_data, stderr_data = proc.communicate(timeout=3)
        except Exception:
            # A descendant may still hold the stdio pipes open.
            stdout_data, stderr_data = "", ""
            pipes_ok = False

        # Recompute from the final state. The leader has been reaped here, so
        # a successful group probe means no descendant survived; an earlier
        # probe that saw the unreaped leader is no longer evidence of failure.
        if os.name != "nt":
            try:
                os.killpg(proc.pid, 0)
                group_gone = False
            except ProcessLookupError:
                group_gone = True
            except PermissionError:
                group_gone = False
            cleanup_verified = group_gone and pipes_ok
        else:
            cleanup_verified = pipes_ok and killed and not is_pid_alive(proc.pid)
    else:
        # A normal exit can still leave a background descendant in the same
        # process group. Check the group before treating cleanup as verified.
        if os.name != "nt":
            try:
                os.killpg(proc.pid, 0)
                cleanup_verified = False
            except ProcessLookupError:
                cleanup_verified = True
            except PermissionError:
                cleanup_verified = False
        else:
            # Windows has no process-group probe without a Job Object; guarded
            # execution is rejected before launch there.
            cleanup_verified = not is_pid_alive(proc.pid)

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
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    policy = policy or {}
    result_file = attempt_dir / "result.md"
    if result_file.exists():
        result_file.unlink()
    events_file = attempt_dir / "events.jsonl"
    stderr_file = attempt_dir / "stderr.log"

    flags = recipe_flags if recipe_flags is not None else get_agent_recipe("codex")
    if policy.get("no_tools"):
        prompt_text = apply_tool_free_instruction(prompt_text, policy)
        print(
            "WARNING: Codex has no CLI switch that disables tools. The tool-free posture is "
            "enforced by prompt instruction and the read-only sandbox only; meta.json records "
            "tools_enforced=false. Use Claude Code or OpenCode when tool use must be impossible.",
            file=sys.stderr,
        )
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
    if policy.get("isolate") and flags.get("has_ignore_rules", True):
        cmd.append("--ignore-rules")
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
    # `stream_errors` are fatal protocol/verification failures. `warnings`
    # collect CLI error events that a later completed turn may have recovered
    # from (for example a retried stream disconnect); they are reported but do
    # not fail a turn that completed with an answer.
    stream_errors: list[str] = []
    warnings: list[str] = []
    denials_observed: list[str] = []
    stream_models: list[str] = []
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
        if ev.get("model"):
            stream_models.append(str(ev["model"]))
            if not observed_model:
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
            elif item.get("type") in ("command_execution", "mcp_tool_call", "tool_call"):
                denial = _tool_denial(item if isinstance(item, dict) else {})
                if denial:
                    denials_observed.append(denial)
        elif ev_type == "turn.failed":
            # An explicit terminal failure is fatal even if a message exists.
            stream_errors.append(json.dumps(ev))
        elif ev_type == "error":
            warnings.append(json.dumps(ev))

    if stream_models and not _models_compatible(stream_models):
        stream_errors.append(
            "model changed within the stream: " + ", ".join(sorted(set(stream_models)))
        )

    if saw_turn_completed and (not result_file.is_file() or not result_file.read_text(encoding="utf-8").strip()) and last_agent_msg:
        result_file.write_text(last_agent_msg.strip() + "\n", encoding="utf-8")

    final_text = result_file.read_text(encoding="utf-8").strip() if (saw_turn_completed and result_file.is_file()) else ""
    err_combined = err + ("\n" + "\n".join(warnings + stream_errors) if (warnings or stream_errors) else "")
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
            "warnings": warnings,
            "denials_observed": denials_observed,
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
            "warnings": warnings,
            "denials_observed": denials_observed,
            "error_class": "model_unavailable",
            "error": f"Model substitution detected: requested '{model_id}', observed '{observed_model}'.",
        }

    if code != 0 or stream_errors or not saw_turn_completed or not final_text:
        # Only structured CLI error text is classification evidence. `out` is
        # the event stream and embeds every command's output, so scanning it
        # would let a test transcript mentioning "429" or "quota" mark a bucket
        # exhausted.
        err_class = classify_failure(code, "", err_combined, timed_out=False)
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
            "warnings": warnings,
            "denials_observed": denials_observed,
            "error_class": err_class,
            "error": err_combined.strip()[:500]
            or _denial_error(denials_observed)
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
        "warnings": warnings,
        "denials_observed": denials_observed,
        "error_class": None,
    }


TOOL_FREE_INSTRUCTION = (
    "Tool-free task: do not run shell commands, read files, fetch URLs, or spawn subagents. "
    "Answer only from the instruction and material in this prompt.\n\n"
)


def apply_tool_free_instruction(prompt_text: str, policy: dict[str, Any] | None) -> str:
    """Prepend the prompt-only tool-free rule for CLIs that cannot deny tools."""
    if (policy or {}).get("no_tools"):
        return TOOL_FREE_INSTRUCTION + prompt_text
    return prompt_text


def tools_enforcement(
    agent: str, policy: dict[str, Any] | None, status: str = "ok"
) -> bool | None:
    """Report whether the CLI actually prevented tool use.

    `None` means a tool-free posture was not requested, or the attempt did not
    complete so enforcement cannot be confirmed. Claude Code and OpenCode can
    deny every tool; Codex and Antigravity can only be told not to use them in
    the prompt, so they report `False` and the caller decides whether that is
    acceptable.
    """
    if not (policy or {}).get("no_tools"):
        return None
    if agent not in ("claude-code", "opencode"):
        return False
    return True if status == "ok" else None


def claude_read_only_tool_args(
    flags: dict[str, Any],
    *,
    no_tools: bool = False,
    allow_shell: bool = False,
    allow_gh: bool = False,
    allow_network: bool = False,
    isolate: bool = False,
) -> list[str]:
    """Build the Claude Code read-only tool/permission arguments.

    `no_tools` is the tool-free posture used for prose review: no tool may run.
    `allow_shell` permits shell commands (inspection and focused tests) while
    edit/write/notebook tools stay denied. `isolate` adds safe mode and strict
    MCP isolation so hooks, project instructions, and MCP servers cannot change
    the reviewer's behavior.
    """
    args: list[str] = []
    if flags.get("has_permission_mode", True):
        args.extend(["--permission-mode", "dontAsk"])
    if flags.get("has_permission_prompts", True):
        args.extend(["--permission-prompts", "none"])
    if isolate or no_tools:
        if flags.get("has_safe_mode", True):
            args.append("--safe-mode")
        if flags.get("has_strict_mcp_config", True):
            args.append("--strict-mcp-config")

    disallowed = ["Edit", "Write", "NotebookEdit"]
    shell_allowed = (allow_shell or allow_gh) and not no_tools
    if no_tools:
        tools: list[str] = []
    else:
        tools = ["Read", "Grep", "Glob"]
        if allow_network:
            tools.extend(["WebFetch", "WebSearch"])
        if shell_allowed:
            tools.append("Bash")
    if not shell_allowed:
        disallowed.append("Bash")

    args.extend(["--tools", ",".join(tools), "--disallowedTools", ",".join(disallowed)])
    if not no_tools and shell_allowed and flags.get("has_allowed_tools", True):
        allowed_specs = ["Read", "Grep", "Glob"]
        if allow_network:
            allowed_specs.extend(["WebFetch", "WebSearch"])
        if allow_shell:
            allowed_specs.append("Bash(*)")
        if allow_gh:
            allowed_specs.extend(
                [
                    "Bash(gh issue view:*)",
                    "Bash(gh issue list:*)",
                    "Bash(gh pr view:*)",
                    "Bash(gh pr diff:*)",
                    "Bash(gh pr list:*)",
                    "Bash(gh search:*)",
                    "Bash(gh repo view:*)",
                ]
            )
        args.extend(["--allowedTools", ",".join(allowed_specs)])
    return args


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
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    policy = policy or {}
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
        cmd.extend(
            claude_read_only_tool_args(
                flags,
                no_tools=bool(policy.get("no_tools")),
                allow_shell=bool(policy.get("allow_shell")),
                allow_gh=allow_gh,
                allow_network=allow_network,
                isolate=bool(policy.get("isolate")),
            )
        )
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
    denials_observed: list[Any] = []
    observed_models: list[str] = []
    payload_parsed = False

    cleaned_out = strip_ansi(out).strip()
    payload = _find_last_json_object(cleaned_out)
    if isinstance(payload, dict):
        payload_parsed = True
        session_id = payload.get("session_id") or session_id
        is_error = bool(payload.get("is_error", False))
        subtype = payload.get("subtype")
        denials_observed = payload.get("permission_denials") or []
        result_text = (payload.get("result") or "").strip()
        u = payload.get("usage") or {}
        usage["input_tokens"] = _int_or_zero(u.get("input_tokens"))
        usage["output_tokens"] = _int_or_zero(u.get("output_tokens"))
        m_usage = payload.get("modelUsage")
        if isinstance(m_usage, dict):
            observed_models = [str(k) for k in m_usage.keys()]

    no_persist = not keep_session and not resume_session_id and flags.get("has_no_session_persistence", True)

    def _matches_requested(model: str) -> bool:
        return _verify_model_match(
            resolved_model,
            route["canonical_key"],
            model,
            expected_provider=route.get("provider"),
            expected_variant=eff,
        )

    # modelUsage can list auxiliary models (subagents, bookkeeping) alongside
    # the answering model. A requested model that is present means the right
    # model answered; only a list without it is evidence of substitution.
    route_key = route.get("canonical_key") or route.get("model_id") or ""
    if "haiku" in route_key:
        observed_primary = observed_models[0] if observed_models else None
        mismatched = [model for model in observed_models if "haiku" not in model.lower()]
    else:
        matching = [model for model in observed_models if _matches_requested(model)]
        non_haiku = [model for model in observed_models if "haiku" not in model.lower()]
        if matching:
            observed_primary = matching[0]
            mismatched = []
        else:
            observed_primary = (
                non_haiku[0] if non_haiku else (observed_models[0] if observed_models else None)
            )
            mismatched = non_haiku
    if mismatched:
        return {
            "status": "failed",
            "exit_code": code or 1,
            "session_id": session_id,
            "usage": usage,
            "requested_model": requested_model,
            "resolved_model": resolved_model,
            "observed_model": observed_primary,
            "observed_models": observed_models,
            "denials_observed": denials_observed,
            "session_cleaned_up": no_persist,
            "cleanup_verified": cleanup_verified,
            "error_class": "model_unavailable",
            "error": (
                f"Claude reported unexpected model(s): {', '.join(mismatched)}; "
                f"requested '{resolved_model}'."
            ),
        }

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

    # Require an explicit terminal subtype == "success". Denials are recorded as
    # warnings, not failures: a completed turn with a final answer succeeded
    # even if one of its tool calls was refused.
    subtype_failed = subtype != "success"
    if code != 0 or is_error or subtype_failed or not result_text:
        if result_text:
            (attempt_dir / "partial_result.md").write_text(result_text + "\n", encoding="utf-8")
        # Only the CLI's own error message (result when is_error) is
        # classification evidence, never the full payload with its transcript.
        err_class = classify_failure(code, result_text if is_error else "", err, timed_out=False)
        if code == 0 and not is_error and (subtype_failed or not result_text):
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
            "denials_observed": denials_observed,
            "session_cleaned_up": no_persist,
            "cleanup_verified": cleanup_verified,
            "error_class": err_class,
            "error": (result_text if is_error else err.strip())[:500]
            or _denial_error(denials_observed)
            or (
                "Claude output was not valid JSON."
                if not payload_parsed
                else (
                    "Claude JSON payload had no result or subtype field."
                    if subtype is None and not result_text
                    else f"Claude terminal subtype={subtype!r} (expected 'success')."
                )
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


def opencode_read_only_config(
    existing_cfg: dict[str, Any],
    *,
    major_ver: int,
    no_tools: bool = False,
    allow_shell: bool = False,
    allow_network: bool = False,
    isolate: bool = False,
) -> tuple[dict[str, Any], bool]:
    """Apply and verify the OpenCode read-only permission config for one attempt."""
    cfg = dict(existing_cfg or {})
    shell_allowed = allow_shell and not no_tools
    net_allowed = allow_network and not no_tools

    if major_ver == 1:
        # v1 has no per-agent configuration, so neither a tool-free posture nor
        # the dedicated shell-review agent can be established.
        if no_tools or allow_shell:
            return cfg, False
        cfg.pop("permissions", None)
        cfg["permission"] = {
            "edit": "deny",
            "bash": "allow" if shell_allowed else "deny",
            "write": "deny",
            "patch": "deny",
            "webfetch": "allow" if net_allowed else "deny",
        }
        perm = cfg["permission"]
        verified = all(perm.get(key) == "deny" for key in ("edit", "write", "patch")) and (
            perm.get("bash") == ("allow" if shell_allowed else "deny")
        )
        return cfg, verified

    cfg.pop("permission", None)
    perm_rules: list[dict[str, str]] = [
        {"action": "edit", "resource": "*", "effect": "deny"},
        {"action": "write", "resource": "*", "effect": "deny"},
        {"action": "patch", "resource": "*", "effect": "deny"},
    ]
    if shell_allowed:
        perm_rules.extend(
            [
                {"action": "shell", "resource": "*", "effect": "allow"},
                {"action": "bash", "resource": "*", "effect": "allow"},
            ]
        )
    else:
        perm_rules.extend(
            [
                {"action": "shell", "resource": "*", "effect": "deny"},
                {"action": "bash", "resource": "*", "effect": "deny"},
            ]
        )
    if net_allowed:
        perm_rules.append({"action": "webfetch", "resource": "*", "effect": "allow"})
    elif no_tools:
        perm_rules.append({"action": "webfetch", "resource": "*", "effect": "deny"})
    cfg["permissions"] = perm_rules

    if isolate or no_tools:
        cfg["share"] = "disabled"
        cfg["update"] = "disable"
        cfg["snapshots"] = False
        plugins = [p for p in (cfg.get("plugins") or []) if isinstance(p, str)]
        for plugin in ("-opencode.config.instruction", "-opencode.config.skill"):
            if plugin not in plugins:
                plugins.append(plugin)
        cfg["plugins"] = plugins
        policies = [p for p in (cfg.get("policies") or []) if isinstance(p, dict)]
        for resource in ("plugin:*", "mcp:*", "skill:*"):
            entry = {"action": "integration.use", "resource": resource, "effect": "deny"}
            if entry not in policies:
                policies.append(entry)
        cfg["policies"] = policies

    agents = cfg.get("agents") if isinstance(cfg.get("agents"), dict) else {}
    if no_tools:
        agents["tools-off"] = {
            "description": "Tool-free subagent",
            "mode": "primary",
            "system": "Answer from the supplied prompt only. Do not use tools, delegation, or recursive reviews.",
            "permissions": [{"action": "*", "resource": "*", "effect": "deny"}],
        }
        cfg["agents"] = agents
    elif shell_allowed:
        # A builtin agent can carry its own restrictions, so an ordered
        # permission list on a dedicated agent is what actually permits shell.
        shell_permissions = [
            {"action": "*", "resource": "*", "effect": "deny"},
            {"action": "read", "resource": "*", "effect": "allow"},
            {"action": "read", "resource": "*.env", "effect": "deny"},
            {"action": "read", "resource": "*.env.*", "effect": "deny"},
            {"action": "glob", "resource": "*", "effect": "allow"},
            {"action": "grep", "resource": "*", "effect": "allow"},
            {"action": "external_directory", "resource": "*", "effect": "deny"},
            {"action": "shell", "resource": "*", "effect": "allow"},
        ]
        if net_allowed:
            shell_permissions.append({"action": "webfetch", "resource": "*", "effect": "allow"})
        agents["review-shell"] = {
            "description": "Read-only reviewer permitted to run shell commands",
            "mode": "primary",
            "permissions": shell_permissions,
        }
        cfg["agents"] = agents

    required = {"edit", "write", "patch"}
    denied = {rule["action"] for rule in perm_rules if rule.get("effect") == "deny"}
    verified = required.issubset(denied) and (
        ("shell" in denied and "bash" in denied) if not shell_allowed else True
    )
    if no_tools:
        verified = verified and isinstance(cfg.get("agents"), dict) and "tools-off" in cfg["agents"]
    elif shell_allowed:
        verified = verified and isinstance(cfg.get("agents"), dict) and "review-shell" in cfg["agents"]
    return cfg, verified


def _opencode_cli_major(flags: dict[str, Any]) -> int:
    """Resolve the OpenCode major version from recipe flags, defaulting to v2."""
    majors: list[int] = []
    major_flag = flags.get("cli_major")
    if isinstance(major_flag, int) and not isinstance(major_flag, bool):
        majors.append(major_flag)
    ver_tuple = parse_semver(str(flags.get("version") or ""))
    if ver_tuple is not None:
        majors.append(ver_tuple[0])
    return min(majors) if majors else 2


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
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    policy = policy or {}
    result_file = attempt_dir / "result.md"
    if result_file.exists():
        result_file.unlink()
    events_file = attempt_dir / "events.jsonl"
    stderr_file = attempt_dir / "stderr.log"

    flags = recipe_flags if recipe_flags is not None else get_agent_recipe("opencode")
    cli_major = _opencode_cli_major(flags)
    if cli_major < 2:
        return {
            "status": "failed",
            "exit_code": 127,
            "session_id": resume_session_id,
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "requested_model": route.get("requested_model") or route["model_id"],
            "resolved_model": route.get("resolved_model_id") or route["model_id"],
            "observed_model": None,
            "session_cleaned_up": True,
            "cleanup_verified": True,
            "error_class": "not_installed",
            "error": (
                f"OpenCode v{cli_major} recipe is not supported: cross-subagents requires "
                "OpenCode v2 or later (>= 2.0.0). Upgrade OpenCode or select another agent."
            ),
        }
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
        major_ver = cli_major

        # `--no-tools` and `--allow-shell` depend on a dedicated agent; without
        # agent selection the builtin agent's own policy would still apply and
        # the reported enforcement would be a claim the run cannot support.
        if major_ver >= 2 and not has_agent_flag and (
            policy.get("no_tools") or policy.get("allow_shell")
        ):
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
                "error": (
                    "Installed OpenCode CLI does not support --agent, which --no-tools and "
                    "--allow-shell require to apply a dedicated permission profile."
                ),
            }

        existing_cfg: dict[str, Any] = {}
        if env_for_proc.get("OPENCODE_CONFIG_CONTENT"):
            try:
                existing_cfg = json.loads(env_for_proc["OPENCODE_CONFIG_CONTENT"])
            except Exception:
                existing_cfg = {}

        if allow_gh and not policy.get("allow_shell"):
            print(
                "WARNING: OpenCode does not support granular read-only command whitelisting. "
                "Shell execution remains strictly denied in read-only sandbox for safety; "
                "pre-fetch GitHub issues/PRs into --context-file instead.",
                file=sys.stderr,
            )

        configured, verified_ro = opencode_read_only_config(
            existing_cfg,
            major_ver=major_ver,
            no_tools=bool(policy.get("no_tools")),
            allow_shell=bool(policy.get("allow_shell")),
            allow_network=allow_network,
            isolate=bool(policy.get("isolate")),
        )

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

        env_for_proc["OPENCODE_CONFIG_CONTENT"] = json.dumps(configured)
        if has_agent_flag:
            if policy.get("no_tools"):
                agent_name = "tools-off"
            elif policy.get("allow_shell"):
                agent_name = "review-shell"
            else:
                agent_name = "plan"
            cmd.extend(["--agent", agent_name])
    elif has_auto:
        cmd.append("--auto")

    if resume_session_id:
        cmd.extend(["-s", resume_session_id])

    # An invocation-specific marker proves the exported user message belongs to
    # this run, not to an older pending turn that finished during a resume.
    turn_nonce = f"[cross-subagents turn: {uuid.uuid4().hex}]"
    prompt_text = f"{prompt_text.rstrip()}\n\n{turn_nonce}\n"

    turn_started_ms = int(time.time() * 1000)
    code, out, err, timed_out, cleanup_verified = run_process_with_timeout(
        cmd, prompt_text, cwd, timeout_sec, env=env_for_proc
    )
    events_file.write_text(out, encoding="utf-8")
    stderr_file.write_text(err, encoding="utf-8")

    session_id = resume_session_id
    stream_step_text: list[str] = []
    stream_final_text = ""
    # Fatal protocol/verification errors vs. recoverable CLI error events.
    # A verified completed turn supersedes the latter; they are reported in
    # `warnings` instead of failing the task.
    stream_errors: list[str] = []
    warnings: list[str] = []
    turn_message_errors: list[str] = []
    denials_observed: list[str] = []
    stream_models: list[str] = []
    usage = {"input_tokens": 0, "output_tokens": 0}
    observed_model: str | None = None
    last_finish_reason = ""

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
        if ev.get("model"):
            stream_models.append(str(ev["model"]))
            if not observed_model:
                observed_model = str(ev["model"])
        if ev_type in ("step_start", "step-start", "message.started"):
            # A new step invalidates any earlier finish. Only a successful
            # finish for the final step can complete the run.
            stream_step_text = []
            stream_final_text = ""
            last_finish_reason = ""
        elif ev_type == "text":
            part = ev.get("part") or {}
            if part.get("text"):
                stream_step_text.append(part["text"])
        elif ev_type in ("step_finish", "step-finish", "finish", "message.completed"):
            part = ev.get("part") or ev
            last_finish_reason = str(part.get("reason") or part.get("finish") or "").lower()
            if last_finish_reason in OPENCODE_SUCCESS_FINISH:
                stream_final_text = "".join(stream_step_text)
        elif ev_type in ("tool", "tool-result", "tool-invocation"):
            part = ev.get("part") or ev
            denial = _tool_denial(part if isinstance(part, dict) else {})
            if denial:
                denials_observed.append(denial)
        elif ev_type == "error":
            warnings.append(json.dumps(ev.get("error") or ev))

    if stream_models and not _models_compatible(stream_models):
        stream_errors.append(
            "model changed within the stream: " + ", ".join(sorted(set(stream_models)))
        )

    export_verified = False
    export_final_text = ""
    turn_models: list[str] = []
    turn_missing_model = False
    turn_missing_provider = False
    last_assistant_had_model = False
    last_user_text = ""
    user_message_id = ""
    session_cleaned_up = False
    cleanup_warning: str | None = None

    if session_id and has_session_cmd:
        if not timed_out:
            try:
                exp_cmd = [binary, "session", "export"]
                if has_standalone:
                    exp_cmd.append("--standalone")
                exp_cmd.append(session_id)
                exp_code, exp_stdout, _exp_stderr, exp_timed_out, exp_cleanup = run_process_with_timeout(
                    exp_cmd, "", cwd, 20, env=env_for_proc
                )
                cleanup_verified = cleanup_verified and exp_cleanup and not exp_timed_out
                if exp_code == 0 and exp_stdout.strip():
                    (attempt_dir / "session_export.json").write_text(exp_stdout, encoding="utf-8")
                    exp_json = json.loads(exp_stdout)
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
                            last_user_text = _message_text(m)
                            user_message_id = str(m.get("id") or m_info.get("id") or "")

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
                            )
                            raw_var = (
                                m_info.get("variant")
                                or nested_model.get("variant")
                                or nested_model.get("effort")
                                or m.get("variant")
                            )
                            last_assistant_had_model = bool(raw_mid)
                            if not raw_mid:
                                turn_missing_model = True
                            parent_id = m_info.get("parentID") or m.get("parentID")
                            if parent_id and (
                                not user_message_id or str(parent_id) != user_message_id
                            ):
                                stream_errors.append(
                                    "assistant message does not belong to the current user turn"
                                )
                            if raw_mid:
                                if "/" not in str(raw_mid) and not raw_pid:
                                    turn_missing_provider = True
                                obs_str = str(raw_mid)
                                if "/" not in obs_str and raw_pid:
                                    obs_str = f"{raw_pid}/{obs_str}"
                                if raw_var and "#" not in obs_str:
                                    obs_str = f"{obs_str}#{raw_var}"
                                # The final message supplies the answer, so
                                # keep the last model. Any change within the
                                # turn is a substitution, not a fallback.
                                turn_models.append(obs_str)
                                observed_model = obs_str
                            if m_info.get("error") or m.get("error"):
                                turn_message_errors.append(
                                    json.dumps(m_info.get("error") or m.get("error"))
                                )
                            completed_ts = (m_info.get("time") or {}).get("completed")
                            if completed_ts is None and isinstance(m.get("time"), dict):
                                completed_ts = m["time"].get("completed")
                            finish_val = str(m_info.get("finish") or m.get("finish") or "").lower()
                            assistant_finishes.append((completed_ts, finish_val))
                            for c in m.get("parts") or m.get("content") or []:
                                if isinstance(c, dict):
                                    denial = _tool_denial(c)
                                    if denial:
                                        denials_observed.append(denial)
                            # Keep the text of the last assistant message only;
                            # earlier steps may be partial tool-call turns.
                            export_final_text = "".join(
                                c["text"]
                                for c in (m.get("parts") or m.get("content") or [])
                                if isinstance(c, dict)
                                and c.get("type") == "text"
                                and c.get("text")
                            )

                    if last_user_idx >= 0 and assistant_finishes:
                        final_completed_ts, final_finish_val = assistant_finishes[-1]
                        completed_ms = _epoch_ms(final_completed_ts)
                        if (
                            completed_ms is not None
                            and completed_ms >= turn_started_ms
                            and final_finish_val in OPENCODE_SUCCESS_FINISH
                            and export_final_text.strip()
                            and turn_nonce in last_user_text
                            and last_assistant_had_model
                            and not turn_missing_model
                        ):
                            export_verified = True
                    if turn_missing_model:
                        stream_errors.append(
                            "assistant message without model metadata in the current turn"
                        )
                    if turn_missing_provider:
                        stream_errors.append(
                            "assistant message without provider metadata in the current turn"
                        )
                    if turn_models and not _models_compatible(turn_models):
                        stream_errors.append(
                            "model changed within the current turn: "
                            + ", ".join(sorted(set(turn_models)))
                        )
            except Exception as exp_exc:
                cleanup_verified = False
                cleanup_warning = f"session export error: {exp_exc}"[:200]

        # A message-level error in an earlier step may have been retried into
        # the verified final answer. Only keep it fatal when the turn could not
        # be verified.
        if turn_message_errors:
            if export_verified:
                warnings.extend(turn_message_errors)
            else:
                stream_errors.extend(turn_message_errors)

        # Always attempt session delete for one-shot invocations, even after timeout or failure
        if not keep_session and not resume_session_id:
            try:
                del_cmd = [binary, "session", "delete"]
                if has_standalone:
                    del_cmd.append("--standalone")
                del_cmd.append(session_id)
                del_code, del_stdout, del_stderr, del_timed_out, del_cleanup = run_process_with_timeout(
                    del_cmd, "", cwd, 15, env=env_for_proc
                )
                cleanup_verified = cleanup_verified and del_cleanup and not del_timed_out
                session_cleaned_up = del_code == 0
                if not session_cleaned_up:
                    cleanup_warning = (
                        del_stderr or del_stdout or "session delete returned non-zero"
                    ).strip()[:200]
            except Exception as exc:
                cleanup_verified = False
                cleanup_warning = str(exc)[:200]

    # Stream and export are independent evidence. Every model either reports
    # must match the requested identity; one source cannot mask the other.
    if export_verified:
        evidence_models = [model for model in stream_models if model] + turn_models
        conflicts = [
            model
            for model in evidence_models
            if not _verify_model_match(
                resolved_model,
                route["canonical_key"],
                model,
                expected_provider=route.get("provider"),
                expected_variant=variant,
            )
        ]
        if conflicts or not _models_compatible(evidence_models):
            stream_errors.append(
                "conflicting model evidence: " + ", ".join(sorted(set(evidence_models)))
            )

    # Retain session_id in metadata whenever --keep-session, --resume, or cleanup did not succeed
    retained_session_id = session_id if (keep_session or resume_session_id or not session_cleaned_up) else None
    # The verified export is authoritative; streamed text may be a partial
    # step before the final answer. Without an export, use the stream only when
    # its last finish reason was successful.
    stream_answer = (
        stream_final_text
        if last_finish_reason in OPENCODE_SUCCESS_FINISH
        else "".join(stream_step_text)
    ).strip()
    final_text = export_final_text.strip() if export_verified else stream_answer
    saw_completion = (
        export_verified if has_session_cmd else last_finish_reason in OPENCODE_SUCCESS_FINISH
    )

    err_combined = err + ("\n" + "\n".join(warnings + stream_errors) if (warnings or stream_errors) else "")
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
            "warnings": warnings,
            "denials_observed": denials_observed,
            "error_class": "timeout",
            "error": "Execution timed out.",
        }

    # Without session export there is no way to prove the turn is current or
    # that the executed model is the requested one. Fail closed on a resumed
    # stream and on any stream that does not report its model.
    if not has_session_cmd and (
        resume_session_id or not observed_model or "/" not in observed_model
    ):
        if not observed_model:
            stream_error = "OpenCode stream did not report the executed model."
            stream_class = "model_unavailable"
        elif "/" not in observed_model:
            stream_error = "OpenCode stream did not report the model provider."
            stream_class = "model_unavailable"
        else:
            stream_error = "A resumed OpenCode turn requires session-export verification."
            stream_class = "format"
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
            "warnings": warnings,
            "denials_observed": denials_observed,
            "error_class": stream_class,
            "error": stream_error,
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
            "warnings": warnings,
            "denials_observed": denials_observed,
            "error_class": "model_unavailable",
            "error": f"OpenCode model substitution detected: requested '{model_spec}', observed '{observed_model}'.",
        }

    if code != 0 or stream_errors or not saw_completion or not final_text:
        if final_text:
            (attempt_dir / "partial_result.md").write_text(final_text + "\n", encoding="utf-8")
        err_class = classify_failure(code, "", err_combined, timed_out=False)
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
            "warnings": warnings,
            "denials_observed": denials_observed,
            "error_class": err_class,
            "error": err_combined.strip()[:500] or _denial_error(denials_observed) or "OpenCode produced no verified completed output.",
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
        "warnings": warnings,
        "denials_observed": denials_observed,
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
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Invoke Antigravity (`agy`) using stream-json stdin so arbitrary prompt lengths work safely."""
    policy = policy or {}
    if policy.get("no_tools"):
        prompt_text = apply_tool_free_instruction(prompt_text, policy)
        print(
            "WARNING: Antigravity cannot disable tools through the CLI; the tool-free posture is "
            "prompt-enforced only and meta.json records tools_enforced=false.",
            file=sys.stderr,
        )
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
            "error_class": "no_option",
            "error": (
                f"Installed Antigravity CLI does not support --mode and --sandbox required for "
                f"{sandbox_mode} mode; retry with --sandbox bypass or choose another agent."
            ),
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
    stream_models: list[str] = []
    denials_observed: list[str] = []

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
        for key in ("step", "tool", "result"):
            candidate = ev.get(key)
            if isinstance(candidate, dict):
                denial = _tool_denial(candidate)
                if denial:
                    denials_observed.append(denial)
        if ev.get("model"):
            stream_models.append(str(ev["model"]))
            if not observed_model:
                observed_model = str(ev["model"])
        if ev.get("event") == "result" and isinstance(ev.get("result"), dict):
            res_obj = ev["result"]
            session_id = res_obj.get("conversation_id") or session_id
            raw_status = res_obj.get("status")
            result_status = str(raw_status).strip().upper() if raw_status is not None else None
            result_error = res_obj.get("error")
            if res_obj.get("model"):
                stream_models.append(str(res_obj["model"]))
                if not observed_model:
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
    model_changed = bool(stream_models) and not _models_compatible(stream_models)
    if model_changed:
        err_combined = (
            err_combined
            + " model changed within the stream: "
            + ", ".join(sorted(set(stream_models)))
        ).strip()
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
            "denials_observed": denials_observed,
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
            "denials_observed": denials_observed,
            "error_class": "model_unavailable",
            "error": f"Antigravity model substitution detected: requested '{resolved_model}', observed '{observed_model}'.",
        }
    if code != 0 or result_status != "SUCCESS" or not response_text or model_changed:
        if response_text:
            (attempt_dir / "partial_result.md").write_text(response_text + "\n", encoding="utf-8")
        err_class = classify_failure(code, "", err_combined, timed_out=False)
        if model_changed:
            err_class = "model_unavailable"
        elif code == 0 and result_status == "SUCCESS" and not response_text:
            err_class = "format"
        elif code == 0 and result_status is None and not model_changed:
            # No terminal `result` event: a protocol/shape change, not a
            # provider failure. Classify as format so self-repair and fallback
            # can run instead of reporting an opaque "failed".
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
            "denials_observed": denials_observed,
            "error_class": err_class,
            "error": err_combined[:500]
            or _denial_error(denials_observed)
            or (
                f"Antigravity returned status={result_status!r} (expected 'SUCCESS')."
                if result_status is not None
                else "Antigravity produced no terminal result event (protocol format changed?)."
            ),
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
        "denials_observed": denials_observed,
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
        "--safe-mode": "has_safe_mode",
        "--strict-mcp-config": "has_strict_mcp_config",
        "--ignore-rules": "has_ignore_rules",
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
    include_vendors: list[str] | None = None,
    max_tier: int | None = None,
    exclude_route_ids: list[str] | None = None,
    no_fallback: bool = False,
    force_route: bool = False,
    dry_run: bool = False,
    context_files: list[str] | None = None,
    references: list[str] | None = None,
    recommend_inspect: bool = False,
    allow_gh: bool = False,
    allow_network: bool = False,
    no_tools: bool = False,
    allow_shell: bool = False,
    isolate: bool = False,
    guard_enabled: bool = False,
    guard_excludes: list[str] | None = None,
    guard_max_files: int = 100_000,
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
    execution_policy = {
        "no_tools": bool(no_tools),
        "allow_shell": bool(allow_shell) and not no_tools,
        "isolate": bool(isolate),
    }

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

            if execution_policy["no_tools"]:
                if caller_supplied_sandbox is None and not resume_task_id:
                    effective_sandbox = "read-only"
                if effective_sandbox != "read-only":
                    raise ValueError(
                        "--no-tools requires --sandbox read-only; a tool-free prompt cannot be "
                        "combined with workspace-write or bypass execution."
                    )

            if guard_enabled and os.name == "nt":
                raise ValueError(
                    "--guard requires POSIX process-group semantics to confirm that no descendant "
                    "survived the attempt; it is not available on Windows."
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
                        incl_vendors={v.strip().lower() for v in (include_vendors or []) if v.strip()},
                        quality_floor=None,
                        is_explicit_route=True,
                        sandbox_mode=effective_sandbox,
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
                        include_vendors=include_vendors,
                        max_tier=max_tier,
                        exclude_route_ids=exclude_route_ids,
                        run_id=eff_run_id,
                        sandbox_mode=effective_sandbox,
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
                    include_vendors=include_vendors,
                    max_tier=max_tier,
                    exclude_route_ids=exclude_route_ids,
                    run_id=eff_run_id,
                    sandbox_mode=effective_sandbox,
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
                "tool_policy": execution_policy,
                "tools_enforced": tools_enforcement(planned["agent"], execution_policy, "dry_run"),
                "guard_enabled": guard_enabled,
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

            # The guard's own artifacts live under the run directory. When the
            # cache is inside --cwd, writing them between snapshot and verify
            # would otherwise look like a mutation.
            guard_patterns = list(guard_excludes or [])
            if guard_enabled:
                cwd_path = Path(effective_cwd).resolve()
                for owned in (Path(run_dir), get_cache_dir()):
                    try:
                        owned_rel = owned.resolve().relative_to(cwd_path).as_posix()
                    except ValueError:
                        continue
                    if owned_rel not in ("", "."):
                        for pattern in (owned_rel, f"{owned_rel}/**"):
                            if pattern not in guard_patterns:
                                guard_patterns.append(pattern)

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
                current_recipe_flags = dict(
                    agent_info.get("recipe_flags") or get_agent_recipe(agent, agent_version)
                )
                if agent == "opencode" and agent_version:
                    current_recipe_flags.setdefault("version", agent_version)

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

                    guard_baseline: dict[str, Any] | None = None
                    guard_dir: Path | None = None
                    if guard_enabled:
                        guard_dir = attempt_dir / "guard"
                        guard_dir.mkdir(parents=True, exist_ok=True)
                        guard_baseline = guard_snapshot(effective_cwd, guard_patterns, guard_max_files)
                        atomic_write_json(guard_dir / "baseline.json", guard_baseline)

                    try:
                        if guard_enabled and guard_baseline and guard_baseline.get("status") != "ok":
                            # Without a usable baseline the attempt cannot be
                            # verified, so do not run the agent at all.
                            res: dict[str, Any] = {
                                "status": "failed",
                                "exit_code": 2,
                                "session_id": resume_session_id,
                                "usage": {"input_tokens": 0, "output_tokens": 0},
                                "requested_model": route.get("requested_model") or route["model_id"],
                                "resolved_model": route.get("resolved_model_id") or route["model_id"],
                                "observed_model": None,
                                "session_cleaned_up": True,
                                "cleanup_verified": True,
                                "error_class": "guard_failed",
                                "error": f"Baseline snapshot failed: {guard_baseline.get('error')}",
                            }
                        elif not agent_info.get("usable") or not binary:
                            unusable_reasons = [
                                str(w).strip()
                                for w in (agent_info.get("warnings") or [])
                                if str(w).strip()
                            ]
                            detail = f" {' '.join(unusable_reasons)}" if unusable_reasons else ""
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
                                "error": f"Agent '{agent}' is not installed or not usable.{detail}",
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
                                policy=execution_policy,
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
                                policy=execution_policy,
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
                                policy=execution_policy,
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
                                policy=execution_policy,
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
                            # The adapter may have launched a process before
                            # raising, so its cleanup state is unknown, not clean.
                            "cleanup_verified": False,
                            "error_class": err_cls,
                            "error": f"Subprocess invocation error: {invoke_exc}",
                        }

                    if guard_enabled and guard_dir is not None:
                        if guard_baseline is None or guard_baseline.get("status") != "ok":
                            guard_report: dict[str, Any] = {
                                "status": "guard_failed",
                                "error": (guard_baseline or {}).get("error") or "baseline snapshot failed",
                            }
                        elif not res.get("cleanup_verified", True):
                            guard_report = {
                                "status": "guard_failed",
                                "error": (
                                    "subprocess cleanup was not confirmed; a descendant may still "
                                    "write protected inputs"
                                ),
                            }
                        else:
                            guard_report = guard_verify(
                                effective_cwd, guard_baseline, max_files=guard_max_files
                            )
                        atomic_write_json(guard_dir / "report.json", guard_report)
                        res["guard"] = {
                            "status": guard_report.get("status"),
                            "report_path": str(guard_dir / "report.json"),
                            "baseline_path": str(guard_dir / "baseline.json"),
                            "counts": guard_report.get("counts"),
                            "changed": guard_report.get("changed"),
                            "added": guard_report.get("added"),
                            "removed": guard_report.get("removed"),
                            "git_changes": guard_report.get("git_changes"),
                            "error": guard_report.get("error"),
                        }
                        if guard_report.get("status") != "clean":
                            res["status"] = "failed"
                            res["exit_code"] = 2
                            res["error_class"] = (
                                "mutated" if guard_report.get("status") == "mutated" else "guard_failed"
                            )
                            details: list[str] = []
                            for key in ("changed", "added", "removed", "git_changes"):
                                values = guard_report.get(key) or []
                                if values:
                                    details.append(
                                        f"{key}={len(values)} ({', '.join(str(v) for v in values[:5])})"
                                    )
                            res["error"] = "Protected inputs were not clean after the attempt" + (
                                ": " + "; ".join(details) if details else f": {guard_report.get('error')}"
                            )

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

                    attempt_result_path = attempt_dir / "result.md"
                    if res["status"] == "ok" and attempt_result_path.is_file():
                        atomic_publish_file(attempt_result_path, published_result_file)
                    attempt_result_file: Path | None = next(
                        (
                            candidate
                            for candidate in (attempt_result_path, attempt_dir / "partial_result.md")
                            if candidate.is_file()
                        ),
                        None,
                    )

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
                        # Always expose the attempt's own answer text, even when
                        # the attempt failed, so the caller never has to mine
                        # events.jsonl for it.
                        "attempt_result_file": str(attempt_result_file)
                        if attempt_result_file is not None
                        else None,
                        "lease_mode": lease.get("mode"),
                        "tool_policy": execution_policy,
                        "tools_enforced": tools_enforcement(
                            agent, execution_policy, str(res.get("status") or "failed")
                        ),
                        "denials_observed": res.get("denials_observed"),
                        "warnings": res.get("warnings") or [],
                        "model_verified": bool(res.get("observed_model")),
                        "guard": res.get("guard"),
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

                    # Unconfirmed cleanup can leave a descendant writing after the
                    # attempt. Never retry or fall back from it, even when the
                    # failure itself looks retryable.
                    if res.get("cleanup_verified") is not True:
                        return meta_payload

                    # A mutated tree or a guard that could not verify it invalidates the whole task;
                    # falling back to another route would hide the change and review a different tree.
                    if res.get("error_class") in ("mutated", "guard_failed"):
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
    parser.add_argument(
        "--include-vendor",
        action="append",
        help="Restrict automatic selection to model vendor(s) (repeatable).",
    )
    parser.add_argument(
        "--max-tier",
        type=int,
        help="Restrict automatic selection to models ranked at or above this vendor tier (1 = flagship).",
    )
    parser.add_argument("--exclude-route", action="append", help="Exclude an explicit route_id (repeatable).")
    parser.add_argument(
        "--no-tools",
        action="store_true",
        help=(
            "Tool-free posture (implies --sandbox read-only). Enforced by Claude Code and OpenCode; "
            "Codex and Antigravity report tools_enforced=false because their CLIs cannot disable tools."
        ),
    )
    parser.add_argument(
        "--allow-shell",
        action="store_true",
        help="In read-only mode, permit shell commands for inspection and focused tests; editing tools stay denied.",
    )
    parser.add_argument(
        "--isolate",
        action="store_true",
        help="Isolate the reviewer: Claude safe mode + strict MCP, OpenCode instruction/skill plugins off, Codex rules off.",
    )
    parser.add_argument(
        "--guard",
        action="store_true",
        help=(
            "Snapshot protected inputs before the attempt and verify them afterwards (opt-in; "
            "POSIX only, because Windows descendant cleanup cannot be verified)."
        ),
    )
    parser.add_argument(
        "--guard-exclude",
        action="append",
        default=[],
        help="Relative glob to keep out of the guard snapshot (repeatable), e.g. 'node_modules/**'.",
    )
    parser.add_argument(
        "--guard-max-files",
        type=int,
        default=100_000,
        help="Guard fails closed above this many files; narrow --cwd or add --guard-exclude.",
    )
    parser.add_argument("--no-fallback", action="store_true", help="Disable automatic fallback chain on failure.")
    parser.add_argument("--dry-run", action="store_true", help="Print planned route and prompt paths without running.")
    parser.add_argument("--cost-gate", type=int, metavar="NUM_CALLS", help="Evaluate cost/call gate for NUM_CALLS fan-out.")
    parser.add_argument("--report", action="store_true", help="Print the recursive subagent tree report for --run-id.")
    parser.add_argument("--only-significant", action="store_true", help="Filter --report to significant tasks only.")
    parser.add_argument("--human", action="store_true", help="Print human-readable output when used with --report.")

    args = parser.parse_args()

    if args.max_tier is not None and args.max_tier < 1:
        print("run: --max-tier must be at least 1", file=sys.stderr)
        return 2

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
        include_vendors=args.include_vendor,
        max_tier=args.max_tier,
        exclude_route_ids=args.exclude_route,
        no_fallback=args.no_fallback,
        force_route=args.force_route,
        dry_run=args.dry_run,
        context_files=args.context_file,
        references=args.reference,
        recommend_inspect=args.recommend_inspect,
        allow_gh=args.allow_gh,
        allow_network=args.allow_network,
        no_tools=args.no_tools,
        allow_shell=args.allow_shell,
        isolate=args.isolate,
        guard_enabled=args.guard,
        guard_excludes=args.guard_exclude,
        guard_max_files=args.guard_max_files,
        task_summary=args.summary,
        outcome_summary=args.outcome_summary,
        significant=sig_flag,
    )
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0 if res.get("status") in ("ok", "dry_run") else 1


if __name__ == "__main__":
    sys.exit(main())
