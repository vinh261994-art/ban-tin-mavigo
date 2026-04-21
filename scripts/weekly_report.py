"""Weekly deep report — Monday 9am VN.

Combines:
  1. 7-day per-shop sales totals from data/sales_history.json
  2. Tracked-keyword state (keyword_tracker run, bucket distribution)
  3. YTrends find_trending_keywords — discover rising niches
  4. YTrends find_hidden_gems — low-competition opportunities
  5. Upcoming events in next 90 days
  6. Gemini narrative synthesis (1-2 sarcastic paragraphs)

Honors DRY_RUN=1 to skip Telegram + Gemini API calls.
"""
from __future__ import annotations

import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import holiday_advisor
import keyword_tracker
import telegram_sender
from gemini_client import GeminiError, generate as gemini_generate
from ytrends_client import YTrendsClient, extract_structured

ROOT = Path(__file__).resolve().parent.parent
HISTORY_FILE = ROOT / "data" / "sales_history.json"

LOOKAHEAD_DAYS = 90


# ==========================================================
#  Sarcastic line pools (weekly-specific)
# ==========================================================

WEEK_ZERO_LINES = [
    "Cả tuần tròn zero, chắc ai cũng nghĩ shop đã đóng cửa.",
    "7 ngày liền không một đơn — shop này định để cho nhện giăng tơ à?",
    "Tuần qua im lìm như ban đêm ở nghĩa trang — bán cho ai?",
]

WEEK_LOW_LINES = [
    "Cả tuần được {n} đơn — chia ra 1 ngày 1 đơn cũng không đủ, thôi chuyển nghề giao hàng đi ông bà.",
    "{n} đơn/7 ngày — số đó khỏi nói ra, sợ hàng xóm cười.",
]

WEEK_OK_LINES = [
    "{n} đơn tuần này — tạm ổn, nhưng đừng ngồi đó vuốt mèo, đối thủ không đợi mình.",
    "Được {n} đơn — chưa đủ giàu, nhưng ít nhất chưa phải bán shop trả nợ.",
]

WEEK_GREAT_LINES = [
    "{n} đơn tuần — khá đấy! Mau nhập thêm inventory kẻo tuần sau cháy hàng không có bán.",
    "Bùng nổ {n} đơn — giữ nhịp này đi, đừng lại tuần sau về mo.",
]

NO_TRENDING_LINES = [
    "YTrends không trả về trending nào rõ ràng — chắc cả sàn đang ngủ đông.",
    "Không có niche nào nổi bật tuần này — tranh thủ optimize listing cũ đi.",
]

NO_GEMS_LINES = [
    "Không có gem nào lộ ra — sàn quá đông hoặc bạn đã biết hết rồi.",
    "Chưa thấy gem ngon — hay thử mở rộng category search xem?",
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
    """For each shop, sum deltas over the last `days` days (skip errors/None)."""
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
    # Sort by weekly sales desc
    out.sort(key=lambda r: r["week_sales"], reverse=True)
    return out


def _ytrends_list(tool: str, args: dict | None = None) -> list[dict]:
    """Call a YTrends tool and try to extract a list of items from common shapes."""
    try:
        with YTrendsClient() as y:
            res = y.call_tool(tool, args or {})
            data = extract_structured(res) or {}
    except Exception as e:
        print(f"[weekly_report] {tool} failed: {type(e).__name__}: {e}")
        return []

    # Try common list containers
    list_keys = ("tags", "results", "items", "keywords", "listings",
                 "trending", "gems", "niches")
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in list_keys:
            v = data.get(key)
            if isinstance(v, list) and v:
                return [x for x in v if isinstance(x, dict)]
        # Nested one level (e.g., {"data": {"tags": [...]}})
        for outer in data.values():
            if isinstance(outer, dict):
                for key in list_keys:
                    v = outer.get(key)
                    if isinstance(v, list) and v:
                        return [x for x in v if isinstance(x, dict)]
    return []


# ==========================================================
#  Format sections
# ==========================================================

def _format_header(today: datetime) -> str:
    start = (today - timedelta(days=6)).strftime("%d/%m")
    end = today.strftime("%d/%m/%Y")
    return f"📊 <b>BẢN TIN MAVIGO — TUẦN</b> ({start} → {end})"


def _format_shop_week(totals: list[dict]) -> str:
    if not totals:
        return ("💰 <b>SALE TUẦN QUA</b>\n"
                "  Chưa có shop nào trong history — có khi scrape toàn lỗi, hoặc bạn chưa điền URL.")

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

    # Top 5 shops
    top = [t for t in totals if t["week_sales"] > 0][:5]
    if top:
        lines.append("▸ Top 5 shop tuần:")
        for t in top:
            lines.append(f"    • <b>{t['name']}</b> ({t['platform']})  "
                         f"<b>+{t['week_sales']}</b> đơn  (total {t['total']})")

    # Dead weight — shops with 0 weekly sales AND enough data to judge
    dead = [t for t in totals if t["week_sales"] == 0 and t["days_with_data"] >= 3]
    if dead:
        names = ", ".join(t["name"] for t in dead[:5])
        more = f" +{len(dead)-5} shop khác" if len(dead) > 5 else ""
        lines.append(f"▸ ☠️ Bán 0 đơn cả tuần: {names}{more}")

    # Scrape-broken shops
    broken = [t for t in totals if t["days_with_data"] == 0 and t["errors"] > 0]
    if broken:
        names = ", ".join(t["name"] for t in broken[:5])
        lines.append(f"▸ ❌ Scrape fail cả tuần: {names} — check URL/IP blocking")

    return "\n".join(lines)


def _format_keywords_tracked(reports: list) -> str:
    if not reports:
        return ("🔍 <b>KEYWORD THEO DÕI</b>\n"
                "  Chưa có keyword nào trong config/keywords.yml.")

    buckets: dict[str, list] = {}
    for r in reports:
        buckets.setdefault(r.bucket, []).append(r)

    lines = [f"🔍 <b>KEYWORD THEO DÕI</b> ({len(reports)} keyword)"]
    for name, emoji in [("spike", "🚀"), ("opportunity", "✨"),
                        ("dying", "💀"), ("crowded", "🏟️"),
                        ("stable", "😐"), ("no_data", "❓"), ("error", "❌")]:
        items = buckets.get(name, [])
        if not items:
            continue
        kws = ", ".join(r.keyword for r in items[:8])
        lines.append(f"▸ {emoji} {name} ({len(items)}): {kws}")
    return "\n".join(lines)


def _format_trending(items: list[dict]) -> str:
    if not items:
        return f"🌟 <b>NICHE RISING (YTrends)</b>\n  {_pick(NO_TRENDING_LINES)}"
    lines = [f"🌟 <b>NICHE RISING (YTrends)</b> — top {min(5, len(items))} keyword đang nổi"]
    for it in items[:5]:
        # Try common key names for keyword + score
        kw = (it.get("keyword") or it.get("tag") or it.get("name")
              or it.get("niche") or "?")
        score = (it.get("momentum") or it.get("momentum_score")
                 or it.get("trend_strength") or it.get("opportunity_score") or "")
        bits = [f"<b>{kw}</b>"]
        if score != "":
            try:
                bits.append(f"score {float(score):.1f}")
            except (TypeError, ValueError):
                bits.append(f"score {score}")
        action = it.get("recommended_action") or it.get("action")
        if action:
            bits.append(f"action: {action}")
        lines.append(f"    • " + "  ·  ".join(bits))
    return "\n".join(lines)


def _format_gems(items: list[dict]) -> str:
    if not items:
        return f"💎 <b>HIDDEN GEMS (YTrends)</b>\n  {_pick(NO_GEMS_LINES)}"
    lines = [f"💎 <b>HIDDEN GEMS (YTrends)</b> — top {min(3, len(items))} cơ hội ít cạnh tranh"]
    for it in items[:3]:
        kw = (it.get("keyword") or it.get("tag") or it.get("name") or "?")
        gem_score = (it.get("gem_score") or it.get("opportunity_score") or "")
        comp = it.get("competition_level") or it.get("competition") or ""
        bits = [f"<b>{kw}</b>"]
        if gem_score != "":
            try:
                bits.append(f"gem {float(gem_score):.1f}")
            except (TypeError, ValueError):
                bits.append(f"gem {gem_score}")
        if comp:
            bits.append(f"comp: {comp}")
        lines.append(f"    • " + "  ·  ".join(bits))
    return "\n".join(lines)


def _format_holidays(events: list) -> str:
    if not events:
        return "🎃 <b>DỊP LỄ / MÙA 90 NGÀY TỚI</b>\n  Không có gì — xả hơi."
    lines = [f"🎃 <b>DỊP LỄ / MÙA 90 NGÀY TỚI</b> ({len(events)} sự kiện)"]
    for e in events[:8]:
        status_emoji = {"late": "🟠", "on_time": "🟢", "upcoming": "🔵"}.get(e.status, "•")
        lines.append(f"    {status_emoji} <b>{e.name_vi}</b> — {e.date} (còn {e.days_until} ngày)")
    if len(events) > 8:
        lines.append(f"    … +{len(events)-8} sự kiện khác")
    return "\n".join(lines)


# ==========================================================
#  Gemini narrative
# ==========================================================

def _build_gemini_prompt(totals: list[dict],
                        kw_reports: list,
                        trending: list[dict],
                        gems: list[dict],
                        events: list) -> str:
    """Compact factual brief for Gemini to roast."""
    week_total = sum(t["week_sales"] for t in totals)
    top_shop = next((t for t in totals if t["week_sales"] > 0), None)
    dead_shops = [t for t in totals if t["week_sales"] == 0 and t["days_with_data"] >= 3]

    spike_kws = [r.keyword for r in kw_reports if r.bucket == "spike"]
    opp_kws = [r.keyword for r in kw_reports if r.bucket == "opportunity"]
    dying_kws = [r.keyword for r in kw_reports if r.bucket == "dying"]

    top_trending = [it.get("keyword") or it.get("tag") or it.get("name")
                    for it in (trending or [])][:3]
    top_gem = None
    if gems:
        g = gems[0]
        top_gem = g.get("keyword") or g.get("tag") or g.get("name")

    nearest_event = events[0] if events else None

    lines = [
        f"Số liệu tuần qua ({len(totals)} shop):",
        f"- Tổng đơn 7 ngày: {week_total}",
        f"- Shop dẫn đầu: {top_shop['name'] if top_shop else 'không có ai'} "
        f"({top_shop['week_sales'] if top_shop else 0} đơn)",
        f"- Số shop bán 0 đơn cả tuần: {len(dead_shops)}"
        + (f" (ví dụ: {', '.join(t['name'] for t in dead_shops[:3])})" if dead_shops else ""),
        f"- Keyword spike: {', '.join(spike_kws) or 'không có'}",
        f"- Keyword opportunity: {', '.join(opp_kws) or 'không có'}",
        f"- Keyword đang chết: {', '.join(dying_kws) or 'không có'}",
        f"- Trending niches YTrends gợi ý: {', '.join(str(x) for x in top_trending) or 'không'}",
        f"- Hidden gem YTrends: {top_gem or 'không có'}",
    ]
    if nearest_event:
        lines.append(f"- Dịp lễ/mùa gần nhất: {nearest_event.name_vi} còn {nearest_event.days_until} ngày ({nearest_event.status})")

    lines.append("")
    lines.append("Hãy viết 1-2 đoạn văn xuôi ngắn (tổng 4-7 câu) tóm tắt tình hình và "
                 "cho lời khuyên cụ thể tuần tới, giọng đanh đá mỉa mai như hướng dẫn. "
                 "Không dùng bullet, không dùng markdown, không lặp lại số liệu thô.")
    return "\n".join(lines)


def _generate_narrative(totals, kw_reports, trending, gems, events) -> str:
    prompt = _build_gemini_prompt(totals, kw_reports, trending, gems, events)
    try:
        text = gemini_generate(prompt, max_tokens=512, temperature=0.95)
    except GeminiError as e:
        print(f"[weekly_report] Gemini failed ({e}) — falling back to template")
        text = ("Tuần qua mình xem xong không biết nên cười hay nên khóc. "
                "Shop thì đứng im, keyword thì trôi theo dòng nước — không ai chủ động vớt. "
                "Tuần tới làm ơn đừng ngồi chờ phép màu, vào optimize listing và "
                "bắn ad cho mấy keyword spike đi, đừng để đối thủ ăn hết.")
    return f"🧠 <b>TỔNG KẾT TUẦN</b>\n{text}"


# ==========================================================
#  Build + send
# ==========================================================

def build_report() -> str:
    now = datetime.now(timezone.utc)

    history = _load_history()
    totals = _weekly_shop_totals(history, days=7)

    kw_reports = keyword_tracker.run()

    print("[weekly_report] fetching YTrends find_trending_keywords")
    trending = _ytrends_list("ytrends_find_trending_keywords", {"country": "US"})

    print("[weekly_report] fetching YTrends find_hidden_gems")
    gems = _ytrends_list("ytrends_find_hidden_gems", {"country": "US"})

    events = holiday_advisor.upcoming(lookahead_days=LOOKAHEAD_DAYS)

    narrative = _generate_narrative(totals, kw_reports, trending, gems, events)

    sections = [
        _format_header(now),
        _format_shop_week(totals),
        _format_keywords_tracked(kw_reports),
        _format_trending(trending),
        _format_gems(gems),
        _format_holidays(events),
        narrative,
    ]
    return "\n\n".join(sections)


def main() -> None:
    report = build_report()
    telegram_sender.send(report)


if __name__ == "__main__":
    main()
