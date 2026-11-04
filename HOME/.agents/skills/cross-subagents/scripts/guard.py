#!/usr/bin/env python3
"""Optional protected-input guard for cross-subagents attempts.

`run.py --guard` snapshots the working tree before a subagent starts and
verifies it afterwards, so a reviewer that silently edits, deletes, or moves
files cannot be mistaken for a clean read-only run. The guard is opt-in: the
default execution path does no snapshotting and pays no cost.

What is recorded:

  * every regular file and symlink under the root that is not excluded:
    SHA-256, mode, size, and symlink target. Directory symlinks are recorded
    but never traversed;
  * Git state for the root repository: HEAD (ref and resolved commit), the
    index, packed-refs, and the contents of in-progress merge/rebase markers.

Git-ignored untracked paths (generated build and codegen outputs such as
`dist/`, `.svelte-kit/`, or compiled test bundles) are skipped by default: a
read-only reviewer that runs a focused build regenerates them, and treating
those derived files as protected inputs discarded otherwise complete reviews.
Tracked files stay protected even when an ignore pattern matches them, because
`git ls-files --others` only lists untracked entries. Pass `--include-ignored`
(CLI) or `include_ignored=True` (`snapshot`) to protect them anyway.

What is not covered: Git object storage, reflogs, external symlink targets,
machine-wide state, and the Git metadata of nested repositories. Nested
repository working trees are walked and hashed like any other directory.

The guard is a change detector, not a sandbox. Process-group checks catch an
ordinary background child, but a descendant that creates a new session or
process group (`setsid`, daemonization) escapes that check and can still write
after verification. Pair the guard with a read-only posture; do not treat a
clean report as proof that no transient write occurred.

Unreadable files, unreadable directories, and unreadable Git metadata fail the
snapshot closed (`guard_failed`) rather than reporting an unverifiable tree as
clean.

Usage:
  python3 guard.py snapshot --root R --out baseline.json [--exclude PATTERN]...
  python3 guard.py verify --root R --baseline baseline.json --out report.json [--exclude PATTERN]...

Exit codes: 0 clean, 10 mutated, 2 guard_failed (snapshot or verification
could not be completed).
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 2
DEFAULT_MAX_FILES = 100_000
# Version-control and tool caches that never carry review-relevant input.
# Repository Git metadata is recorded separately by `git_state()`.
ALWAYS_SKIP_DIRS = frozenset({".git", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"})
CHUNK_SIZE = 1024 * 1024
GIT_IGNORED_TIMEOUT_SECONDS = 30


def _normalize_excludes(patterns: Iterable[str] | None) -> list[str]:
    cleaned: list[str] = []
    for raw in patterns or []:
        p = str(raw).strip().replace(os.sep, "/")
        while p.startswith("./"):
            p = p[2:]
        p = p.lstrip("/")
        if p:
            cleaned.append(p.rstrip("/"))
    return cleaned


def _is_excluded(rel: str, patterns: list[str]) -> bool:
    if not patterns:
        return False
    for pattern in patterns:
        variants = [pattern]
        if pattern.startswith("**/"):
            # `**/scripts` must also exclude descendants of a root-level
            # `scripts` directory, not just a nested one.
            variants.append(pattern[3:])
        for p in variants:
            if fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(rel, f"{p}/*"):
                return True
    return False


def _is_ignored(rel: str, ignored: Iterable[str]) -> bool:
    """True when `rel` is (or lives under) a Git-ignored path.

    `git ls-files --directory` lists ignored directories with a trailing slash
    and files without one; a file entry must match exactly so `debug.log` does
    not also skip `debug.log.bak`.
    """
    for entry in ignored:
        if entry.endswith("/"):
            if rel == entry[:-1] or rel.startswith(entry):
                return True
        elif rel == entry:
            return True
    return False


def _git_ignored_paths(root: Path) -> tuple[list[str], str | None]:
    """Return untracked paths Git ignores, relative to `root`.

    Git applies ignore rules to untracked files, which is exactly the set of
    generated build and codegen outputs a read-only reviewer may recreate (for
    example `dist/`, `.svelte-kit/`, or compiled test bundles). Tracked files
    stay protected even when an ignore pattern matches them, because `git
    ls-files --others` only lists untracked entries. A missing `git` binary or
    a failed call is not an error: the guard then scans those paths like any
    other file (more protection, never less), and the reason is recorded in the
    snapshot.
    """
    if shutil.which("git") is None:
        return [], "git executable not found"
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                "--directory",
                "-z",
            ],
            capture_output=True,
            timeout=GIT_IGNORED_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [], f"{type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        return [], detail[:200] or f"git ls-files exited {proc.returncode}"
    paths = [item for item in proc.stdout.decode("utf-8", "replace").split("\0") if item]
    return paths, None


def _sha256_file(path: Path) -> tuple[str | None, str | None]:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest(), None
    except OSError as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _file_entry(path: Path) -> tuple[dict[str, Any], str | None]:
    """Return `(entry, error)`. A non-null error makes the snapshot fail closed."""
    try:
        st = path.lstat()
    except OSError as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}, f"{type(exc).__name__}: {exc}"
    if stat.S_ISLNK(st.st_mode):
        try:
            target = os.readlink(path)
        except OSError as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}, f"{type(exc).__name__}: {exc}"
        return {"type": "symlink", "target": target, "mode": stat.S_IMODE(st.st_mode)}, None
    if stat.S_ISREG(st.st_mode):
        digest, error = _sha256_file(path)
        if error:
            return (
                {"type": "file", "error": error, "mode": stat.S_IMODE(st.st_mode), "size": st.st_size},
                error,
            )
        return (
            {"type": "file", "sha256": digest, "mode": stat.S_IMODE(st.st_mode), "size": st.st_size},
            None,
        )
    return {"type": "other", "mode": stat.S_IMODE(st.st_mode)}, None


def _tree_digest(path: Path) -> str | None:
    """Hash a directory's files and symlinks, without following symlinks."""
    digest = hashlib.sha256()
    errors: list[str] = []

    def _onerror(exc: OSError) -> None:
        errors.append(str(exc))

    for dirpath, dirnames, filenames in os.walk(path, followlinks=False, onerror=_onerror):
        dirnames[:] = sorted(d for d in dirnames if d not in ALWAYS_SKIP_DIRS)
        base = Path(dirpath)
        for name in sorted(filenames) + sorted(dirnames):
            full = base / name
            rel = full.relative_to(path).as_posix()
            entry, error = _file_entry(full)
            if error:
                errors.append(f"{rel}: {error}")
            digest.update(rel.encode("utf-8", "replace"))
            digest.update(json.dumps(entry, sort_keys=True).encode("utf-8"))
    if errors:
        return None
    return digest.hexdigest()


def _marker_digest(path: Path) -> dict[str, Any] | None:
    if not os.path.lexists(path):
        return None
    if path.is_file():
        digest, error = _sha256_file(path)
        return {"kind": "file", "sha256": digest, "error": error}
    if path.is_dir():
        digest = _tree_digest(path)
        return {"kind": "dir", "sha256": digest, "error": None if digest else "unreadable directory"}
    return {"kind": "other", "sha256": None, "error": None}


def _resolve_gitdir(root: Path) -> tuple[Path | None, str | None]:
    """Return `(gitdir, error)`. Absent Git metadata is not an error."""
    dot_git = root / ".git"
    try:
        if dot_git.is_dir():
            return dot_git, None
        if dot_git.is_file():
            text = dot_git.read_text(encoding="utf-8", errors="replace").strip()
            if not text.lower().startswith("gitdir:"):
                return None, f"unrecognized .git pointer: {text[:120]}"
            target = text.split(":", 1)[1].strip()
            candidate = Path(target)
            if not candidate.is_absolute():
                candidate = (root / candidate).resolve()
            if not candidate.is_dir():
                return None, f"gitdir target is missing or not a directory: {candidate}"
            return candidate, None
        if os.path.lexists(dot_git):
            return None, f"cannot inspect .git at {dot_git}"
        return None, None
    except OSError as exc:
        return None, f"cannot read .git: {type(exc).__name__}: {exc}"


def _read_text_state(path: Path) -> tuple[str | None, str | None]:
    """Return `(text, error)`; a missing file is not an error, an unreadable one is."""
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip(), None
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _resolve_ref(gitdir: Path, ref: str) -> tuple[str | None, str | None]:
    common, common_error = _read_text_state(gitdir / "commondir")
    if common_error:
        return None, f"commondir: {common_error}"
    common_dir = (gitdir / common).resolve() if common else gitdir
    for base in (gitdir, common_dir):
        value, error = _read_text_state(base / ref)
        if error:
            return None, f"{ref}: {error}"
        if value:
            return value, None
    for base in (common_dir, gitdir):
        packed, packed_error = _read_text_state(base / "packed-refs")
        if packed_error:
            return None, f"packed-refs: {packed_error}"
        if not packed:
            continue
        for line in packed.splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "^")):
                continue
            parts = line.split(" ", 1)
            if len(parts) == 2 and parts[1] == ref:
                return parts[0], None
    return None, None


def git_state(root: Path) -> dict[str, Any]:
    """Record the parts of Git state a reviewer must not mutate."""
    gitdir, gitdir_error = _resolve_gitdir(root)
    if gitdir_error:
        return {
            "is_repo": True,
            "error": f"cannot resolve Git metadata: {gitdir_error}",
        }
    if gitdir is None:
        return {"is_repo": False}
    common, common_error = _read_text_state(gitdir / "commondir")
    if common_error:
        return {
            "is_repo": True,
            "gitdir": str(gitdir),
            "error": f"cannot read Git commondir: {common_error}",
        }
    common_dir = (gitdir / common).resolve() if common else gitdir
    if common and not common_dir.is_dir():
        return {
            "is_repo": True,
            "gitdir": str(gitdir),
            "error": f"Git commondir target is missing: {common_dir}",
        }
    head_raw, head_error = _read_text_state(gitdir / "HEAD")
    if head_error:
        return {
            "is_repo": True,
            "gitdir": str(gitdir),
            "error": f"cannot read Git HEAD: {head_error}",
        }
    if not head_raw:
        return {
            "is_repo": True,
            "gitdir": str(gitdir),
            "error": "cannot read Git HEAD",
        }
    head_ref = head_raw[5:].strip() if head_raw.lower().startswith("ref:") else None
    resolved: str | None = head_raw
    if head_ref:
        resolved, ref_error = _resolve_ref(gitdir, head_ref)
        if ref_error:
            return {
                "is_repo": True,
                "gitdir": str(gitdir),
                "error": f"cannot resolve Git ref: {ref_error}",
            }
    index_path = gitdir / "index"
    index_digest, index_error = _sha256_file(index_path) if index_path.is_file() else (None, None)
    if index_error:
        return {
            "is_repo": True,
            "gitdir": str(gitdir),
            "error": f"cannot read Git index: {index_error}",
        }
    packed = common_dir / "packed-refs"
    packed_digest, packed_error = _sha256_file(packed) if packed.is_file() else (None, None)
    if packed_error:
        return {
            "is_repo": True,
            "gitdir": str(gitdir),
            "error": f"cannot read packed-refs: {packed_error}",
        }
    markers = {}
    for name in (
        "MERGE_HEAD",
        "REBASE_HEAD",
        "CHERRY_PICK_HEAD",
        "BISECT_LOG",
        "rebase-merge",
        "rebase-apply",
        "sequencer",
    ):
        markers[name] = _marker_digest(gitdir / name)
    marker_errors = [
        f"{name}: {entry['error']}"
        for name, entry in markers.items()
        if isinstance(entry, dict) and entry.get("error")
    ]
    if marker_errors:
        return {
            "is_repo": True,
            "gitdir": str(gitdir),
            "error": f"cannot read Git marker(s): {'; '.join(marker_errors)}",
        }
    return {
        "is_repo": True,
        "gitdir": str(gitdir),
        "commondir": common,
        "common_dir": str(common_dir),
        "head_raw": head_raw,
        "head_ref": head_ref,
        "head_sha": resolved,
        "index_sha256": index_digest,
        "packed_refs_sha256": packed_digest,
        "state_markers": markers,
    }


def snapshot(
    root: Path | str,
    excludes: Iterable[str] | None = None,
    max_files: int = DEFAULT_MAX_FILES,
    include_ignored: bool = False,
) -> dict[str, Any]:
    """Hash the protected working tree and Git state under `root`."""
    root_path = Path(root).expanduser().resolve()
    if not root_path.is_dir():
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "guard_failed",
            "root": str(root_path),
            "error": f"guard root is not a directory: {root_path}",
        }
    patterns = _normalize_excludes(excludes)
    ignored, ignored_error = (
        ([], None) if include_ignored else _git_ignored_paths(root_path)
    )
    files: dict[str, Any] = {}
    errors: list[str] = []
    scanned = 0
    pruned_ignored = 0

    def _onerror(exc: OSError) -> None:
        errors.append(f"walk: {type(exc).__name__}: {exc}")

    for dirpath, dirnames, filenames in os.walk(
        root_path, topdown=True, onerror=_onerror, followlinks=False
    ):
        dirnames[:] = sorted(d for d in dirnames if d not in ALWAYS_SKIP_DIRS)
        base = Path(dirpath)

        # Directory symlinks are recorded but never traversed; os.walk keeps
        # them in `dirnames` because followlinks=False.
        for name in list(dirnames):
            full = base / name
            rel = full.relative_to(root_path).as_posix()
            if _is_ignored(rel, ignored):
                dirnames.remove(name)
                pruned_ignored += 1
                continue
            if _is_excluded(rel, patterns):
                dirnames.remove(name)
                continue
            try:
                is_link = full.is_symlink()
            except OSError as exc:
                errors.append(f"{rel}: {type(exc).__name__}: {exc}")
                dirnames.remove(name)
                continue
            if is_link:
                entry, error = _file_entry(full)
                if error:
                    errors.append(f"{rel}: {error}")
                files[rel] = entry
                dirnames.remove(name)

        for name in sorted(filenames):
            full = base / name
            rel = full.relative_to(root_path).as_posix()
            if _is_ignored(rel, ignored):
                pruned_ignored += 1
                continue
            if _is_excluded(rel, patterns):
                continue
            scanned += 1
            if scanned > max_files:
                return {
                    "schema_version": SCHEMA_VERSION,
                    "status": "guard_failed",
                    "root": str(root_path),
                    "error": (
                        f"protected input exceeds --guard-max-files={max_files} "
                        f"(scanned {scanned}); narrow --cwd or add --guard-exclude"
                    ),
                    "counts": {"files": len(files)},
                    "excludes": patterns,
                    "include_ignored": include_ignored,
                }
            entry, error = _file_entry(full)
            if error:
                errors.append(f"{rel}: {error}")
            files[rel] = entry

    git = git_state(root_path)
    if git.get("error"):
        errors.append(f"git: {git['error']}")
    if errors:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "guard_failed",
            "root": str(root_path),
            "error": f"{len(errors)} protected input(s) could not be read",
            "errors": errors[:50],
            "counts": {"files": len(files)},
            "excludes": patterns,
            "include_ignored": include_ignored,
            "ignored": ignored,
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        "root": str(root_path),
        "files": files,
        "git": git,
        "counts": {"files": len(files), "ignored_pruned": pruned_ignored},
        "excludes": patterns,
        "include_ignored": include_ignored,
        "ignored": ignored,
        "ignored_error": ignored_error,
    }


def _git_changes(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    if before.get("is_repo") != after.get("is_repo"):
        return ["git:repository-presence"]
    if not before.get("is_repo"):
        return []
    changes: list[str] = []
    for key in (
        "gitdir",
        "commondir",
        "common_dir",
        "head_raw",
        "head_ref",
        "head_sha",
        "index_sha256",
        "packed_refs_sha256",
    ):
        if before.get(key) != after.get(key):
            changes.append(f"git:{key}")
    before_markers = before.get("state_markers") or {}
    after_markers = after.get("state_markers") or {}
    for name in sorted(set(before_markers) | set(after_markers)):
        if before_markers.get(name) != after_markers.get(name):
            changes.append(f"git:state:{name}")
    return changes


def verify(
    root: Path | str,
    baseline: dict[str, Any],
    excludes: Iterable[str] | None = None,
    max_files: int = DEFAULT_MAX_FILES,
    include_ignored: bool | None = None,
) -> dict[str, Any]:
    """Re-hash the tree and report any difference from `baseline`."""
    if not isinstance(baseline, dict) or baseline.get("status") != "ok":
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "guard_failed",
            "error": "baseline snapshot is missing or was not completed",
        }
    baseline_files = baseline.get("files") or {}
    # Verification must keep the baseline's exclusions and may add guard-owned
    # outputs (such as this run's report path). The union is deterministic, so
    # repeated verifications compare the same set.
    patterns = list(
        dict.fromkeys((baseline.get("excludes") or []) + _normalize_excludes(excludes))
    )
    eff_include_ignored = (
        bool(baseline.get("include_ignored", False))
        if include_ignored is None
        else include_ignored
    )
    current = snapshot(
        root,
        excludes=patterns,
        max_files=max_files,
        include_ignored=eff_include_ignored,
    )
    if current.get("status") != "ok":
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "guard_failed",
            "root": current.get("root"),
            "error": current.get("error") or "verification snapshot failed",
            "errors": current.get("errors"),
        }

    # Apply the effective exclusions and ignored paths to both maps. Otherwise
    # a path that became ignored during the attempt would be reported as
    # removed/added even though it is a derived artifact.
    ignored = list(
        dict.fromkeys(
            (baseline.get("ignored") or []) + (current.get("ignored") or [])
        )
    )
    before_files = {
        rel: entry
        for rel, entry in baseline_files.items()
        if not _is_excluded(rel, patterns) and not _is_ignored(rel, ignored)
    }
    after_files = {
        rel: entry
        for rel, entry in (current.get("files") or {}).items()
        if not _is_excluded(rel, patterns) and not _is_ignored(rel, ignored)
    }
    changed = sorted(
        rel
        for rel in (set(before_files) & set(after_files))
        if before_files[rel] != after_files[rel]
    )
    added = sorted(set(after_files) - set(before_files))
    removed = sorted(set(before_files) - set(after_files))
    git_changes = _git_changes(baseline.get("git") or {}, current.get("git") or {})
    mutated = bool(changed or added or removed or git_changes)

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "mutated" if mutated else "clean",
        "root": current.get("root"),
        "changed": changed[:200],
        "added": added[:200],
        "removed": removed[:200],
        "git_changes": git_changes,
        "truncated": len(changed) > 200 or len(added) > 200 or len(removed) > 200,
        "counts": {
            "before": len(before_files),
            "after": len(after_files),
            "changed": len(changed),
            "added": len(added),
            "removed": len(removed),
        },
    }


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _relative_to_root(root: str, path: str) -> str | None:
    try:
        return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Snapshot and verify protected inputs around an attempt.")
    sub = parser.add_subparsers(dest="command", required=True)

    snap_p = sub.add_parser("snapshot", help="Record a baseline snapshot.")
    snap_p.add_argument("--root", required=True)
    snap_p.add_argument("--out", required=True)
    snap_p.add_argument("--exclude", action="append", default=[])
    snap_p.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    snap_p.add_argument(
        "--include-ignored",
        action="store_true",
        help="Also protect Git-ignored (untracked) files; generated build outputs are skipped by default.",
    )

    ver_p = sub.add_parser("verify", help="Verify a tree against a baseline snapshot.")
    ver_p.add_argument("--root", required=True)
    ver_p.add_argument("--baseline", required=True)
    ver_p.add_argument("--out", required=True)
    ver_p.add_argument("--exclude", action="append", default=[])
    ver_p.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    ver_p.add_argument(
        "--include-ignored",
        action="store_true",
        default=None,
        help="Override the baseline's ignored-file posture (default: inherit it).",
    )

    args = parser.parse_args(argv)
    if args.command == "snapshot":
        excludes = list(args.exclude)
        # The baseline file is guard-owned output; keep it out of the snapshot
        # so a store inside the root cannot trigger its own mutation report.
        baseline_rel = _relative_to_root(args.root, args.out)
        if baseline_rel:
            excludes.append(baseline_rel)
        data = snapshot(args.root, excludes, args.max_files, include_ignored=args.include_ignored)
        _write_json(Path(args.out), data)
        if data["status"] != "ok":
            print(f"guard: {data.get('error')}", file=sys.stderr)
            for detail in (data.get("errors") or [])[:10]:
                print(f"  {detail}", file=sys.stderr)
            return 2
        print(f"guard: snapshot {data['counts']['files']} files under {data['root']}")
        return 0

    try:
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"guard: cannot read baseline: {exc}", file=sys.stderr)
        return 2
    verify_excludes = list(args.exclude)
    report_rel = _relative_to_root(args.root, args.out)
    if report_rel:
        verify_excludes.append(report_rel)
    report = verify(
        args.root,
        baseline,
        verify_excludes,
        max_files=args.max_files,
        include_ignored=args.include_ignored,
    )
    _write_json(Path(args.out), report)
    if report["status"] == "guard_failed":
        print(f"guard: {report.get('error')}", file=sys.stderr)
        return 2
    if report["status"] == "mutated":
        print(
            "guard: protected inputs changed "
            f"(changed={report['counts']['changed']}, added={report['counts']['added']}, "
            f"removed={report['counts']['removed']}, git={len(report['git_changes'])})",
            file=sys.stderr,
        )
        return 10
    print(f"guard: clean ({report['counts']['after']} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
