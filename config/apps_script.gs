/**
 * ==========================================================
 *  MAVIGO — Google Apps Script
 * ==========================================================
 *  Đọc shop từ tab "shops", fetch Etsy/eBay qua Google IP,
 *  ghi sale vào tab "sales". Dựng trigger chạy hàng ngày 7h VN.
 *
 *  CÀI ĐẶT (5 phút):
 *    1. Mở Sheet "Mavigo shop"
 *    2. Extensions → Apps Script
 *    3. Xóa code mẫu, paste toàn bộ file này
 *    4. Ctrl+S để lưu
 *    5. Chạy thử: chọn hàm `scrapeAllShops` → nút Run
 *       (lần đầu sẽ hỏi quyền — Allow)
 *    6. Đặt trigger: icon đồng hồ bên trái → Add Trigger
 *         Function: scrapeAllShops
 *         Event source: Time-driven
 *         Type: Day timer
 *         Time: 7am to 8am (VN timezone)
 *
 *  Sheet phải có 2 tab:
 *    - "shops": platform | name | url | active   (đã có)
 *    - "sales": date | platform | name | total_sales | error
 *              (script tự tạo nếu chưa có)
 * ==========================================================
 */

const ETSY_PATTERNS = [
  />\s*([\d,]+)\s*Sales?\s*</i,
  /"transaction_sold_count"\s*:\s*(\d+)/,
  /"num_sold"\s*:\s*(\d+)/,
];

const EBAY_PATTERNS = [
  /"text"\s*:\s*"([\d,]+)"\s*,\s*"styles"\s*:\s*\["BOLD"\][^{]*\{\s*"_type"\s*:\s*"TextSpan"\s*,\s*"text"\s*:\s*"\s*items sold"/,
  /([\d,]+)\s*items sold/i,
];

const USER_AGENTS = [
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
  'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
];

function scrapeAllShops() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const shopsSheet = ss.getSheetByName('shops') || ss.getSheets()[0];
  let salesSheet = ss.getSheetByName('sales');
  if (!salesSheet) {
    salesSheet = ss.insertSheet('sales');
    salesSheet.appendRow(['date', 'platform', 'name', 'total_sales', 'error']);
    salesSheet.setFrozenRows(1);
  }

  const shops = _loadShops(shopsSheet);
  const today = Utilities.formatDate(new Date(), 'UTC', 'yyyy-MM-dd');
  console.log(`[mavigo] ${today} · scraping ${shops.length} shops`);

  const rows = [];
  shops.forEach((shop, i) => {
    const r = _scrapeShop(shop.platform, shop.url);
    const marker = r.error ? `⚠ ${r.error}` : `total=${r.total}`;
    console.log(`  [${i + 1}/${shops.length}] ${shop.platform} ${shop.name} ${marker}`);
    rows.push([today, shop.platform, shop.name, r.total || '', r.error || '']);
    if (i < shops.length - 1) {
      Utilities.sleep(3000 + Math.floor(Math.random() * 3000));
    }
  });

  if (rows.length) {
    const startRow = salesSheet.getLastRow() + 1;
    salesSheet.getRange(startRow, 1, rows.length, 5).setValues(rows);
  }
  console.log(`[mavigo] done — ${rows.length} rows written to 'sales'`);
}

function _loadShops(sheet) {
  const data = sheet.getDataRange().getValues();
  if (data.length < 2) return [];
  const headers = data[0].map(h => String(h).trim().toLowerCase());
  const iPlatform = headers.indexOf('platform');
  const iName = headers.indexOf('name');
  const iUrl = headers.indexOf('url');
  const iActive = headers.indexOf('active');
  if (iPlatform < 0 || iUrl < 0) {
    throw new Error("Tab 'shops' thiếu cột platform hoặc url");
  }

  const out = [];
  for (let i = 1; i < data.length; i++) {
    const row = data[i];
    const platform = String(row[iPlatform] || '').toLowerCase().trim();
    const url = String(row[iUrl] || '').trim();
    const name = String(iName >= 0 ? row[iName] : '').trim();
    const activeRaw = iActive >= 0 ? String(row[iActive]).toUpperCase().trim() : '';
    const active = activeRaw === '' || ['TRUE', '1', 'YES', 'Y'].includes(activeRaw);

    if (!platform || !url) continue;
    if (platform !== 'etsy' && platform !== 'ebay') continue;
    if (!active) continue;
    out.push({ platform: platform, name: name || url.split('/').filter(Boolean).pop(), url: url });
  }
  return out;
}

function _scrapeShop(platform, url) {
  const ua = USER_AGENTS[Math.floor(Math.random() * USER_AGENTS.length)];
  let html;
  try {
    const resp = UrlFetchApp.fetch(url, {
      muteHttpExceptions: true,
      followRedirects: true,
      headers: {
        'User-Agent': ua,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Upgrade-Insecure-Requests': '1',
      },
    });
    const code = resp.getResponseCode();
    if (code !== 200) return { total: null, error: `HTTP ${code}` };
    html = resp.getContentText();
  } catch (e) {
    return { total: null, error: `fetch: ${e.message || e}` };
  }

  const patterns = platform === 'etsy' ? ETSY_PATTERNS : EBAY_PATTERNS;
  for (const pat of patterns) {
    const m = html.match(pat);
    if (m && m[1]) {
      return { total: parseInt(m[1].replace(/,/g, ''), 10), error: null };
    }
  }
  return { total: null, error: 'parse failed' };
}

/** Helper: clean rows older than 60 days from 'sales' tab to keep it tidy. */
function pruneOldSales(keepDays) {
  if (!keepDays) keepDays = 60;
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sh = ss.getSheetByName('sales');
  if (!sh || sh.getLastRow() < 2) return;
  const cutoff = new Date();
  cutoff.setDate(cutoff.getDate() - keepDays);
  const cutoffStr = Utilities.formatDate(cutoff, 'UTC', 'yyyy-MM-dd');
  const data = sh.getDataRange().getValues();
  const kept = [data[0]];
  for (let i = 1; i < data.length; i++) {
    if (String(data[i][0]) >= cutoffStr) kept.push(data[i]);
  }
  sh.clearContents();
  sh.getRange(1, 1, kept.length, kept[0].length).setValues(kept);
  sh.setFrozenRows(1);
  console.log(`[mavigo] pruned, kept ${kept.length - 1} rows`);
}
