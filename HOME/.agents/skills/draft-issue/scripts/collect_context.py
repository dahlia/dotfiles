#!/usr/bin/env python3
"""Collect what you need to know before drafting an issue.

Prints the target repository's issue templates, labels, issue types,
custom issue fields, milestones, and a sample of recent issues. The sample is also the evidence for
choosing the language of the draft: the script reports the script mix, but the
decision stays with the reader.

Usage:
    python3 collect_context.py                     # repo from the current checkout
    python3 collect_context.py --repo OWNER/REPO
    python3 collect_context.py --repo OWNER/REPO --recent 20 --json out.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

MAX_TEMPLATE_CHARS = 12000
MAX_BODY_CHARS = 900
GH_TIMEOUT = 60


def run(cmd: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=GH_TIMEOUT
        )
    except FileNotFoundError:
        return 127, "", f"{cmd[0]} is not installed"
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    return proc.returncode, proc.stdout, proc.stderr


def gh_json(args: list[str]) -> tuple[object | None, str | None]:
    code, out, err = run(["gh", *args])
    if code != 0:
        message = (err or out).strip().splitlines()
        return None, message[-1] if message else f"gh exited with {code}"
    try:
        return json.loads(out), None
    except json.JSONDecodeError as exc:
        return None, f"unparseable response: {exc}"


def parse_remote(url: str) -> tuple[str, str] | None:
    """Return (host, owner/repo) for the common remote URL forms."""
    url = url.strip()
    if not url:
        return None

    scp = re.match(r"^[\w.-]+@([\w.-]+):(.+?)(?:\.git)?/?$", url)
    if scp:
        return scp.group(1), scp.group(2)

    for prefix in ("https://", "http://", "ssh://", "git://"):
        if url.startswith(prefix):
            rest = url[len(prefix):]
            rest = re.sub(r"^[^@/]+@", "", rest)
            host, _, path = rest.partition("/")
            host = host.split(":")[0]
            path = path.split("?")[0].rstrip("/")
            if path.endswith(".git"):
                path = path[:-4]
            if host and path:
                return host, path
    return None


def resolve_target(args) -> tuple[str | None, str | None, str | None, str | None]:
    """Return (host, owner_repo, local_root, remote_url)."""
    cwd = os.path.abspath(args.cwd or os.getcwd())
    local_root = None
    code, out, _ = run(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
    if code == 0 and out.strip():
        local_root = out.strip()

    remote_url = None
    if args.repo:
        owner_repo = args.repo.strip()
        if owner_repo.startswith(("http://", "https://", "ssh://", "git@")):
            remote_url = owner_repo
        else:
            return "github.com", owner_repo, local_root, None
    if remote_url is None and local_root:
        code, out, _ = run(["git", "remote", "get-url", "origin"], cwd=local_root)
        if code == 0:
            remote_url = out.strip()

    if not remote_url:
        return None, None, local_root, None

    parsed = parse_remote(remote_url)
    if not parsed:
        return None, None, local_root, remote_url
    host, owner_repo = parsed
    return host, owner_repo, local_root, remote_url


def fetch_raw(owner_repo: str, path: str) -> str | None:
    code, out, err = run([
        "gh", "api", "-H", "Accept: application/vnd.github.raw",
        f"repos/{owner_repo}/contents/{path}",
    ])
    if code == 0 and out.strip():
        return out
    return None


def listing(owner_repo: str, path: str) -> tuple[list[dict] | None, str | None]:
    data, err = gh_json(["api", f"repos/{owner_repo}/contents/{path}"])
    if isinstance(data, list):
        return data, None
    if data is not None:
        return None, "the path is not a directory"
    if err and "404" in err:
        return None, None  # the directory does not exist
    return None, err or "unknown error"


def truncate(text: str, limit: int) -> str:
    text = text.rstrip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n\n[... truncated; read the full file for the rest]"


def strip_code(text: str) -> str:
    text = re.sub(r"~~~.*?~~~|```.*?```", " ", text, flags=re.S)
    return re.sub(r"`[^`\n]*`", " ", text)


def script_mix(text: str) -> dict[str, int]:
    counts = {"Latin": 0, "Hangul": 0, "Han": 0, "Kana": 0, "Cyrillic": 0}
    for ch in text:
        code = ord(ch)
        if 0xAC00 <= code <= 0xD7A3 or 0x1100 <= code <= 0x11FF:
            counts["Hangul"] += 1
        elif 0x4E00 <= code <= 0x9FFF:
            counts["Han"] += 1
        elif 0x3040 <= code <= 0x30FF:
            counts["Kana"] += 1
        elif 0x0400 <= code <= 0x04FF:
            counts["Cyrillic"] += 1
        elif ch.isalpha() and code < 0x80:
            counts["Latin"] += 1
    return {name: n for name, n in counts.items() if n}


def github_section(owner_repo: str, recent: int, local_root: str | None) -> dict:
    result: dict[str, object] = {}

    print("\n## Issue templates\n")
    tdir, terr = listing(owner_repo, ".github/ISSUE_TEMPLATE")
    found = False
    if terr:
        print(f"_Could not list `.github/ISSUE_TEMPLATE`: {terr}_\n")
    elif tdir is not None:
        for entry in sorted(tdir, key=lambda e: e.get("name", "")):
            name = entry.get("name", "")
            if not name.lower().endswith((".yml", ".yaml", ".md")):
                continue
            if name in ("config.yml", "config.yaml"):
                continue
            found = True
            print(f"### `{name}`\n")
            body = fetch_raw(owner_repo, f".github/ISSUE_TEMPLATE/{name}")
            if body is None:
                print("_Could not read this file._\n")
            else:
                lang = "markdown" if name.lower().endswith(".md") else "yaml"
                print(f"~~~~ {lang}\n{truncate(body, MAX_TEMPLATE_CHARS)}\n~~~~\n")
    for legacy in (".github/ISSUE_TEMPLATE.md", ".github/issue_template.md",
                   "docs/ISSUE_TEMPLATE.md", "ISSUE_TEMPLATE.md"):
        body = fetch_raw(owner_repo, legacy)
        if body:
            found = True
            print(f"### `{legacy}`\n")
            print(f"~~~~ markdown\n{truncate(body, MAX_TEMPLATE_CHARS)}\n~~~~\n")
    if not found and not terr:
        print("_No issue template found at the repository level. Check the "
              "organization's `.github` repository before concluding there is "
              "none._\n")

    config = fetch_raw(owner_repo, ".github/ISSUE_TEMPLATE/config.yml")
    if config:
        print("### `config.yml` (chooser settings, not a template)\n")
        print(f"~~~~ yaml\n{truncate(config, 1500)}\n~~~~\n")

    print("## Issue types\n")
    types, err = gh_json([
        "api", "graphql",
        "-f", "query=query($owner: String!, $name: String!) {"
              " repository(owner: $owner, name: $name) {"
              " issueTypes(first: 50) { nodes { name } } } }",
        "-F", f"owner={owner_repo.split('/')[0]}",
        "-F", f"name={owner_repo.split('/')[-1]}",
    ])
    repository = ((types.get("data") or {}).get("repository") or {}) if isinstance(types, dict) else {}
    connection = repository.get("issueTypes")
    if err or (isinstance(types, dict) and types.get("errors")) or not isinstance(connection, dict):
        message = err or "incomplete GraphQL response"
        print(f"_Unavailable: {message}_\n")
        result["issue_types"] = None
        result["issue_types_error"] = message
    else:
        names = [node["name"] for node in connection.get("nodes") or []
                 if node and node.get("name")]
        result["issue_types"] = names
        print((", ".join(names) if names else
               "_No issue types available to the current viewer._") + "\n")

    print("## Custom issue fields\n")
    fragments = " ".join(
        f"... on IssueField{kind} {{ id name dataType description visibility"
        + (" options { id name }" if kind in ("SingleSelect", "MultiSelect") else "")
        + " }"
        for kind in ("Date", "Number", "Text", "SingleSelect", "MultiSelect")
    )
    fields, ferr = gh_json([
        "api", "graphql", "-f",
        "query=query($owner: String!, $name: String!) {"
        " repository(owner: $owner, name: $name) { viewerCanSeeIssueFields"
        " issueFields(first: 100) { nodes { __typename " + fragments
        + " } pageInfo { hasNextPage } } } }",
        "-F", f"owner={owner_repo.split('/')[0]}",
        "-F", f"name={owner_repo.split('/')[-1]}",
    ])
    repository = ((fields.get("data") or {}).get("repository") or {}) if isinstance(fields, dict) else {}
    connection = repository.get("issueFields")
    if ferr or (isinstance(fields, dict) and fields.get("errors")) or not isinstance(connection, dict):
        message = ferr or "incomplete GraphQL response"
        print(f"_Unavailable: {message}_\n")
        result["issue_fields"] = None
        result["issue_fields_error"] = message
    else:
        definitions = [node for node in connection.get("nodes") or [] if node]
        result["issue_fields"] = definitions
        result["viewer_can_see_issue_fields"] = repository.get("viewerCanSeeIssueFields")
        if definitions:
            print("~~~~ json\n" + json.dumps(definitions, ensure_ascii=False, indent=2)
                  + "\n~~~~\n")
        else:
            print("_No issue fields visible to the current viewer; this does not "
                  "rule out restricted fields._\n")
        if (connection.get("pageInfo") or {}).get("hasNextPage"):
            result["issue_fields_truncated"] = True
            print("_More fields exist; paginate issueFields before choosing values._\n")

    print("## Labels\n")
    labels, err = gh_json(["label", "list", "--repo", owner_repo, "--limit", "200",
                           "--json", "name,description"])
    label_names = []
    if isinstance(labels, list):
        label_names = [l.get("name", "") for l in labels if l.get("name")]
        if label_names:
            print(truncate(", ".join(label_names), 2500) + "\n")
        else:
            print("_No labels defined._\n")
    else:
        print(f"_Unavailable: {err}_\n")
    result["labels"] = label_names

    print("## Open milestones\n")
    milestones, err = gh_json(["api", f"repos/{owner_repo}/milestones?state=open&per_page=100"])
    titles = [m.get("title", "") for m in milestones] if isinstance(milestones, list) else []
    if titles:
        print(", ".join(titles) + "\n")
    else:
        print("_None open._" + (f" (API said: {err})" if err else "") + "\n")
    result["milestones"] = titles

    print("## Recent issues\n")
    issues, err = gh_json([
        "issue", "list", "--repo", owner_repo, "--state", "all",
        "--limit", str(recent),
        "--json", "number,title,body,author,state,labels,createdAt",
    ])
    if not isinstance(issues, list):
        print(f"_Unavailable: {err}_\n")
        return result

    texts = []
    for issue in issues:
        body = issue.get("body") or ""
        author = ((issue.get("author") or {}).get("login")) or "unknown"
        labels = ", ".join(l.get("name", "") for l in issue.get("labels") or [])
        print(f"### #{issue.get('number')} {issue.get('title')}\n")
        print(f"{author}, {issue.get('state', '').lower()}"
              f"{', ' + labels if labels else ''}\n")
        print(truncate(body, MAX_BODY_CHARS) + "\n")
        texts.append(issue.get("title") or "")
        texts.append(body)

    print("## Script mix (advisory)\n")
    mix = script_mix(strip_code("\n".join(texts)))
    if mix:
        print(", ".join(f"{name} {count:,}" for name, count in
                        sorted(mix.items(), key=lambda kv: -kv[1])) + "\n")
        print("_This only counts characters. Read the samples above and decide "
              "which language the tracker actually uses._\n")
    else:
        print("_Not enough text to judge._\n")
    result["script_mix"] = mix
    return result


def local_templates(local_root: str | None) -> None:
    print("\n## Local template directories\n")
    if not local_root:
        print("_Not inside a Git checkout._\n")
        return
    candidates = [
        ".github/ISSUE_TEMPLATE", ".gitea/ISSUE_TEMPLATE", ".gitea/issue_template",
        ".forgejo/ISSUE_TEMPLATE", ".gitlab/issue_templates",
    ]
    any_found = False
    for rel in candidates:
        path = os.path.join(local_root, rel)
        if os.path.isdir(path):
            any_found = True
            print(f"`{rel}/`: " + ", ".join(sorted(os.listdir(path))) + "\n")
    for rel in ("ISSUE_TEMPLATE.md", "ISSUE_TEMPLATE.yml", "docs/ISSUE_TEMPLATE.md",
                ".github/ISSUE_TEMPLATE.md", ".gitea/ISSUE_TEMPLATE.md",
                ".forgejo/ISSUE_TEMPLATE.md", "CONTRIBUTING.md"):
        path = os.path.join(local_root, rel)
        if os.path.isfile(path):
            any_found = True
            print(f"`{rel}`: {os.path.getsize(path):,} bytes\n")
    if not any_found:
        print("_None found in the checkout._\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", help="OWNER/REPO or a remote URL")
    parser.add_argument("--cwd", help="directory to inspect (default: current)")
    parser.add_argument("--recent", type=int, default=12,
                        help="how many recent issues to sample (default: 12)")
    parser.add_argument("--json", metavar="PATH", help="also write a JSON digest here")
    args = parser.parse_args()

    host, owner_repo, local_root, remote_url = resolve_target(args)

    print("# Repository context\n")
    print(f" -  Repository: `{owner_repo}`" if owner_repo else " -  Repository: _not resolved_")
    print(f" -  Forge: `{host}`" if host else " -  Forge: _unknown_")
    print(f" -  Local checkout: `{local_root}`" if local_root else " -  Local checkout: _none_")
    if remote_url:
        print(f" -  Remote: `{remote_url}`")
    print()

    digest: dict[str, object] = {"host": host, "repo": owner_repo,
                                 "local_root": local_root, "remote": remote_url}

    if host == "github.com" and owner_repo:
        context_repo = owner_repo
        parent, perr = gh_json(["repo", "view", owner_repo, "--json", "parent"])
        if isinstance(parent, dict):
            up = (parent.get("parent") or {}).get("nameWithOwner")
            if up:
                context_repo = up
                print(f" -  Fork of: `{up}` (templates, labels, and issues come "
                      "from there)\n")
        elif perr:
            print(f" -  Fork check unavailable: {perr}\n")
        digest.update(github_section(context_repo, args.recent, local_root))
        digest["context_repo"] = context_repo
    elif host:
        print("\n## Forge\n")
        print(f"`{host}` is not GitHub. Use *references/forges.md* for where this "
              "forge keeps its templates and labels.\n")
    else:
        print("\n## Forge\n")
        print("_Could not resolve a repository. Pass `--repo OWNER/REPO`._\n")

    if local_root:
        local_templates(local_root)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(digest, handle, ensure_ascii=False, indent=2)
        print(f"\nWrote digest to `{args.json}`.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
