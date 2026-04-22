"""Daily brief — scrape shops, load upcoming holidays, format Vietnamese report, send to Telegram.

Run order:
  1. shop_tracker.run() → updates data/sales_history.json
  2. holiday_advisor.upcoming() → list events in next 60 days
  3. (Phase 2) keyword_tracker.run() → trend spikes
  4. format message and send via telegram_sender.send()

Voice: Vietnamese sarcastic/đanh đá, teasing the shop owner. Pools of snarky
lines picked by random.choice so daily reports don't read identical.

Env vars (see .env.example):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DRY_RUN
"""
from __future__ import annotations

import html
import os
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import shop_tracker
import holiday_advisor
import keyword_tracker
import telegram_sender
import ytrends_analytics as yta


ROOT = Path(__file__).resolve().parent.parent


# ==========================================================
#  SARCASTIC LINE POOLS — giọng đanh đá, trêu chủ shop
# ==========================================================

SALE_ZERO_LINES = [
    "Cả hội {n} shop đứng im như trời trồng — bán thế này có cạp đất cũng không ra tiền em nhỉ?",
    "Hôm nay tròn 0 đơn. Vườn hồng mở cửa chả ma nào vào.",
    "Đơn về = 0. Chắc khách đang bận xem nhà ai cãi nhau.",
    "Cả làng im phăng phắc, bán cho ma còn không ai thèm đi qua.",
    "0 đơn, 0 hy vọng — thôi tối nay ăn mì tôm.",
    "Tròn zero. Mở shop mà như mở viện bảo tàng, khách vào ngắm xong đi ra.",
]

SALE_LOW_LINES = [  # 1-4 orders
    "Lèo tèo {n} đơn — tạm đủ tiền ăn sáng, chưa đủ mua thuốc đau lưng.",
    "{n} đơn về tay — mừng vì chưa phải chuyển nghề bán bánh mì.",
    "Được {n} đơn — thôi còn hơn không, đỡ bị vợ mắng.",
    "{n} đơn lẻ tẻ — xem ra hôm nay chỉ hên được một chút.",
]

SALE_OK_LINES = [  # 5-19 orders
    "Tổng {n} đơn — tạm gọi là có tiền đổ xăng tuần này.",
    "{n} đơn về — không giàu lên được nhưng chưa đói.",
    "Được {n} đơn, ổn. Đừng vội mừng, mai lại có thể xuống đáy.",
]

SALE_GREAT_LINES = [  # 20+ orders
    "Bùng nổ {n} đơn — mai mốt mời mình đi nhậu nhé!",
    "Hôm nay {n} đơn, có vẻ hên — nhưng đừng quên ngày mai có thể về mo.",
    "Cháy hàng {n} đơn! Liệu mà nạp thêm inventory trước khi hết đồ bán.",
]

IDLE_LINES = [
    "ngủ đông {d} ngày — khách mở cửa thấy bảng 'đi ăn phở' à?",
    "đứng hình {d} ngày — đang định chuyển nghề bán bánh mì à?",
    "im {d} ngày liền — traffic còn không bằng nghĩa trang.",
    "bất động {d} ngày — chắc shop đã đi nghỉ mát quên báo.",
    "đóng băng {d} ngày — listing bị ẩn hay khách chết hết rồi?",
]

ERROR_LINES = [
    "scrape không ra — URL sai hay shop bị ban rồi?",
    "cạy data không được — có khi bị sàn block luôn rồi đó.",
    "lỗi tải trang — kiểm tra xem URL còn sống không.",
    "không đọc được page — sàn đổi HTML hay shop bị xóa?",
]

ALL_ERRORS_LINES = [
    "Toàn bộ shop scrape lỗi — chắc IP bị cho lên giàn, không đánh giá được hôm nay.",
    "Không cạy được shop nào cả — có vẻ cả nhà bị chặn, đợi GitHub Actions chạy lại xem sao.",
    "Cả đội scrape fail — mạng đứt hay bị sàn block tập thể đấy?",
]

FIRST_RUN_LINES = [
    "{n} shop mới — mai mới có delta, để xem bán được củ khoai nào.",
    "{n} shop chạy lần đầu — chờ ngày mai so sánh, giờ chưa biết sống chết ra sao.",
]

HOLIDAY_LATE_LINES = [
    "còn {d} ngày mà vẫn chưa list xong — đẩy ad cứu vớt đi ông ơi!",
    "còn {d} ngày rồi. Giờ mới list thì thôi, làm bản tin tưởng niệm cho nhanh.",
    "còn {d} ngày — trễ rồi, giờ chỉ còn cách đốt tiền chạy ad.",
    "còn {d} ngày — định để khách đi mua shop khác à?",
]

HOLIDAY_ONTIME_LINES = [
    "còn {d} ngày — đúng thời điểm vàng, list và bắn ad ngay.",
    "còn {d} ngày — vừa vặn, đừng ngồi đó vuốt mèo nữa.",
    "còn {d} ngày — list đi là vừa, chần chừ là mất miếng ngon.",
]

HOLIDAY_UPCOMING_LINES = [
    "còn {d} ngày — còn sớm, chuẩn bị từ từ kẻo cuống phút chót.",
    "còn {d} ngày — từ giờ chuẩn bị, đừng nước đến chân mới nhảy.",
    "còn {d} ngày — thong thả làm, nhưng đừng quên kẻo bỏ lỡ.",
]

NO_HOLIDAY_LINES = [
    "Không có gì sắp tới — coi như xả hơi, nhưng đừng lười quá kẻo bị Halloween đánh úp.",
    "60 ngày tới yên bình — đây là lúc nạp thêm listing để chờ mùa.",
]

ACTION_IDLE_PREFIX = [
    "Check listing/inventory gấp cho:",
    "Vào cứu mấy shop này kẻo chết luôn:",
    "Mấy shop này im quá lâu, xem lại có list bị hidden/expired không:",
]

ACTION_HOLIDAY_LATE = [
    "{event} còn {d} ngày — đẩy ad khẩn cấp, đừng list mới nữa mất công.",
    "{event} còn {d} ngày — chạy ad đi, list mới thì SEO không kịp lên đâu.",
]

ACTION_HOLIDAY_ONTIME = [
    "{event} còn {d} ngày — list và promote ngay, vàng đấy.",
    "{event} còn {d} ngày — chuẩn thời điểm, list + ad combo tới tới.",
]

ACTION_NOTHING_LINES = [
    "Hôm nay không có gì gấp — cứ theo dõi, đừng ngồi ngủ quên.",
    "Chưa có action khẩn — tranh thủ nghỉ, mai chiến tiếp.",
    "Bình yên — nhân lúc rảnh đi optimize title/tag đi, đừng lướt TikTok suốt.",
]

KW_SPIKE_LINES = [
    "đang bốc cháy ({trend}, strength {ts:+.2f}) — nhào vô kẻo muộn!",
    "rising mạnh (revenue {rev:+.0f}%) — lướt theo sóng ngay, đừng ngồi ngắm.",
    "trend nổi, opp {opp:.0f} — món này hot, chậm là hết phần.",
]

KW_OPPORTUNITY_LINES = [
    "YTrends chấm '{action}' (opp {opp:.0f}) — cơ hội đây, còn chờ gì?",
    "action={action}, comp={comp} — vào sớm ăn non, đợi đông rồi thì chỉ còn xương.",
]

KW_DYING_LINES = [
    "đang chết ({trend}, revenue {rev:+.0f}%) — bỏ track đi, đừng nuôi hy vọng hão.",
    "tuột dốc không phanh — quên nó đi, tìm cái khác ngon hơn.",
]

KW_CROWDED_LINES = [
    "đông như hội chợ (comp={comp}, opp {opp:.0f}) — vào phải có USP mạnh, không là chết chìm.",
    "cạnh tranh cao mà opp chỉ {opp:.0f} — đây là vũng lầy, né đi là vừa.",
]

KW_NO_DATA_LINES = [
    "sàn chưa có đủ listing để đánh giá — hoặc keyword quá niche, hoặc sai chính tả.",
    "không đủ data — thử keyword cụ thể hơn (ví dụ 'linen apron' thay vì 'apron').",
]


def _pick(pool: list[str], **kwargs) -> str:
    return random.choice(pool).format(**kwargs)


def _esc(s: object) -> str:
    # unescape first to avoid double-encoding pre-escaped API text (e.g. &#39;)
    raw = html.unescape(str(s))
    return (raw.replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _esc_trim(s: object, n: int) -> str:
    """Unescape → trim to N chars (clean text) → re-escape. Avoids cutting
    mid-entity like `&#39;` which would then get mangled to `&amp;#`."""
    raw = html.unescape(str(s or ""))[:n]
    return (raw.replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _fmt_money(x) -> str:
    try:
        return f"${float(x):,.0f}"
    except (TypeError, ValueError):
        return "?"


# ==========================================================
#  FORMAT SECTIONS
# ==========================================================

def _format_shop_section(history: dict) -> str:
    """Summarize today's scrape with sarcastic commentary."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = []
    for name, entry in history.get("shops", {}).items():
        snaps = entry.get("snapshots") or []
        if not snaps or snaps[-1]["date"] != today:
            continue
        last = snaps[-1]
        rows.append({
            "name": name,
            "platform": entry.get("platform", "?"),
            "delta": last.get("delta"),
            "total": last.get("total_sales"),
            "error": last.get("error"),
            "idle_days": _count_idle(snaps),
        })

    if not rows:
        return ("💰 <b>SALE 24H QUA</b>\n"
                "  Chưa có dữ liệu — script chạy lần đầu, mai mới có delta mà mỉa mai.")

    errors = [r for r in rows if r["error"]]
    with_delta = [r for r in rows if r["delta"] is not None]
    first_run = [r for r in rows if r["delta"] is None and not r["error"]]

    lines = [f"💰 <b>SALE 24H QUA</b> ({len(rows)} shop)"]

    # Case: ALL rows are errors — scrape infrastructure broken
    if not with_delta and errors and len(errors) == len(rows):
        lines.append(f"▸ ❌ Toàn bộ {len(rows)} shop scrape lỗi.")
        lines.append(f"  💬 {_pick(ALL_ERRORS_LINES)}")
        names = ", ".join(r["name"] for r in errors[:5])
        more = f" +{len(errors)-5} shop khác" if len(errors) > 5 else ""
        lines.append(f"  Shop lỗi: {names}{more}")
        return "\n".join(lines)

    # Case: normal — compute deltas
    etsy_delta = sum(r["delta"] or 0 for r in with_delta if r["platform"] == "etsy")
    ebay_delta = sum(r["delta"] or 0 for r in with_delta if r["platform"] == "ebay")
    total_delta = etsy_delta + ebay_delta

    lines.append(f"▸ Tổng: <b>{total_delta:+d}</b> đơn  (Etsy {etsy_delta:+d} | eBay {ebay_delta:+d})")

    # Snarky summary based on total
    if total_delta <= 0:
        snark = _pick(SALE_ZERO_LINES, n=len(rows))
    elif total_delta <= 4:
        snark = _pick(SALE_LOW_LINES, n=total_delta)
    elif total_delta <= 19:
        snark = _pick(SALE_OK_LINES, n=total_delta)
    else:
        snark = _pick(SALE_GREAT_LINES, n=total_delta)
    lines.append(f"  💬 {snark}")

    # Top growers
    top = sorted([r for r in with_delta if (r["delta"] or 0) > 0],
                 key=lambda r: r["delta"], reverse=True)[:5]
    if top:
        lines.append("▸ Mấy đứa kéo team hôm nay:")
        for r in top:
            lines.append(f"    • {r['name']}  <b>+{r['delta']}</b>  (total {r['total']})")

    # Full breakdown — every shop, sorted platform then name
    lines.append("▸ 📊 Chi tiết từng shop:")
    for r in sorted(rows, key=lambda x: (x["platform"], x["name"].lower())):
        tag = f"[{r['platform']}]"
        if r["error"]:
            lines.append(f"    • {tag} <b>{r['name']}</b> — ⚠ {r['error']}")
        elif r["delta"] is None:
            lines.append(f"    • {tag} <b>{r['name']}</b> — total {r['total']} (mới)")
        else:
            sign = f"+{r['delta']}" if r["delta"] > 0 else str(r["delta"])
            lines.append(f"    • {tag} <b>{r['name']}</b> — total {r['total']} (Δ{sign})")

    # Idle shops
    idle = [r for r in rows if r["idle_days"] >= 3]
    if idle:
        lines.append("▸ ⚠️ Đứng im ≥3 ngày:")
        for r in idle[:5]:
            lines.append(f"    • <b>{r['name']}</b> — {_pick(IDLE_LINES, d=r['idle_days'])}")
        if len(idle) > 5:
            lines.append(f"    … +{len(idle)-5} shop khác cũng ngủ đông")

    # Errors (partial)
    if errors and len(errors) < len(rows):
        lines.append("▸ ❌ Scrape lỗi:")
        for r in errors[:5]:
            lines.append(f"    • <b>{r['name']}</b> — {_pick(ERROR_LINES)}")
        if len(errors) > 5:
            lines.append(f"    … +{len(errors)-5} shop khác cũng lỗi")

    # First-run notices
    if first_run:
        lines.append(f"▸ ℹ️ {_pick(FIRST_RUN_LINES, n=len(first_run))}")

    return "\n".join(lines)


def _count_idle(snaps: list[dict]) -> int:
    """Count trailing consecutive days with delta == 0 (non-null, non-error)."""
    count = 0
    for s in reversed(snaps):
        if s.get("error"):
            continue
        d = s.get("delta")
        if d == 0:
            count += 1
        else:
            break
    return count


def _format_holiday_section(events: list) -> str:
    if not events:
        return f"🎃 <b>DỊP LỄ / MÙA 60 NGÀY TỚI</b>\n  {_pick(NO_HOLIDAY_LINES)}"

    lines = [f"🎃 <b>DỊP LỄ / MÙA 60 NGÀY TỚI</b> ({len(events)} sự kiện)"]
    for e in events[:5]:
        lines.append(f"• <b>{e.name_vi}</b> — {e.date}")
        if e.status == "late":
            emoji = "🟠"
            comment = _pick(HOLIDAY_LATE_LINES, d=e.days_until)
        elif e.status == "on_time":
            emoji = "🟢"
            comment = _pick(HOLIDAY_ONTIME_LINES, d=e.days_until)
        else:
            emoji = "🔵"
            comment = _pick(HOLIDAY_UPCOMING_LINES, d=e.days_until)
        lines.append(f"    {emoji} {comment}")
        if e.keywords:
            kws = ", ".join(e.keywords[:6])
            lines.append(f"    Keyword gợi ý: <i>{kws}</i>")
        elif e.categories:
            cats = ", ".join(e.categories[:6])
            lines.append(f"    Category: <i>{cats}</i>")
    if len(events) > 5:
        lines.append(f"… +{len(events)-5} sự kiện khác (lười liệt kê tiếp)")
    return "\n".join(lines)


BUCKET_BADGES = {
    "spike": "🚀 BỐC CHÁY",
    "opportunity": "✨ CƠ HỘI",
    "crowded": "🏟️ ĐÔNG",
    "dying": "💀 CHẾT",
    "stable": "😐 ỔN ĐỊNH",
    "no_data": "❓ THIẾU DATA",
    "error": "❌ LỖI",
}

BUCKET_PRIO = {"spike": 0, "opportunity": 1, "crowded": 2, "dying": 3,
               "stable": 4, "no_data": 5, "error": 6}


def _format_keyword_section(reports: list) -> str:
    if not reports:
        return ("🔥 <b>KEYWORD THEO DÕI</b>\n"
                "  Chưa có keyword nào — điền vào tab <code>keywords</code> của Sheet "
                "(hoặc <code>config/keywords.yml</code>), có dữ liệu mới mỉa mai được.")

    # Sort: interesting signals first, then by opportunity_score desc
    sorted_reports = sorted(
        reports,
        key=lambda r: (BUCKET_PRIO.get(r.bucket, 99), -r.opportunity_score),
    )

    lines = [f"🔥 <b>KEYWORD THEO DÕI</b> ({len(reports)} keyword)"]

    for r in sorted_reports:
        badge = BUCKET_BADGES.get(r.bucket, r.bucket)

        if r.bucket == "error":
            lines.append(f"▸ <b>{_esc(r.keyword)}</b> [{badge}] — {_esc(r.error)}")
            continue

        # Header line: name + badge + niche size + competition + opportunity
        header = (f"▸ <b>{_esc(r.keyword)}</b> [{badge}] "
                  f"· <b>{r.total_listings:,}</b> listing "
                  f"· comp {_esc(r.competition)} "
                  f"· opp {r.opportunity_score:.0f}")
        lines.append(header)

        # Snark based on bucket (skip for stable — it's the default)
        snark = None
        if r.bucket == "spike":
            snark = _pick(KW_SPIKE_LINES, trend=r.trend, ts=r.trend_strength,
                          rev=r.revenue_change_pct, opp=r.opportunity_score)
        elif r.bucket == "opportunity":
            snark = _pick(KW_OPPORTUNITY_LINES, action=r.action,
                          opp=r.opportunity_score, comp=r.competition)
        elif r.bucket == "dying":
            snark = _pick(KW_DYING_LINES, trend=r.trend, rev=r.revenue_change_pct)
        elif r.bucket == "crowded":
            snark = _pick(KW_CROWDED_LINES, comp=r.competition, opp=r.opportunity_score)
        elif r.bucket == "no_data":
            snark = _pick(KW_NO_DATA_LINES)
        if snark:
            lines.append(f"    💬 {snark}")

        # Price hint — skip placeholder ("?") and YTrends' "nan-nan" for no_data
        if r.price_range and r.price_range not in ("?", "nan-nan"):
            lines.append(f"    💵 giá gợi ý ${_esc(r.price_range)}")

        # Top shop (by revenue in this niche)
        if r.top_shop_id:
            country = r.top_shop_country or "?"
            lines.append(
                f"    🏪 Top shop: <code>#{r.top_shop_id}</code> ({_esc(country)}) "
                f"— {_fmt_money(r.top_shop_revenue_usd)} doanh thu, "
                f"{r.top_shop_listings} listing"
            )

        # Top listing (by total_sold in this niche)
        if r.top_listing_id:
            title = _esc_trim(r.top_listing_title, 70)
            url = f"https://www.etsy.com/listing/{r.top_listing_id}"
            price_str = (f"${r.top_listing_price_usd:.2f}"
                         if r.top_listing_price_usd else "?")
            lines.append(
                f'    ⭐ Top listing: <a href="{url}">{title}</a> '
                f'— {price_str}, đã bán {r.top_listing_total_sold}'
            )

    return "\n".join(lines)


def _format_hot_today(hot: list[dict]) -> str:
    """Compact 3-listing summary. Full thumbnails sent as separate album."""
    if not hot:
        return ""
    lines = [f"🔥 <b>HOT HÔM NAY</b> — top {min(3, len(hot))} listing đang outperform niche"]
    for i, h in enumerate(hot[:3], 1):
        title = _esc_trim(h.get("title"), 55)
        price = _fmt_money(h.get("price_usd") or h.get("price"))
        country = h.get("shop_country") or "?"
        conv_x = h.get("conversion_multiplier")
        try:
            conv_str = f"{float(conv_x):.1f}x conv"
        except (TypeError, ValueError):
            conv_str = ""
        lines.append(f"{i}. <b>{title}…</b>")
        lines.append(f"   · {price} · {country} · {conv_str}")
    lines.append("  📷 <i>ảnh album bên dưới</i>")
    return "\n".join(lines)


def _hot_media_group(hot: list[dict], n: int = 3) -> list[dict]:
    items = []
    for h in hot[:n]:
        url = h.get("image_url")
        if not url:
            continue
        title = _esc_trim(h.get("title"), 80)
        price = _fmt_money(h.get("price_usd") or h.get("price"))
        country = h.get("shop_country") or "?"
        conv_x = h.get("conversion_multiplier")
        why = _esc_trim(h.get("why_hot_detail"), 150)
        listing_id = h.get("listing_id")
        link = f"https://www.etsy.com/listing/{listing_id}" if listing_id else ""
        try:
            conv_str = f"{float(conv_x):.1f}x conv"
        except (TypeError, ValueError):
            conv_str = ""
        cap = [f"🔥 <b>{title}…</b>",
               f"{price} · {country} · {conv_str}".strip()]
        if why:
            cap.append(f"💡 {why}")
        if link:
            cap.append(f'<a href="{link}">Xem trên Etsy</a>')
        items.append({"photo": url, "caption": "\n".join(cap)})
    return items


def _format_actions(history: dict, events: list, kw_reports: list) -> str:
    actions = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    idle = []
    for name, entry in history.get("shops", {}).items():
        snaps = entry.get("snapshots") or []
        if snaps and snaps[-1]["date"] == today and _count_idle(snaps) >= 3:
            idle.append(name)
    if idle:
        prefix = _pick(ACTION_IDLE_PREFIX)
        actions.append(f"▸ {prefix} {', '.join(idle[:3])}")

    urgent = next((e for e in events if e.status in ("on_time", "late")), None)
    if urgent:
        pool = ACTION_HOLIDAY_LATE if urgent.status == "late" else ACTION_HOLIDAY_ONTIME
        actions.append(f"▸ {_pick(pool, event=urgent.name_vi, d=urgent.days_until)}")

    # Keyword urgency — 1 line for hottest spike/opportunity
    hot = [r for r in kw_reports if r.bucket in ("spike", "opportunity")]
    if hot:
        # Sort: opportunity first (clearer signal), then spike by trend_strength
        hot.sort(key=lambda r: (0 if r.bucket == "opportunity" else 1, -r.trend_strength))
        r = hot[0]
        if r.bucket == "spike":
            actions.append(f"▸ Keyword <b>{r.keyword}</b> đang rising mạnh — list thêm, đặt giá ${r.price_range}, bắn ad theo trend.")
        else:
            actions.append(f"▸ Keyword <b>{r.keyword}</b> YTrends chấm '{r.action}' — list ngay, giá khuyến nghị ${r.price_range}.")

    if not actions:
        actions.append(f"▸ {_pick(ACTION_NOTHING_LINES)}")

    return "💡 <b>GỢI Ý HÀNH ĐỘNG</b>\n" + "\n".join(actions)


# ==========================================================
#  MAIN
# ==========================================================

def build_report() -> tuple[str, list[dict]]:
    """Return (text_report, hot_media_items). Caller sends both."""
    today_vn = datetime.now(timezone.utc).strftime("%d/%m/%Y")

    history = shop_tracker.run()
    events = holiday_advisor.upcoming()
    kw_reports = keyword_tracker.run()

    print("[daily_report] fetching find_hot_listings")
    hot = yta.get_hot_listings(limit=5)

    sections = [
        f"🌞 <b>BẢN TIN MAVIGO</b> — {today_vn}",
        _format_shop_section(history),
        _format_hot_today(hot),
        _format_keyword_section(kw_reports),
        _format_holiday_section(events),
        _format_actions(history, events, kw_reports),
    ]
    text = "\n\n".join(s for s in sections if s)
    album = _hot_media_group(hot, n=3)
    return text, album


def main() -> None:
    text, album = build_report()
    telegram_sender.send(text)
    if album:
        print(f"[daily_report] sending hot album ({len(album)} photos)")
        telegram_sender.send_media_group(album)


if __name__ == "__main__":
    main()
