"""Track daily sales deltas — read everything from the Sheet `Data` tab.

Etsy and eBay sales are fetched server-side by Apps Script bound to the Google
Sheet (see scripts/apps_script_ebay.gs for the eBay fetcher template). Apps
Script runs on Google US infrastructure, costs nothing, and isn't blocked by
Akamai. This module only reads the resulting `Data` tab and updates
data/sales_history.json — it never makes outbound HTTP calls to Etsy/eBay.

Output: data/sales_history.json (one snapshot per shop per UTC day).
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Windows cp1252 console can't print unicode; force UTF-8 when attached to a terminal
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import yaml

import sheet_loader

ROOT = Path(__file__).resolve().parent.parent
SHOPS_FILE = ROOT / "config" / "shops.yml"
HISTORY_FILE = ROOT / "data" / "sales_history.json"


@dataclass
class Snapshot:
    date: str          # ISO date YYYY-MM-DD (UTC)
    total_sales: int   # cumulative lifetime sales reported by Apps Script
    delta: Optional[int] = None   # vs previous snapshot; None on first run or err
    error: Optional[str] = None   # set if Apps Script reported a non-OK status


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


def _load_sales_from_sheet() -> dict[str, dict]:
    """Pull all sales (Etsy + eBay) from Apps Script's Data tab.

    Returns empty dict if SHOPS_SHEET_URL unset or read fails — every shop will
    then be marked "missing from sheet Data tab" so the bulletin still renders.
    """
    sheet_url = (os.environ.get("SHOPS_SHEET_URL") or "").strip()
    if not sheet_url:
        print("[shop_tracker] SHOPS_SHEET_URL không set — không có data nào để đọc")
        return {}
    try:
        return sheet_loader.load_sales(sheet_url)
    except Exception as e:
        print(f"[shop_tracker] không đọc được tab Data ({type(e).__name__}: {e})")
        return {}


def run() -> dict:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    shops = load_shops()
    history = load_history()
    shop_map = history.setdefault("shops", {})
    sales = _load_sales_from_sheet()

    n_in_sheet = sum(1 for s in shops if s["name"] in sales)
    print(f"[shop_tracker] {today} · {len(shops)} shops — "
          f"{n_in_sheet} có data trong Sheet, {len(shops) - n_in_sheet} thiếu")

    for i, shop in enumerate(shops, 1):
        key = shop["name"]
        if key in sales:
            snap_data = sales[key]
            total = snap_data["total_sales"]
            err = snap_data["error"] if snap_data["error"] else (
                None if total is not None else "missing total in sheet")
        else:
            total, err = None, "missing from sheet Data tab"

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
            print(f"  [{i:2}/{len(shops)}] {shop['platform']:5} {key:25} ⚠ {err}")
        else:
            delta = None
            if prev and prev.get("total_sales") is not None and not prev.get("error"):
                delta = total - prev["total_sales"]
            snap = Snapshot(date=today, total_sales=total, delta=delta)
            marker = "✓" if delta is None else f"Δ{delta:+d}"
            print(f"  [{i:2}/{len(shops)}] {shop['platform']:5} {key:25} "
                  f"{marker} (total={total})")

        # If we already wrote a snapshot for `today`, overwrite (idempotent same-day runs)
        if snaps and snaps[-1]["date"] == today:
            snaps[-1] = asdict(snap)
        else:
            snaps.append(asdict(snap))

    history["last_updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_history(history)
    return history


if __name__ == "__main__":
    run()
