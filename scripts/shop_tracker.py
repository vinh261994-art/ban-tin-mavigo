"""Track daily sales deltas — Etsy từ Sheet, eBay scrape direct.

Etsy: Apps Script trong Sheet ghi vào tab `Data` → Python đọc qua
      sheet_loader.load_sales(). Không HTTP call.

eBay: Apps Script bị eBay block (Security Measure cho IP Google), eBay
      Developer API pending. Tạm scrape direct từ GitHub Actions IP US:
      seller pages /usr/<ID> và /str/<ID> trả HTML có
      `<span class="str-text-span BOLD">N</span><!--F#@1--> items sold`.
      Polite 3-6s delay giữa shops để eBay không flag GH IP range.

Output: data/sales_history.json (1 snapshot / shop / UTC day).
"""
from __future__ import annotations

import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx
import yaml

import sheet_loader

ROOT = Path(__file__).resolve().parent.parent
SHOPS_FILE = ROOT / "config" / "shops.yml"
HISTORY_FILE = ROOT / "data" / "sales_history.json"

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

# eBay markup 4/2026: <span class="str-text-span BOLD">11</span><!--F#@1--> items sold
EBAY_PATTERN_PRIMARY = re.compile(
    r'BOLD">\s*([\d,]+)\s*</span>(?:<!--[^>]*-->|\s)*items sold',
    re.IGNORECASE,
)
EBAY_PATTERN_FALLBACK = re.compile(r'([\d,]+)\s*items sold', re.IGNORECASE)

# Trả "Security Measure" page → eBay block IP, không phải shop chết
EBAY_SECURITY_MARKERS = ("Security Measure | eBay", "Please verify yourself")
# Seller chưa từng bán → ghi 0 thay vì "parse failed"
EBAY_INACTIVE_MARKERS = ('"totalFeedback":0', '"totalFeedback":"0"', "No active listings")


@dataclass
class Snapshot:
    date: str
    total_sales: int
    delta: Optional[int] = None
    error: Optional[str] = None


def fetch_ebay_html(url: str, timeout: float = 25.0) -> str:
    headers = {**HEADERS_BASE, "User-Agent": random.choice(USER_AGENTS)}
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
        r = client.get(url)
        r.raise_for_status()
        return r.text


def parse_ebay_sales(html: str) -> Optional[int]:
    m = EBAY_PATTERN_PRIMARY.search(html)
    if m:
        return int(m.group(1).replace(",", ""))
    m = EBAY_PATTERN_FALLBACK.search(html)
    if m:
        return int(m.group(1).replace(",", ""))
    return None


def is_ebay_blocked(html: str) -> bool:
    return any(m in html for m in EBAY_SECURITY_MARKERS)


def is_ebay_inactive(html: str) -> bool:
    return any(m in html for m in EBAY_INACTIVE_MARKERS)


def scrape_ebay(url: str) -> tuple[Optional[int], Optional[str]]:
    """Returns (total_sales, error). One of them is None."""
    try:
        html = fetch_ebay_html(url)
    except httpx.HTTPStatusError as e:
        return None, f"HTTP {e.response.status_code}"
    except httpx.TransportError as e:
        return None, f"network: {type(e).__name__}"

    if is_ebay_blocked(html):
        return None, "blocked (Security Measure)"

    total = parse_ebay_sales(html)
    if total is not None:
        return total, None

    if is_ebay_inactive(html):
        return 0, "inactive (no sold data)"
    return None, "parse failed (selector may have changed)"


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
    sheet_url = (os.environ.get("SHOPS_SHEET_URL") or "").strip()
    if not sheet_url:
        return {}
    try:
        return sheet_loader.load_sales(sheet_url)
    except Exception as e:
        print(f"[shop_tracker] không đọc được tab Data ({type(e).__name__}: {e})")
        return {}


def run(delay_range: tuple[float, float] = (3.0, 6.0)) -> dict:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    shops = load_shops()
    history = load_history()
    shop_map = history.setdefault("shops", {})
    etsy_sales = _load_etsy_sales_from_sheet()

    n_etsy_sheet = sum(1 for s in shops if s["platform"] == "etsy" and s["name"] in etsy_sales)
    n_ebay = sum(1 for s in shops if s["platform"] == "ebay")
    print(f"[shop_tracker] {today} · {len(shops)} shops — "
          f"{n_etsy_sheet} Etsy từ Sheet, {n_ebay} eBay scrape direct")

    ebay_idx = 0  # đếm để biết khi nào cần delay (giữa eBay shops thôi)
    for i, shop in enumerate(shops, 1):
        key = shop["name"]
        scraped = False

        if shop["platform"] == "etsy":
            if key in etsy_sales:
                snap_data = etsy_sales[key]
                total = snap_data["total_sales"]
                err = snap_data["error"] if snap_data["error"] else (
                    None if total is not None else "missing total in sheet")
            else:
                total, err = None, "missing from sheet Data tab"
        else:  # ebay
            if ebay_idx > 0:
                time.sleep(random.uniform(*delay_range))
            total, err = scrape_ebay(shop["url"])
            ebay_idx += 1
            scraped = True

        entry = shop_map.setdefault(key, {
            "platform": shop["platform"],
            "url": shop["url"],
            "snapshots": [],
        })
        entry["platform"] = shop["platform"]
        entry["url"] = shop["url"]

        snaps = entry["snapshots"]
        prev = snaps[-1] if snaps else None

        if err:
            snap = Snapshot(date=today,
                            total_sales=total if total is not None
                                        else (prev["total_sales"] if prev else 0),
                            delta=None, error=err)
            src = "scrape" if scraped else "sheet"
            print(f"  [{i:2}/{len(shops)}] {shop['platform']:5} {key:25} ⚠ {err} [{src}]")
        else:
            delta = None
            if prev and prev.get("total_sales") is not None and not prev.get("error"):
                delta = total - prev["total_sales"]
            snap = Snapshot(date=today, total_sales=total, delta=delta)
            marker = "✓" if delta is None else f"Δ{delta:+d}"
            src = "scrape" if scraped else "sheet"
            print(f"  [{i:2}/{len(shops)}] {shop['platform']:5} {key:25} "
                  f"{marker} (total={total}) [{src}]")

        if snaps and snaps[-1]["date"] == today:
            snaps[-1] = asdict(snap)
        else:
            snaps.append(asdict(snap))

    history["last_updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_history(history)
    return history


if __name__ == "__main__":
    delays = (0.5, 1.5) if "--test" in sys.argv else (3.0, 6.0)
    run(delay_range=delays)
