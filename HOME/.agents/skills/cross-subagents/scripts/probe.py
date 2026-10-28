#!/usr/bin/env python3
"""Probe installed coding agent CLIs, versions, authentication status, and executable recipes.

Usage:
  python3 probe.py [--agent <name>] [--refresh] [--human]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

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


def cache_cli_help_and_recipe(agent: str, binary: str, version: str | None) -> dict[str, Any]:
    """Capture `<agent> --help` (only when exit code is 0) and build versioned executable recipe flags."""
    help_dir = get_cache_dir() / "cli-help"
    help_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    help_file = help_dir / f"{agent}.txt"

    help_text = ""
    sub_help_text = ""
    try:
        proc = run_cmd([binary, "--help"], timeout=8)
        if proc.returncode == 0 and (proc.stdout or "").strip():
            help_text = strip_ansi(proc.stdout)
            if agent in ("codex", "opencode"):
                sub_cmd = "exec" if agent == "codex" else "run"
                sub_proc = run_cmd([binary, sub_cmd, "--help"], timeout=8)
                if sub_proc.returncode == 0 and (sub_proc.stdout or "").strip():
                    sub_help_text = strip_ansi(sub_proc.stdout)
            combined_help = help_text + ("\n\n--- SUBCOMMAND HELP ---\n" + sub_help_text if sub_help_text else "")
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
    all_help = f"{help_text}\n{sub_help_text}"

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
        }
    elif agent == "opencode":
        flags = {
            "cli_major": major or 2,
            "has_standalone": ("--standalone" in all_help) if all_help else (major >= 2),
            "has_auto": ("--auto" in all_help) if all_help else (major >= 2),
            "has_agent_flag": ("--agent" in all_help) if all_help else True,
            "has_format_json": ("--format" in all_help) if all_help else True,
            "has_session_cmd": ("session" in help_text) if help_text else True,
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
        }
    else:
        flags = {}

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
    version = None
    try:
        v_proc = run_cmd([binary, "--version"], timeout=8)
        if v_proc.returncode == 0:
            version = extract_version_string(v_proc.stdout or v_proc.stderr)
    except Exception:
        pass

    ver_tuple = parse_semver(version or "")
    major = ver_tuple[0] if ver_tuple is not None else 0
    host_version_ok = major >= 2
    target_version_ok = major >= 1
    warnings: list[str] = []

    if base["duplicate_installs"]:
        warnings.append(
            f"Multiple installations detected for opencode: {base['distinct_targets']}. Using {binary}."
        )

    if not target_version_ok:
        warnings.append(
            f"OpenCode version '{version}' could not be parsed as >= 1.0.0. Marking OpenCode unusable."
        )
        return {
            **base,
            "version": version,
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

    if not host_version_ok:
        warnings.append(
            f"OpenCode v1 ({version}) detected. OpenCode v1 cannot act as a host for this skill (`host_usable: false`), "
            "and upgrading to OpenCode v2 (>= 2.0.0) is recommended. Target CLI invocation remains available via v1 recipe."
        )

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
                clean = strip_ansi(a_proc_txt.stdout or "")
                for line in clean.splitlines():
                    m = re.search(r"[●•*]\s+(.+?)\s{2,}(API key|OAuth|Token|Configured)", line, re.IGNORECASE)
                    if m:
                        raw_name = m.group(1).strip()
                        auth_kind = m.group(2).strip().lower()
                        slug = raw_name.lower().replace(" ", "-")
                        providers.append(
                            {
                                "provider": slug,
                                "name": raw_name,
                                "auth_type": auth_kind,
                                "auth_method": auth_kind,
                            }
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
    target_usable = bool(target_version_ok and authenticated)
    host_usable = bool(host_version_ok and authenticated)
    return {
        **base,
        "version": version,
        "cli_major": major,
        "version_ok": host_version_ok,
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
