# PLAN — Bản Tin Mavigo

Hệ thống tự động thu thập dữ liệu sale, trend, dịp lễ rồi gửi báo cáo Telegram cho team. Chạy trên GitHub Actions (miễn phí).

## Mục tiêu

1. **Daily brief (8h sáng VN, hàng ngày)** — 4 phần:
   - Số đơn mới 24h qua của 15 shop Etsy + 20 shop eBay, sắp xếp theo tăng trưởng
   - Keyword nào trong list theo dõi có trend đột biến (dựa trên momentum score YTrends)
   - Dịp lễ sắp tới trong 60 ngày + gợi ý sản phẩm (cross-check với `ytrends_trend_calendar`)
   - Gợi ý hành động: keyword nào cần đẩy ad, shop nào đứng im cần xem lại
2. **Weekly deep (9h sáng thứ 2)** — phân tích sâu dùng 5 tools YTrends + LLM viết narrative:
   - Market snapshot, top trending keywords, hidden gems, scout opportunities
   - Intersect 3 tools → "sweet spot kép" (niche xuất hiện ở nhiều tool)
   - Top 3 niche → `ytrends_explore_niche` deep dive
   - 90-day events → khớp niche với dịp lễ
   - Gemini 2.5 Flash tổng hợp → báo cáo tư vấn tiếng Việt

## Kiến trúc tổng quan

```
┌─────────────┐     ┌─────────────┐     ┌──────────────┐
│ shops.yml   │────▶│shop_tracker │────▶│sales_history │
│ (35 shop)   │     │  (scrape)   │     │   .json      │
└─────────────┘     └─────────────┘     └──────────────┘
                                               │
┌─────────────┐     ┌─────────────┐     ┌──────▼───────┐
│Google Sheet │────▶│keyword_track│────▶│trend_history │
│(keyword)    │     │  (YTrends)  │     │   .json      │
└─────────────┘     └─────────────┘     └──────┬───────┘
                                               │
┌─────────────┐     ┌─────────────┐     ┌──────▼───────┐
│holidays.json│────▶│holiday_advsr│────▶│daily_report  │
└─────────────┘     └─────────────┘     │   (Telegram) │
                                        └──────────────┘
```

## Cấu trúc thư mục

```
ban-tin-mavigo/
├── scripts/
│   ├── shop_tracker.py       # Scrape public shop pages → delta sales
│   ├── keyword_tracker.py    # Đọc Sheet → YTrends momentum → spike
│   ├── holiday_advisor.py    # holidays.json + ytrends_trend_calendar
│   ├── daily_report.py       # Gộp 3 cái trên → Telegram (template thuần)
│   ├── weekly_report.py      # 5 YTrends tools + Gemini narrative
│   ├── ytrends_client.py     # Helper gọi YTrends MCP qua HTTP
│   ├── gemini_client.py      # Helper gọi Gemini 2.5 Flash
│   └── telegram.py           # Helper gửi tin nhắn
├── config/
│   ├── shops.yml             # 15 Etsy + 20 eBay URLs (bạn tự điền/chỉnh)
│   └── holidays.json         # Lễ lớn 2026–2027 (đã điền sẵn)
├── data/                     # State, auto-commit sau mỗi lần chạy
│   ├── sales_history.json
│   └── trend_history.json
├── .github/workflows/
│   ├── daily.yml             # cron 0 1 * * *  (8h VN)
│   └── weekly.yml            # cron 0 2 * * 1  (9h VN thứ 2)
├── requirements.txt
├── README.md
├── CLAUDE.md
└── PLAN.md                   # File này
```

## Chi tiết từng component

### 1. `scripts/shop_tracker.py`

**Input:** `config/shops.yml`
**Output:** update `data/sales_history.json`

**Logic:**
1. Đọc danh sách 35 shop URL từ `shops.yml`
2. Với mỗi shop:
   - Etsy: GET `https://www.etsy.com/shop/<NAME>` → parse `X Sales` từ header
   - eBay: GET `https://www.ebay.com/str/<NAME>` hoặc `/usr/<NAME>` → parse `items sold` từ JSON embed trong HTML. Pattern xác nhận từ page stephanie9121: `"text":"N","styles":["BOLD"]},{"_type":"TextSpan","text":" items sold"`
   - Random User-Agent + delay 3–5s giữa các request
   - Retry 3 lần với exponential backoff nếu lỗi
3. Load `sales_history.json` → so sánh total hôm qua
4. Tính delta = `total_hôm_nay - total_hôm_qua`
5. Ghi lại snapshot mới + delta vào history

**Anti-block:** GitHub Actions runner → header User-Agent như Chrome thật, delay ngẫu nhiên, nếu bị 403/429 → alert Telegram để biết.

**Etsy 403 issue:** Test từ máy local thấy Etsy block curl/httpx (403). Cần test thực tế trên GitHub Actions runner (IP khác) trước khi kết luận. Nếu vẫn block, phương án dự phòng: dùng `httpx` với full browser headers + cookie, hoặc chuyển sang endpoint mobile `m.etsy.com`.

### 2. `scripts/keyword_tracker.py`

**Input:** Google Sheet CSV URL (từ env var `KEYWORD_SHEET_URL`)
**Output:** update `data/trend_history.json`

**Format Google Sheet bạn cần tạo:**
| keyword | category | platform | note |
|---|---|---|---|
| halloween mug | ceramic | etsy | theo dõi từ T9 |
| vintage watch | accessory | ebay | |

Publish Sheet → File → Share → Publish to web → CSV → copy link. Dán link đó vào GitHub Secrets với tên `KEYWORD_SHEET_URL`.

**Logic (dùng YTrends làm nguồn dữ liệu chính):**
1. `pandas.read_csv(KEYWORD_SHEET_URL)` → list keywords (giới hạn ≤ 50 để tránh rate limit 60 calls/phút)
2. Với mỗi keyword: gọi `ytrends_research_keyword` → lấy:
   - `momentum_score` (0–100, độ nóng hiện tại)
   - `opportunity_score` (0–100, cơ hội tổng hợp)
   - `competition_level` (low/medium/high/very_high)
   - `history_30_days` (điểm mỗi ngày trong 30 ngày gần nhất)
   - Related keywords + top listings
3. Lưu snapshot `{date, momentum, opportunity, competition}` vào `trend_history.json`
4. Phát hiện spike:
   - Spike mạnh: `today_momentum ≥ 1.5 × avg(last_7_days)` **và** `competition_level ≤ medium`
   - Spike cảnh báo: tăng ≥ 100% nhưng `competition_level = very_high` → "cạnh tranh gay gắt, cần USP"
5. Xếp hạng top 5 theo `momentum_growth × opportunity_score`
6. **Fallback:** nếu YTrends trả `insufficient_data` → gọi `ytrends_search` với keyword rộng hơn → lưu flag để user biết niche quá nhỏ

### 3. `scripts/holiday_advisor.py`

**Input:** `config/holidays.json` + YTrends `ytrends_trend_calendar` (cache hàng tuần)
**Output:** list dịp lễ trong 60 ngày tới kèm gợi ý dữ liệu thực

**Logic:**
1. Load `holidays.json` (curated list của mình)
2. Filter các event có `date` trong khoảng `[today, today + 60 ngày]`
3. Mỗi tuần (hoặc khi cache cũ > 7 ngày): gọi `ytrends_trend_calendar` → lấy data-driven events + keyword ăn khách thực tế → save vào `data/ytrends_calendar_cache.json`
4. Cross-reference: với mỗi event trong holidays.json, tìm match trong YTrends cache (so khớp tên hoặc ngày ±3 ngày) → merge `keywords` từ 2 nguồn, ưu tiên YTrends (dữ liệu thực)
5. Với mỗi event → trả về:
   - Tên, ngày, số ngày nữa
   - `lead_days` khuyến nghị (nếu còn đủ → "đang đúng lúc", nếu đã quá hạn → "hơi muộn, đẩy ad thay vì list mới")
   - Top 10 keyword từ YTrends (nếu có) + category gợi ý
   - `momentum_score` của category (nếu lấy được từ `ytrends_market_snapshot`)

### 4. `scripts/daily_report.py`

Gộp output 3 script trên → format message Telegram tiếng Việt → gửi qua `telegram.py`.

**Mẫu báo cáo daily (dữ liệu giàu hơn nhờ YTrends):**
```
🌞 BẢN TIN MAVIGO — 21/04/2026

💰 SALE HÔM QUA (35 shop)
▸ Tổng: +148 đơn (Etsy 92 | eBay 56) · vs 7 ngày TB: +12%
▸ Top tăng trưởng:
   1. EtsyShop_Luna   +18 đơn (+65% vs TB 7 ngày)
   2. eBay_vintage99  +12 đơn (+40%)
   3. EtsyShop_Halo   +9 đơn (+28%)
▸ ⚠️ Đứng im 3+ ngày: Shop_X, Shop_Y, Shop_Z

🔥 KEYWORD ĐỘT BIẾN (từ YTrends momentum)
   1. "halloween mug"        momentum 82 (↑120%) · competition: medium
      → 🟢 Nên đẩy ngay
   2. "mother day necklace"  momentum 75 (↑85%) · competition: high
      → 🟡 Cạnh tranh gay gắt, cần USP
   3. "vintage gold watch"   momentum 68 (↑62%) · competition: low
      → 🟢 Cơ hội tốt, low competition

🎃 DỊP LỄ SẮP TỚI (từ holidays.json + ytrends_trend_calendar)
   • Mother's Day — 10/05/2026 (còn 19 ngày)
     → Keyword hot từ YTrends: mom necklace, birth flower jewelry, mama mug
     → Lead time khuyến nghị: 30 ngày (hơi muộn → đẩy ad listing cũ)
   • Memorial Day — 25/05/2026 (còn 34 ngày)
     → Keyword hot: american flag shirt, military mom, veteran gift
     → Lead time: 14 ngày → đúng lúc bắt đầu list

💡 GỢI Ý HÀNH ĐỘNG
   ▸ Ưu tiên đẩy "halloween mug" + "vintage gold watch" — cả 2 spike mạnh, cạnh tranh thấp/vừa
   ▸ Shop_X đứng im 5 ngày → kiểm tra inventory/listing quality
   ▸ Bắt đầu chuẩn bị Memorial Day (34 ngày nữa) — list trong tuần này

📊 Full data: [link artifact GitHub]
```

### 5. `scripts/weekly_report.py`

Báo cáo deep dùng 5 tools YTrends + Gemini LLM viết narrative tiếng Việt.

**Flow (tự động hoá "buổi nghiên cứu 30 phút" trong PDF YTrends):**

1. **Market snapshot:** `ytrends_market_snapshot` — ảnh chụp tổng thể Etsy tuần này (hot categories, overall temperature)
2. **Trending keywords:** `ytrends_find_trending_keywords` (limit 20) — top keyword đang lên
3. **Hidden gems:** `ytrends_find_hidden_gems` — niche có conversion cao + ít đối thủ
4. **Scout opportunities:** `ytrends_scout_opportunities` với filter `opportunity_score ≥ 60, competition ≤ medium` — niche đáng vào
5. **Intersect** 3 output (2, 3, 4) → niche nào xuất hiện ở ≥ 2 danh sách = "sweet spot kép" (theo mẹo PDF)
6. **Deep dive top 3 sweet spots:** với mỗi niche → `ytrends_explore_niche` → lấy sub-niche, giá, top listings, gap
7. **90-day events:** `ytrends_trend_calendar` — sự kiện 90 ngày tới kèm keyword
8. **Shop performance review:** tổng kết 7 ngày của 35 shop (từ `sales_history.json`) — top 3 tăng, top 3 đứng im
9. **Keyword weekly summary:** từ `trend_history.json` — keyword nào giữ momentum cả tuần, keyword nào flash-in-the-pan
10. **LLM narrative (Gemini 2.5 Flash):** gom tất cả data → prompt Gemini viết advisory tiếng Việt:
    - Tóm tắt thị trường 3 câu
    - 3 khuyến nghị cụ thể cho tuần tới (list sản phẩm gì, đẩy keyword nào, dịp lễ nào cần chuẩn bị)
    - Cảnh báo (nếu có): shop nào dropping, niche nào đang bão hoà

**Ngân sách Gemini:** 1 report/tuần × ~15–20 tool calls MCP + 1 narrative generation ≈ 0 chi phí (nằm trong Gemini free tier 1000 req/ngày).

### 6. Workflow GitHub Actions

**`daily.yml`:**
```yaml
name: Daily Brief
on:
  schedule:
    - cron: "0 1 * * *"   # 8h sáng VN = 01:00 UTC
  workflow_dispatch:
jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: "3.11"}
      - run: pip install -r requirements.txt
      - run: python scripts/daily_report.py
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
          KEYWORD_SHEET_URL: ${{ secrets.KEYWORD_SHEET_URL }}
          YTRENDS_API_KEY: ${{ secrets.YTRENDS_API_KEY }}
      - uses: stefanzweifel/git-auto-commit-action@v5
        with:
          commit_message: "data: update daily snapshot"
          file_pattern: "data/*.json"
```

**`weekly.yml`:** tương tự, cron `0 2 * * 1` (thứ 2 9h VN).

## Data schema

### `data/sales_history.json`
```json
{
  "last_updated": "2026-04-21T01:00:00Z",
  "shops": {
    "EtsyShop_Luna": {
      "platform": "etsy",
      "url": "https://www.etsy.com/shop/EtsyShop_Luna",
      "snapshots": [
        {"date": "2026-04-20", "total_sales": 1248, "delta": 15},
        {"date": "2026-04-21", "total_sales": 1266, "delta": 18}
      ]
    }
  }
}
```

### `data/trend_history.json`
```json
{
  "last_updated": "2026-04-21T01:00:00Z",
  "keywords": {
    "halloween mug": {
      "history": [
        {"date": "2026-04-14", "score": 45},
        {"date": "2026-04-20", "score": 47},
        {"date": "2026-04-21", "score": 140}
      ]
    }
  }
}
```

## YTrends MCP — cách tích hợp

**Endpoint:** `https://mcp.trends.ytuong.ai/mcp` — Public, no auth, no API key.

**Giao thức:** JSON-RPC 2.0 qua HTTP (streamable MCP).

**Python client helper** (`scripts/ytrends_client.py`):
```python
import httpx, uuid, time

MCP_URL = "https://mcp.trends.ytuong.ai/mcp"

def call_tool(name: str, arguments: dict, retries: int = 3) -> dict:
    """Gọi 1 YTrends tool. Trả về result dict hoặc raise nếu sau retries vẫn fail."""
    for attempt in range(retries):
        try:
            r = httpx.post(MCP_URL, json={
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments}
            }, timeout=30)
            r.raise_for_status()
            return r.json()["result"]
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:   # rate limit 60/min
                time.sleep(10 * (attempt + 1))
                continue
            raise
    raise RuntimeError(f"YTrends call failed after {retries} retries")
```

**12 tools YTrends có** (xem PDF để biết chi tiết):
- `ytrends_explore_niche` — research niche A-Z
- `ytrends_research_keyword` — deep keyword (dùng daily)
- `ytrends_scout_opportunities` — scan nhiều niche (dùng weekly)
- `ytrends_browse_new_listings` — listing mới bán chạy
- `ytrends_find_trending_keywords` — keyword đang lên (dùng weekly)
- `ytrends_find_hot_listings` — listing vượt trội
- `ytrends_find_hidden_gems` — mỏ vàng (dùng weekly)
- `ytrends_analyze_competition` — phân tích đối thủ
- `ytrends_market_snapshot` — snapshot thị trường (dùng weekly)
- `ytrends_trend_calendar` — lịch sự kiện (dùng daily cache)
- `ytrends_search` — fallback khi insufficient_data
- `ytrends_fetch` — lấy chi tiết theo ID

**Rate limit:** 60 calls/phút. Daily dùng ~50 calls (50 keyword), weekly ~15 calls → đều dưới hạn.

**Độ tươi dữ liệu:** median ~28 ngày tuổi, 20% dữ liệu từ 14 ngày gần đây. Đủ cho trend analysis theo tuần/tháng, không đủ cho realtime.

## Gemini LLM cho weekly narrative

**API key:** bạn đã có Gemini project `banana-464314` (từ dự án `my-ai-assistant`). Reuse key.

**Model:** `gemini-2.5-flash` — nhanh, rẻ, nằm trong free tier 1000 req/ngày.

**Flow:** Python gom data từ YTrends + history → build prompt có structure → Gemini tạo narrative Vietnamese → inject vào template Telegram.

Không dùng Gemini MCP connector trực tiếp (phức tạp hơn), mà mình gọi YTrends MCP từ Python trước, rồi truyền data vào Gemini để viết → kiểm soát tốt hơn, dễ debug, vẫn dùng được free tier.

## Secrets cần thêm vào GitHub

| Secret | Giá trị | Bắt buộc? |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Token từ @BotFather | ✅ |
| `TELEGRAM_CHAT_ID` | Chat ID của channel/group | ✅ |
| `KEYWORD_SHEET_URL` | Link CSV export của Google Sheet | ✅ |
| `GEMINI_API_KEY` | Key Gemini (reuse từ project `banana-464314`) | ✅ cho weekly, ⬜ cho daily |
| `YTRENDS_API_KEY` | (hiện tại **không cần** — YTrends public) | ⬜ |

## Roadmap triển khai

**Phase 1 — Scaffold:**
- ✅ Tạo `PLAN.md`, `config/shops.yml`, `config/holidays.json`
- ⬜ Viết `ytrends_client.py` + test gọi 1 tool (vd `ytrends_market_snapshot`)
- ⬜ Viết `shop_tracker.py` + test với 1–2 shop thật
- ⬜ Viết `holiday_advisor.py` + tích hợp `ytrends_trend_calendar`
- ⬜ Viết `telegram.py`

**Phase 2 — Daily report:**
- ⬜ Tạo Google Sheet keyword template
- ⬜ Viết `keyword_tracker.py` dùng `ytrends_research_keyword`
- ⬜ Viết `daily_report.py` gộp 3 component + format tiếng Việt
- ⬜ Test end-to-end local với DRY_RUN

**Phase 3 — Deploy daily:**
- ⬜ Push repo lên GitHub
- ⬜ Add secrets (Telegram bot, chat ID, Sheet URL)
- ⬜ Test `workflow_dispatch` cho daily.yml
- ⬜ Bật cron daily, chạy thử 3 ngày

**Phase 4 — Weekly deep report:**
- ⬜ Viết `gemini_client.py` + test với 1 prompt
- ⬜ Viết `weekly_report.py` với flow 10 bước
- ⬜ Test narrative output từ Gemini
- ⬜ Add workflow `weekly.yml`
- ⬜ Chạy thử 1 tuần → tune prompt Gemini cho output chất lượng nhất

**Phase 5 — Tune & expand:**
- ⬜ Điều chỉnh ngưỡng spike (1.5x có quá nhạy không?)
- ⬜ Thêm so sánh shop mình vs top shop trong niche (dùng `ytrends_analyze_competition`)
- ⬜ Thêm gợi ý pricing cho listing mới (dùng `ytrends_explore_niche` price range)

## Rủi ro & giảm thiểu

| Rủi ro | Giảm thiểu |
|---|---|
| IP GitHub Actions bị Etsy/eBay chặn | Random UA, delay 3–5s, retry, alert nếu 429/403 |
| HTML shop đổi selector | Parse cả CSS selector + regex fallback, alert khi fail |
| Sheet URL sai/offline | Catch exception, skip phần keyword, vẫn gửi 2 phần còn lại |
| Data file conflict khi 2 workflow chạy cùng lúc | Daily 01:00 UTC, weekly 02:00 UTC — cách 1 tiếng, không xung đột |
| GitHub cron delay | Documented, không sửa được — chấp nhận ±15 phút |

## Ngân sách chi phí

**0 đồng** — tất cả chạy trên GitHub Actions free tier:
- 2000 phút/tháng free
- Daily job ~2 phút × 30 ngày = 60 phút
- Weekly job ~3 phút × 4 tuần = 12 phút
- Tổng: ~72 phút/tháng (3.6% quota)
