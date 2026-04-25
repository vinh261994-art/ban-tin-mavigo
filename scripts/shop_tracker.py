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
    # Stealth proxy can take 60–150s — Akamai challenge solve is slow
    t = 180.0 if platform == "etsy" else 45.0
    with httpx.Client(timeout=t, follow_redirects=True) as client:
        r = client.get(api_url)
    if r.status_code >= 400:
        # Spb-original-status-code header carries target's status when available
        target_status = r.headers.get("Spb-original-status-code", "")
        if target_status:
            raise _ScrapeError(f"HTTP {target_status}")
        body_snippet = (r.text or "")[:200].replace("\n", " ")
        print(f"[scrapingbee] {r.status_code} body={body_snippet!r}")
        raise _ScrapeError(f"API HTTP {r.status_code}")
    return r.text


def _fetch_direct(url: str, timeout: float = 25.0) -> str:
    headers = {**HEADERS_BASE, "User-Agent": random.choice(USER_AGENTS)}
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
        r = client.get(url)
        r.raise_for_status()
        return r.text


def _fetch(url: str, platform: str | None = None, timeout: float = 25.0) -> str:
    # eBay seller pages (/usr/) are public and load fine from GH Actions US IP
    # without a proxy. Try direct first to save ScrapingBee credits; fall back
    # to ScrapingBee only when direct fails (rate-limit, region block, etc.).
    if platform == "ebay":
        try:
            return _fetch_direct(url, timeout=timeout)
        except Exception as e:
            if not SCRAPINGBEE_API_KEY:
                raise
            print(f"[ebay] direct fetch failed ({e}) — fallback ScrapingBee")
            return _fetch_scrapingbee(url, platform)
    if SCRAPINGBEE_API_KEY:
        return _fetch_scrapingbee(url, platform)
    return _fetch_direct(url, timeout=timeout)


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

# Inactive-seller markers — present on /usr/ pages that rendered fully but have
# no items-sold widget because the account has never sold (sie374867 case).
EBAY_INACTIVE_MARKERS = (
    "No active listings",
    "0 Followers",
    '"totalFeedback":0',
    '"totalFeedback":"0"',
)


def parse_ebay_sales(html: str) -> Optional[int]:
    m = EBAY_SOLD_PATTERN.search(html)
    if m:
        return int(m.group(1).replace(",", ""))
    # Fallback (risk: may catch unrelated "items sold" in footer scripts)
    m = EBAY_SOLD_PATTERN_FALLBACK.search(html)
    if m:
        return int(m.group(1).replace(",", ""))
    return None


def is_ebay_inactive(html: str) -> bool:
    """True if page rendered fully but seller has no sales history.

    Only returns true when we see BOTH the core profile module (proves render
    is complete) AND at least one inactive-seller signal — otherwise a partial
    render would be misclassified as 0 sales.
    """
    if "PRESENCE_INFORMATION_MODULE" not in html and "profileModule" not in html:
        return False
    return any(m in html for m in EBAY_INACTIVE_MARKERS)


# ---------- Shop tracking flow ----------

def _scrape_once(platform: str, url: str) -> tuple[Optional[int], Optional[str], str]:
    """Single attempt. Returns (total, err, html) — html is empty on fetch fail."""
    try:
        html = _fetch(url, platform=platform)
    except _ScrapeError as e:
        return None, str(e), ""
    except httpx.HTTPStatusError as e:
        return None, f"proxy HTTP {e.response.status_code}", ""
    except httpx.TransportError as e:
        return None, f"network: {type(e).__name__}", ""

    if platform == "etsy":
        total = parse_etsy_sales(html)
    elif platform == "ebay":
        total = parse_ebay_sales(html)
    else:
        return None, f"unknown platform: {platform}", html

    if total is None:
        return None, "parse failed (selector may have changed)", html
    return total, None, html


def scrape_shop(platform: str, url: str) -> tuple[Optional[int], Optional[str]]:
    """Returns (total_sales, error_msg). One of them is None.

    Retries once on parse failure — ScrapingBee classic proxy occasionally
    returns partial renders that miss the items-sold widget. Inactive eBay
    sellers (no sold data at all) are reported as 0 with a distinct error tag
    so the bulletin can render them softly instead of as a scrape failure.
    """
    total, err, html = _scrape_once(platform, url)
    if total is None and err and "parse failed" in err:
        if platform == "ebay" and is_ebay_inactive(html):
            return 0, "inactive (no sold data)"
        time.sleep(2.0)
        total, err, html = _scrape_once(platform, url)
        if total is None and platform == "ebay" and is_ebay_inactive(html):
            return 0, "inactive (no sold data)"
    return total, err


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


def _load_etsy_sales_from_sheet() -> dict[str, dict]:
    """Pull Etsy sales from Apps Script's Data tab (free, no Akamai block).

    Returns empty dict if SHOPS_SHEET_URL unset or read fails — caller falls
    back to live ScrapingBee scrape in that case.
    """
    sheet_url = (os.environ.get("SHOPS_SHEET_URL") or "").strip()
    if not sheet_url:
        return {}
    try:
        return sheet_loader.load_etsy_sales(sheet_url)
    except Exception as e:
        print(f"[shop_tracker] không đọc được tab Data ({type(e).__name__}: {e}) "
              f"— fallback scrape Etsy trực tiếp")
        return {}


def run(delay_range: tuple[float, float] = (3.0, 6.0)) -> dict:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    shops = load_shops()
    history = load_history()
    shop_map = history.setdefault("shops", {})
    etsy_sales = _load_etsy_sales_from_sheet()

    mode = "via ScrapingBee" if SCRAPINGBEE_API_KEY else "direct"
    etsy_from_sheet = sum(1 for s in shops if s["platform"] == "etsy" and s["name"] in etsy_sales)
    print(f"[shop_tracker] {today} · {len(shops)} shops — "
          f"{etsy_from_sheet} Etsy từ Sheet, còn lại scrape ({mode})")

    for i, shop in enumerate(shops, 1):
        key = shop["name"]
        used_sheet = False

        if shop["platform"] == "etsy":
            # Etsy luôn đọc từ Sheet Data tab — không scrape live (ScrapingBee
            # stealth tốn 75 credit/call, không đáng cho dữ liệu daily).
            if key in etsy_sales:
                snap_data = etsy_sales[key]
                total = snap_data["total_sales"]
                err = snap_data["error"] if snap_data["error"] else (
                    None if total is not None else "missing total in sheet")
            else:
                total, err = None, "missing from sheet Data tab"
            used_sheet = True
        else:
            total, err = scrape_shop(shop["platform"], shop["url"])

        entry = shop_map.setdefault(key, {
            "platform": shop["platform"],
            "url": shop["url"],
            "snapshots": [],
        })
        # Keep url/platform fresh if user edited the shop list
        entry["platform"] = shop["platform"]
        entry["url"] = shop["url"]

        snaps = entry["snapshots"]
        prev = snaps[-1] if snaps else None

        if err:
            snap = Snapshot(date=today,
                            total_sales=total if total is not None
                                        else (prev["total_sales"] if prev else 0),
                            delta=None, error=err)
            src = "sheet" if used_sheet else "scrape"
            print(f"  [{i:2}/{len(shops)}] {shop['platform']:5} {key:25} ⚠ {err} [{src}]")
        else:
            delta = None
            if prev and prev.get("total_sales") is not None and not prev.get("error"):
                delta = total - prev["total_sales"]
            snap = Snapshot(date=today, total_sales=total, delta=delta)
            marker = "✓" if delta is None else f"Δ{delta:+d}"
            src = "sheet" if used_sheet else "scrape"
            print(f"  [{i:2}/{len(shops)}] {shop['platform']:5} {key:25} "
                  f"{marker} (total={total}) [{src}]")

        # If we already wrote a snapshot for `today`, overwrite (idempotent same-day runs)
        if snaps and snaps[-1]["date"] == today:
            snaps[-1] = asdict(snap)
        else:
            snaps.append(asdict(snap))

        # Polite delay only when we actually hit the network
        if i < len(shops) and not used_sheet:
            time.sleep(random.uniform(*delay_range))

    history["last_updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_history(history)
    return history


if __name__ == "__main__":
    # Allow smoke-test with `python shop_tracker.py --test` (shorter delay)
    delays = (0.5, 1.5) if "--test" in sys.argv else (3.0, 6.0)
    run(delay_range=delays)
