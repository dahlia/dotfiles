#!/usr/bin/env python3
"""Read repository GHSA metadata through gh; succeed only if all are public."""
import argparse
import json
import re
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("advisories", nargs="+", metavar="OWNER/REPO/GHSA-ID")
    args = parser.parse_args()
    targets = list(dict.fromkeys(args.advisories))
    pattern = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/GHSA-[23456789cfghjmpqrvwx]{4}-[23456789cfghjmpqrvwx]{4}-[23456789cfghjmpqrvwx]{4}"
    if any(not re.fullmatch(pattern, target) for target in targets):
        parser.error("Use OWNER/REPO/GHSA-xxxx-xxxx-xxxx for every advisory.")
    results = []
    for target in targets:
        owner, repo, ghsa = target.split("/")
        try:
            response = subprocess.run(
                ["gh", "api", f"repos/{owner}/{repo}/security-advisories/{ghsa}"],
                capture_output=True, text=True, timeout=45,
            )
            if response.returncode:
                # Avoid echoing response bodies or private advisory details.
                raise ValueError("gh API request failed; publication is unverified")
            data = json.loads(response.stdout)
            if not isinstance(data, dict) or data.get("ghsa_id") != ghsa:
                raise ValueError("Unexpected advisory metadata; publication is unverified")
            state = data.get("state")
            withdrawn = data.get("withdrawn_at")
            public = state == "published" and bool(data.get("published_at")) and not withdrawn
            results.append({
                "target": target, "public": public, "state": state,
                "published_at": data.get("published_at"),
                "withdrawn_at": withdrawn,
                "primary_id": (data.get("cve_id") or ghsa) if public else None,
                "url": data.get("html_url") if public else None,
            })
        except (OSError, subprocess.TimeoutExpired, ValueError) as error:
            results.append({"target": target, "public": False, "error": str(error)})
    print(json.dumps({"all_public": all(r["public"] for r in results), "advisories": results}, indent=2))
    return 0 if all(r["public"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
