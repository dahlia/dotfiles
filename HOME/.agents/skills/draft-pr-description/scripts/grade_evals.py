#!/usr/bin/env python3
"""Grade a draft-pr-description eval run mechanically.

The eval cases live in *evals/<case>/ prompt.md, case.yaml, graders/*.md*. Each
run is a directory holding the draft and, when the harness provides one, a
`clipboard.txt` recorded by the clipboard stub. This script checks the
assertions that can be decided from those files and writes a *grading.json* the
way the skill-creator viewer expects.

Usage:

    python3 scripts/grade_evals.py --list
    python3 scripts/grade_evals.py <case> <run-dir>
    python3 scripts/grade_evals.py <case> <run-dir> --out /tmp/grading.json

Run directories usually contain `data/pr-drafts/...` (when XDG_DATA_HOME points
into the run directory) and `clipboard.txt`. `--include-outputs` also looks
inside an `outputs/` directory.

`premise-mismatch` expects no draft at all: it grades the premise-check gate,
and that reply has to be read by a human. It is marked `manual: true` in its
*case.yaml* and this script refuses to grade it.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

EVALS = Path(__file__).resolve().parent.parent / "evals"


def parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    raw, body = text[4:end], text[end + 5:]
    data: dict = {}
    key = None
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.match(r"^([A-Za-z][\w-]*):\s*(.*)$", line)
        if match and not line.startswith((" ", "\t")):
            key, value = match.group(1), match.group(2).strip()
            if value.startswith("[") and value.endswith("]"):
                data[key] = [v.strip().strip("\"'") for v in value[1:-1].split(",") if v.strip()]
            elif value:
                data[key] = value.strip("\"'")
            else:
                data[key] = []
            continue
        match = re.match(r"^\s*-\s*(.*)$", line)
        if match and key:
            if not isinstance(data.get(key), list):
                data[key] = []
            data[key].append(match.group(1).strip().strip("\"'"))
    return data, body


def headings(body: str) -> list[tuple[int, str]]:
    lines = body.splitlines()
    out: list[tuple[int, str]] = []
    in_fence = False
    for index, line in enumerate(lines):
        if re.match(r"^\s*(~~~|```)", line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = re.match(r"^(#{1,6})\s+(.*)$", line)
        if match:
            out.append((len(match.group(1)), match.group(2).strip()))
            continue
        if index + 1 < len(lines) and line.strip() and re.match(r"^(=+|-{3,})\s*$", lines[index + 1]) \
                and not line.startswith((" ", "\t", "-", "*", "|", ">", "<")):
            out.append((1 if lines[index + 1].startswith("=") else 2, line.strip()))
    return out


def heading_spans(body: str) -> list[tuple[int, str, int, int]]:
    """Return (level, title, content_start, content_end) for each heading."""
    lines = body.splitlines()
    found: list[tuple[int, str, int]] = []
    in_fence = False
    index = 0
    while index < len(lines):
        line = lines[index]
        if re.match(r"^\s*(~~~|```)", line):
            in_fence = not in_fence
            index += 1
            continue
        if in_fence:
            index += 1
            continue
        match = re.match(r"^(#{1,6})\s+(.*)$", line)
        if match:
            found.append((len(match.group(1)), match.group(2).strip(), index + 1))
            index += 1
            continue
        if index + 1 < len(lines) and line.strip() and re.match(r"^(=+|-{3,})\s*$", lines[index + 1]) \
                and not line.startswith((" ", "\t", "-", "*", "|", ">", "<")):
            found.append((1 if lines[index + 1].startswith("=") else 2, line.strip(), index + 2))
            index += 2
            continue
        index += 1

    spans: list[tuple[int, str, int, int]] = []
    for position, (level, title, content_start) in enumerate(found):
        content_end = found[position + 1][2] - 1 if position + 1 < len(found) else len(lines)
        # The next heading's underline line belongs to the heading, not this section.
        if position + 1 < len(found):
            content_end = found[position + 1][2] - 1
            if content_end > content_start and re.match(r"^(=+|-{3,})\s*$", lines[content_end - 1]):
                content_end -= 1
        spans.append((level, title, content_start, content_end))
    return spans


def section_body(body: str, label: str) -> str:
    lines = body.splitlines()
    for level, title, start, end in heading_spans(body):
        if label.lower() in title.lower():
            return "\n".join(lines[start:end]).strip()
    return ""


def strip_code(text: str) -> str:
    text = re.sub(r"~~~.*?~~~|```.*?```", " ", text, flags=re.S)
    return re.sub(r"`[^`\n]*`", " ", text)


def hangul(text: str) -> int:
    return sum(1 for char in text if 0xAC00 <= ord(char) <= 0xD7A3)


def prose_paragraphs(body: str) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] = []
    in_fence = False
    for line in body.splitlines():
        if re.match(r"^\s*(~~~|```)", line):
            in_fence = not in_fence
            if current:
                blocks.append(current)
                current = []
            continue
        if in_fence:
            continue
        stripped = line.strip()
        if not stripped:
            if current:
                blocks.append(current)
                current = []
            continue
        if re.match(r"^(#{1,6}\s|[-*+]\s|\d+[.)]\s|\||>|<|\[[^\]]+\]:)", stripped) \
                or re.match(r"^(=+|-{3,})$", stripped):
            if current:
                blocks.append(current)
                current = []
            continue
        current.append(stripped)
    if current:
        blocks.append(current)
    return blocks


def result(text: str, passed: bool, evidence: str) -> dict:
    return {"text": text, "passed": bool(passed), "evidence": evidence}


def collect(root: Path, include_outputs: bool) -> list[Path]:
    out = []
    for path in sorted(root.rglob("*.md")):
        parts = path.parts
        if "repo" in parts or ".git" in parts or "notes" in path.stem.lower():
            continue
        if "outputs" in parts and not include_outputs:
            continue
        out.append(path)
    return out


def pick_draft(root: Path, include_outputs: bool) -> Path | None:
    drafts = collect(root, include_outputs)
    if not drafts:
        return None
    with_title = []
    for path in drafts:
        text = path.read_text(encoding="utf-8")
        frontmatter, _ = parse_frontmatter(text)
        if frontmatter.get("title"):
            with_title.append(path)
    pool = with_title or drafts
    return max(pool, key=lambda path: path.stat().st_size)


def hongdown_ok(path: Path) -> tuple[bool, str]:
    binary = shutil.which("hongdown")
    if not binary:
        return False, "hongdown is not on PATH"
    proc = subprocess.run(
        [binary, "--check", "--no-line-width", str(path)],
        capture_output=True, text=True,
    )
    if proc.returncode == 0:
        return True, "hongdown --check passed"
    detail = (proc.stderr or proc.stdout).strip().splitlines()
    return False, detail[0] if detail else f"hongdown exited with {proc.returncode}"


def clipboard_matches(root: Path, body: str) -> tuple[bool, str]:
    path = root / "clipboard.txt"
    if not path.exists():
        return False, "no clipboard.txt in the run directory"
    copied = path.read_text(encoding="utf-8")
    if "---\n" in copied[:20] and "title:" in copied[:200]:
        return False, "the clipboard contains the frontmatter"
    normalize = lambda text: re.sub(r"\s+", " ", text).strip()
    if normalize(body) == normalize(copied):
        return True, "the clipboard matches the body"
    if normalize(body)[:120] in normalize(copied):
        return True, "the clipboard contains the body"
    return False, "the clipboard differs from the body"


def read_draft(root: Path, include_outputs: bool) -> tuple[Path | None, dict, str, list[dict]]:
    draft = pick_draft(root, include_outputs)
    if draft is None:
        return None, {}, "", [result("A draft was produced.", False,
                                     "No Markdown draft found under the run directory.")]
    text = draft.read_text(encoding="utf-8")
    frontmatter, body = parse_frontmatter(text)
    return draft, frontmatter, body, []


def title_ok(frontmatter: dict) -> tuple[bool, str]:
    title = str(frontmatter.get("title") or "").strip()
    problems = []
    if not title:
        problems.append("missing title")
    if title.endswith("."):
        problems.append("trailing period")
    if re.match(r"^(\[[^\]]+\]|(fix|feat|docs|chore|refactor)\s*:)", title, re.I):
        problems.append("kind prefix")
    if hangul(title) > 0:
        problems.append("not English")
    if re.match(r"^(this|the)\s", title, re.I):
        problems.append("starts with a determiner instead of an imperative")
    return not problems, f"title={title!r}, problems={problems}" if problems else f"title={title!r}"


def check_hongdown_docs_fix(root: Path, include_outputs: bool) -> list[dict]:
    draft, frontmatter, body, failed = read_draft(root, include_outputs)
    if draft is None:
        return failed
    checks = []

    parts = draft.parts
    checks.append(result(
        "The draft is saved outside the checkout under `pr-drafts/dahlia-hongdown/`.",
        "pr-drafts" in parts and any(part == "dahlia-hongdown" for part in parts),
        f"path={draft}",
    ))

    ok, evidence = title_ok(frontmatter)
    checks.append(result(
        "The frontmatter has a title in imperative sentence case with no trailing period or kind prefix.",
        ok, evidence,
    ))

    prose = strip_code(body)
    words = len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'’.-]*", prose))
    checks.append(result(
        "The body is in English and no longer than about 250 words.",
        hangul(prose) < 20 and 10 <= words <= 280,
        f"Hangul={hangul(prose)}, words={words}",
    ))

    lower = prose.lower()
    explains = ("tilde" in lower or "fence" in lower) and (
        "close" in lower or "closing" in lower or "early" in lower
    ) and ("longer" in lower or "five" in lower or "4" in lower or "four" in lower)
    checks.append(result(
        "The body explains why a longer fence is needed instead of restating the diff.",
        explains,
        "The reason is present." if explains else "No explanation of the early-close problem.",
    ))

    checks.append(result(
        "The draft italicizes *STYLE.md* and contains no em dash.",
        "*STYLE.md*" in body and "`STYLE.md`" not in body
        and "\u2014" not in body and " -- " not in body,
        f"italic={'*STYLE.md*' in body}, backticked={'`STYLE.md`' in body}, "
        f"em_dash={'\u2014' in body}, spaced_hyphen={' -- ' in body}",
    ))

    slash = re.search(r"\s/\s", prose)
    checks.append(result(
        "The draft leaves no spaces around slashes in paths and terms.",
        slash is None,
        "No spaced slash." if slash is None else f"Found {slash.group(0)!r} near {prose[max(0, slash.start()-30):slash.end()+30]!r}",
    ))

    found = headings(body)
    checks.append(result(
        "The body has no headings, or only `##` headings, and no boilerplate sections.",
        all(level >= 2 for level, _ in found)
        and not any("summary" in title.lower() for _, title in found),
        f"headings={found}",
    ))

    ok, evidence = hongdown_ok(draft)
    wrapped = [block for block in prose_paragraphs(body) if len(block) > 1]
    checks.append(result(
        "The draft is formatted with hongdown and is not hard-wrapped.",
        ok and not wrapped,
        evidence + (f"; wrapped paragraph: {wrapped[0][0][:60]!r}" if wrapped else ""),
    ))

    ok, evidence = clipboard_matches(root, body)
    checks.append(result(
        "The clipboard received the body without the frontmatter.",
        ok, evidence,
    ))

    disclosure = re.search(
        r"(?i)(assisted-by|ai[- ]assisted|ai assistance|drafted with|generated with|"
        r"opencode|deepseek|claude|copilot|chatgpt|gemini|gpt-)",
        body,
    )
    checks.append(result(
        "The draft omits an AI disclosure because the maintainer exemption applies.",
        disclosure is None,
        "No disclosure found." if disclosure is None
        else f"Found {disclosure.group(0)!r}",
    ))
    return checks


def check_widget_template(root: Path, include_outputs: bool) -> list[dict]:
    draft, frontmatter, body, failed = read_draft(root, include_outputs)
    if draft is None:
        return failed
    checks = []

    parts = draft.parts
    checks.append(result(
        "The draft is saved outside the checkout under a `pr-drafts/` directory.",
        "pr-drafts" in parts,
        f"path={draft}",
    ))

    found = headings(body)
    level_two = [title.lower() for level, title in found if level == 2]
    order = ["summary", "related issues", "checklist", "screenshots"]
    positions = [level_two.index(name) for name in order if name in level_two]
    checks.append(result(
        "The body keeps the template's `##` headings in order.",
        len(positions) == 4 and positions == sorted(positions)
        and all(level == 2 for level, title in found
                if title.lower() in order),
        f"headings={found}",
    ))

    checks.append(result(
        "The body removes the template's HTML comments.",
        "<!--" not in body and "-->" not in body,
        "No HTML comment remains." if "<!--" not in body else "An HTML comment remains.",
    ))

    test_line = next((line for line in body.splitlines()
                      if re.match(r"^\s*-\s*\[[ xX]\]", line) and "test" in line.lower()), "")
    docs_line = next((line for line in body.splitlines()
                      if re.match(r"^\s*-\s*\[[ xX]\]", line)
                      and ("documentation" in line.lower() or "docs" in line.lower())), "")
    checks.append(result(
        "The tests checkbox is checked and the documentation checkbox is not.",
        bool(test_line) and bool(docs_line)
        and re.match(r"^\s*-\s*\[[xX]\]", test_line) is not None
        and re.match(r"^\s*-\s*\[ \]", docs_line) is not None,
        f"tests={test_line!r}, docs={docs_line!r}",
    ))

    summary = section_body(body, "summary").lower()
    explains = ("empty" in summary) and ("reject" in summary or "throw" in summary or "error" in summary)
    checks.append(result(
        "The Summary explains the empty-button symptom and the new rejection.",
        explains,
        f"summary={summary[:300]!r}",
    ))

    checks.append(result(
        "The draft does not invent an issue number.",
        re.search(r"(fixes|closes|resolves)\s+#\d+", body, re.I) is None,
        "No closing keyword." if re.search(r"(fixes|closes|resolves)\s+#\d+", body, re.I) is None
        else "Found a closing keyword with an invented issue.",
    ))

    checks.append(result(
        "The draft contains no em dash.",
        "\u2014" not in body and " -- " not in body,
        f"em_dash={'\u2014' in body}, spaced_hyphen={' -- ' in body}",
    ))

    ok, evidence = title_ok(frontmatter)
    checks.append(result(
        "The frontmatter title is English and in imperative sentence case.",
        ok, evidence,
    ))

    ok, evidence = hongdown_ok(draft)
    checks.append(result(
        "The draft is formatted with hongdown.",
        ok, evidence,
    ))

    ok, evidence = clipboard_matches(root, body)
    checks.append(result(
        "The clipboard received the body without the frontmatter.",
        ok, evidence,
    ))
    return checks


CASES = {
    "hongdown-docs-fix": check_hongdown_docs_fix,
    "widget-template": check_widget_template,
}


def case_metadata(name: str) -> dict:
    path = EVALS / name / "case.yaml"
    data: dict = {}
    if not path.exists():
        return data
    lines = path.read_text(encoding="utf-8").splitlines()
    index = 0
    while index < len(lines):
        match = re.match(r"^([A-Za-z][\w-]*):\s*(.*)$", lines[index])
        if not match:
            index += 1
            continue
        key, value = match.group(1), match.group(2).strip()
        if value in (">", "|", ">-", "|-"):
            block = []
            index += 1
            while index < len(lines) and (not lines[index].strip() or lines[index].startswith(" ")):
                block.append(lines[index].strip())
                index += 1
            data[key] = " ".join(part for part in block if part)
            continue
        data[key] = value.strip("\"'")
        index += 1
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("case", nargs="?", help="eval case name")
    parser.add_argument("run_dir", nargs="?", help="directory holding the draft")
    parser.add_argument("--list", action="store_true", help="list the cases and exit")
    parser.add_argument("--out", help="write grading.json here (default: <run-dir>/grading.json)")
    parser.add_argument("--include-outputs", action="store_true",
                        help="also look inside an outputs/ directory")
    args = parser.parse_args()

    if args.list:
        for name in sorted(CASES):
            meta = case_metadata(name)
            kind = "manual" if meta.get("manual") == "true" else "automatic"
            print(f"{name:24} {kind:9} {meta.get('description', '')}")
        return 0

    if not args.case or not args.run_dir:
        parser.error("provide a case name and a run directory, or --list")
    if args.case not in CASES:
        parser.error(f"unknown case {args.case!r}; try --list")

    meta = case_metadata(args.case)
    if meta.get("manual") == "true":
        print(f"{args.case} is graded by hand: {meta.get('description', '')}", file=sys.stderr)
        print(f"Read {EVALS / args.case / 'prompt.md'} for what counts as a good result.", file=sys.stderr)
        return 2

    root = Path(args.run_dir).resolve()
    checks = CASES[args.case](root, args.include_outputs)
    passed = sum(1 for check in checks if check["passed"])
    grading = {
        "expectations": checks,
        "summary": {"passed": passed, "failed": len(checks) - passed,
                    "total": len(checks), "pass_rate": round(passed / len(checks), 2)},
    }
    out = Path(args.out) if args.out else root / "grading.json"
    out.write_text(json.dumps(grading, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{args.case}: {passed}/{len(checks)}")
    for check in checks:
        print(("  PASS " if check["passed"] else "  FAIL ") + check["text"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
