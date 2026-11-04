#!/usr/bin/env python3
"""Probe installed coding agent CLIs, versions, authentication status, and executable recipes.

Usage:
  python3 probe.py [--agent <name>] [--refresh] [--human]
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable

from common import (
    AGENT_BINARIES,
    SCHEMA_VERSION,
    canonical_agent_name,
    compute_agent_config_fingerprint,
    enforce_utf8,
    get_cache_dir,
    get_os_key,
    locked_update_json,
    now_iso,
    parse_iso_age_seconds,
    read_json_file,
    resolve_agent_binary,
)

PROBE_TTL_SECONDS = 86400  # 24 hours
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def probe_binary_version(binary: str) -> str | None:
    """Return a CLI's version string, or `None` when it cannot be probed."""
    try:
        proc = run_cmd([binary, "--version"], timeout=8)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return extract_version_string(proc.stdout or proc.stderr)


def mise_version_candidates(agent: str) -> list[str]:
    """Return binaries from installed mise versions, not just the active shim.

    `resolve_agent_binary` prefers the first executable on `PATH`. When that is
    a mise shim pinned to an old global version, a newer installed version is
    invisible to the probe even though the CLI supports the required flags. Scan
    the install tree for the usual layouts (`node_modules/.bin`, `bin`, and the
    install root) so the probe can prefer a supported version.
    """
    canon = canonical_agent_name(agent)
    names = AGENT_BINARIES.get(canon) or []
    data_dir = Path(
        os.environ.get("MISE_DATA_DIR") or (Path.home() / ".local" / "share" / "mise")
    )
    installs = data_dir / "installs" / canon
    if not installs.is_dir():
        return []
    found: list[str] = []
    for version_dir in sorted(
        (entry for entry in installs.iterdir() if entry.is_dir()), reverse=True
    ):
        for name in names:
            for rel in (Path("node_modules") / ".bin" / name, Path("bin") / name, Path(name)):
                candidate = version_dir / rel
                try:
                    if candidate.is_file() and os.access(str(candidate), os.X_OK):
                        path = str(candidate)
                        if path not in found:
                            found.append(path)
                except OSError:
                    continue
    return found


def select_supported_binary(
    candidates: list[str],
    min_major: int,
    *,
    version_probe: Callable[[str], str | None] = probe_binary_version,
) -> tuple[str | None, str | None, list[tuple[str, str]]]:
    """Pick the first candidate whose major version is at least `min_major`.

    Returns `(binary, version, probed)` where `probed` lists every candidate
    that answered a version query, in preference order. When no candidate meets
    the floor, the first probeable candidate is returned so the caller can
    report the unsupported version it actually has.
    """
    probed: list[tuple[str, str]] = []
    for candidate in candidates:
        version = version_probe(candidate)
        if version:
            probed.append((candidate, version))
    for candidate, version in probed:
        parsed = parse_semver(version)
        if parsed is not None and parsed[0] >= min_major:
            return candidate, version, probed
    if probed:
        return probed[0][0], probed[0][1], probed
    return (candidates[0] if candidates else None), None, probed



def strip_ansi(text: str) -> str:
    return ANSI_ESCAPE_RE.sub("", text)


def run_cmd(
    cmd: list[str],
    timeout: int = 12,
    input_text: str | None = None,
    stdin_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        input=input_text if input_text is not None else stdin_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def parse_semver(version_text: str) -> tuple[int, int, int] | None:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", version_text)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def extract_version_string(raw: str) -> str | None:
    m = re.search(r"(\d+\.\d+\.\d+(?:-[A-Za-z0-9._-]+)?)", raw)
    return m.group(1) if m else (raw.strip().splitlines()[0] if raw.strip() else None)


PARSER_PROBE_SENTINEL = "--cross-subagents-probe-unknown-option"

# Claude Code flags that `--help` cannot be trusted to advertise. 2.1.312 hides
# `--tools` and truncates the help text when stdout is a pipe, which can cut the
# list off before `--permission-mode`, `--safe-mode`, or `--effort`. Each entry
# is `(recipe key, CLI flag, sample value)`; the sample value is what the parser
# needs to consume, and "" means the flag takes no value.
CLAUDE_PARSER_FLAG_CHECKS: tuple[tuple[str, str, str], ...] = (
    ("has_tools", "--tools", ""),
    ("has_disallowed_tools", "--disallowedTools", "Edit"),
    ("has_allowed_tools", "--allowedTools", "Read"),
    ("has_permission_mode", "--permission-mode", "dontAsk"),
    ("has_permission_prompts", "--permission-prompts", "none"),
    ("has_safe_mode", "--safe-mode", ""),
    ("has_strict_mcp_config", "--strict-mcp-config", ""),
    ("has_no_session_persistence", "--no-session-persistence", ""),
    ("has_effort", "--effort", "high"),
    ("has_dangerously_skip_permissions", "--dangerously-skip-permissions", ""),
)


def parser_accepts_flag(binary: str, flag: str, value: str = "") -> bool | None:
    """Ask a CLI's argument parser whether it knows `flag`.

    `--help` is not authoritative: Claude Code omits supported options from it
    (2.1.312 lists no `--tools`) and truncates the help text when stdout is not
    a terminal. Appending a sentinel option makes the parser fail at argument
    parsing, before authentication or any model call, and the error names
    whichever option it rejected first. Returns `None` when inconclusive.
    """
    cmd = [binary, "-p", flag, value, PARSER_PROBE_SENTINEL, ""]
    try:
        proc = run_cmd(cmd, timeout=10, stdin_text="")
    except Exception:
        return None
    text = strip_ansi(f"{proc.stdout or ''}\n{proc.stderr or ''}")
    if PARSER_PROBE_SENTINEL in text:
        return True
    if "unknown option" in text.lower():
        return False
    return None


def cache_cli_help_and_recipe(agent: str, binary: str, version: str | None) -> dict[str, Any]:
    """Capture `<agent> --help` (only when exit code is 0) and build versioned executable recipe flags."""
    help_dir = get_cache_dir() / "cli-help"
    help_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    help_file = help_dir / f"{agent}.txt"

    help_text = ""
    sub_help_text = ""

    def _help_output(proc: subprocess.CompletedProcess[str]) -> str:
        # Some CLIs (notably OpenCode v1) print --help to stderr rather than stdout.
        return strip_ansi(f"{proc.stdout or ''}\n{proc.stderr or ''}").strip()

    try:
        proc = run_cmd([binary, "--help"], timeout=8)
        if proc.returncode == 0:
            help_text = _help_output(proc)
            if agent in ("codex", "opencode"):
                sub_cmd = "exec" if agent == "codex" else "run"
                sub_proc = run_cmd([binary, sub_cmd, "--help"], timeout=8)
                if sub_proc.returncode == 0:
                    sub_help_text = _help_output(sub_proc)
            combined_help = help_text + ("\n\n--- SUBCOMMAND HELP ---\n" + sub_help_text if sub_help_text else "")
            if combined_help.strip():
                help_file.write_text(combined_help, encoding="utf-8")
    except Exception:
        pass

    if not help_text and help_file.is_file():
        try:
            help_text = help_file.read_text(encoding="utf-8")
            if "--- SUBCOMMAND HELP ---" in help_text:
                help_text, sub_help_text = help_text.split("--- SUBCOMMAND HELP ---", 1)
        except Exception:
            pass

    ver_tuple = parse_semver(version or "")
    major = ver_tuple[0] if ver_tuple else 0
    all_help = "\n".join(part for part in (help_text, sub_help_text) if part).strip()
    # `run`, `exec`, and their sibling subcommands take their own flags plus a
    # subset of global flags. Prefer the subcommand help when it is available so
    # a top-level-only flag is never mistaken for a run-time flag.
    sub_help = sub_help_text.strip() or all_help

    if agent == "codex":
        flags = {
            "has_no_daemon": "--no-daemon" in help_text or not help_text,
            "has_ephemeral": "--ephemeral" in all_help or not all_help,
            "has_skip_git_repo_check": "--skip-git-repo-check" in all_help or not all_help,
            "has_json": "--json" in all_help or not all_help,
            "has_dangerously_bypass": "--dangerously-bypass-approvals-and-sandbox" in all_help or not all_help,
            "has_ignore_rules": "--ignore-rules" in all_help or not all_help,
        }
    elif agent == "claude-code":
        flags = {
            "has_no_session_persistence": "--no-session-persistence" in all_help or not all_help,
            "has_effort": "--effort" in all_help or not all_help,
            "has_permission_mode": "--permission-mode" in all_help or not all_help,
            "has_permission_prompts": "--permission-prompts" in all_help or not all_help,
            "has_tools": "--tools" in all_help or not all_help,
            "has_allowed_tools": "--allowedTools" in all_help or "--allowed-tools" in all_help or not all_help,
            "has_disallowed_tools": "--disallowedTools" in all_help or "--disallowed-tools" in all_help or not all_help,
            "has_dangerously_skip_permissions": "--dangerously-skip-permissions" in all_help or not all_help,
            "has_safe_mode": "--safe-mode" in all_help or not all_help,
            "has_strict_mcp_config": "--strict-mcp-config" in all_help or not all_help,
            "has_add_dir": "--add-dir" in all_help or not all_help,
        }
    elif agent == "opencode":
        flags = {
            "cli_major": major or 2,
            "has_standalone": ("--standalone" in sub_help) if sub_help else (major >= 2),
            "has_auto": ("--auto" in sub_help) if sub_help else (major >= 2),
            "has_agent_flag": ("--agent" in sub_help) if sub_help else True,
            "has_format_json": ("--format" in sub_help) if sub_help else True,
            "has_session_cmd": ("session" in all_help) if all_help else True,
        }
    elif agent == "antigravity":
        flags = {
            "has_input_format": "--input-format" in all_help or not all_help,
            "has_output_format": "--output-format" in all_help or not all_help,
            "has_effort": "--effort" in all_help or not all_help,
            "has_mode": "--mode" in all_help or not all_help,
            "has_agent": "--agent" in all_help or not all_help,
            "has_sandbox": "--sandbox" in all_help or not all_help,
            "has_dangerously_skip_permissions": "--dangerously-skip-permissions" in all_help or not all_help,
            "has_disable_slash_commands": "--disable-slash-commands" in all_help or not all_help,
            "has_print_timeout": "--print-timeout" in all_help or not all_help,
            "has_add_dir": "--add-dir" in all_help or not all_help,
            "has_settings": "--settings" in all_help or not all_help,
        }
    else:
        flags = {}

    if agent == "claude-code" and all_help:
        # Help text is truncated when stdout is a pipe, so a flag that is
        # missing there may still exist; confirm with the parser instead of
        # recording a false "unsupported" that blocks read-only runs.
        for key, cli_flag, sample in CLAUDE_PARSER_FLAG_CHECKS:
            if flags.get(key) is not False:
                continue
            verdict = parser_accepts_flag(binary, cli_flag, sample)
            if verdict is not None:
                flags[key] = verdict

    recipe_entry = {
        "agent": agent,
        "version": version or "unknown",
        "os": get_os_key(),
        "updated_at": now_iso(),
        "flags": flags,
    }

    recipes_file = get_cache_dir() / "recipes.json"

    def _update_recipes(current: dict[str, Any] | None) -> dict[str, Any]:
        base = current if isinstance(current, dict) else {"schema_version": SCHEMA_VERSION, "recipes": {}}
        base["schema_version"] = SCHEMA_VERSION
        key = f"{agent}:{version or 'unknown'}:{get_os_key()}"
        base.setdefault("recipes", {})[key] = recipe_entry
        base["recipes"][f"{agent}:latest:{get_os_key()}"] = recipe_entry
        return base

    locked_update_json(recipes_file, _update_recipes)
    return flags


def get_agent_recipe(agent: str, version: str | None = None) -> dict[str, Any]:
    """Read the cached flag recipe for an agent."""
    recipes_file = get_cache_dir() / "recipes.json"
    data = read_json_file(recipes_file) or {}
    recipes = data.get("recipes") or {}
    os_key = get_os_key()
    if version:
        entry = recipes.get(f"{agent}:{version}:{os_key}")
        if isinstance(entry, dict) and isinstance(entry.get("flags"), dict):
            return entry["flags"]
    latest = recipes.get(f"{agent}:latest:{os_key}")
    if isinstance(latest, dict) and isinstance(latest.get("flags"), dict):
        return latest["flags"]
    return {}


def probe_codex() -> dict[str, Any]:
    base = resolve_agent_binary("codex")
    if not base["installed"] or not base["binary"]:
        return {
            **base,
            "version": None,
            "version_ok": False,
            "authenticated": False,
            "auth_check_transient": False,
            "usable": False,
            "host_usable": False,
            "target_usable": False,
            "collected_at": now_iso(),
            "stale": False,
            "config_fingerprint": compute_agent_config_fingerprint("codex", None, None),
        }

    binary = base["binary"]
    version = None
    try:
        v_proc = run_cmd([binary, "--version"], timeout=8)
        if v_proc.returncode == 0:
            version = extract_version_string(v_proc.stdout or v_proc.stderr)
    except Exception:
        pass

    recipe_flags = cache_cli_help_and_recipe("codex", binary, version)

    authenticated = False
    auth_mode = None
    auth_transient = False
    try:
        a_proc = run_cmd([binary, "login", "status"], timeout=10)
        combined = strip_ansi(f"{a_proc.stdout}\n{a_proc.stderr}").strip()
        if a_proc.returncode == 0 and "logged in" in combined.lower():
            authenticated = True
            auth_mode = combined.splitlines()[0].strip()
        elif "not logged in" in combined.lower():
            authenticated = False
        elif a_proc.returncode != 0:
            auth_transient = True
    except Exception:
        auth_transient = True

    usable = bool(version and authenticated)
    return {
        **base,
        "version": version,
        "version_ok": version is not None,
        "authenticated": authenticated,
        "auth_check_transient": auth_transient,
        "auth_mode": auth_mode,
        "recipe_flags": recipe_flags,
        "usable": usable,
        "host_usable": usable,
        "target_usable": usable,
        "collected_at": now_iso(),
        "stale": False,
        "config_fingerprint": compute_agent_config_fingerprint("codex", binary, version),
        "warnings": (
            [
                f"Multiple installations detected for codex: {base['distinct_targets']}. Using {binary}."
            ]
            if base["duplicate_installs"]
            else []
        ),
    }


def probe_claude_code() -> dict[str, Any]:
    base = resolve_agent_binary("claude-code")
    if not base["installed"] or not base["binary"]:
        return {
            **base,
            "version": None,
            "version_ok": False,
            "authenticated": False,
            "auth_check_transient": False,
            "usable": False,
            "host_usable": False,
            "target_usable": False,
            "collected_at": now_iso(),
            "stale": False,
            "config_fingerprint": compute_agent_config_fingerprint("claude-code", None, None),
        }

    binary = base["binary"]
    version = None
    try:
        v_proc = run_cmd([binary, "--version"], timeout=8)
        if v_proc.returncode == 0:
            version = extract_version_string(v_proc.stdout or v_proc.stderr)
    except Exception:
        pass

    recipe_flags = cache_cli_help_and_recipe("claude-code", binary, version)

    authenticated = False
    auth_method = None
    plan_type = None
    api_provider = None
    auth_transient = False
    try:
        a_proc = run_cmd([binary, "auth", "status"], timeout=10)
        raw_out = strip_ansi(a_proc.stdout or "").strip()
        if raw_out.startswith("{"):
            auth_json = json.loads(raw_out)
            authenticated = bool(auth_json.get("loggedIn"))
            auth_method = auth_json.get("authMethod")
            plan_type = auth_json.get("subscriptionType")
            api_provider = auth_json.get("apiProvider")
        elif a_proc.returncode != 0:
            auth_transient = True
    except Exception:
        auth_transient = True

    usable = bool(version and authenticated)
    return {
        **base,
        "version": version,
        "version_ok": version is not None,
        "authenticated": authenticated,
        "auth_check_transient": auth_transient,
        "auth_method": auth_method,
        "plan_type": plan_type,
        "api_provider": api_provider,
        "recipe_flags": recipe_flags,
        "usable": usable,
        "host_usable": usable,
        "target_usable": usable,
        "collected_at": now_iso(),
        "stale": False,
        "config_fingerprint": compute_agent_config_fingerprint("claude-code", binary, version),
        "warnings": (
            [
                f"Multiple installations detected for claude-code: {base['distinct_targets']}. Using {binary}."
            ]
            if base["duplicate_installs"]
            else []
        ),
    }


def parse_opencode_auth_text(text: str) -> list[dict[str, str]]:
    """Parse `opencode auth list` text output across CLI output formats.

    OpenCode v2 prints ``OpenCode Go     API key   stored`` while v1 prints
    ``●  OpenCode Go api``. Both carry the provider name and credential method,
    but neither shape survives the other's field spacing or method labels.
    """
    providers: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_line in strip_ansi(text).splitlines():
        line = raw_line.strip().lstrip("│┃|").strip()
        if not line or line[0] in "┌└├─┐┘┤┬┴┼":
            continue
        line = re.sub(r"^[●•*\-]\s*", "", line).strip()
        match = re.search(
            r"\s+(api\s*key|api|oauth|token|configured)\b(?:\s+stored)?\s*$",
            line,
            re.IGNORECASE,
        )
        if not match:
            continue
        name = line[: match.start()].strip()
        if not name:
            continue
        method = re.sub(r"\s+", " ", match.group(1).lower())
        if method in ("api key", "api", "key"):
            method = "api_key"
        slug = name.lower().replace(" ", "-")
        if slug in seen:
            continue
        seen.add(slug)
        providers.append(
            {
                "provider": slug,
                "name": name,
                "auth_type": method,
                "auth_method": method,
            }
        )
    return providers


def probe_opencode() -> dict[str, Any]:
    base = resolve_agent_binary("opencode")
    if not base["installed"] or not base["binary"]:
        return {
            **base,
            "version": None,
            "version_ok": False,
            "authenticated": False,
            "auth_check_transient": False,
            "usable": False,
            "host_usable": False,
            "target_usable": False,
            "collected_at": now_iso(),
            "stale": False,
            "config_fingerprint": compute_agent_config_fingerprint("opencode", None, None),
        }

    binary = base["binary"]
    candidates: list[str] = []
    for candidate in [binary] + list(base.get("all_paths") or []) + mise_version_candidates(
        "opencode"
    ):
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    selected_binary, version, probed = select_supported_binary(candidates, 2)
    binary = selected_binary or binary
    warnings: list[str] = []
    if binary != base["binary"]:
        warnings.append(
            "Selected OpenCode binary "
            f"'{binary}' (version {version or 'unknown'}) instead of the PATH default "
            f"'{base['binary']}' because cross-subagents requires OpenCode v2 or later. "
            f"Probed: {', '.join(f'{path} ({ver})' for path, ver in probed)}."
        )
    base = {**base, "binary": binary, "all_paths": candidates}

    ver_tuple = parse_semver(version or "")
    major = ver_tuple[0] if ver_tuple is not None else 0
    version_ok = major >= 2

    if base["duplicate_installs"]:
        warnings.append(
            f"Multiple installations detected for opencode: {base['distinct_targets']}. Using {binary}."
        )

    if not version_ok:
        warnings.append(
            f"OpenCode version '{version}' is not supported: cross-subagents requires "
            "OpenCode v2 or later (>= 2.0.0). Marking OpenCode unusable; upgrade the CLI "
            "or select another agent."
        )
        return {
            **base,
            "version": version,
            "cli_major": major,
            "version_ok": False,
            "authenticated": False,
            "auth_check_transient": False,
            "authenticated_providers": [],
            "usable": False,
            "host_usable": False,
            "target_usable": False,
            "collected_at": now_iso(),
            "stale": False,
            "config_fingerprint": compute_agent_config_fingerprint("opencode", binary, version),
            "warnings": warnings,
        }

    recipe_flags = cache_cli_help_and_recipe("opencode", binary, version)

    providers: list[dict[str, str]] = []
    auth_cmd_ok = False
    auth_transient = False
    try:
        # Prefer structured JSON output from `opencode auth list --format json`
        a_proc = run_cmd([binary, "auth", "list", "--format", "json"], timeout=10)
        if a_proc.returncode == 0 and (a_proc.stdout or "").strip().startswith("["):
            auth_cmd_ok = True
            items = json.loads(a_proc.stdout)
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict) or not item.get("id"):
                        continue
                    prov_id = str(item["id"]).strip().lower()
                    conns = item.get("connections") or []
                    method = "configured"
                    if isinstance(conns, list) and conns and isinstance(conns[0], dict):
                        method = str(conns[0].get("method") or conns[0].get("label") or "configured").lower()
                    providers.append(
                        {
                            "provider": prov_id,
                            "name": str(item.get("name") or prov_id),
                            "auth_type": method,
                            "auth_method": method,
                        }
                    )
        else:
            # Fallback to text parsing for older CLI builds without `--format json`
            a_proc_txt = run_cmd([binary, "auth", "list"], timeout=10)
            if a_proc_txt.returncode == 0:
                auth_cmd_ok = True
                providers.extend(parse_opencode_auth_text(a_proc_txt.stdout or ""))
                if not providers:
                    warnings.append(
                        "`opencode auth list` succeeded but no provider credentials could be parsed."
                    )
            else:
                auth_transient = True
                warnings.append(
                    f"`opencode auth list` failed with exit code {a_proc_txt.returncode}: "
                    f"{strip_ansi(a_proc_txt.stderr or '').strip()[:200]}"
                )
    except Exception as exc:
        auth_transient = True
        warnings.append(f"`opencode auth list` raised an error: {exc}")

    # OpenCode includes built-in free models under the `opencode` provider even when no external provider credentials exist
    authenticated = auth_cmd_ok
    target_usable = bool(version_ok and authenticated)
    host_usable = bool(version_ok and authenticated)
    return {
        **base,
        "version": version,
        "cli_major": major,
        "version_ok": version_ok,
        "authenticated": authenticated,
        "auth_check_transient": auth_transient,
        "authenticated_providers": providers,
        "builtin_free_provider_available": auth_cmd_ok,
        "recipe_flags": recipe_flags,
        "usable": target_usable,
        "host_usable": host_usable,
        "target_usable": target_usable,
        "collected_at": now_iso(),
        "stale": False,
        "config_fingerprint": compute_agent_config_fingerprint("opencode", binary, version),
        "warnings": warnings,
    }


def probe_antigravity() -> dict[str, Any]:
    base = resolve_agent_binary("antigravity")
    if not base["installed"] or not base["binary"]:
        return {
            **base,
            "version": None,
            "version_ok": False,
            "authenticated": False,
            "auth_check_transient": False,
            "usable": False,
            "host_usable": False,
            "target_usable": False,
            "collected_at": now_iso(),
            "stale": False,
            "config_fingerprint": compute_agent_config_fingerprint("antigravity", None, None),
        }

    binary = base["binary"]
    version = None
    cli_version = None
    try:
        v_proc = run_cmd([binary, "--version"], timeout=8)
        if v_proc.returncode == 0:
            lines = [ln.strip() for ln in (v_proc.stdout or v_proc.stderr).splitlines() if ln.strip()]
            if lines:
                version = extract_version_string(lines[0])
            for ln in lines:
                if "cli" in ln.lower():
                    cli_version = extract_version_string(ln)
    except Exception:
        pass

    recipe_flags = cache_cli_help_and_recipe("antigravity", binary, version)

    authenticated = False
    model_count = 0
    auth_transient = False
    try:
        m_proc = run_cmd([binary, "models"], timeout=12)
        if m_proc.returncode == 0 and m_proc.stdout.strip():
            tsv_lines = [ln for ln in m_proc.stdout.splitlines() if "\t" in ln]
            model_count = len(tsv_lines)
            authenticated = model_count > 0
        elif m_proc.returncode != 0:
            auth_transient = True
    except Exception:
        auth_transient = True

    usable = bool(version and authenticated)
    return {
        **base,
        "version": version,
        "cli_version": cli_version,
        "version_ok": version is not None,
        "authenticated": authenticated,
        "auth_check_transient": auth_transient,
        "discovered_model_count": model_count,
        "recipe_flags": recipe_flags,
        "usable": usable,
        "host_usable": usable,
        "target_usable": usable,
        "collected_at": now_iso(),
        "stale": False,
        "config_fingerprint": compute_agent_config_fingerprint("antigravity", binary, version),
        "warnings": (
            [
                f"Multiple installations detected for antigravity: {base['distinct_targets']}. Using {binary}."
            ]
            if base["duplicate_installs"]
            else []
        ),
    }


PROBE_DISPATCH = {
    "codex": probe_codex,
    "claude-code": probe_claude_code,
    "opencode": probe_opencode,
    "antigravity": probe_antigravity,
}


def _agent_entry_is_fresh(agent: str, entry: dict[str, Any] | None) -> bool:
    if not isinstance(entry, dict) or entry.get("stale"):
        return False
    age = parse_iso_age_seconds(entry.get("collected_at") or entry.get("updated_at"))
    if age is None or age >= PROBE_TTL_SECONDS:
        return False
    # Resolve current binary on PATH and current config/auth file stat signature
    current_bin_info = resolve_agent_binary(agent)
    current_bin = current_bin_info.get("binary")
    if current_bin != entry.get("binary"):
        return False
    expected_fp = compute_agent_config_fingerprint(agent, current_bin, entry.get("version"))
    if entry.get("config_fingerprint") and entry.get("config_fingerprint") != expected_fp:
        return False
    return True


def probe_all(agents: list[str] | None = None, refresh: bool = False) -> dict[str, Any]:
    cache_path = get_cache_dir() / "agents.json"
    target_agents = [canonical_agent_name(a) for a in agents] if agents else list(AGENT_BINARIES.keys())

    existing = read_json_file(cache_path)
    existing_agents = (
        existing.get("agents")
        if isinstance(existing, dict)
        and existing.get("schema_version") == SCHEMA_VERSION
        and isinstance(existing.get("agents"), dict)
        else {}
    )

    agents_to_probe: list[str] = []
    for agent in target_agents:
        if refresh or not _agent_entry_is_fresh(agent, existing_agents.get(agent)):
            agents_to_probe.append(agent)

    if not agents_to_probe and existing:
        return existing

    fresh_results: dict[str, dict[str, Any]] = {}
    for agent in agents_to_probe:
        prev = existing_agents.get(agent)
        try:
            res = PROBE_DISPATCH[agent]()
            if res.get("auth_check_transient") and isinstance(prev, dict) and prev.get("usable"):
                stale_copy = dict(prev)
                stale_copy["stale"] = True
                stale_copy["probe_error"] = "Transient auth/status check failure; retaining previous usable state."
                fresh_results[agent] = stale_copy
            else:
                fresh_results[agent] = res
        except Exception as exc:
            if isinstance(prev, dict) and prev:
                stale_copy = dict(prev)
                stale_copy["stale"] = True
                stale_copy["probe_error"] = str(exc)
                fresh_results[agent] = stale_copy
            else:
                fresh_results[agent] = {
                    "agent": agent,
                    "installed": False,
                    "usable": False,
                    "host_usable": False,
                    "target_usable": False,
                    "stale": True,
                    "collected_at": now_iso(),
                    "probe_error": str(exc),
                }

    def _merge(current: dict[str, Any] | None) -> dict[str, Any]:
        merged_agents: dict[str, Any] = {}
        if (
            isinstance(current, dict)
            and current.get("schema_version") == SCHEMA_VERSION
            and isinstance(current.get("agents"), dict)
        ):
            merged_agents.update(current["agents"])
        merged_agents.update(fresh_results)
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_at": now_iso(),
            "stale": any(
                not _agent_entry_is_fresh(a_name, a_val)
                for a_name, a_val in merged_agents.items()
                if isinstance(a_val, dict)
            ),
            "os": get_os_key(),
            "cache_dir": str(get_cache_dir()),
            "agents": merged_agents,
        }

    return locked_update_json(cache_path, _merge)


def format_human_summary(data: dict[str, Any]) -> str:
    lines = [f"Cache directory: {data.get('cache_dir')} (updated: {data.get('updated_at')})"]
    agents = data.get("agents") or {}
    for name, info in agents.items():
        status = "USABLE" if info.get("usable") else ("INSTALLED (UNAUTH)" if info.get("installed") else "MISSING")
        if info.get("stale"):
            status += " [STALE]"
        ver = info.get("version") or "-"
        bin_path = info.get("binary") or "-"
        dup = " [DUPLICATE INSTALLS]" if info.get("duplicate_installs") else ""
        lines.append(f"- {name}: {status} | version={ver} | binary={bin_path}{dup}")
        for w in info.get("warnings") or []:
            lines.append(f"    warning: {w}")
    return "\n".join(lines)


def main() -> int:
    enforce_utf8()
    parser = argparse.ArgumentParser(description="Probe coding agent CLIs and authentication state.")
    parser.add_argument("--agent", action="append", help="Probe only the specified agent(s).")
    parser.add_argument("--refresh", action="store_true", help="Force fresh probe ignoring 24h TTL.")
    parser.add_argument("--human", action="store_true", help="Print concise human-readable summary.")
    args = parser.parse_args()

    result = probe_all(agents=args.agent, refresh=args.refresh)
    if args.human:
        print(format_human_summary(result))
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
