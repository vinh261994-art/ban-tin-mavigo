"""Higher-level wrappers around YTrends MCP tools for the weekly/daily reports.

Thin functional helpers — no state, no I/O besides the MCP client itself.
"""
from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict
from typing import Any, Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from ytrends_client import YTrendsClient, extract_structured


# ==========================================================
#  Low-level wrappers
# ==========================================================

def _call(tool: str, args: dict | None = None) -> dict:
    """Call one MCP tool and return the `data` payload (dict), or {} on error."""
    try:
        with YTrendsClient() as y:
            res = y.call_tool(tool, args or {})
            data = extract_structured(res) or {}
    except Exception as e:
        print(f"[ytrends_analytics] {tool} failed: {type(e).__name__}: {e}")
        return {}
    if isinstance(data, dict):
        inner = data.get("data")
        if isinstance(inner, dict):
            return inner
    return data if isinstance(data, dict) else {}


def get_market_snapshot(country: str = "US") -> dict:
    """Return a flat dict of key macro stats."""
    d = _call("ytrends_market_snapshot", {"country": country})
    if not d:
        return {}
    ov = d.get("overview") or {}
    mkt = d.get("market") or {}
    return {
        "total_listings": ov.get("total_listings"),
        "total_sellers": ov.get("total_sellers"),
        "avg_price": ov.get("avg_price"),
        "median_price": ov.get("median_price"),
        "avg_conversion_rate": ov.get("avg_conversion_rate"),
        "recommended_action": ov.get("recommended_action"),
        "price_p25": mkt.get("ms.price_p25"),
        "price_p75": mkt.get("ms.price_p75"),
        "pct_new_sellers": mkt.get("ms.ms.pct_new_sellers"),
        "sales_per_seller_per_day": mkt.get("ms.ms.sales_per_seller_per_day"),
        "country": country,
    }


def get_hot_listings(limit: int = 8) -> list[dict]:
    """Return top-N hot listings (performance_score sorted by API)."""
    d = _call("ytrends_find_hot_listings", {"limit": limit})
    items = d.get("listings") or []
    return [x for x in items if isinstance(x, dict)]


def get_trending(limit: int = 20) -> list[dict]:
    d = _call("ytrends_find_trending_keywords", {"limit": limit})
    return [x for x in (d.get("tags") or []) if isinstance(x, dict)]


def get_gems(limit: int = 20) -> list[dict]:
    d = _call("ytrends_find_hidden_gems", {"limit": limit})
    return [x for x in (d.get("tags") or []) if isinstance(x, dict)]


def explore_niche(seed: str) -> dict:
    """Return the full explore_niche packet for one seed, or {} if unindexed."""
    d = _call("ytrends_explore_niche", {"seed": seed})
    if not d:
        return {}
    return {
        "overview": d.get("overview") or {},
        "adjacent_tags": d.get("adjacent_tags") or [],
        "top_listings": d.get("top_listings") or [],
        "price_sweet_spot": d.get("price_sweet_spot") or {},
    }


# ==========================================================
#  Cross-reference: trending ∩ gems
# ==========================================================

def intersection(trending: list[dict], gems: list[dict]) -> list[dict]:
    """Return keywords appearing in BOTH lists. High-signal niches."""
    gem_index = {g.get("tag", "").lower(): g for g in gems if g.get("tag")}
    out = []
    for t in trending:
        tag = (t.get("tag") or "").lower()
        if tag and tag in gem_index:
            g = gem_index[tag]
            out.append({
                "tag": t.get("tag"),
                "momentum_score": t.get("momentum_score"),
                "gem_score": g.get("gem_score"),
                "competition_level": t.get("competition_level") or g.get("competition_level"),
                "avg_price": t.get("avg_price") or g.get("avg_price"),
                "avg_conversion_rate": t.get("avg_conversion_rate") or g.get("avg_conversion_rate"),
                "seller_count": t.get("seller_count") or g.get("seller_count"),
                "action_reason": t.get("action_reason") or g.get("action_reason"),
                "data_confidence": g.get("data_confidence"),
            })
    # Sort by combined momentum + gem
    def _score(x: dict) -> float:
        m = x.get("momentum_score") or 0
        g = x.get("gem_score") or 0
        try:
            return float(m) + float(g)
        except (TypeError, ValueError):
            return 0
    out.sort(key=_score, reverse=True)
    return out


# ==========================================================
#  Cluster: group keywords by shared token
# ==========================================================

_STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "of", "to", "in", "on", "with",
    "my", "your", "his", "her", "its", "this", "that", "these", "those",
    "is", "are", "be", "as", "at", "by", "from", "new", "custom",
}


def _tokenize(phrase: str) -> list[str]:
    return [w for w in re.findall(r"[a-z]+", phrase.lower())
            if len(w) > 2 and w not in _STOPWORDS]


def cluster_by_token(items: list[dict], key: str = "tag",
                     min_cluster: int = 2) -> list[dict]:
    """Group items that share a salient token in their keyword.

    Returns clusters sorted by size desc. Each cluster:
        {theme: str, size: int, items: [original dicts]}
    """
    if not items:
        return []
    # Index tokens -> items
    token_to_items: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        phrase = it.get(key) or ""
        for tok in _tokenize(phrase):
            token_to_items[tok].append(it)

    # Keep tokens used by >= min_cluster distinct items
    clusters = []
    used_ids = set()
    # Prefer longer tokens & larger groups
    token_order = sorted(token_to_items.keys(),
                         key=lambda t: (len(token_to_items[t]), len(t)),
                         reverse=True)
    for tok in token_order:
        members = token_to_items[tok]
        # Dedupe by id(obj); also avoid double-assigning same item to multiple clusters
        dedup = []
        seen = set()
        for m in members:
            mid = id(m)
            if mid in seen or mid in used_ids:
                continue
            seen.add(mid)
            dedup.append(m)
        if len(dedup) < min_cluster:
            continue
        for m in dedup:
            used_ids.add(id(m))
        clusters.append({"theme": tok, "size": len(dedup), "items": dedup})

    clusters.sort(key=lambda c: c["size"], reverse=True)
    return clusters


# ==========================================================
#  Seasonal picks: for each upcoming event, explore top seed(s)
# ==========================================================

def _score_niche(packet: dict) -> float:
    """Rank a niche packet by how much 'hot quality' its top listings have.

    TOP_PERFORMER verdict = strong signal. Combines count of TOP_PERFORMER
    listings with avg conversion rate of top 3.
    """
    top = packet.get("top_listings") or []
    if not top:
        return 0.0
    top_perf = sum(1 for t in top if "TOP_PERFORMER" in (t.get("listing_verdict") or ""))
    avg_conv = 0.0
    sample = top[:3]
    if sample:
        convs = [t.get("conversion_rate") or 0 for t in sample]
        try:
            avg_conv = sum(float(c) for c in convs) / len(sample)
        except (TypeError, ValueError):
            avg_conv = 0.0
    # Weight: each TOP_PERFORMER worth 1.0, avg conversion in percent points
    return float(top_perf) + avg_conv * 100


def seasonal_picks(events: list, max_events: int = 3,
                   min_top_listings: int = 2,
                   seeds_per_event: int = 3) -> list[dict]:
    """For each of the nearest `max_events` on_time/late events, try multiple
    seeds from the event's keyword list and pick the one with the highest
    "hot quality" niche (TOP_PERFORMER count + avg conversion rate).

    Returns list of {event, seed, niche, tried} dicts.
    """
    out = []
    for ev in events:
        if len(out) >= max_events:
            break
        if getattr(ev, "status", None) not in ("on_time", "late"):
            continue
        kws = list(getattr(ev, "keywords", None) or [])
        if not kws:
            continue

        # Try up to N seeds, score each, keep the best
        tried = []
        best = None
        best_score = -1.0
        best_seed = None
        for cand in kws[:seeds_per_event]:
            packet = explore_niche(cand)
            top = packet.get("top_listings") or []
            score = _score_niche(packet) if len(top) >= min_top_listings else 0.0
            tried.append((cand, len(top), round(score, 1)))
            if score > best_score:
                best_score = score
                best = packet
                best_seed = cand

        if best and best_score > 0:
            out.append({"event": ev, "seed": best_seed, "niche": best, "tried": tried})
        else:
            print(f"[ytrends_analytics] no seed matched for {ev.name} — tried {tried}")

    return out
