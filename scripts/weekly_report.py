"""Weekly deep report — Monday 9am VN.

Produces a multi-part Telegram delivery:
  1. Text bulletin with 6 sections:
       - MACRO (market_snapshot)
       - HOT TODAY (find_hot_listings — summary only)
       - TRENDING ∩ GEMS (intersection)
       - CLUSTERS (token-matched themes)
       - SEASONAL PICKS (explore_niche per upcoming event — summary)
       - WEEKLY SHOP SALES + KEYWORDS TRACKED + GEMINI NARRATIVE
  2. Media group of top-3 hot listings.
  3. Media group per upcoming event (≤ 60d, status on_time/late) showing top-3 listings.

Honors DRY_RUN=1 to skip Telegram + Gemini API calls.
"""
from __future__ import annotations

import html
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import holiday_advisor
import keyword_tracker
import run_lock
import telegram_sender
import ytrends_analytics as yta
from gemini_client import GeminiError, generate as gemini_generate

ROOT = Path(__file__).resolve().parent.parent
HISTORY_FILE = ROOT / "data" / "sales_history.json"

LOOKAHEAD_DAYS = 60
MAX_SEASONAL_EVENTS = 3   # how many upcoming events get a deep dive


# ==========================================================
#  Sarcastic line pools (weekly-specific)
# ==========================================================

WEEK_ZERO_LINES = [
    "Cả tuần tròn zero — shop đã thành nghĩa địa rồi à?",
    "7 ngày liền không một đơn, chắc ai cũng tưởng shop đã đóng cửa.",
    "Tuần qua im lìm như ban đêm ở nghĩa trang — bán cho ai?",
]

WEEK_LOW_LINES = [
    "Cả tuần {n} đơn — chia ra 1 ngày 1 đơn chưa đủ, thôi chuyển nghề giao hàng đi.",
    "{n} đơn/7 ngày — số đó đừng nói ra, hàng xóm cười cho.",
]

WEEK_OK_LINES = [
    "{n} đơn tuần này — tạm ổn, nhưng đừng ngồi đó vuốt mèo, đối thủ không đợi.",
    "Được {n} đơn — chưa giàu, nhưng ít nhất chưa phải bán shop trả nợ.",
]

WEEK_GREAT_LINES = [
    "{n} đơn tuần — khá đấy! Nhập thêm inventory kẻo tuần sau cháy hàng.",
    "Bùng nổ {n} đơn — giữ nhịp đi, đừng tuần sau về mo.",
]


def _pick(pool: list[str], **kwargs) -> str:
    return random.choice(pool).format(**kwargs)


# ==========================================================
#  Data aggregation
# ==========================================================

def _load_history() -> dict:
    if not HISTORY_FILE.exists():
        return {"shops": {}}
    return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))


def _weekly_shop_totals(history: dict, days: int = 7) -> list[dict]:
    today = datetime.now(timezone.utc).date()
    cutoff = today - timedelta(days=days)
    out = []
    for name, entry in (history.get("shops") or {}).items():
        snaps = entry.get("snapshots") or []
        total_week = 0
        days_with_data = 0
        errors = 0
        latest_total = None
        for s in snaps:
            try:
                d = datetime.strptime(s["date"], "%Y-%m-%d").date()
            except (KeyError, ValueError):
                continue
            if d < cutoff:
                continue
            if s.get("error"):
                errors += 1
                continue
            delta = s.get("delta")
            if delta is None:
                continue
            total_week += delta
            days_with_data += 1
            latest_total = s.get("total_sales", latest_total)
        out.append({
            "name": name,
            "platform": entry.get("platform", "?"),
            "week_sales": total_week,
            "days_with_data": days_with_data,
            "errors": errors,
            "total": latest_total,
        })
    out.sort(key=lambda r: r["week_sales"], reverse=True)
    return out


# ==========================================================
#  Format helpers
# ==========================================================

def _esc(s: object) -> str:
    """HTML-escape untrusted external text for Telegram HTML mode. Unescapes
    first so we don't double-encode API text that's already HTML-escaped."""
    raw = html.unescape(str(s))
    return (raw.replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _esc_trim(s: object, n: int) -> str:
    """Unescape → trim to N chars (clean text) → re-escape. Avoids cutting
    mid-entity like `&#39;` which would get mangled to `&amp;#`."""
    raw = html.unescape(str(s or ""))[:n]
    return (raw.replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _fmt_money(x) -> str:
    try:
        return f"${float(x):,.0f}"
    except (TypeError, ValueError):
        return "?"


def _fmt_pct(x) -> str:
    try:
        return f"{float(x)*100:.1f}%"
    except (TypeError, ValueError):
        return "?"


# ==========================================================
#  Section formatters
# ==========================================================

def _format_header(today: datetime) -> str:
    start = (today - timedelta(days=6)).strftime("%d/%m")
    end = today.strftime("%d/%m/%Y")
    return f"📊 <b>BẢN TIN MAVIGO — TUẦN</b> ({start} → {end})"


def _format_macro(snap: dict) -> str:
    if not snap:
        return "🌐 <b>BỐI CẢNH THỊ TRƯỜNG</b>\n  YTrends không trả về — bỏ qua."
    lines = ["🌐 <b>BỐI CẢNH THỊ TRƯỜNG (US)</b>"]
    if snap.get("total_listings"):
        lines.append(f"▸ {snap['total_listings']:,} listings · "
                     f"{snap.get('total_sellers', 0):,} sellers")
    if snap.get("avg_price"):
        lines.append(f"▸ Giá TB: {_fmt_money(snap['avg_price'])} · "
                     f"median {_fmt_money(snap.get('median_price'))} · "
                     f"sweet spot ${snap.get('price_p25','?')}-${snap.get('price_p75','?')}")
    if snap.get("avg_conversion_rate") is not None:
        lines.append(f"▸ Conversion TB: {_fmt_pct(snap['avg_conversion_rate'])} · "
                     f"{snap.get('pct_new_sellers', 0)}% seller là 'new'")
    if snap.get("recommended_action"):
        lines.append(f"  💬 {_esc(snap['recommended_action'])}")
    return "\n".join(lines)


def _format_hot_summary(hot: list[dict]) -> str:
    if not hot:
        return "🔥 <b>HOT NGAY HÔM NAY</b>\n  YTrends im — chắc cả sàn đang chill."
    lines = [f"🔥 <b>HOT NGAY HÔM NAY</b> — top {min(5, len(hot))} listing outperform peer"]
    for i, h in enumerate(hot[:5], 1):
        title = _esc_trim(h.get("title"), 60)
        price = _fmt_money(h.get("price_usd") or h.get("price"))
        conv_x = h.get("conversion_multiplier")
        sales_x = h.get("sales_multiplier")
        country = h.get("shop_country") or "?"
        why = _esc_trim(h.get("why_hot_detail"), 120)
        bits = [f"{price}", f"{country}"]
        if conv_x:
            try:
                bits.append(f"conv {float(conv_x):.1f}x")
            except (TypeError, ValueError):
                pass
        if sales_x:
            try:
                bits.append(f"sold {float(sales_x):.0f}x")
            except (TypeError, ValueError):
                pass
        lines.append(f"{i}. <b>{title}…</b>")
        lines.append(f"   · {' · '.join(bits)}")
        if why:
            lines.append(f"   · {why}")
    lines.append("  📷 <i>xem ảnh album bên dưới</i>")
    return "\n".join(lines)


def _format_intersection(inter: list[dict]) -> str:
    if not inter:
        return ("🎯 <b>TRENDING ∩ HIDDEN GEMS</b>\n"
                "  Tuần này trending và gems không giao nhau — không có signal đặc biệt.")
    lines = [f"🎯 <b>TRENDING ∩ HIDDEN GEMS</b> — {len(inter)} keyword signal mạnh (vừa nổi vừa ít đối thủ)"]
    for it in inter[:5]:
        tag = _esc(it.get("tag", "?"))
        m = it.get("momentum_score")
        g = it.get("gem_score")
        comp = it.get("competition_level", "?")
        price = _fmt_money(it.get("avg_price"))
        conv = _fmt_pct(it.get("avg_conversion_rate"))
        try:
            score_str = f"mom {float(m):.0f}/gem {float(g):.0f}"
        except (TypeError, ValueError):
            score_str = f"mom {m}/gem {g}"
        lines.append(f"▸ <b>{tag}</b> · {score_str} · {comp} · {price} · conv {conv}")
    return "\n".join(lines)


def _format_clusters(clusters: list[dict]) -> str:
    if not clusters:
        return ""
    # Keep only clusters size >= 3 to avoid noise
    big = [c for c in clusters if c["size"] >= 3][:4]
    if not big:
        return ""
    lines = ["🧩 <b>NHÓM THEME</b> — niche đang tập trung"]
    for c in big:
        tags = ", ".join(_esc(it.get("tag", "?")) for it in c["items"][:6])
        lines.append(f"▸ <b>#{_esc(c['theme'])}</b> ({c['size']} kw): {tags}")
    return "\n".join(lines)


def _format_seasonal_summary(picks: list[dict]) -> str:
    if not picks:
        return ("📅 <b>HOT THEO DỊP LỄ</b>\n"
                "  Không có dịp lễ nào trong lead time — xả hơi.")
    lines = ["📅 <b>HOT THEO DỊP LỄ SẮP TỚI</b>"]
    for p in picks:
        ev = p["event"]
        seed = p["seed"]
        niche = p["niche"]
        ov = niche.get("overview") or {}
        top = niche.get("top_listings") or []
        adj = niche.get("adjacent_tags") or []

        status_emoji = {"late": "🟠", "on_time": "🟢", "upcoming": "🔵"}.get(ev.status, "•")
        lines.append("")
        lines.append(f"{status_emoji} <b>{_esc(ev.name_vi)}</b> — còn {ev.days_until} ngày "
                     f"(seed: <code>{_esc(seed)}</code>)")
        if ov.get("listings") and ov.get("avg_price_usd"):
            lines.append(f"   📊 {ov.get('listings', 0):,} listings · "
                         f"giá TB {_fmt_money(ov.get('avg_price_usd'))} · "
                         f"conv {_fmt_pct(ov.get('avg_conversion_rate'))}")
        # Top 3 listings brief
        for i, t in enumerate(top[:3], 1):
            title = _esc_trim(t.get("title"), 50)
            price = _fmt_money(t.get("price_usd"))
            conv = _fmt_pct(t.get("conversion_rate"))
            verdict = t.get("listing_verdict") or ""
            lines.append(f"   {i}. <b>{title}…</b> · {price} · conv {conv} · {_esc(verdict)}")
        # Top adjacent tags (MUST_USE)
        must = [a for a in adj if "MUST" in (a.get("action_reason") or "").upper()][:3]
        if must:
            tag_names = ", ".join(_esc(a.get("tag", "?")) for a in must)
            lines.append(f"   💡 Tag phải dùng: {tag_names}")
        lines.append(f"   📷 <i>xem album ảnh bên dưới</i>")
    return "\n".join(lines)


def _format_shop_week(totals: list[dict]) -> str:
    if not totals:
        return ("💰 <b>SALE TUẦN QUA</b>\n"
                "  Chưa có shop nào trong history.")
    week_total = sum(t["week_sales"] for t in totals)
    lines = [f"💰 <b>SALE TUẦN QUA</b> ({len(totals)} shop)"]
    lines.append(f"▸ Tổng 7 ngày: <b>{week_total}</b> đơn")
    if week_total == 0:
        snark = _pick(WEEK_ZERO_LINES)
    elif week_total <= 7:
        snark = _pick(WEEK_LOW_LINES, n=week_total)
    elif week_total <= 35:
        snark = _pick(WEEK_OK_LINES, n=week_total)
    else:
        snark = _pick(WEEK_GREAT_LINES, n=week_total)
    lines.append(f"  💬 {snark}")

    top = [t for t in totals if t["week_sales"] > 0][:5]
    if top:
        lines.append("▸ Top shop tuần:")
        for t in top:
            lines.append(f"    • <b>{_esc(t['name'])}</b> ({t['platform']})  "
                         f"<b>+{t['week_sales']}</b> đơn")
    dead = [t for t in totals if t["week_sales"] == 0 and t["days_with_data"] >= 3]
    if dead:
        names = ", ".join(_esc(t["name"]) for t in dead[:5])
        lines.append(f"▸ ☠️ Bán 0 đơn: {names}")
    broken = [t for t in totals if t["days_with_data"] == 0 and t["errors"] > 0]
    if broken:
        names = ", ".join(_esc(t["name"]) for t in broken[:5])
        lines.append(f"▸ ❌ Scrape fail: {names}")
    return "\n".join(lines)


def _format_keywords_tracked(reports: list) -> str:
    if not reports:
        return ""
    buckets: dict[str, list] = {}
    for r in reports:
        buckets.setdefault(r.bucket, []).append(r)
    lines = [f"🔍 <b>KEYWORD THEO DÕI</b> ({len(reports)} kw)"]
    for name, emoji in [("spike", "🚀"), ("opportunity", "✨"),
                        ("dying", "💀"), ("crowded", "🏟️"),
                        ("stable", "😐"), ("no_data", "❓"), ("error", "❌")]:
        items = buckets.get(name, [])
        if not items:
            continue
        kws = ", ".join(_esc(r.keyword) for r in items[:8])
        lines.append(f"▸ {emoji} {name} ({len(items)}): {kws}")
    return "\n".join(lines)


# ==========================================================
#  Gemini narrative (with rich context)
# ==========================================================

def _build_gemini_prompt(snap: dict, hot: list[dict], inter: list[dict],
                        picks: list[dict], totals: list[dict],
                        kw_reports: list) -> str:
    week_total = sum(t["week_sales"] for t in totals)
    top_shop = next((t for t in totals if t["week_sales"] > 0), None)
    dead_shops = [t for t in totals if t["week_sales"] == 0 and t["days_with_data"] >= 3]

    # Extract 3 hottest product titles for narrative grounding
    hot_titles = []
    for h in hot[:3]:
        t = (h.get("title") or "")[:60]
        p = h.get("price_usd") or h.get("price")
        if t and p:
            hot_titles.append(f"{t} (${p})")

    inter_tags = [it.get("tag") for it in inter[:3]]

    lines = [
        f"MACRO US: {snap.get('total_listings', '?')} listings, "
        f"giá TB ${snap.get('avg_price', '?')}, conv TB {_fmt_pct(snap.get('avg_conversion_rate'))}",
        f"TOP HOT LISTING: {' | '.join(hot_titles) or 'không có'}",
        f"SIGNAL MẠNH (trending ∩ gems): {', '.join(inter_tags) or 'không'}",
        f"DỊP LỄ TỚI: " + (
            ", ".join(f"{p['event'].name_vi} ({p['event'].days_until}d, seed '{p['seed']}')"
                     for p in picks) or "không có"),
        f"SHOP TUẦN: {week_total} đơn tổng, top {top_shop['name'] if top_shop else 'không có'}, "
        f"dead {len(dead_shops)} shop",
    ]

    spike_kws = [r.keyword for r in kw_reports if r.bucket == "spike"]
    opp_kws = [r.keyword for r in kw_reports if r.bucket == "opportunity"]
    if spike_kws or opp_kws:
        lines.append(f"KEYWORD SPIKE/OPP: {', '.join(spike_kws + opp_kws)}")

    lines.append("")
    lines.append(
        "Viết 2 đoạn văn xuôi ngắn (tổng 5-7 câu) tổng kết tuần này cho seller "
        "Etsy/eBay Việt Nam. Giọng đanh đá, mỉa mai có mục đích — chê đúng chỗ "
        "đáng chê, chỉ rõ hướng đi đáng bắt. Đoạn 1: tình hình (macro + shop). "
        "Đoạn 2: gợi ý cụ thể tuần tới dựa trên signal + dịp lễ. "
        "Không dùng bullet, không markdown, không lặp lại số thô."
    )
    return "\n".join(lines)


def _generate_narrative(snap, hot, inter, picks, totals, kw_reports) -> str:
    prompt = _build_gemini_prompt(snap, hot, inter, picks, totals, kw_reports)
    try:
        text = gemini_generate(prompt, max_tokens=600, temperature=0.95)
    except GeminiError as e:
        print(f"[weekly_report] Gemini failed ({e}) — using fallback")
        text = ("Tuần qua shop thì đứng im, keyword thì trôi theo dòng nước — "
                "không ai chủ động vớt. Signal tuần này rõ ràng ở mấy niche "
                "personalized + seasonal, vấn đề là bạn có đủ nhanh để list không. "
                "Tuần tới làm ơn đừng ngồi chờ phép màu — vào optimize listing "
                "và bắn ad cho keyword đang spike, kẻo đối thủ ăn hết.")
    return f"🧠 <b>TỔNG KẾT TUẦN</b>\n{text}"


# ==========================================================
#  Media group builders
# ==========================================================

def _hot_media_group(hot: list[dict]) -> list[dict]:
    """Build media group for top hot listings with thumbnails."""
    items = []
    for h in hot[:5]:
        url = h.get("image_url")
        if not url:
            continue
        title = _esc((h.get("title") or "")[:80])
        price = _fmt_money(h.get("price_usd") or h.get("price"))
        conv_x = h.get("conversion_multiplier")
        country = h.get("shop_country") or "?"
        why = _esc((h.get("why_hot_detail") or "")[:150])
        listing_id = h.get("listing_id")
        link = f"https://www.etsy.com/listing/{listing_id}" if listing_id else ""

        try:
            conv_str = f"{float(conv_x):.1f}x conv"
        except (TypeError, ValueError):
            conv_str = ""

        caption_lines = [
            f"🔥 <b>{title}…</b>",
            f"{price} · {country} · {conv_str}".strip(),
        ]
        if why:
            caption_lines.append(f"💡 {why}")
        if link:
            caption_lines.append(f'<a href="{link}">Xem trên Etsy</a>')
        caption = "\n".join(caption_lines)
        items.append({"photo": url, "caption": caption})
    return items


def _seasonal_media_group(pick: dict) -> list[dict]:
    """Build media group for one seasonal event's top listings."""
    ev = pick["event"]
    niche = pick["niche"]
    top = niche.get("top_listings") or []
    items = []
    for t in top[:4]:
        url = t.get("image_url")
        if not url:
            continue
        title = _esc((t.get("title") or "")[:80])
        price = _fmt_money(t.get("price_usd"))
        conv = _fmt_pct(t.get("conversion_rate"))
        verdict = _esc(t.get("listing_verdict") or "")
        insights = _esc((t.get("competitive_insights") or "")[:200])
        listing_id = t.get("listing_id")
        link = f"https://www.etsy.com/listing/{listing_id}" if listing_id else ""

        caption_lines = [
            f"🎯 <b>{_esc(ev.name_vi)}</b> — top pick",
            f"<b>{title}…</b>",
            f"{price} · conv {conv} · {verdict}",
        ]
        if insights:
            caption_lines.append(f"💡 {insights}")
        if link:
            caption_lines.append(f'<a href="{link}">Xem trên Etsy</a>')
        caption = "\n".join(caption_lines)
        items.append({"photo": url, "caption": caption})
    return items


# ==========================================================
#  Main build + send
# ==========================================================

def build_and_send() -> None:
    now = datetime.now(timezone.utc)

    history = _load_history()
    totals = _weekly_shop_totals(history, days=7)
    kw_reports = keyword_tracker.run()
    events = holiday_advisor.upcoming(lookahead_days=LOOKAHEAD_DAYS)

    print("[weekly_report] fetching market_snapshot")
    snap = yta.get_market_snapshot(country="US")

    print("[weekly_report] fetching find_hot_listings")
    hot = yta.get_hot_listings(limit=8)

    print("[weekly_report] fetching trending + gems")
    trending = yta.get_trending(limit=25)
    gems = yta.get_gems(limit=25)
    inter = yta.intersection(trending, gems)
    clusters = yta.cluster_by_token(trending + gems, key="tag", min_cluster=3)

    print(f"[weekly_report] seasonal picks (up to {MAX_SEASONAL_EVENTS} events)")
    picks = yta.seasonal_picks(events, max_events=MAX_SEASONAL_EVENTS)

    narrative = _generate_narrative(snap, hot, inter, picks, totals, kw_reports)

    # ── Main text message ──────────────────────────────────
    sections = [
        _format_header(now),
        _format_macro(snap),
        _format_hot_summary(hot),
        _format_intersection(inter),
        _format_clusters(clusters),
        _format_seasonal_summary(picks),
        _format_shop_week(totals),
        _format_keywords_tracked(kw_reports),
        narrative,
    ]
    main_text = "\n\n".join(s for s in sections if s)

    telegram_sender.send(main_text)

    # ── Media group: hot listings ─────────────────────────
    hot_album = _hot_media_group(hot)
    if hot_album:
        print(f"[weekly_report] sending hot-listings album ({len(hot_album)} photos)")
        telegram_sender.send_media_group(hot_album)

    # ── One media group per seasonal pick ─────────────────
    for p in picks:
        album = _seasonal_media_group(p)
        if album:
            print(f"[weekly_report] sending seasonal album for {p['event'].name} "
                  f"({len(album)} photos)")
            telegram_sender.send_media_group(album)


def main() -> None:
    if run_lock.already_sent("weekly"):
        print("[weekly_report] already sent this ISO week — skipping (use FORCE_SEND=1 to override)")
        return
    build_and_send()
    run_lock.mark_sent("weekly")


if __name__ == "__main__":
    main()
