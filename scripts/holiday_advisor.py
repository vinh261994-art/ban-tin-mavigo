"""Upcoming holidays advisor.

Reads config/holidays.json (curated) and enriches each near-term event with
keywords from YTrends trend_calendar (cached in data/ytrends_calendar_cache.json
for 7 days so we don't re-fetch daily).
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from ytrends_client import YTrendsClient, extract_structured, extract_text_content

ROOT = Path(__file__).resolve().parent.parent
HOLIDAYS_FILE = ROOT / "config" / "holidays.json"
CALENDAR_CACHE = ROOT / "data" / "ytrends_calendar_cache.json"

DEFAULT_LOOKAHEAD_DAYS = 60
CACHE_TTL_DAYS = 7


@dataclass
class UpcomingEvent:
    name: str
    name_vi: str
    date: str                  # YYYY-MM-DD
    days_until: int
    lead_days: int
    market: str
    status: str                # "on_time" | "late" | "upcoming"
    categories: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)


# ---------- YTrends calendar cache ----------

def _cache_fresh() -> bool:
    if not CALENDAR_CACHE.exists():
        return False
    try:
        cached = json.loads(CALENDAR_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return False
    ts = cached.get("cached_at")
    if not ts:
        return False
    age = datetime.now(timezone.utc) - datetime.fromisoformat(ts)
    return age < timedelta(days=CACHE_TTL_DAYS)


def _load_ytrends_calendar() -> dict:
    """Return cached ytrends_trend_calendar data, refetching if cache is stale."""
    if _cache_fresh():
        return json.loads(CALENDAR_CACHE.read_text(encoding="utf-8"))["data"]

    print("[holiday_advisor] refreshing YTrends calendar cache")
    try:
        with YTrendsClient() as y:
            res = y.call_tool("ytrends_trend_calendar", {})
            data = extract_structured(res) or {"text": extract_text_content(res)}
    except Exception as e:
        print(f"[holiday_advisor] YTrends calendar fetch failed: {e!r} — using empty")
        data = {}

    CALENDAR_CACHE.parent.mkdir(parents=True, exist_ok=True)
    CALENDAR_CACHE.write_text(
        json.dumps({"cached_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "data": data}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return data


def _ytrends_keywords_for(event_name: str, event_date: str, ytrends_data: dict) -> list[str]:
    """Try to find keyword suggestions for an event from YTrends calendar data.

    The exact structure of ytrends_trend_calendar output is schema-dependent;
    we do a best-effort match by name or nearby date.
    """
    if not ytrends_data:
        return []

    events = []
    # Hunt for a list of events in common shape names
    for key in ("events", "calendar", "items", "data"):
        v = ytrends_data.get(key)
        if isinstance(v, list):
            events = v
            break
    if not events and isinstance(ytrends_data, list):
        events = ytrends_data

    name_l = event_name.lower()
    try:
        target = datetime.strptime(event_date, "%Y-%m-%d").date()
    except ValueError:
        target = None

    for ev in events:
        if not isinstance(ev, dict):
            continue
        ev_name = str(ev.get("name", "") or ev.get("event", "") or "").lower()
        ev_date_str = ev.get("date") or ev.get("event_date") or ""
        match = False
        if ev_name and (name_l in ev_name or ev_name in name_l):
            match = True
        elif target and ev_date_str:
            try:
                ev_date = datetime.strptime(ev_date_str[:10], "%Y-%m-%d").date()
                if abs((ev_date - target).days) <= 3:
                    match = True
            except ValueError:
                pass
        if match:
            kws = ev.get("keywords") or ev.get("top_keywords") or ev.get("tags") or []
            if isinstance(kws, list):
                return [str(k) for k in kws if k][:10]
    return []


# ---------- Main advisor logic ----------

def load_holidays() -> list[dict]:
    data = json.loads(HOLIDAYS_FILE.read_text(encoding="utf-8"))
    return data.get("holidays", [])


def upcoming(lookahead_days: int = DEFAULT_LOOKAHEAD_DAYS,
             today: Optional[date] = None) -> list[UpcomingEvent]:
    today = today or datetime.now(timezone.utc).date()
    cutoff = today + timedelta(days=lookahead_days)

    events_raw = load_holidays()
    ytrends_data = _load_ytrends_calendar()

    results: list[UpcomingEvent] = []
    for ev in events_raw:
        try:
            ev_date = datetime.strptime(ev["date"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue
        if ev_date < today or ev_date > cutoff:
            continue

        days_until = (ev_date - today).days
        lead = int(ev.get("lead_days", 14))
        # late = under half the prep window left → SEO won't rank, ads-only
        # on_time = inside prep window (+ 2-week buffer before it officially starts)
        # upcoming = farther out, just monitor
        if days_until <= lead // 2:
            status = "late"
        elif days_until <= lead + 14:
            status = "on_time"
        else:
            status = "upcoming"

        yt_kw = _ytrends_keywords_for(ev["name"], ev["date"], ytrends_data)
        keywords = yt_kw or list(ev.get("keywords", []))

        results.append(UpcomingEvent(
            name=ev["name"],
            name_vi=ev.get("name_vi", ev["name"]),
            date=ev["date"],
            days_until=days_until,
            lead_days=lead,
            market=ev.get("market", "Global"),
            status=status,
            categories=list(ev.get("categories", [])),
            keywords=keywords,
        ))

    results.sort(key=lambda e: e.days_until)
    return results


if __name__ == "__main__":
    events = upcoming()
    print(f"[holiday_advisor] {len(events)} events in next {DEFAULT_LOOKAHEAD_DAYS} days\n")
    for e in events:
        print(f"• {e.name_vi} ({e.name})  {e.date}  · còn {e.days_until} ngày · {e.status}")
        print(f"    market={e.market} · lead_days={e.lead_days}")
        if e.keywords:
            print(f"    keywords: {', '.join(e.keywords[:6])}")
        print()
