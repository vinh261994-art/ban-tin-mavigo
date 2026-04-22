"""Load shop list from a public Google Sheet (CSV export).

Expected columns (case-insensitive, any order):
    platform   etsy | ebay
    name       display name
    url        public shop URL
    active     TRUE/FALSE/1/0/YES/NO  (optional — default TRUE)

Sheet must be shared "Anyone with the link → Viewer". We fetch the
`/export?format=csv` endpoint, no auth needed.
"""
from __future__ import annotations

import csv
import re
import sys
from io import StringIO

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx

ALLOWED_PLATFORMS = {"etsy", "ebay"}
TRUTHY = {"TRUE", "1", "YES", "Y", "T", "X", "✓"}


def _to_csv_url(url: str) -> str:
    """Convert any Google Sheets URL into its CSV export form.

    Accepts:
        - /spreadsheets/d/<ID>/edit#gid=<GID>
        - /spreadsheets/d/<ID>/edit?usp=sharing
        - /spreadsheets/d/<ID>/export?format=csv&gid=<GID>   (passthrough)
    """
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    if not m:
        raise ValueError(f"Không phải URL Google Sheets hợp lệ: {url}")
    sheet_id = m.group(1)
    gid_m = re.search(r"[?#&]gid=(\d+)", url)
    base = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
    # Only pin gid if the user explicitly pointed at a tab. Without gid,
    # Google exports the first tab — works regardless of its internal ID.
    return f"{base}&gid={gid_m.group(1)}" if gid_m else base


def _is_active(val: str) -> bool:
    v = (val or "").strip().upper()
    if not v:
        return True   # default active when column omitted
    return v in TRUTHY


def load_shops(sheet_url: str, timeout: float = 20.0) -> list[dict]:
    """Fetch the Sheet and parse rows into shop dicts.

    Returns list of {"platform", "name", "url"} for active rows only.
    Raises on network/auth/parse errors — caller decides whether to fallback.
    """
    csv_url = _to_csv_url(sheet_url)
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        r = client.get(csv_url)
        r.raise_for_status()
    # Google returns HTML (sign-in page) if Sheet isn't public — detect that
    if "<html" in r.text.lower()[:500] or "signin" in r.url.path.lower():
        raise RuntimeError(
            "Sheet không public — cần chia sẻ 'Anyone with the link → Viewer'")

    text = r.text.lstrip("﻿")   # strip BOM
    reader = csv.DictReader(StringIO(text))

    shops = []
    skipped = 0
    for row in reader:
        norm = {(k or "").strip().lower(): (v or "").strip()
                for k, v in row.items() if k}
        platform = norm.get("platform", "").lower()
        name = norm.get("name", "")
        url = norm.get("url", "")
        if not (platform and url):
            skipped += 1
            continue
        if platform not in ALLOWED_PLATFORMS:
            print(f"[sheet_loader] bỏ qua row platform lạ: {platform!r}")
            skipped += 1
            continue
        if not _is_active(norm.get("active", "")):
            skipped += 1
            continue
        shops.append({
            "platform": platform,
            "name": name or url.rsplit("/", 1)[-1],
            "url": url,
        })
    print(f"[sheet_loader] loaded {len(shops)} shop (bỏ {skipped} row trống/tắt)")
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
