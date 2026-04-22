"""Keyword trend tracker.

Reads config/keywords.yml → queries YTrends ytrends_research_keyword for each →
classifies into buckets: spike / opportunity / dying / crowded / stable.

Cache: data/keyword_cache.json, 20h TTL so same-day re-runs hit cache instead of
burning API calls.
"""
from __future__ import annotations

import dataclasses
import json
import os
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

import sheet_loader
from ytrends_client import YTrendsClient, extract_structured

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
KEYWORDS_FILE = ROOT / "config" / "keywords.yml"
CACHE_FILE = ROOT / "data" / "keyword_cache.json"

CACHE_TTL_HOURS = 20


@dataclass
class KeywordReport:
    keyword: str
    bucket: str          # spike | opportunity | dying | crowded | stable | error
    opportunity_score: float = 0.0
    competition: str = "?"
    action: str = "?"
    trend: str = "?"
    trend_strength: float = 0.0
    revenue_change_pct: float = 0.0
    price_range: str = "?"
    total_listings: int = 0
    demand_supply_ratio: float = 0.0
    error: str = ""
    action_reason_en: str = ""
    # Top competitor shop in this niche (by revenue). YTrends exposes id only.
    top_shop_id: int = 0
    top_shop_country: str = ""
    top_shop_revenue_usd: float = 0.0
    top_shop_listings: int = 0
    # Best-selling listing in this niche (by total_sold).
    top_listing_id: int = 0
    top_listing_title: str = ""
    top_listing_price_usd: float = 0.0
    top_listing_total_sold: int = 0
    top_listing_revenue_usd: float = 0.0


# ---------- Config + cache ----------

def _load_keywords_from_yaml() -> list[str]:
    if not KEYWORDS_FILE.exists():
        return []
    data = yaml.safe_load(KEYWORDS_FILE.read_text(encoding="utf-8")) or {}
    return [k.strip() for k in (data.get("keywords") or []) if k and str(k).strip()]


def load_keywords() -> list[str]:
    """Prefer Google Sheet `keywords` tab when SHOPS_SHEET_URL is set, else YAML."""
    sheet_url = (os.environ.get("SHOPS_SHEET_URL") or "").strip()
    if sheet_url:
        try:
            kws = sheet_loader.load_keywords(sheet_url)
            if kws:
                return kws
            print("[keyword_tracker] Sheet trả về 0 keyword — fallback keywords.yml")
        except Exception as e:
            print(f"[keyword_tracker] Sheet load failed ({type(e).__name__}: {e}) "
                  f"— fallback keywords.yml")
    return _load_keywords_from_yaml()


def _load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _cache_fresh(ts: str) -> bool:
    if not ts:
        return False
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(ts)
        return age < timedelta(hours=CACHE_TTL_HOURS)
    except Exception:
        return False


# ---------- Classification ----------

def _f(v, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _classify(stats: dict, latest_timeline: dict | None) -> str:
    action = str(stats.get("recommended_action", "")).lower()
    comp = str(stats.get("competition_level", "")).lower()
    opp = _f(stats.get("opportunity_score"))
    listings = int(_f(stats.get("total_listings")))

    t = latest_timeline or {}
    trend_dir = str(t.get("trend_direction", "")).lower()
    trend_str = _f(t.get("trend_strength"))
    rev_pct = _f(t.get("revenue_change_pct"))

    # Not enough data to evaluate (YTrends flag or too few listings)
    if action == "insufficient_data" or listings < 5:
        return "no_data"
    # Spike: strong rising trend or big revenue jump
    if trend_dir == "rising" and (trend_str >= 0.2 or rev_pct >= 30):
        return "spike"
    # Clear opportunity per YTrends
    if action in ("enter_immediately", "enter_now", "strong_opportunity"):
        return "opportunity"
    # Dying: falling trend with meaningful magnitude
    if trend_dir in ("falling", "declining") and (trend_str <= -0.1 or rev_pct <= -30):
        return "dying"
    # Crowded: high competition + low opportunity
    if comp in ("high", "very_high") and opp < 40:
        return "crowded"
    return "stable"


# ---------- Query ----------

def _top_shop(client: YTrendsClient, keyword: str) -> dict:
    """Top competitor shop by revenue. Returns {} on failure (non-critical)."""
    try:
        res = client.call_tool("ytrends_analyze_competition",
                               {"seed": keyword, "seed_type": "keyword"})
        data = extract_structured(res) or {}
        shops = ((data.get("data") or {}).get("top_shops")) or []
        return shops[0] if shops else {}
    except Exception as e:
        print(f"    [analyze_competition fail: {type(e).__name__}: {e}]")
        return {}


def _top_listing(client: YTrendsClient, keyword: str) -> dict:
    """Best-selling listing in this niche. Returns {} on failure (non-critical)."""
    try:
        res = client.call_tool("ytrends_find_hot_listings",
                               {"keyword": keyword, "sort": "sales", "limit": 1})
        data = extract_structured(res) or {}
        listings = ((data.get("data") or {}).get("listings")) or []
        return listings[0] if listings else {}
    except Exception as e:
        print(f"    [find_hot_listings fail: {type(e).__name__}: {e}]")
        return {}


def _research_one(client: YTrendsClient, keyword: str) -> KeywordReport:
    try:
        res = client.call_tool("ytrends_research_keyword", {"keyword": keyword})
        data = extract_structured(res) or {}
    except Exception as e:
        return KeywordReport(keyword=keyword, bucket="error", error=f"{type(e).__name__}: {e}")

    stats = ((data.get("data") or {}).get("stats")) or {}
    timeline = ((data.get("data") or {}).get("timeline")) or []
    latest = timeline[-1] if timeline else None

    if not stats:
        return KeywordReport(keyword=keyword, bucket="error", error="empty stats in response")

    # Supplementary calls — failures degrade gracefully (empty top_shop/top_listing)
    shop = _top_shop(client, keyword)
    listing = _top_listing(client, keyword)

    bucket = _classify(stats, latest)
    return KeywordReport(
        keyword=keyword,
        bucket=bucket,
        opportunity_score=_f(stats.get("opportunity_score")),
        competition=str(stats.get("competition_level") or "?"),
        action=str(stats.get("recommended_action") or "?"),
        trend=str((latest or {}).get("trend_direction") or "?"),
        trend_strength=_f((latest or {}).get("trend_strength")),
        revenue_change_pct=_f((latest or {}).get("revenue_change_pct")),
        price_range=str(stats.get("recommended_price_range") or "?"),
        total_listings=int(_f(stats.get("total_listings"))),
        demand_supply_ratio=_f(stats.get("demand_supply_ratio")),
        action_reason_en=str(stats.get("action_reason") or ""),
        top_shop_id=int(_f(shop.get("shop_id"))),
        top_shop_country=str(shop.get("shop_country") or ""),
        top_shop_revenue_usd=_f(shop.get("total_revenue_usd")),
        top_shop_listings=int(_f(shop.get("listings"))),
        top_listing_id=int(_f(listing.get("listing_id"))),
        top_listing_title=str(listing.get("title") or ""),
        top_listing_price_usd=_f(listing.get("price_usd") or listing.get("price")),
        top_listing_total_sold=int(_f(listing.get("total_sold"))),
        top_listing_revenue_usd=_f(listing.get("revenue")),
    )


def run(delay: float = 1.2) -> list[KeywordReport]:
    kws = load_keywords()
    print(f"[keyword_tracker] {len(kws)} keyword")
    if not kws:
        return []

    cache = _load_cache()
    reports: list[KeywordReport] = []
    calls_made = 0

    known_fields = {f.name for f in dataclasses.fields(KeywordReport)}

    with YTrendsClient() as client:
        for i, kw in enumerate(kws, 1):
            entry = cache.get(kw) or {}
            if _cache_fresh(entry.get("cached_at", "")):
                # Drop fields the current schema doesn't know about (and let
                # new fields default) so old caches don't crash on migration.
                raw = entry.get("report") or {}
                rep = KeywordReport(**{k: v for k, v in raw.items() if k in known_fields})
                print(f"  [{i:2}/{len(kws)}] {kw:30} · cache ({rep.bucket})")
                reports.append(rep)
                continue

            rep = _research_one(client, kw)
            cache[kw] = {
                "cached_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "report": asdict(rep),
            }
            reports.append(rep)
            calls_made += 1

            if rep.bucket == "error":
                print(f"  [{i:2}/{len(kws)}] {kw:30} ⚠ {rep.error}")
            else:
                print(f"  [{i:2}/{len(kws)}] {kw:30} {rep.bucket} "
                      f"(opp={rep.opportunity_score:.0f} comp={rep.competition} trend={rep.trend})")

            if i < len(kws):
                time.sleep(delay)

    _save_cache(cache)
    return reports


if __name__ == "__main__":
    for r in run():
        print(f"\n• {r.keyword} → {r.bucket}")
        if r.error:
            print(f"    error: {r.error}")
            continue
        print(f"    opportunity={r.opportunity_score:.1f}  competition={r.competition}  "
              f"action={r.action}")
        print(f"    trend={r.trend}  strength={r.trend_strength:+.2f}  "
              f"revenue_change={r.revenue_change_pct:+.1f}%")
        print(f"    price_range={r.price_range}  listings={r.total_listings}  "
              f"demand_supply={r.demand_supply_ratio:.2f}")
        if r.action_reason_en:
            print(f"    reason: {r.action_reason_en}")
