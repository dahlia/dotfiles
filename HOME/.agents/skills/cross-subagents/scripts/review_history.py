"""Local CLI review performance, derived from receipts (never transcripts)."""

from collections import defaultdict
from datetime import datetime
import json
import re
from statistics import median
import time
from pathlib import Path
from typing import Any

WINDOW_SECONDS = 30 * 86400
MAX_SAMPLES_PER_ROUTE = 40
LEGACY_FIRST_ROUND = re.compile(r"step[123]-r1(?:-a1)?")


def load_review_history(runs_dir: Path) -> dict[str, dict[str, Any]]:
    now = time.time()
    samples: dict[str, list[tuple[float, str, int]]] = defaultdict(list)
    for path in runs_dir.glob("*/manifest.jsonl"):
        try:
            if path.stat().st_mtime < now - WINDOW_SECONDS:
                continue
            with path.open(encoding="utf-8", errors="replace") as stream:
                seen: set[tuple[str, str]] = set()
                for line in stream:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(row, dict) or row.get("event") != "task_finished":
                        continue
                    task = row.get("task_id")
                    route = row.get("route_id")
                    if not isinstance(task, str) or not isinstance(route, str):
                        continue
                    category = row.get("task_category")
                    if category is None:
                        # Legacy receipts lack an explicit category or resume flag.
                        # Use only unambiguous first-round code-review records.
                        summary = row.get("task_summary")
                        if (not LEGACY_FIRST_ROUND.fullmatch(task) or not isinstance(summary, str)
                                or not re.search(r"\bcode[ _-]review\b", summary, re.I)):
                            continue
                    elif category != "code_review" or row.get("resumed"):
                        continue
                    if (row.get("cleanup_verified") is False or row.get("denial_count")
                            or row.get("status") not in ("ok", "timeout")):
                        continue
                    duration = row.get("duration_ms")
                    if type(duration) is not int or duration <= 0:
                        continue
                    try:
                        ended = datetime.fromisoformat(str(row["ended_at"]).replace("Z", "+00:00")).timestamp()
                    except (KeyError, ValueError, OverflowError, OSError):
                        continue
                    key = (task, route)
                    if not 0 <= now - ended <= WINDOW_SECONDS or key in seen:
                        continue
                    seen.add(key)
                    samples[route].append((ended, row["status"], duration))
        except OSError:
            continue
    result = {}
    for route, entries in samples.items():
        recent = sorted(entries, reverse=True)[:MAX_SAMPLES_PER_ROUTE]
        completed = [ms for _, status, ms in recent if status == "ok"]
        result[route] = {
            "samples": len(recent),
            "timeouts": len(recent) - len(completed),
            "median_completion_seconds": round(median(completed) / 1000, 1) if completed else None,
        }
    return result


def review_history_adjustment(history: dict[str, Any], speed: str) -> float:
    """Bounded soft penalty; shrink sparse evidence toward the neutral score."""
    n = history["samples"]
    timeout_penalty = 0.24 * history["timeouts"] / (n + 5)
    seconds = history["median_completion_seconds"]
    latency_penalty = 0.0 if seconds is None else (
        0.12 * min(1.0, max(0.0, (seconds - 90) / 510))
        * (n - history["timeouts"]) / (n - history["timeouts"] + 5)
    )
    return round(-(timeout_penalty + latency_penalty) * (0.5 if speed == "normal" else 1.0), 4)
