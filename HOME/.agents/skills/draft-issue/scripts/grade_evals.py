#!/usr/bin/env python3
"""Grade a draft-issue eval run mechanically.

The eval cases live in *evals/<case>/ prompt.md, case.yaml, graders/*.md*.  Each
run is a directory holding the drafts the agent produced; this script checks the
assertions that can be decided from those files and writes a *grading.json* the
way the skill-creator viewer expects.

Usage:

    python3 scripts/grade_evals.py --list
    python3 scripts/grade_evals.py <case> <run-dir>
    python3 scripts/grade_evals.py <case> <run-dir> --out /tmp/grading.json

Run directories are usually a copy of the drafts, so `--include-outputs` makes
the collector look inside an `outputs/` directory as well.

The label name sets below were captured from the target repositories when the
cases were written.  A repository's labels change, so refresh them with
*scripts/collect_context.py* before trusting a label failure.

`hono-bug-form` expects no draft at all: it grades the premise-check gate, and
that reply has to be read by a human.  It is marked `manual: true` in its
*case.yaml* and this script refuses to grade it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

EVALS = Path(__file__).resolve().parent.parent / "evals"

HONO_LABELS = set("""enhancement bug document middleware test "help wanted" "on hold"
announcement v4.4 v5 v4.5 triage "not bug" approved v4.6 stale v4.7 v4.8 v4.9
cr-tracked v4.10 v4.11 v4.12 v4.13""".replace('"', "").split())
HONGDOWN_LABELS = {"bug", "documentation", "duplicate", "enhancement",
                   "good first issue", "help wanted", "invalid", "question", "wontfix"}


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
    out = []
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


def hangul(text: str) -> int:
    return sum(1 for char in text if 0xAC00 <= ord(char) <= 0xD7A3)


def collect(root: Path, include_outputs: bool) -> list[Path]:
    out = []
    for path in sorted(root.rglob("*.md")):
        parts = path.parts
        if "repo" in parts or ".git" in parts:
            continue
        if "outputs" in parts and not include_outputs:
            continue
        if "notes" in path.stem.lower():
            continue
        out.append(path)
    return out


def result(text: str, passed: bool, evidence: str) -> dict:
    return {"text": text, "passed": bool(passed), "evidence": evidence}


def section_body(body: str, label: str) -> str:
    lines = body.splitlines()
    start = None
    for index, line in enumerate(lines):
        if label.lower() in line.strip().lstrip("#").strip().lower():
            start = index + 1
            break
    if start is None:
        return ""
    chunk = []
    for line in lines[start:]:
        if re.match(r"^#{1,6}\s", line):
            break
        chunk.append(line)
    return "\n".join(chunk).strip()


def check_hongdown_feature(root: Path, include_outputs: bool) -> list[dict]:
    drafts = collect(root, include_outputs)
    if not drafts:
        return [result("A draft was produced.", False, "No Markdown draft found under the run directory.")]
    draft = drafts[0]
    text = draft.read_text(encoding="utf-8")
    frontmatter, body = parse_frontmatter(text)

    checks = [
        result("The draft is written in English, including the title.",
               hangul(text) < 20 and len(re.findall(r"[A-Za-z]", text)) > 80,
               f"Hangul characters: {hangul(text)}; path: {draft.name}"),
        result("The body contains no Markdown headings at all.",
               not headings(body),
               f"Headings found: {headings(body)}" if headings(body) else "No headings."),
        result("The frontmatter has a title and applies the existing `enhancement` label.",
               bool(frontmatter.get("title")) and "enhancement" in (frontmatter.get("labels") or []),
               f"title={frontmatter.get('title')!r}, labels={frontmatter.get('labels')}"),
        result("The draft file is written outside the `repo/` checkout.",
               all("repo" not in path.parts for path in drafts),
               f"Paths: {[str(path.relative_to(root)) for path in drafts]}"),
    ]
    wrapped = [block for block in prose_paragraphs(body) if len(block) > 1]
    checks.append(result("The prose is not word-wrapped: each paragraph occupies a single source line.",
                         not wrapped,
                         "All paragraphs are single-line." if not wrapped
                         else f"Wrapped paragraph: {wrapped[0][0][:80]!r}..."))
    return checks


def check_hono(root: Path, include_outputs: bool) -> list[dict]:
    drafts = collect(root, include_outputs)
    if not drafts:
        return [result("A draft was produced.", False, "No Markdown draft found under the run directory.")]
    draft = max(drafts, key=lambda path: path.stat().st_size)
    text = draft.read_text(encoding="utf-8")
    frontmatter, body = parse_frontmatter(text)
    found = headings(body)
    labels = frontmatter.get("labels") or []

    fields = ["What version of Hono are you using",
              "What runtime/platform is your app running on",
              "What steps can reproduce the bug",
              "What is the expected behavior",
              "What do you see instead",
              "Additional information"]
    matched = [field for field in fields
               if any(field.lower() in heading.lower() and level == 3 for level, heading in found)]
    checks = [
        result("The draft follows the bug form, using `###` headings that match the form's field labels.",
               len(matched) >= 4, f"Matched {len(matched)}/6 form headings: {matched}"),
        result("The body contains no `#` or `##` headings.",
               all(level >= 3 for level, _ in found),
               f"Heading levels: {sorted({level for level, _ in found})}"),
        result("The draft includes the Hono version 4.6.3 and the Cloudflare Workers runtime.",
               "4.6.3" in text and "cloudflare" in text.lower(),
               f"4.6.3 present: {'4.6.3' in text}; Cloudflare present: {'cloudflare' in text.lower()}"),
    ]
    expected = section_body(body, "What is the expected behavior")
    actual = section_body(body, "see instead")
    checks.append(result("The draft includes reproduction steps and both the expected and the actual behavior.",
                         bool(section_body(body, "What steps can reproduce the bug")) and bool(expected) and bool(actual),
                         f"repro={bool(section_body(body, 'What steps can reproduce the bug'))}, "
                         f"expected={bool(expected)}, actual={bool(actual)}"))
    unknown = [label for label in labels if label not in HONO_LABELS]
    checks.append(result("The frontmatter uses only labels that exist in honojs/hono, including the form's default `triage`.",
                         not unknown and "triage" in labels,
                         f"labels={labels}, unknown={unknown}"))
    return checks


def check_sidey(root: Path, include_outputs: bool) -> list[dict]:
    drafts = collect(root, include_outputs)
    if not drafts:
        return [result("A draft was produced.", False, "No Markdown draft found under the run directory.")]
    draft = max(drafts, key=lambda path: path.stat().st_size)
    text = draft.read_text(encoding="utf-8")
    frontmatter, body = parse_frontmatter(text)
    found = headings(body)
    labels = frontmatter.get("labels") or []

    fields = ["컴퓨터의 운영체제는 무엇인가요", "사용 중인 애플리케이션 버전이 무엇인가요",
              "어떤 문제가 발생하고 있나요", "버그를 어떻게 재현할 수 있나요",
              "원래 예상했던 동작은 무엇인가요"]
    matched = [field for field in fields
               if any(field in heading and level == 3 for level, heading in found)]
    invented = bool(re.search(r"user-attachments|\[[^\]]+\]\([^)]*\.(?:log|zip)\)", text))

    return [
        result("The draft is written in Korean, including the title.",
               hangul(text) > 100, f"Hangul characters: {hangul(text)}"),
        result("The draft follows the Korean bug form, with its emoji `###` field headings in the form's order.",
               len(matched) >= 4, f"Matched {len(matched)}/5 form headings: {matched}"),
        result("The frontmatter sets `type: Bug` and uses the `bug` and `macOS` labels.",
               str(frontmatter.get("type", "")).lower() == "bug" and {"bug", "macOS"} <= set(labels),
               f"type={frontmatter.get('type')!r}, labels={labels}"),
        result("The draft does not invent a log file or attachment that the user did not provide.",
               not invented,
               "No attachment or log reference found." if not invented
               else "Found an attachment or log reference."),
    ]


def check_multi(root: Path, include_outputs: bool) -> list[dict]:
    drafts = collect(root, include_outputs)
    texts = [(path, path.read_text(encoding="utf-8")) for path in drafts]
    parsed = [(path, *parse_frontmatter(text)) for path, text in texts]
    checks = [
        result("Both drafts are saved in the same directory outside the `repo/` checkout.",
               len(drafts) == 2 and len({path.parent for path in drafts}) == 1
               and all("repo" not in path.parts for path in drafts),
               f"drafts={[str(path.relative_to(root)) for path in drafts]}"),
    ]
    checks.append(result("Both drafts are written in English, and any headings start at `##`.",
                         len(texts) == 2 and all(hangul(text) < 20 for _, text in texts)
                         and all(level >= 2 for _, _, body in parsed for level, _ in headings(body)),
                         f"headings={[[(level, heading) for level, heading in headings(body)] for _, _, body in parsed]}"))
    relations = []
    for path, frontmatter, _ in parsed:
        for key in ("relates-to", "blocked-by", "blocks", "parent", "sub-issues"):
            value = frontmatter.get(key)
            values = value if isinstance(value, list) else [value]
            if any(isinstance(item, str) and "draft:" in item for item in values if item):
                relations.append((path.name, key))
    checks.append(result("The second draft records its dependency on the first with a `draft:` reference in a relational frontmatter key such as `blocked-by`.",
                         bool(relations),
                         f"draft: reference in {relations}" if relations
                         else "No relational key carries a draft: reference."))
    invalid = [(path.name, frontmatter.get("title"), frontmatter.get("labels"))
               for path, frontmatter, _ in parsed
               if not frontmatter.get("title") or not frontmatter.get("labels")
               or any(label not in HONGDOWN_LABELS for label in frontmatter.get("labels") or [])]
    checks.append(result("Both drafts carry frontmatter with a title and labels that exist in the repository.",
                         not invalid and len(parsed) == 2,
                         f"problems={invalid}" if invalid else "Both look valid."))
    return checks


CASES = {
    "hongdown-feature-simple": check_hongdown_feature,
    # The case is manual while it grades the premise-check gate; these checks
    # apply again if it goes back to expecting a draft.
    "hono-bug-form": check_hono,
    "sidey-bug-korean": check_sidey,
    "multi-related-issues": check_multi,
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
    parser.add_argument("run_dir", nargs="?", help="directory holding the drafts")
    parser.add_argument("--list", action="store_true", help="list the cases and exit")
    parser.add_argument("--out", help="write grading.json here (default: <run-dir>/grading.json)")
    parser.add_argument("--include-outputs", action="store_true",
                        help="also look inside an outputs/ directory")
    args = parser.parse_args()

    if args.list:
        for name in sorted(CASES):
            meta = case_metadata(name)
            kind = "manual" if meta.get("manual") == "true" else "automatic"
            print(f"{name:28} {kind:9} {meta.get('description', '')}")
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
