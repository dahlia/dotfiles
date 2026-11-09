#!/usr/bin/env python3
"""Copy the body of a PR draft to the clipboard, without its frontmatter.

The draft is a Markdown file with an optional YAML frontmatter block. This
script strips that block, normalizes trailing whitespace, and sends the body to
the platform clipboard. It also writes the body to a file when --body-out is
given, which is what `gh pr create --body-file` wants.

Usage:
    python3 copy_body.py draft.md
    python3 copy_body.py draft.md --body-out /tmp/pr-body.md
    python3 copy_body.py draft.md --print
    python3 copy_body.py draft.md --command "wl-copy"

Clipboard selection order:
    - --command, or PR_CLIPBOARD_CMD when set
    - macOS: pbcopy
    - WSL: clip.exe
    - Wayland: wl-copy
    - X11: xclip or xsel
    - Termux: termux-clipboard-set

When none is available, the body is printed to stdout and the script exits with
status 2, so a caller can tell that the copy did not happen.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def split_frontmatter(text: str) -> tuple[str, str]:
    """Return (frontmatter, body). Frontmatter excludes the --- fences."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith("---\n"):
        return "", normalized
    end = normalized.find("\n---\n", 4)
    if end == -1:
        return "", normalized
    return normalized[4:end], normalized[end + 5:]


def clean_body(body: str) -> str:
    lines = body.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines) + "\n"


def find_clipboard() -> tuple[list[str], str] | None:
    override = os.environ.get("PR_CLIPBOARD_CMD")
    candidates: list[tuple[list[str], str]] = []
    if override:
        candidates.append((shlex.split(override), "PR_CLIPBOARD_CMD"))

    if sys.platform == "darwin":
        candidates.append((["pbcopy"], "pbcopy (macOS)"))
    elif sys.platform.startswith("win"):
        candidates.append((["clip"], "clip (Windows)"))
    else:
        is_wsl = "microsoft" in Path("/proc/version").read_text(errors="ignore").lower() \
            if Path("/proc/version").exists() else False
        if is_wsl:
            candidates.append((["clip.exe"], "clip.exe (WSL)"))
        if os.environ.get("WAYLAND_DISPLAY"):
            candidates.append((["wl-copy"], "wl-copy (Wayland)"))
        if os.environ.get("DISPLAY"):
            candidates.append((["xclip", "-selection", "clipboard"], "xclip (X11)"))
            candidates.append((["xsel", "--clipboard", "--input"], "xsel (X11)"))
        if "com.termux" in os.environ.get("PREFIX", ""):
            candidates.append((["termux-clipboard-set"], "termux-clipboard-set"))

    for command, label in candidates:
        if shutil.which(command[0]):
            return command, label
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("draft", help="Markdown draft with optional YAML frontmatter")
    parser.add_argument("--body-out", metavar="PATH",
                        help="also write the body to this file")
    parser.add_argument("--print", dest="print_body", action="store_true",
                        help="print the body to stdout as well")
    parser.add_argument("--command", help="clipboard command to use instead of detection")
    args = parser.parse_args()

    path = Path(args.draft)
    if not path.is_file():
        print(f"error: {path} is not a file", file=sys.stderr)
        return 1

    frontmatter, body = split_frontmatter(path.read_text(encoding="utf-8"))
    body = clean_body(body)
    if not body.strip():
        print(f"error: {path} has no body after the frontmatter", file=sys.stderr)
        return 1

    if args.body_out:
        out = Path(args.body_out)
        out.write_text(body, encoding="utf-8")
        print(f"body written to {out}", file=sys.stderr)

    if args.command:
        command, label = shlex.split(args.command), args.command
    else:
        found = find_clipboard()
        if not found:
            print("no clipboard command found; printing the body instead",
                  file=sys.stderr)
            sys.stdout.write(body)
            return 2
        command, label = found

    try:
        # Clipboard owners may daemonize and retain stdout/stderr. Pipes make
        # communicate() wait for that daemon indefinitely after the parent exits.
        # A temporary file preserves bounded failure diagnostics without EOF waits.
        with tempfile.TemporaryFile(mode="w+b") as diagnostics:
            proc = subprocess.run(command, input=body, text=True,
                                  stdout=diagnostics, stderr=diagnostics)
            diagnostics.seek(0)
            detail = diagnostics.read(4096).decode("utf-8", "replace").strip()
    except OSError as exc:
        print(f"error: could not run {label}: {exc}", file=sys.stderr)
        sys.stdout.write(body)
        return 2

    if proc.returncode != 0:
        print(f"error: {label} exited with {proc.returncode}"
              + (f": {detail}" if detail else ""), file=sys.stderr)
        sys.stdout.write(body)
        return 2

    print(f"copied the body to the clipboard with {label}", file=sys.stderr)
    if args.print_body:
        sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
