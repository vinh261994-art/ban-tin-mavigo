"""Scrape public shop pages (Etsy + eBay) to track daily sales deltas.

Etsy: https://www.etsy.com/shop/<NAME>         → "X Sales" in header
eBay: https://www.ebay.com/str/<NAME>          → "N items sold" in embedded JSON
      https://www.ebay.com/usr/<NAME>          → same pattern, user profile variant

Output: data/sales_history.json (appended per day).
"""
from __future__ import annotations

import json
import os
import random
import re
import sys
import time

# Windows cp1252 console can't print unicode; force UTF-8 when attached to a terminal
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
import yaml

import sheet_loader

ROOT = Path(__file__).resolve().parent.parent
SHOPS_FILE = ROOT / "config" / "shops.yml"
HISTORY_FILE = ROOT / "data" / "sales_history.json"

# Rotating UAs — real Chrome versions, kept short to avoid detection heuristics
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]

HEADERS_BASE = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


@dataclass
class Snapshot:
    date: str          # ISO date YYYY-MM-DD (UTC)
    total_sales: int   # cumulative lifetime sales at time of scrape
    delta: Optional[int] = None   # vs previous snapshot; None on first run or parse fail
    error: Optional[str] = None   # set if scrape/parse failed this run


SCRAPINGBEE_API_KEY = (os.environ.get("SCRAPINGBEE_API_KEY") or "").strip()


class _ScrapeError(RuntimeError):
    """Raised when the proxy returns a non-2xx target status."""


def _fetch_scrapingbee(url: str, platform: str | None) -> str:
    """Fetch via ScrapingBee API. Returns target HTML.

    Credits (render_js=false saves ~5x):
      eBay → classic proxy, 1 credit/call.
      Etsy → stealth_proxy=true (only mode that bypasses Akamai bot
             challenge), 75 credits/call. Premium proxy alone returns 403.
    """
    from urllib.parse import urlencode
    params = {
        "api_key": SCRAPINGBEE_API_KEY,
        "url": url,
        "render_js": "false",
        "country_code": "us",
    }
    if platform == "etsy":
        params["stealth_proxy"] = "true"
    api_url = f"https://app.scrapingbee.com/api/v1/?{urlencode(params)}"
    # Premium proxy can take 30–60s
    t = 90.0 if platform == "etsy" else 45.0
    with httpx.Client(timeout=t, follow_redirects=True) as client:
        r = client.get(api_url)
    if r.status_code >= 400:
        # Spb-original-status-code header carries target's status when available
        target_status = r.headers.get("Spb-original-status-code", "")
        if target_status:
            raise _ScrapeError(f"HTTP {target_status}")
        raise _ScrapeError(f"API HTTP {r.status_code}")
    return r.text


def _fetch(url: str, platform: str | None = None, timeout: float = 25.0) -> str:
    if SCRAPINGBEE_API_KEY:
        return _fetch_scrapingbee(url, platform)
    # Direct fetch — used for local dev; will 403 on GHA IP for Etsy
    headers = {**HEADERS_BASE, "User-Agent": random.choice(USER_AGENTS)}
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
        r = client.get(url)
        r.raise_for_status()
        return r.text


# ---------- Etsy parsers ----------

ETSY_SALES_PATTERNS = [
    # Common Etsy header markup — "X,XXX Sales" or "X Sale"
    re.compile(r'>\s*([\d,]+)\s*Sales?\s*<', re.IGNORECASE),
    # JSON embed (less stable key name — Etsy swaps these occasionally)
    re.compile(r'"transaction_sold_count"\s*:\s*(\d+)'),
    re.compile(r'"num_sold"\s*:\s*(\d+)'),
]


def parse_etsy_sales(html: str) -> Optional[int]:
    for pat in ETSY_SALES_PATTERNS:
        m = pat.search(html)
        if m:
            return int(m.group(1).replace(",", ""))
    return None


# ---------- eBay parsers ----------

# Confirmed from stephanie9121 page: PRESENCE_INFORMATION_MODULE has a TextSpan
# with the bold number immediately followed by a span " items sold"
EBAY_SOLD_PATTERN = re.compile(
    r'"text"\s*:\s*"([\d,]+)"\s*,\s*"styles"\s*:\s*\["BOLD"\]\s*\}\s*,\s*'
    r'\{\s*"_type"\s*:\s*"TextSpan"\s*,\s*"text"\s*:\s*"\s*items sold"'
)
# Fallback — simpler text proximity search
EBAY_SOLD_PATTERN_FALLBACK = re.compile(r'([\d,]+)\s*items sold', re.IGNORECASE)


def parse_ebay_sales(html: str) -> Optional[int]:
    m = EBAY_SOLD_PATTERN.search(html)
    if m:
        return int(m.group(1).replace(",", ""))
    # Fallback (risk: may catch unrelated "items sold" in footer scripts)
    m = EBAY_SOLD_PATTERN_FALLBACK.search(html)
    if m:
        return int(m.group(1).replace(",", ""))
    return None


# ---------- Shop tracking flow ----------

def scrape_shop(platform: str, url: str) -> tuple[Optional[int], Optional[str]]:
    """Returns (total_sales, error_msg). One of them is None."""
    try:
        html = _fetch(url, platform=platform)
    except _ScrapeError as e:
        return None, str(e)
    except httpx.HTTPStatusError as e:
        return None, f"proxy HTTP {e.response.status_code}"
    except httpx.TransportError as e:
        return None, f"network: {type(e).__name__}"

    if platform == "etsy":
        total = parse_etsy_sales(html)
    elif platform == "ebay":
        total = parse_ebay_sales(html)
    else:
        return None, f"unknown platform: {platform}"

    if total is None:
        return None, "parse failed (selector may have changed)"
    return total, None


def load_history() -> dict:
    if not HISTORY_FILE.exists():
        return {"last_updated": None, "shops": {}}
    with HISTORY_FILE.open(encoding="utf-8") as f:
        return json.load(f)


def save_history(history: dict) -> None:
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_FILE.open("w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def _load_shops_from_yaml() -> list[dict]:
    if not SHOPS_FILE.exists():
        raise FileNotFoundError(f"Missing {SHOPS_FILE}")
    with SHOPS_FILE.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    shops = []
    for platform in ("etsy", "ebay"):
        for entry in data.get(platform) or []:
            if not entry or "url" not in entry:
                continue
            shops.append({
                "name": entry.get("name") or entry["url"].rsplit("/", 1)[-1],
                "url": entry["url"],
                "platform": platform,
            })
    return shops


def load_shops() -> list[dict]:
    """Load shop list. Prefer Google Sheet if SHOPS_SHEET_URL is set, else YAML."""
    sheet_url = (os.environ.get("SHOPS_SHEET_URL") or "").strip()
    if sheet_url:
        try:
            shops = sheet_loader.load_shops(sheet_url)
            if shops:
                return shops
            print("[shop_tracker] Sheet trả về 0 shop — fallback shops.yml")
        except Exception as e:
            print(f"[shop_tracker] Sheet load failed ({type(e).__name__}: {e}) "
                  f"— fallback shops.yml")
    return _load_shops_from_yaml()


def run(delay_range: tuple[float, float] = (3.0, 6.0)) -> dict:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    shops = load_shops()
    history = load_history()
    shop_map = history.setdefault("shops", {})

    mode = "via ScrapingBee" if SCRAPINGBEE_API_KEY else "direct"
    print(f"[shop_tracker] {today} · scraping {len(shops)} shops ({mode})")

    for i, shop in enumerate(shops, 1):
        key = shop["name"]
        total, err = scrape_shop(shop["platform"], shop["url"])

        entry = shop_map.setdefault(key, {
            "platform": shop["platform"],
            "url": shop["url"],
            "snapshots": [],
        })
        # Keep url/platform fresh if user edited shops.yml
        entry["platform"] = shop["platform"]
        entry["url"] = shop["url"]

        snaps = entry["snapshots"]
        prev = snaps[-1] if snaps else None

        if err:
            snap = Snapshot(date=today, total_sales=prev["total_sales"] if prev else 0,
                            delta=None, error=err)
            print(f"  [{i:2}/{len(shops)}] {shop['platform']:5} {key:25} ⚠ {err}")
        else:
            delta = None
            if prev and prev.get("total_sales") is not None and not prev.get("error"):
                delta = total - prev["total_sales"]
            snap = Snapshot(date=today, total_sales=total, delta=delta)
            marker = "✓" if delta is None else f"Δ{delta:+d}"
            print(f"  [{i:2}/{len(shops)}] {shop['platform']:5} {key:25} {marker} (total={total})")

        # If we already wrote a snapshot for `today`, overwrite (idempotent same-day runs)
        if snaps and snaps[-1]["date"] == today:
            snaps[-1] = asdict(snap)
        else:
            snaps.append(asdict(snap))

        # Polite delay — ScraperAPI handles pacing itself but small gap is harmless
        if i < len(shops):
            time.sleep(random.uniform(*delay_range))

    history["last_updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_history(history)
    return history


if __name__ == "__main__":
    # Allow smoke-test with `python shop_tracker.py --test` (shorter delay)
    delays = (0.5, 1.5) if "--test" in sys.argv else (3.0, 6.0)
    run(delay_range=delays)
