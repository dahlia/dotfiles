#!/usr/bin/env python3
"""Shared utilities for cross-subagents skill scripts.

Provides:
- Cross-platform cache directory resolution ($SKILL_CACHE_DIR)
- UTF-8 stream enforcement on Windows/macOS/Linux
- Atomic JSON read/write with file locking and locked read-modify-write (`locked_update_json`)
- Strict identifier validation against path traversal and Windows reserved names
- Portable threaded JSON-RPC client for `codex app-server`
- Binary discovery across PATH and `mise` shims (`which -a` / `where` equivalent)
- Route ID and canonical model key normalization
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import queue
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Iterator

SCHEMA_VERSION = 2
# Bump whenever `normalize_official_model_id`, `normalize_model_route`, or family/vendor inference changes,
# so cached `models/<agent>.json` route records are rebuilt instead of carrying stale derived fields.
ROUTE_NORMALIZER_VERSION = 3
SKILL_NAME = "cross-subagents"

AGENT_BINARIES: dict[str, list[str]] = {
    "codex": ["codex"],
    "claude-code": ["claude"],
    "opencode": ["opencode"],
    "antigravity": ["agy", "antigravity"],
}

AGENT_ALIASES: dict[str, str] = {
    "codex": "codex",
    "openai codex": "codex",
    "openai-codex": "codex",
    "claude": "claude-code",
    "claude-code": "claude-code",
    "claudecode": "claude-code",
    "claude code": "claude-code",
    "opencode": "opencode",
    "open-code": "opencode",
    "open code": "opencode",
    "agy": "antigravity",
    "antigravity": "antigravity",
}

OFFICIAL_AGENT_NAMES: dict[str, str] = {
    "codex": "Codex",
    "claude-code": "Claude Code",
    "opencode": "OpenCode",
    "antigravity": "Antigravity",
}

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max", "ultra")

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "COM1",
    "COM2",
    "COM3",
    "COM4",
    "COM5",
    "COM6",
    "COM7",
    "COM8",
    "COM9",
    "LPT1",
    "LPT2",
    "LPT3",
    "LPT4",
    "LPT5",
    "LPT6",
    "LPT7",
    "LPT8",
    "LPT9",
}

# Verified cross-provider aliases where a provider prefixes or alters a vendor release version
VERIFIED_CANONICAL_ALIASES: dict[str, str] = {
    "deepseek-v4.1-flash": "deepseek-flash",
    "deepseek-v4-1-flash": "deepseek-flash",
    "deepseek-v4.1-reasoner": "deepseek-reasoner",
    "deepseek-v4-1-reasoner": "deepseek-reasoner",
}

BARE_FAMILY_OR_INVALID_MODEL_IDS: set[str] = {
    "gpt",
    "gpt-5",
    "gpt-6",
    "claude",
    "gemini",
    "deepseek",
    "qwen",
    "glm",
    "kimi",
    "minimax",
    "mimo",
}


def enforce_utf8() -> None:
    """Force UTF-8 encoding on stdout/stderr across all platforms (especially Windows)."""
    os.environ["PYTHONIOENCODING"] = "utf-8"
    os.environ["PYTHONUTF8"] = "1"
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def validate_safe_identifier(value: str, label: str = "identifier") -> str:
    """Validate that an identifier is safe for use as a single path component on POSIX and Windows."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"Invalid {label}: value must be a non-empty string.")
    if value != value.strip():
        raise ValueError(f"Invalid {label}: '{value}' must not have leading or trailing whitespace.")
    if "/" in value or "\\" in value or ".." in value:
        raise ValueError(f"Invalid {label}: '{value}' must not contain path separators or '..'.")
    if value.endswith("."):
        raise ValueError(f"Invalid {label}: '{value}' must not end with a dot.")
    if not SAFE_ID_RE.match(value):
        raise ValueError(
            f"Invalid {label}: '{value}'. Use 1-128 ASCII alphanumeric, '.', '_', or '-' characters."
        )
    stem = value.split(".", 1)[0].upper()
    if stem in WINDOWS_RESERVED_NAMES:
        raise ValueError(f"Invalid {label}: '{value}' is a Windows reserved device name.")
    return value


def make_bounded_instance_id(prefix: str, task_id: str) -> str:
    """Build a unique, safe instance identifier bounded to <= 64 characters."""
    safe_prefix = re.sub(r"[^A-Za-z0-9_-]+", "", prefix)[:16] or "inst"
    safe_task = re.sub(r"[^A-Za-z0-9._-]+", "-", task_id).strip(".-_")[:32] or "task"
    candidate = f"{safe_prefix}-{safe_task}-{secrets.token_hex(4)}"
    return validate_safe_identifier(candidate, "instance_id")


def ensure_child_path(base_dir: Path, child_path: Path | str) -> Path:
    """Verify that child_path resolves strictly inside base_dir."""
    resolved_base = base_dir.resolve()
    cand = Path(child_path)
    resolved_child = cand.resolve() if cand.is_absolute() else (resolved_base / cand).resolve()
    if not resolved_child.is_relative_to(resolved_base) or resolved_child == resolved_base:
        raise ValueError(f"Path '{child_path}' escapes base directory '{base_dir}'.")
    return resolved_child


def canonical_agent_name(name: str) -> str:
    """Normalize agent name aliases (e.g. 'claude' -> 'claude-code', 'agy' -> 'antigravity')."""
    key = re.sub(r"\s+", " ", name.strip().lower())
    if key not in AGENT_ALIASES:
        raise ValueError(
            f"Unknown agent '{name}'. Expected one of: {', '.join(sorted(AGENT_BINARIES))}"
        )
    return AGENT_ALIASES[key]


def official_agent_name(name: str) -> str:
    """Return the official display name for a coding agent ('Codex', 'Claude Code', 'OpenCode', 'Antigravity')."""
    canon = canonical_agent_name(name)
    return OFFICIAL_AGENT_NAMES[canon]


def get_cache_dir() -> Path:
    """Resolve $SKILL_CACHE_DIR following OS conventions."""
    override = os.environ.get("CROSS_SUBAGENTS_CACHE_DIR")
    if override:
        path = Path(override).expanduser()
    elif os.name == "nt" or sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            path = Path(local_app_data) / SKILL_NAME
        else:
            path = Path.home() / "AppData" / "Local" / SKILL_NAME
    else:
        xdg_cache = os.environ.get("XDG_CACHE_HOME")
        if xdg_cache:
            path = Path(xdg_cache).expanduser() / SKILL_NAME
        else:
            path = Path.home() / ".cache" / SKILL_NAME

    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def now_iso() -> str:
    """Return current UTC time in ISO-8601 format (`YYYY-MM-DDTHH:MM:SSZ`)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_age_seconds(iso_str: str | None) -> float | None:
    """Return age in seconds of an ISO-8601 timestamp, or None if invalid/missing."""
    if not iso_str or not isinstance(iso_str, str):
        return None
    try:
        cleaned = iso_str.strip()
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
    except Exception:
        return None


@contextmanager
def file_lock(target_path: Path, timeout: float = 15.0) -> Iterator[None]:
    """Cross-platform advisory file lock using `.lock` companion file."""
    lock_path = target_path.with_suffix(target_path.suffix + ".lock")
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    start = time.monotonic()
    locked = False
    try:
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError as lock_err:
                if time.monotonic() - start >= timeout:
                    raise TimeoutError(
                        f"Timed out waiting {timeout:.1f}s for exclusive file lock on '{lock_path}'."
                    ) from lock_err
                time.sleep(0.05)
        yield
    finally:
        if locked:
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


def _write_json_unlocked(path: Path, data: Any) -> None:
    """Atomically write JSON to `path` via a temp file in the same directory (caller holds lock)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def atomic_write_json(path: Path, data: Any) -> None:
    """Atomically write JSON to `path` under an advisory file lock."""
    with file_lock(path):
        _write_json_unlocked(path, data)


def atomic_write_text(path: Path, text: str) -> None:
    """Atomically write text to `path` via a temp file in the same directory under advisory file lock."""
    with file_lock(path):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass


def read_json_file(path: Path, require_schema: bool = True) -> dict[str, Any] | None:
    """Read a JSON dictionary from `path`.

    When `require_schema=True`, requires `schema_version == SCHEMA_VERSION`.
    """
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        if not isinstance(data, dict):
            return None
        if require_schema and data.get("schema_version") != SCHEMA_VERSION:
            return None
        return data
    except Exception:
        return None


def locked_update_json(
    path: Path,
    update_fn: Callable[[dict[str, Any]], dict[str, Any]],
    default_factory: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Perform an atomic locked read-modify-write update on a JSON file."""
    with file_lock(path):
        current = read_json_file(path, require_schema=True)
        if current is None:
            current = (
                default_factory()
                if default_factory is not None
                else {"schema_version": SCHEMA_VERSION}
            )
        updated = update_fn(current)
        if isinstance(updated, dict) and "schema_version" not in updated:
            updated["schema_version"] = SCHEMA_VERSION
        _write_json_unlocked(path, updated)
        return updated


def codex_app_server_rpc(
    binary: str,
    requests: list[dict[str, Any]],
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Portable threaded JSON-RPC client for `codex app-server` over stdio."""
    proc = subprocess.Popen(
        [binary, "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    out_q: queue.Queue[dict[str, Any] | None] = queue.Queue()
    stderr_lines: list[str] = []

    def _read_stdout() -> None:
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    msg = json.loads(stripped)
                    if isinstance(msg, dict):
                        out_q.put(msg)
                except Exception:
                    continue
        finally:
            out_q.put(None)

    def _read_stderr() -> None:
        assert proc.stderr is not None
        try:
            for line in proc.stderr:
                if len(stderr_lines) < 50:
                    stderr_lines.append(line.rstrip())
        except Exception:
            pass

    t_out = threading.Thread(target=_read_stdout, daemon=True)
    t_err = threading.Thread(target=_read_stderr, daemon=True)
    t_out.start()
    t_err.start()

    responses: dict[Any, dict[str, Any]] = {}
    notifications: list[dict[str, Any]] = []
    expected_ids = {req["id"] for req in requests if "id" in req and req.get("id") != 1}

    try:
        assert proc.stdin is not None
        init_seq = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": SKILL_NAME, "version": "0.1.0"}},
            },
            {"jsonrpc": "2.0", "method": "initialized"},
        ]
        for msg in init_seq + requests:
            proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

        deadline = time.monotonic() + timeout
        while expected_ids - set(responses.keys()):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                item = out_q.get(timeout=min(0.25, remaining))
            except queue.Empty:
                if proc.poll() is not None:
                    break
                continue
            if item is None:
                break
            if "id" in item:
                responses[item["id"]] = item
            elif "method" in item:
                notifications.append(item)
    finally:
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass

    return {
        "responses": responses,
        "notifications": notifications,
        "stderr": "\n".join(stderr_lines),
    }


def _stat_signature(path: Path) -> str:
    """Return a non-secret stat fingerprint (`realpath:mtime_ns:size`) for a file or directory."""
    try:
        if not path.exists():
            return f"{path}:missing"
        rp = path.resolve()
        st = rp.stat()
        return f"{rp}:{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        return f"{path}:err"


def _env_digest(var_name: str) -> str:
    val = os.environ.get(var_name, "")
    if not val:
        return f"{var_name}=unset"
    return f"{var_name}={hashlib.sha256(val.encode('utf-8')).hexdigest()[:12]}"


def compute_agent_config_fingerprint(
    agent: str,
    binary: str | None = None,
    version: str | None = None,
) -> str:
    """Compute a non-secret fingerprint of current executable, config files, and auth/account identity."""
    canon = canonical_agent_name(agent)
    resolved = resolve_agent_binary(canon)
    active_binary = resolved.get("binary") or binary

    parts = [canon, version or ""]
    if active_binary:
        parts.append(_stat_signature(Path(active_binary)))
    else:
        parts.append("no-binary")

    home = Path.home()
    if canon == "codex":
        codex_home = Path(os.environ.get("CODEX_HOME") or (home / ".codex"))
        parts.append(_stat_signature(codex_home / "auth.json"))
        parts.append(_stat_signature(codex_home / "config.toml"))
        parts.append(_env_digest("OPENAI_API_KEY"))
        parts.append(_env_digest("OPENAI_BASE_URL"))
        parts.append(_env_digest("OPENAI_ORG_ID"))
    elif canon == "claude-code":
        claude_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (home / ".claude"))
        parts.append(_stat_signature(claude_dir / ".credentials.json"))
        parts.append(_stat_signature(claude_dir / "settings.json"))
        parts.append(_env_digest("ANTHROPIC_API_KEY"))
        parts.append(_env_digest("ANTHROPIC_AUTH_TOKEN"))
        parts.append(_env_digest("CLAUDE_CODE_USE_BEDROCK"))
        parts.append(_env_digest("CLAUDE_CODE_USE_VERTEX"))
        parts.append(_env_digest("AWS_PROFILE"))
        parts.append(_env_digest("GOOGLE_CLOUD_PROJECT"))
    elif canon == "opencode":
        xdg_data = Path(os.environ.get("XDG_DATA_HOME") or (home / ".local" / "share"))
        xdg_cfg = Path(os.environ.get("XDG_CONFIG_HOME") or (home / ".config"))
        parts.append(_stat_signature(xdg_data / "opencode" / "auth.json"))
        parts.append(_stat_signature(xdg_cfg / "opencode" / "opencode.json"))
        if os.environ.get("OPENCODE_CONFIG"):
            parts.append(_stat_signature(Path(os.environ["OPENCODE_CONFIG"])))
        parts.append(_env_digest("OPENCODE_CONFIG_CONTENT"))
    elif canon == "antigravity":
        agy_dir = home / ".gemini" / "antigravity-cli"
        parts.append(_stat_signature(agy_dir / "settings.json"))
        parts.append(_env_digest("GEMINI_API_KEY"))
        parts.append(_env_digest("GOOGLE_API_KEY"))

    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def find_all_executables(binary_name: str) -> list[str]:
    """Find all occurrences of binary_name on PATH (cross-platform equivalent of which -a / where)."""
    path_env = os.environ.get("PATH", "")
    if not path_env:
        return []

    if os.name == "nt":
        pathext = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";")
        has_ext = any(binary_name.lower().endswith(ext.lower()) for ext in pathext if ext)
        candidates = [binary_name] if has_ext else [binary_name + ext for ext in pathext if ext]
    else:
        candidates = [binary_name]

    found: list[str] = []
    for directory in path_env.split(os.pathsep):
        if not directory:
            continue
        dir_path = Path(directory)
        for cand in candidates:
            full = dir_path / cand
            try:
                if full.is_file() and os.access(str(full), os.X_OK):
                    full_str = str(full)
                    if full_str not in found:
                        found.append(full_str)
            except OSError:
                continue
    return found


def resolve_agent_binary(agent: str) -> dict[str, Any]:
    """Locate an agent's executable paths, resolving mise shims and detecting duplicates."""
    canon = canonical_agent_name(agent)
    names = AGENT_BINARIES[canon]
    all_paths: list[str] = []
    for name in names:
        for p in find_all_executables(name):
            if p not in all_paths:
                all_paths.append(p)

    selected: str | None = all_paths[0] if all_paths else None
    install_method = "path"
    resolved_binary = selected
    mise_bin = shutil.which("mise")

    if selected and ("/shims/" in selected.replace("\\", "/")):
        install_method = "mise-shim"
        if mise_bin:
            try:
                proc = subprocess.run(
                    [mise_bin, "which", Path(selected).stem],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=5,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    resolved_binary = proc.stdout.strip().splitlines()[0]
                    if resolved_binary not in all_paths:
                        all_paths.insert(0, resolved_binary)
            except Exception:
                pass
    elif selected and "/mise/installs/" in selected.replace("\\", "/"):
        install_method = "mise"

    if not resolved_binary and mise_bin:
        for name in names:
            try:
                proc = subprocess.run(
                    [mise_bin, "which", name],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=5,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    resolved_binary = proc.stdout.strip().splitlines()[0]
                    install_method = "mise"
                    all_paths.append(resolved_binary)
                    break
            except Exception:
                continue

    distinct_real_paths: list[str] = []
    for p in all_paths:
        rp = p
        if "/shims/" in p.replace("\\", "/") and mise_bin:
            try:
                proc = subprocess.run(
                    [mise_bin, "which", Path(p).stem],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=5,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    rp = proc.stdout.strip().splitlines()[0]
            except Exception:
                pass
        try:
            rp = str(Path(rp).resolve())
        except OSError:
            pass
        if rp not in distinct_real_paths:
            distinct_real_paths.append(rp)

    return {
        "agent": canon,
        "installed": resolved_binary is not None,
        "binary": resolved_binary,
        "all_paths": all_paths,
        "distinct_targets": distinct_real_paths,
        "duplicate_installs": len(distinct_real_paths) > 1,
        "install_method": install_method if resolved_binary else None,
    }


def get_os_key() -> str:
    """Return normalized OS identifier for recipe caches."""
    sys_name = platform.system().lower()
    if sys_name.startswith("linux"):
        return "linux"
    if sys_name.startswith("darwin"):
        return "macos"
    if sys_name.startswith("win"):
        return "windows"
    return sys_name


def normalize_official_model_id(
    raw_model_id: str,
    alias_lookup: dict[str, str] | None = None,
) -> tuple[str, str | None, str | None]:
    """Normalize a raw model string into `(official_model_id, extracted_effort, revision_date)`.

    Rules enforced:
    - Strip leading `<provider>/` prefix (e.g. `opencode-go/glm-5.3` -> `glm-5.3`)
    - Strip `#<variant>` suffix (e.g. `deepseek-flash#high` -> `deepseek-flash`)
    - Strip bracketed context-window or variant suffixes (e.g. `claude-sonnet-5[1m]` -> `claude-sonnet-5`)
    - Strip trailing release date stamp (`-YYYYMMDD` or `-MM-YYYY`)
    - Strip trailing effort variant suffix (`-low`, `-medium`, `-high`, `-xhigh`, `-max`, `-ultra`)
      such as `claude-opus-5-5-low` -> `claude-opus-5-5` or `gemini-4-argon-high` -> `gemini-4-argon`
    - Normalize DeepSeek V4.1+ versioned identifiers (`deepseek-v4.1-flash` -> `deepseek-flash`)
    - Resolve short CLI aliases (`fable`, `opus`, `sonnet`, `haiku`) via discovered catalog when available
    """
    cleaned = raw_model_id.strip()
    extracted_effort: str | None = None
    if "#" in cleaned:
        cleaned, extracted_effort = cleaned.split("#", 1)
        extracted_effort = extracted_effort.strip().lower() or None

    # Strip colons or convert tags (e.g. `deepseek-r1:free` -> `deepseek-r1-free`).
    # Colons are invalid in safe file path identifiers and canonical keys.
    cleaned = cleaned.replace(":", "-")

    # Strip provider prefix if present (e.g. opencode-go/glm-5.3 -> glm-5.3)
    if "/" in cleaned:
        cleaned = cleaned.split("/", 1)[1].strip()

    # Strip bracketed context-length or variant tags (e.g. claude-sonnet-5[1m] -> claude-sonnet-5)
    cleaned = re.sub(r"\[[^\]]*\]", "", cleaned).strip()

    lower_id = cleaned.lower()
    if alias_lookup and lower_id in alias_lookup:
        lower_id = alias_lookup[lower_id].lower()

    # Extract trailing release date suffix
    revision_date: str | None = None
    m_date = re.search(r"-(20\d{6}|\d{2}-20\d{2})$", lower_id)
    if m_date:
        revision_date = m_date.group(1)
        base_no_date = lower_id[: m_date.start()]
    else:
        base_no_date = lower_id

    # Strip trailing effort suffix (-low, -medium, -high, -xhigh, -max, -ultra) across all agents
    effort_suffix_families = ("claude-", "gpt-", "gemini-")
    for lvl in EFFORT_LEVELS:
        suffix = f"-{lvl}"
        # `-max`/`-ultra` are part of official names in some families (e.g. `qwen3-max`), so only
        # treat them as effort variants for families known to bake effort into route IDs.
        if lvl in ("max", "ultra") and not base_no_date.startswith(effort_suffix_families):
            continue
        if base_no_date.endswith(suffix) and len(base_no_date) > len(suffix):
            if not extracted_effort:
                extracted_effort = lvl
            base_no_date = base_no_date[: -len(suffix)]
            break

    norm = base_no_date.replace("_", "-")
    norm = re.sub(r"[^A-Za-z0-9._-]", "-", norm)
    norm = re.sub(r"-+", "-", norm).strip(".-")
    if norm.startswith("claude-"):
        norm = norm.replace(".", "-")

    # Normalize DeepSeek V4.1+ identifiers where official naming omits the version number
    norm = re.sub(r"^deepseek-v4(?:[.-]\d+)?-(flash|reasoner|chat)\b", r"deepseek-\1", norm)
    if norm in VERIFIED_CANONICAL_ALIASES:
        norm = VERIFIED_CANONICAL_ALIASES[norm]

    return norm, extracted_effort, revision_date


BARE_GENERATION_PATTERNS: dict[str, re.Pattern[str]] = {
    "gpt": re.compile(r"^gpt(?:-\d+(?:\.\d+)?)?$", re.IGNORECASE),
    "claude": re.compile(r"^claude(?:-\d+(?:\.\d+)?)?$", re.IGNORECASE),
    "gemini": re.compile(r"^gemini(?:-\d+(?:\.\d+)?)?$", re.IGNORECASE),
    "deepseek": re.compile(r"^deepseek(?:-v\d+(?:\.\d+)?)?$", re.IGNORECASE),
    "qwen": re.compile(r"^qwen(?:-\d+(?:\.\d+)?)?$", re.IGNORECASE),
    "glm": re.compile(r"^glm(?:-\d+)?$", re.IGNORECASE),
}


def is_bare_generation_prefix(model_id: str) -> bool:
    """Return True if model_id is a bare family or generation prefix rather than a concrete model."""
    cleaned = model_id.strip().lower()
    for pat in BARE_GENERATION_PATTERNS.values():
        if pat.match(cleaned):
            return True
    return False


def validate_official_model_id(model_id: str) -> str:
    """Validate that model_id is a concrete, official model identifier.

    Rejects bare generation prefixes such as `gpt-6`, `claude-5`, or `deepseek`.
    """
    cleaned = model_id.strip().lower()
    if is_bare_generation_prefix(cleaned):
        raise ValueError(
            f"'{model_id}' is a bare family or generation prefix, not a concrete official model identifier. "
            f"Specify a concrete model (e.g. 'gpt-6.1-sol', 'gpt-6-astra', 'claude-fable-5-1', 'deepseek-flash')."
        )
    return cleaned


def normalize_model_route(
    agent: str,
    provider: str,
    raw_model_id: str,
    display_name: str = "",
    efforts: list[str] | None = None,
    efforts_verified: bool = True,
    variant: str | None = None,
    hidden: bool = False,
    alias: str | None = None,
    quota_bucket: str | None = None,
    auth_plan: str | None = None,
) -> dict[str, Any]:
    """Normalize a raw model identifier into a structured route dictionary."""
    canon_agent = canonical_agent_name(agent)
    official_agent = OFFICIAL_AGENT_NAMES[canon_agent]
    clean_id = raw_model_id.strip()
    extracted_variant = variant
    if "#" in clean_id:
        clean_id, extracted_variant = clean_id.split("#", 1)

    canonical_key, baked_or_extracted_effort, revision_date = normalize_official_model_id(clean_id)
    if not extracted_variant and baked_or_extracted_effort:
        extracted_variant = baked_or_extracted_effort

    family = infer_model_family(canonical_key)
    vendor = infer_model_vendor(canonical_key, provider)

    norm_provider = provider.strip().lower()
    if canon_agent == "antigravity" and norm_provider in ("", "antigravity", "default"):
        norm_provider = vendor if vendor != "unknown" else "antigravity"

    resolved_efforts: list[str] = []
    if efforts:
        resolved_efforts = [e.lower() for e in efforts]
    elif baked_or_extracted_effort:
        resolved_efforts = [baked_or_extracted_effort]

    applicable_buckets = infer_applicable_quota_buckets(canon_agent, norm_provider, canonical_key, clean_id)
    primary_bucket = quota_bucket or applicable_buckets[-1]

    # Preserve explicit #variant in route_id when present (Antigravity keeps -high in clean_id)
    id_with_variant = (
        f"{clean_id}#{extracted_variant}"
        if (extracted_variant and canon_agent != "antigravity" and not clean_id.lower().endswith(f"-{extracted_variant}"))
        else clean_id
    )
    route_id = f"{canon_agent}/{norm_provider}/{id_with_variant}"
    norm_auth = (auth_plan or "unknown").strip().lower()
    auth_slug = re.sub(r"[^a-z0-9_-]+", "-", norm_auth).strip("-") or "unknown"
    execution_identity = f"{route_id}@{auth_slug}#{primary_bucket}"
    is_bare = is_bare_generation_prefix(canonical_key)

    return {
        "route_id": route_id,
        "execution_identity": execution_identity,
        "agent": canon_agent,
        "official_agent": official_agent,
        "provider": norm_provider,
        "vendor": vendor,
        "model_id": clean_id,
        "resolved_model_id": clean_id,
        "official_model_id": canonical_key,
        "family": family,
        "variant": extracted_variant,
        "revision_date": revision_date,
        "canonical_key": canonical_key,
        "efforts": resolved_efforts,
        "efforts_verified": efforts_verified,
        "display_name": display_name or id_with_variant,
        "alias": alias,
        "auth_plan": auth_plan or "unknown",
        "quota_bucket": primary_bucket,
        "applicable_buckets": applicable_buckets,
        "hidden": bool(hidden) or is_bare,
        "is_bare_prefix": is_bare,
    }


def infer_model_vendor(canonical_key: str, provider: str = "") -> str:
    """Infer the model creator/vendor for diversity filtering across any model family."""
    k = canonical_key.strip().lower()
    if k.startswith("claude-"):
        return "anthropic"
    if k.startswith("gpt-") or re.match(r"^o\d+\b", k):
        return "openai"
    if k.startswith("gemini-") or k.startswith("gemma-"):
        return "google"
    if k.startswith("deepseek-"):
        return "deepseek"
    if k.startswith("qwen"):
        return "qwen"
    if k.startswith("glm-"):
        return "zhipu"
    if k.startswith("kimi-"):
        return "moonshot"
    if k.startswith("minimax-"):
        return "minimax"
    if k.startswith("mimo-"):
        return "xiaomi"
    if k.startswith("llama-"):
        return "meta"
    if k.startswith("mistral-") or k.startswith("codestral"):
        return "mistral"
    prov = provider.strip().lower()
    if prov and prov not in ("opencode", "opencode-go", "github-copilot", "antigravity", "default"):
        return prov
    parts = k.split("-")
    return parts[0] if parts and parts[0] else "unknown"


def infer_model_family(canonical_key: str) -> str:
    """Group canonical model keys into a family directory name (open-ended across any model vendor)."""
    if canonical_key.startswith("claude-fable"):
        return "claude-fable"
    if canonical_key.startswith("claude-opus"):
        return "claude-opus"
    if canonical_key.startswith("claude-sonnet"):
        return "claude-sonnet"
    if canonical_key.startswith("claude-haiku"):
        return "claude-haiku"
    m_gpt = re.match(r"^(gpt-\d+(?:\.\d+)?)", canonical_key)
    if m_gpt:
        return m_gpt.group(1)
    if canonical_key.startswith("gpt-oss"):
        return "gpt-oss"
    if canonical_key.startswith("gpt-daybreak"):
        return "gpt-daybreak"
    m_gemini = re.match(r"^(gemini-\d+(?:\.\d+)?)", canonical_key)
    if m_gemini:
        return m_gemini.group(1)
    for prefix in ("deepseek", "qwen", "glm", "kimi", "minimax", "mimo", "llama", "mistral", "codestral", "gemma"):
        if canonical_key.startswith(prefix):
            return prefix
    parts = canonical_key.split("-")
    return "-".join(parts[:2]) if len(parts) >= 2 else canonical_key


def infer_applicable_quota_buckets(
    agent: str,
    provider: str,
    canonical_key: str,
    model_id: str | None = None,
) -> list[str]:
    """Return all quota buckets that must be eligible for a route to run."""
    c_key = (model_id or canonical_key).lower()
    if agent == "codex":
        return ["codex:codex"]
    if agent == "claude-code":
        buckets = ["claude-code:session", "claude-code:all-models"]
        if canonical_key.startswith("claude-fable") or "fable" in c_key:
            buckets.append("claude-code:fable")
        return buckets
    if agent == "opencode":
        return [f"opencode:{provider}"]
    if agent == "antigravity":
        return ["antigravity:default"]
    return [f"{agent}:{provider}"]


def infer_default_quota_bucket(agent: str, provider: str, canonical_key: str) -> str:
    """Infer primary quota bucket identifier for a route."""
    return infer_applicable_quota_buckets(agent, provider, canonical_key)[-1]
