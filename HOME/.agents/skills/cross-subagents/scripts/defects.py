#!/usr/bin/env python3
"""Record and list defects found in the cross-subagents skill itself.

The skill improves by reporting its own bugs while it is being used. When an
execution reveals a defect in a helper script, a documented flag, an adapter's
parsing, or the skill documentation, the running agent records a short report
here instead of only working around it and forgetting. At the end of the
workflow the agent tells the user which reports were created.

Reports are plain Markdown files under `$SKILL_CACHE_DIR/defects/` (override
with `CROSS_SUBAGENTS_DEFECT_DIR`). They are scratch documents, not run
artifacts; resolving a report moves it to `defects/resolved/` with a note.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from common import atomic_write_text, enforce_utf8, get_cache_dir, now_iso

SEVERITIES = ("blocker", "major", "minor")
DEFAULT_SEVERITY = "major"


def get_defect_dir() -> Path:
    """Return the directory that holds open defect reports."""
    override = os.environ.get("CROSS_SUBAGENTS_DEFECT_DIR")
    root = Path(override).expanduser() if override else get_cache_dir() / "defects"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def _slugify(text: str, max_len: int = 48) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug[:max_len].rstrip("-") or "defect")


def _report_id() -> str:
    return now_iso().replace(":", "").replace("-", "").rstrip("Z")


def _render_report(
    *,
    report_id: str,
    title: str,
    severity: str,
    area: str | None,
    summary: str,
    observed: str | None,
    expected: str | None,
    reproduction: str | None,
    impact: str | None,
    suggested_fix: str | None,
    evidence: str | None,
    step: str | None,
    run_id: str | None,
    task_id: str | None,
) -> str:
    def block(value: str | None, placeholder: str = "_(not recorded)_") -> str:
        text = (value or "").strip()
        return text if text else placeholder

    lines = [
        f"# Skill defect: {title.strip()}",
        "",
        "## Summary",
        "",
        block(summary),
        "",
        "## Reported from",
        "",
        f"-  Detected at: {now_iso()}",
        f"-  Severity: `{severity}`",
        f"-  Skill area: {block(area, '_(unknown)_')}",
        f"-  Workflow step: {block(step, '_(unknown)_')}",
        f"-  Run ID: {block(run_id, '_(none)_')}",
        f"-  Task ID: {block(task_id, '_(none)_')}",
        "",
        "## Observed behavior",
        "",
        block(observed),
        "",
        "## Expected behavior",
        "",
        block(expected),
        "",
        "## Impact",
        "",
        block(impact),
        "",
        "## Reproduction",
        "",
        block(reproduction),
        "",
        "## Suggested fix",
        "",
        block(suggested_fix),
        "",
        "## Evidence",
        "",
        block(evidence),
        "",
    ]
    return "\n".join(lines)


def create_report(args: argparse.Namespace) -> dict[str, Any]:
    """Write a new defect report and return its id and path."""
    title = (args.title or "").strip()
    if not title:
        raise ValueError("--title is required and must not be empty.")
    summary = (args.summary or "").strip()
    if not summary:
        raise ValueError("--summary is required: state what went wrong in one or two sentences.")
    severity = (args.severity or DEFAULT_SEVERITY).lower()
    if severity not in SEVERITIES:
        raise ValueError(f"--severity must be one of: {', '.join(SEVERITIES)}")

    directory = get_defect_dir()
    report_id = _report_id()
    path = directory / f"{report_id}-{_slugify(title)}.md"
    suffix = 2
    while path.exists():
        path = directory / f"{report_id}-{_slugify(title)}-{suffix}.md"
        suffix += 1

    text = _render_report(
        report_id=report_id,
        title=title,
        severity=severity,
        area=args.area,
        summary=summary,
        observed=args.observed,
        expected=args.expected,
        reproduction=args.reproduction,
        impact=args.impact,
        suggested_fix=args.suggested_fix,
        evidence=args.evidence,
        step=args.step,
        run_id=args.run_id,
        task_id=args.task_id,
    )
    atomic_write_text(path, text)
    return {"id": path.stem, "path": str(path), "severity": severity, "title": title}


def _parse_header(path: Path) -> dict[str, Any]:
    """Extract the title, severity, area, and run id from a report file."""
    title = path.stem
    severity = "unknown"
    area = None
    run_id = None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {
            "id": path.stem,
            "path": str(path),
            "title": title,
            "severity": severity,
            "area": area,
            "run_id": run_id,
        }
    first = text.splitlines()[0] if text else ""
    if first.startswith("# Skill defect:"):
        title = first[len("# Skill defect:") :].strip() or title
    match = re.search(r"^-\s+Severity:\s+`?([a-z]+)`?", text, re.MULTILINE)
    if match:
        severity = match.group(1)
    match = re.search(r"^-\s+Skill area:\s+(.+)$", text, re.MULTILINE)
    if match:
        area = match.group(1).strip()
        if area == "_(unknown)_":
            area = None
    match = re.search(r"^-\s+Run ID:\s+(.+)$", text, re.MULTILINE)
    if match:
        candidate = match.group(1).strip()
        if candidate and candidate != "_(none)_":
            run_id = candidate
    return {
        "id": path.stem,
        "path": str(path),
        "title": title,
        "severity": severity,
        "area": area,
        "run_id": run_id,
    }


def list_reports(run_id: str | None = None) -> list[dict[str, Any]]:
    """Return open reports, newest first, optionally filtered to one run."""
    directory = get_defect_dir()
    reports = [_parse_header(p) for p in directory.glob("*.md") if p.is_file()]
    if run_id is not None:
        reports = [r for r in reports if r.get("run_id") == run_id]
    order = {name: idx for idx, name in enumerate(SEVERITIES)}
    reports.sort(key=lambda r: r["id"], reverse=True)
    reports.sort(key=lambda r: order.get(r.get("severity") or "", len(SEVERITIES)))
    return reports


def resolve_report(report_id: str, note: str | None) -> dict[str, Any]:
    """Move an open report to `resolved/` and append an optional resolution note."""
    directory = get_defect_dir()
    match = next((p for p in directory.glob(f"{report_id}*.md") if p.is_file()), None)
    if match is None:
        raise FileNotFoundError(f"No open defect report matching '{report_id}'.")
    resolved_dir = directory / "resolved"
    resolved_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = resolved_dir / match.name
    text = match.read_text(encoding="utf-8")
    if note:
        text = text.rstrip() + f"\n\n## Resolution\n\n{note.strip()}\n"
    atomic_write_text(target, text)
    match.unlink()
    return {"id": match.stem, "path": str(target)}


def main(argv: list[str] | None = None) -> int:
    enforce_utf8()
    parser = argparse.ArgumentParser(
        description="Record and list defects found in the cross-subagents skill itself."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    new = sub.add_parser("new", help="Write a new defect report.")
    new.add_argument("--title", required=True, help="One-line description of the defect.")
    new.add_argument("--summary", required=True, help="What went wrong, in one or two sentences.")
    new.add_argument("--severity", default=DEFAULT_SEVERITY, choices=SEVERITIES)
    new.add_argument("--area", help="Skill file or script involved, e.g. scripts/run.py --sandbox.")
    new.add_argument("--step", help="Workflow step being executed, e.g. 'Step 4: invoke run.py'.")
    new.add_argument("--observed", help="What actually happened.")
    new.add_argument("--expected", help="What should have happened.")
    new.add_argument("--reproduction", help="Exact command or minimal steps.")
    new.add_argument("--impact", help="What this cost the run (wasted quota, wrong artifact, ...).")
    new.add_argument("--suggested-fix", help="Optional concrete fix.")
    new.add_argument("--evidence", help="Paths to attempt dirs, logs, or events.jsonl excerpts.")
    new.add_argument("--run-id")
    new.add_argument("--task-id")
    new.add_argument("--json", action="store_true", help="Print JSON instead of the report path.")

    listing = sub.add_parser("list", help="List open defect reports.")
    listing.add_argument("--severity", choices=SEVERITIES, help="Only show this severity.")
    listing.add_argument("--run-id", help="Only show reports recorded for this run.")
    listing.add_argument("--human", action="store_true", help="Human-readable output.")

    resolve = sub.add_parser("resolve", help="Move a report to resolved/ after fixing the skill.")
    resolve.add_argument("--id", required=True, help="Report id (file stem or unique prefix).")
    resolve.add_argument("--note", help="What fixed it.")

    args = parser.parse_args(argv)

    try:
        if args.command == "new":
            result = create_report(args)
            if args.json:
                print(json.dumps(result, ensure_ascii=False))
            else:
                print(result["path"])
            return 0
        if args.command == "list":
            reports = list_reports(run_id=args.run_id)
            if args.severity:
                reports = [r for r in reports if r.get("severity") == args.severity]
            if args.human:
                if not reports:
                    print("No open skill defect reports.")
                for report in reports:
                    area = f" [{report['area']}]" if report.get("area") else ""
                    run_note = f" (run {report['run_id']})" if report.get("run_id") else ""
                    print(
                        f"{report['id']}  {report['severity']:>7}  "
                        f"{report['title']}{area}{run_note}"
                    )
            else:
                print(json.dumps({"count": len(reports), "reports": reports}, ensure_ascii=False))
            return 0
        if args.command == "resolve":
            result = resolve_report(args.id, args.note)
            print(json.dumps(result, ensure_ascii=False))
            return 0
    except (ValueError, FileNotFoundError) as exc:
        print(f"defects.py: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
