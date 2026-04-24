"""Dedup lock — prevent sending the same bulletin twice on the same day/week.

Multiple cron times in the workflow fire redundant runs as a safety net against
GitHub Actions skipping or delaying a cron tick. This module ensures only the
first successful run actually sends to Telegram; subsequent runs no-op.

State file: data/last_sent.json
    {"daily": "2026-04-24", "weekly": "2026-W17"}

Override with FORCE_SEND=1 to bypass (for manual testing).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "data" / "last_sent.json"


def _load() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _week_key() -> str:
    # ISO year-week, e.g. 2026-W17. A Monday is always the first day of its ISO week.
    y, w, _ = datetime.now(timezone.utc).isocalendar()
    return f"{y}-W{w:02d}"


def _current_key(kind: str) -> str:
    if kind == "daily":
        return _today_key()
    if kind == "weekly":
        return _week_key()
    raise ValueError(f"unknown kind: {kind}")


def already_sent(kind: str) -> bool:
    """Return True if the bulletin of this kind was already sent this period.

    Respects FORCE_SEND=1 as an override for manual re-runs.
    """
    if os.environ.get("FORCE_SEND", "").strip() in ("1", "true", "yes"):
        print(f"[run_lock] FORCE_SEND set — bypassing dedup for {kind}")
        return False
    state = _load()
    return state.get(kind) == _current_key(kind)


def mark_sent(kind: str) -> None:
    """Persist that this bulletin was successfully sent for the current period."""
    state = _load()
    state[kind] = _current_key(kind)
    _save(state)
    print(f"[run_lock] marked {kind}={state[kind]}")
