#!/usr/bin/env python3
"""Collect what you need to know before drafting a pull request description.

Prints the target repository, the branch range, the commits and diff stat, the
PR template(s), recent merged pull requests, labels, milestones, and any
existing PR for the head branch. The recent PRs are also the evidence for
choosing the language of the draft: the script reports the script mix, but the
decision stays with the reader.

Usage:
    python3 collect_context.py                     # repo from the current checkout
    python3 collect_context.py --repo OWNER/REPO --base main
    python3 collect_context.py --recent 12 --json out.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

MAX_TEMPLATE_CHARS = 12000
MAX_BODY_CHARS = 1200
GH_TIMEOUT = 60

TEMPLATE_FILES = [
    ".github/pull_request_template.md",
    ".github/PULL_REQUEST_TEMPLATE.md",
    "docs/pull_request_template.md",
    "docs/PULL_REQUEST_TEMPLATE.md",
    "pull_request_template.md",
    "PULL_REQUEST_TEMPLATE.md",
    ".gitea/PULL_REQUEST_TEMPLATE.md",
    ".forgejo/PULL_REQUEST_TEMPLATE.md",
    ".bitbucket/PULL_REQUEST_TEMPLATE.md",
]

TEMPLATE_DIRS = [
    ".github/PULL_REQUEST_TEMPLATE",
    ".gitlab/merge_request_templates",
    ".gitea/PULL_REQUEST_TEMPLATE",
    ".forgejo/PULL_REQUEST_TEMPLATE",
]


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


def gh_json(args: list[str], cwd: str | None = None) -> tuple[object | None, str | None]:
    code, out, err = run(["gh", *args], cwd=cwd)
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


def pick_remote(local_root: str) -> str | None:
    """Choose the remote that most likely points at the upstream repository."""
    code, out, _ = run(["git", "remote"], cwd=local_root)
    remotes = out.split() if code == 0 else []
    if not remotes:
        return None
    if "origin" in remotes:
        return "origin"
    code, out, _ = run(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        cwd=local_root,
    )
    if code == 0 and out.strip() and "/" in out.strip():
        name = out.strip().split("/", 1)[0]
        if name in remotes:
            return name
    if "upstream" in remotes:
        return "upstream"
    return remotes[0]


def resolve_target(args) -> tuple[str | None, str | None, str | None, str | None]:
    """Return (host, owner_repo, local_root, remote_name)."""
    cwd = os.path.abspath(args.cwd or os.getcwd())
    local_root = None
    code, out, _ = run(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
    if code == 0 and out.strip():
        local_root = out.strip()

    if args.repo:
        owner_repo = args.repo.strip()
        if owner_repo.startswith(("http://", "https://", "ssh://", "git@")):
            parsed = parse_remote(owner_repo)
            if parsed:
                return parsed[0], parsed[1], local_root, None
            return None, None, local_root, None
        return "github.com", owner_repo, local_root, None

    if not local_root:
        return None, None, None, None

    remote = pick_remote(local_root)
    if not remote:
        return None, None, local_root, None
    code, out, _ = run(["git", "remote", "get-url", remote], cwd=local_root)
    if code != 0:
        return None, None, local_root, remote
    parsed = parse_remote(out)
    if not parsed:
        return None, None, local_root, remote
    return parsed[0], parsed[1], local_root, remote


def fetch_raw(owner_repo: str, path: str) -> str | None:
    code, out, _ = run([
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
        return None, None
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


def git_out(local_root: str | None, args: list[str]) -> str | None:
    if not local_root:
        return None
    code, out, _ = run(["git", *args], cwd=local_root)
    return out.rstrip() if code == 0 else None


def resolve_base(local_root: str | None, context_repo: str | None,
                 explicit: str | None, remote: str | None) -> str | None:
    if explicit:
        return explicit
    if context_repo:
        data, _ = gh_json(["repo", "view", context_repo, "--json", "defaultBranchRef"])
        if isinstance(data, dict):
            name = (data.get("defaultBranchRef") or {}).get("name")
            if name:
                return name
    if local_root:
        if remote:
            code, out, _ = run(
                ["git", "symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD"],
                cwd=local_root,
            )
            if code == 0 and out.strip():
                return out.strip().split("/", 1)[-1]
        code, out, _ = run(
            ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            cwd=local_root,
        )
        if code == 0 and out.strip():
            return out.strip().split("/", 1)[-1]
        for candidate in ("main", "master"):
            code, _, _ = run(["git", "rev-parse", "--verify", candidate], cwd=local_root)
            if code == 0:
                return candidate
    return None


def base_ref(local_root: str | None, base: str | None,
             remote: str | None) -> str | None:
    if not local_root or not base:
        return None
    candidates = [f"{remote}/{base}", f"origin/{base}", base] if remote else [f"origin/{base}", base]
    for candidate in candidates:
        code, _, _ = run(["git", "rev-parse", "--verify", candidate], cwd=local_root)
        if code == 0:
            return candidate
    return None


def local_diff(local_root: str | None, base: str | None, head: str | None,
               remote: str | None) -> dict:
    result: dict[str, object] = {}
    if not local_root:
        print("\n## Branch range\n")
        print("_Not inside a Git checkout. Pass a checkout or read the diff yourself._\n")
        return result

    print("\n## Branch range\n")
    current = git_out(local_root, ["branch", "--show-current"])
    print(f" -  Current branch: `{current or '(detached)'}`")
    print(f" -  Base branch: `{base or '(unknown)'}`")
    if head:
        print(f" -  Head branch: `{head}`")
    upstream = git_out(local_root, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"])
    print(f" -  Upstream: `{upstream}`" if upstream else " -  Upstream: _not set_")
    status = git_out(local_root, ["status", "--short"])
    print(f" -  Working tree: {'clean' if not status else 'dirty (uncommitted changes are not part of the PR)'}")
    if status:
        print("\n~~~~ text\n" + truncate(status, 1200) + "\n~~~~\n")

    ref = base_ref(local_root, base, remote)
    if not ref:
        print("_Could not resolve the base branch in this checkout._\n")
        result["range_error"] = "base branch not resolvable"
        return result

    merge_base = git_out(local_root, ["merge-base", ref, "HEAD"])
    if not merge_base:
        print("_Could not find a merge base._\n")
        result["range_error"] = "no merge base"
        return result

    count = git_out(local_root, ["rev-list", "--count", f"{merge_base}..HEAD"])
    print(f"\n### Commits ({count or '?'} since {ref})\n")
    log = git_out(local_root, [
        "log", "--format=%h %s", "--no-decorate", f"{merge_base}..HEAD",
    ])
    print("~~~~ text\n" + (truncate(log, 4000) if log else "(no commits)") + "\n~~~~\n")

    print("### Diff stat\n")
    stat = git_out(local_root, ["diff", "--stat", f"{merge_base}..HEAD"])
    print("~~~~ text\n" + (truncate(stat, 6000) if stat else "(no diff)") + "\n~~~~\n")

    print("### Changed files\n")
    names = git_out(local_root, ["diff", "--name-status", f"{merge_base}..HEAD"])
    print("~~~~ text\n" + (truncate(names, 4000) if names else "(no files)") + "\n~~~~\n")

    result.update({
        "current_branch": current,
        "base_ref": ref,
        "merge_base": merge_base,
        "commit_count": int(count) if count and count.isdigit() else None,
    })
    return result


def own_pr(local_root: str | None, context_repo: str | None, head: str | None) -> dict:
    print("\n## Open pull request for this branch\n")
    data = None
    err = None
    if context_repo and head:
        listed, err = gh_json([
            "pr", "list", "--repo", context_repo, "--head", head,
            "--state", "open", "--limit", "5", "--json",
            "number,title,state,url,isDraft,baseRefName,headRefName",
        ])
        if isinstance(listed, list) and listed:
            data = listed[0]
    elif local_root:
        data, err = gh_json([
            "pr", "view", "--json",
            "number,title,state,url,isDraft,baseRefName,headRefName",
        ], cwd=local_root)
        if isinstance(data, dict) and data.get("state") != "OPEN":
            data = None
    if not data:
        print("_None open._" + (f" (gh said: {err})" if err else "") + "\n")
        return {}
    print(f" -  #{data.get('number')} {data.get('title')}")
    print(f" -  {data.get('state')}, draft: {data.get('isDraft')}, "
          f"{data.get('headRefName')} → {data.get('baseRefName')}")
    print(f" -  {data.get('url')}\n")
    return data


def github_section(context_repo: str, recent: int) -> dict:
    result: dict[str, object] = {}

    print("\n## Pull request templates\n")
    found = False
    tdir, terr = listing(context_repo, ".github/PULL_REQUEST_TEMPLATE")
    if terr:
        print(f"_Could not list `.github/PULL_REQUEST_TEMPLATE`: {terr}_\n")
    elif tdir is not None:
        for entry in sorted(tdir, key=lambda e: e.get("name", "")):
            name = entry.get("name", "")
            if not name.lower().endswith((".md", ".markdown", ".txt")):
                continue
            found = True
            print(f"### `.github/PULL_REQUEST_TEMPLATE/{name}`\n")
            body = fetch_raw(context_repo, f".github/PULL_REQUEST_TEMPLATE/{name}")
            print(f"~~~~ markdown\n{truncate(body, MAX_TEMPLATE_CHARS) if body else '_Could not read this file._'}\n~~~~\n")

    for path in TEMPLATE_FILES:
        body = fetch_raw(context_repo, path)
        if body:
            found = True
            print(f"### `{path}`\n")
            print(f"~~~~ markdown\n{truncate(body, MAX_TEMPLATE_CHARS)}\n~~~~\n")

    # Organization default, only when the project has none of its own.
    if not found:
        org = context_repo.split("/")[0]
        body = fetch_raw(f"{org}/.github", ".github/pull_request_template.md")
        if body is None:
            body = fetch_raw(f"{org}/.github", "pull_request_template.md")
        if body:
            found = True
            print(f"### Organization default from `{org}/.github`\n")
            print(f"~~~~ markdown\n{truncate(body, MAX_TEMPLATE_CHARS)}\n~~~~\n")
    if not found:
        print("_No pull request template found. Choose a shape from "
              "*references/structure.md*._\n")
    result["templates_found"] = found

    print("## Labels\n")
    labels, err = gh_json(["label", "list", "--repo", context_repo, "--limit", "200",
                           "--json", "name,description"])
    label_names = []
    if isinstance(labels, list):
        label_names = [l.get("name", "") for l in labels if l.get("name")]
        print(truncate(", ".join(label_names), 3000) + "\n" if label_names
              else "_No labels defined._\n")
    else:
        print(f"_Unavailable: {err}_\n")
    result["labels"] = label_names

    print("## Open milestones\n")
    milestones, err = gh_json([
        "api", f"repos/{context_repo}/milestones?state=open&per_page=100",
    ])
    titles = [m.get("title", "") for m in milestones] if isinstance(milestones, list) else []
    print(", ".join(titles) + "\n" if titles
          else "_None open._" + (f" (API said: {err})" if err else "") + "\n")
    result["milestones"] = titles

    print("## Recent merged pull requests\n")
    prs, err = gh_json([
        "pr", "list", "--repo", context_repo, "--state", "merged",
        "--limit", str(recent),
        "--json", "number,title,body,author,mergedAt,labels",
    ])
    if not isinstance(prs, list):
        print(f"_Unavailable: {err}_\n")
        return result

    texts = []
    for pr in prs:
        body = pr.get("body") or ""
        author = ((pr.get("author") or {}).get("login")) or "unknown"
        labels = ", ".join(l.get("name", "") for l in pr.get("labels") or [])
        print(f"### #{pr.get('number')} {pr.get('title')}\n")
        print(f"{author}, merged {str(pr.get('mergedAt') or '')[:10]}"
              f"{', ' + labels if labels else ''}\n")
        print(truncate(body, MAX_BODY_CHARS) + "\n")
        texts.append(pr.get("title") or "")
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


def ai_policy_section(host: str | None, local_root: str | None,
                      context_repo: str | None) -> dict:
    """Print the AI policy and the authenticated user's role on the target.

    The role matters because many policies exempt maintainers from the
    disclosure they require of outside contributors.
    """
    print("\n## AI policy\n")
    result: dict[str, object] = {}
    body = None
    source = None
    candidates = ["AI_POLICY.md", "docs/AI_POLICY.md", ".github/AI_POLICY.md"]
    if host == "github.com" and context_repo:
        for path in candidates:
            body = fetch_raw(context_repo, path)
            if body:
                source = path
                break
    if body is None and local_root:
        for path in candidates:
            full = os.path.join(local_root, path)
            if os.path.isfile(full):
                try:
                    with open(full, encoding="utf-8") as handle:
                        body = handle.read()
                    source = path
                except OSError:
                    pass
                break
    if body:
        print(f"### `{source}`\n")
        print(f"~~~~ markdown\n{truncate(body, 8000)}\n~~~~\n")
        result["ai_policy"] = source
    else:
        print("_No AI policy found. Check `CONTRIBUTING.md` before concluding "
              "there is none._\n")

    user, err = gh_json(["api", "user"])
    login = user.get("login") if isinstance(user, dict) else None
    if isinstance(login, str) and login:
        print(f" -  Authenticated account: `{login}`")
        result["login"] = login
    else:
        print(f" -  Authenticated account: _unavailable_{f' ({err})' if err else ''}")

    if host == "github.com" and context_repo:
        perms, perr = gh_json(["api", f"repos/{context_repo}", "--jq", ".permissions"])
        if isinstance(perms, dict):
            granted = [name for name, value in perms.items() if value]
            print(f" -  Permissions on `{context_repo}`: {', '.join(granted)}")
            maintainer = bool(perms.get("admin") or perms.get("maintain"))
            print(f" -  Maintainer exemption: "
                  f"{'applies' if maintainer else 'does not apply'}")
            print("_Read the policy's scope. If it exempts maintainers and this "
                  "account has `maintain` or `admin`, omit the disclosure._\n")
            result["permissions"] = {name: bool(value) for name, value in perms.items()}
            result["maintainer"] = maintainer
        else:
            print(f" -  Permissions: _unavailable_{f' ({perr})' if perr else ''}\n")
    return result


def local_templates(local_root: str | None, show_bodies: bool = True) -> None:
    print("\n## Local template directories\n")
    if not local_root:
        print("_Not inside a Git checkout._\n")
        return
    any_found = False
    for rel in TEMPLATE_DIRS:
        path = os.path.join(local_root, rel)
        if not os.path.isdir(path):
            continue
        any_found = True
        print(f"### `{rel}/`\n")
        for name in sorted(os.listdir(path)):
            if not name.lower().endswith((".md", ".markdown", ".txt")):
                continue
            if not show_bodies:
                print(f" -  `{name}`\n")
                continue
            try:
                with open(os.path.join(path, name), encoding="utf-8") as handle:
                    body = handle.read()
            except OSError:
                continue
            print(f"#### `{name}`\n")
            print(f"~~~~ markdown\n{truncate(body, MAX_TEMPLATE_CHARS)}\n~~~~\n")
    for rel in TEMPLATE_FILES:
        path = os.path.join(local_root, rel)
        if not os.path.isfile(path):
            continue
        any_found = True
        if not show_bodies:
            print(f"`{rel}`: {os.path.getsize(path):,} bytes\n")
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                body = handle.read()
        except OSError:
            continue
        print(f"### `{rel}`\n")
        print(f"~~~~ markdown\n{truncate(body, MAX_TEMPLATE_CHARS)}\n~~~~\n")
    for rel in ("CONTRIBUTING.md", "README.md"):
        path = os.path.join(local_root, rel)
        if os.path.isfile(path):
            print(f"`{rel}`: {os.path.getsize(path):,} bytes\n")
    if not any_found:
        print("_None found in the checkout._\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", help="OWNER/REPO or a remote URL")
    parser.add_argument("--cwd", help="directory to inspect (default: current)")
    parser.add_argument("--base", help="base branch (default: repository default)")
    parser.add_argument("--head", help="head branch (default: current branch)")
    parser.add_argument("--recent", type=int, default=8,
                        help="how many recent merged PRs to sample (default: 8)")
    parser.add_argument("--json", metavar="PATH", help="also write a JSON digest here")
    args = parser.parse_args()

    host, owner_repo, local_root, remote = resolve_target(args)

    print("# Repository context\n")
    print(f" -  Repository: `{owner_repo}`" if owner_repo else " -  Repository: _not resolved_")
    print(f" -  Forge: `{host}`" if host else " -  Forge: _unknown_")
    print(f" -  Local checkout: `{local_root}`" if local_root else " -  Local checkout: _none_")
    if remote:
        print(f" -  Git remote: `{remote}`")
    print()

    digest: dict[str, object] = {"host": host, "repo": owner_repo,
                                 "local_root": local_root, "remote": remote}

    context_repo = owner_repo
    if host == "github.com" and owner_repo:
        parent, perr = gh_json(["repo", "view", owner_repo, "--json", "parent"])
        if isinstance(parent, dict):
            up = (parent.get("parent") or {}).get("nameWithOwner")
            if up:
                context_repo = up
                print(f" -  Fork of: `{up}` (templates, labels, and recent PRs come "
                      "from there)\n")
        elif perr:
            print(f" -  Fork check unavailable: {perr}\n")
        digest["context_repo"] = context_repo

    base = resolve_base(local_root, context_repo, args.base, remote)
    head = args.head or git_out(local_root, ["branch", "--show-current"])
    print(f" -  Base: `{base or '(unknown)'}`")
    print(f" -  Head: `{head or '(unknown)'}`\n")

    digest.update(local_diff(local_root, base, head, remote))
    if context_repo:
        digest.update(own_pr(local_root, context_repo, head))

    if host == "github.com" and context_repo:
        digest.update(github_section(context_repo, args.recent))
        digest.update(ai_policy_section(host, local_root, context_repo))
    elif host:
        print("\n## Forge\n")
        print(f"`{host}` is not GitHub. Use *references/forges.md* for where this "
              "forge keeps its templates and labels.\n")
        digest.update(ai_policy_section(host, local_root, context_repo))
    else:
        print("\n## Forge\n")
        print("_Could not resolve a repository. Pass `--repo OWNER/REPO`._\n")
        digest.update(ai_policy_section(host, local_root, context_repo))

    local_templates(local_root, show_bodies=not bool(digest.get("templates_found")))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(digest, handle, ensure_ascii=False, indent=2)
        print(f"\nWrote digest to `{args.json}`.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
