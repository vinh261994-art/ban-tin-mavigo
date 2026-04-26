"""Load shop list & Etsy sales data from a public Google Sheet.

Sheet layout (Sheet B, "Apps Script + bulletin"):
    Config_Shops  — Etsy shops  (cols: Shop | URL | Active?)
    shops_ebay    — eBay shops  (cols: Shop | URL | Active?)
    Data          — Etsy sales history written by Apps Script
                    (cols: Shop | Date | Sales_Total | Sales_Daily | ... |
                           Fetch_Status | ...)
    keywords      — tracked keywords (cols: keyword | active?)

Sheet must be shared "Anyone with the link → Viewer". We fetch gviz
CSV endpoints, no auth needed.
"""
from __future__ import annotations

import csv
import re
import sys
from io import StringIO

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx

TRUTHY = {"TRUE", "1", "YES", "Y", "T", "X", "✓"}


def _is_active(val: str) -> bool:
    v = (val or "").strip().upper()
    if not v:
        return True   # default active when column omitted
    return v in TRUTHY


def _fetch_tab_csv(sheet_url: str, tab_name: str, timeout: float = 20.0) -> str:
    """Fetch a tab by name via gviz CSV export.

    Returns CSV text with BOM stripped. Raises RuntimeError if sheet isn't
    public or tab doesn't exist (gviz falls back to first tab silently —
    caller should validate expected columns).
    """
    gviz_url = _to_gviz_url(sheet_url, tab_name)
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        r = client.get(gviz_url)
        r.raise_for_status()
    text = r.text.lstrip("﻿")
    if "<html" in text.lower()[:500]:
        raise RuntimeError(
            f"Không đọc được tab '{tab_name}' — Sheet chưa public hoặc tab chưa tạo")
    return text


def _parse_shop_tab(csv_text: str, platform: str) -> tuple[list[dict], int]:
    """Parse a shop list tab. Expected columns: Shop | URL | Active? (optional).

    Returns (shops, skipped_count).
    """
    reader = csv.DictReader(StringIO(csv_text))
    shops: list[dict] = []
    skipped = 0
    for row in reader:
        norm = {(k or "").strip().lower(): (v or "").strip()
                for k, v in row.items() if k}
        # Accept "shop" or "name" as the display-name column
        name = norm.get("shop") or norm.get("name") or ""
        url = norm.get("url", "")
        if not url:
            skipped += 1
            continue
        if not _is_active(norm.get("active", "") or norm.get("active?", "")):
            skipped += 1
            continue
        shops.append({
            "platform": platform,
            "name": name or url.rsplit("/", 1)[-1],
            "url": url,
        })
    return shops, skipped


def load_shops(sheet_url: str, timeout: float = 20.0) -> list[dict]:
    """Fetch Etsy (Config_Shops) + eBay (shops_ebay) tabs and merge into one list.

    Returns list of {"platform", "name", "url"} for active rows only.
    Raises on network/auth errors. If shops_ebay is missing, logs and continues
    with Etsy-only (caller fallback still applies).
    """
    etsy_csv = _fetch_tab_csv(sheet_url, "Config_Shops", timeout)
    etsy_shops, etsy_skip = _parse_shop_tab(etsy_csv, "etsy")

    ebay_shops: list[dict] = []
    ebay_skip = 0
    try:
        ebay_csv = _fetch_tab_csv(sheet_url, "shops_ebay", timeout)
        ebay_shops, ebay_skip = _parse_shop_tab(ebay_csv, "ebay")
    except Exception as e:
        print(f"[sheet_loader] tab 'shops_ebay' không đọc được ({type(e).__name__}: {e}) "
              f"— bỏ qua eBay")

    shops = etsy_shops + ebay_shops
    print(f"[sheet_loader] loaded {len(etsy_shops)} etsy + {len(ebay_shops)} ebay "
          f"(bỏ {etsy_skip + ebay_skip} row trống/tắt)")
    return shops


def _to_gviz_url(url: str, sheet_name: str) -> str:
    """Address a tab by name via gviz — works without knowing the tab's gid."""
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    if not m:
        raise ValueError(f"Không phải URL Google Sheets hợp lệ: {url}")
    sheet_id = m.group(1)
    from urllib.parse import quote
    return (f"https://docs.google.com/spreadsheets/d/{sheet_id}"
            f"/gviz/tq?tqx=out:csv&sheet={quote(sheet_name)}")


def _parse_int(v: str) -> int | None:
    s = (v or "").strip().replace(",", "")
    if not s:
        return None
    try:
        return int(float(s))   # tolerate "12.0" that Sheets sometimes emits
    except ValueError:
        return None


def load_sales(sheet_url: str, timeout: float = 20.0) -> dict[str, dict]:
    """Read the `Data` tab written by Apps Script → latest snapshot per shop.

    Returns one entry per shop name regardless of platform — Apps Script writes
    Etsy and eBay rows to the same tab using the same schema:
        Shop | Date | Sales_Total | Sales_Daily | ... | Fetch_Status | ...

    Returns dict keyed by stripped shop name:
        {"date": "YYYY-MM-DD", "total_sales": int | None,
         "delta": int | None, "error": str | None}

    Per shop we keep the row with the latest date, and within same date the last
    row encountered (Apps Script appends chronologically, so that's the freshest).
    Fetch_Status != "OK 200" becomes error; total/delta still filled when parseable.
    """
    text = _fetch_tab_csv(sheet_url, "Data", timeout)
    reader = csv.DictReader(StringIO(text))

    latest: dict[str, dict] = {}
    for row in reader:
        norm = {(k or "").strip().lower(): (v or "").strip()
                for k, v in row.items() if k}
        shop = norm.get("shop", "")
        date = norm.get("date", "")
        if not shop or not date:
            continue

        prev = latest.get(shop)
        # Keep the row with max date; on tie, later row wins (Apps Script appends in order)
        if prev and prev["date"] > date:
            continue

        status = norm.get("fetch_status", "")
        err = None if status.upper().startswith("OK") else (status or "unknown status")
        latest[shop] = {
            "date": date,
            "total_sales": _parse_int(norm.get("sales_total", "")),
            "delta": _parse_int(norm.get("sales_daily", "")),
            "error": err,
        }

    print(f"[sheet_loader] loaded sales for {len(latest)} shop từ tab Data")
    return latest


# Backward-compat alias — older callers still import load_etsy_sales
load_etsy_sales = load_sales


def load_keywords(sheet_url: str, timeout: float = 20.0) -> list[str]:
    """Fetch the `keywords` tab. Expected columns: keyword | active (optional).

    Returns list of active keyword strings. Raises when tab doesn't exist or
    has wrong schema — caller decides fallback to YAML.
    """
    gviz_url = _to_gviz_url(sheet_url, "keywords")
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        r = client.get(gviz_url)
        r.raise_for_status()
    text = r.text.lstrip("﻿")
    if "<html" in text.lower()[:500]:
        raise RuntimeError(
            "Không đọc được tab 'keywords' — Sheet chưa public hoặc tab chưa tạo")

    reader = csv.DictReader(StringIO(text))
    headers = {(h or "").strip().lower() for h in (reader.fieldnames or [])}
    # Accept both 'keyword' and 'keywords' (user often types plural by instinct)
    kw_col = "keyword" if "keyword" in headers else ("keywords" if "keywords" in headers else None)
    # gviz silently falls back to the first tab when target missing — guard
    if not kw_col:
        raise RuntimeError(
            "Tab 'keywords' chưa tồn tại hoặc sai schema (cần cột 'keyword' hoặc 'keywords')")

    out: list[str] = []
    skipped = 0
    for row in reader:
        norm = {(k or "").strip().lower(): (v or "").strip()
                for k, v in row.items() if k}
        kw = norm.get(kw_col, "")
        if not kw:
            skipped += 1
            continue
        if not _is_active(norm.get("active", "")):
            skipped += 1
            continue
        out.append(kw)
    print(f"[sheet_loader] loaded {len(out)} keyword (bỏ {skipped} row trống/tắt)")
    return out


if __name__ == "__main__":
    # Quick CLI test: python sheet_loader.py <url>
    import os
    url = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SHOPS_SHEET_URL", "")
    if not url:
        print("Usage: python sheet_loader.py <sheet_url>")
        sys.exit(2)
    for s in load_shops(url):
        print(f"  {s['platform']:5} {s['name']:25} {s['url']}")
    print("\n--- keywords ---")
    try:
        for kw in load_keywords(url):
            print(f"  {kw}")
    except Exception as e:
        print(f"  (load_keywords: {type(e).__name__}: {e})")
    print("\n--- sales (from Data tab) ---")
    try:
        for shop, snap in load_sales(url).items():
            print(f"  {shop:25} {snap['date']} total={snap['total_sales']} "
                  f"delta={snap['delta']} err={snap['error']}")
    except Exception as e:
        print(f"  (load_sales: {type(e).__name__}: {e})")
