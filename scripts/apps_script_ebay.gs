/**
 * eBay sales fetcher — chạy trong Google Sheets qua Apps Script trigger.
 *
 * AN TOÀN với script Etsy có sẵn: mọi const/helper đều nằm trong scope của
 * fetchEbayAll, KHÔNG khai báo gì ở global ngoài entry function. Có thể paste
 * vào file mới `ebay.gs` cùng project mà không sợ trùng tên với Etsy script.
 *
 * SETUP:
 *   1. Sheet → Extensions → Apps Script → File menu → "+" → Script
 *      → đặt tên `ebay.gs` → paste toàn bộ nội dung này vào
 *   2. Save (Ctrl+S). Nếu báo "Identifier X already declared" → có nghĩa
 *      script Etsy đã có biến/hàm cùng tên `fetchEbayAll` (xác suất rất thấp);
 *      đổi tên `fetchEbayAll` thành `fetchEbayAllV2` ở cả 2 chỗ trong file này.
 *   3. Trên thanh trên, chọn function `fetchEbayAll` → click Run
 *      → cấp quyền `UrlFetchApp` + `SpreadsheetApp` khi Google hỏi
 *   4. Triggers (icon đồng hồ bên trái) → Add Trigger:
 *        - Function: fetchEbayAll
 *        - Event source: Time-driven
 *        - Type: Day timer
 *        - Time: 6am-7am (GMT+7) — chạy trước Python 8:13 VN
 *
 * SCHEMA tab `Data` (đã khớp với Etsy fetcher hiện có):
 *   Shop | Date | Sales_Total | Sales_Daily | ... | Fetch_Status
 *   (script này chỉ ghi 5 cột trên + để trống các cột khác)
 *
 * SCHEMA tab `shops_ebay`:
 *   Shop | URL | Active?  (Active để trống = active)
 */

/**
 * Debug helper: fetch 1 URL, in HTML stats để biết pattern thực tế eBay trả.
 * Cách dùng: chọn function `debugEbayOne` ở dropdown → Run → mở Logger
 * (View → Logs hoặc tab "Nhật ký thực thi"). Paste output cho dev.
 */
function debugEbayOne() {
  const url = 'https://www.ebay.com/usr/stephanie9121';  // đổi URL nếu cần
  const r = UrlFetchApp.fetch(url, {
    muteHttpExceptions: true,
    followRedirects: true,
    headers: {
      'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
      'Accept-Language': 'en-US,en;q=0.9',
      'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    },
  });
  const html = r.getContentText();
  Logger.log('URL: ' + url);
  Logger.log('HTTP: ' + r.getResponseCode());
  Logger.log('Length: ' + html.length);
  Logger.log('Has "items sold": ' + (html.indexOf('items sold') !== -1));
  Logger.log('Has "Items Sold": ' + (html.indexOf('Items Sold') !== -1));
  Logger.log('Has "sold": count=' + (html.match(/sold/gi) || []).length);
  Logger.log('Has "PRESENCE_INFORMATION_MODULE": ' + (html.indexOf('PRESENCE_INFORMATION_MODULE') !== -1));
  Logger.log('Has "profileModule": ' + (html.indexOf('profileModule') !== -1));
  Logger.log('Has "Robot or human": ' + (html.indexOf('Robot or human') !== -1));
  Logger.log('Has "Pardon Our Interruption": ' + (html.indexOf('Pardon Our Interruption') !== -1));
  Logger.log('Has "captcha": ' + (html.toLowerCase().indexOf('captcha') !== -1));

  // In các đoạn 200 char xung quanh chữ "sold" đầu tiên (case-insensitive)
  const lower = html.toLowerCase();
  let i = lower.indexOf('sold'), printed = 0;
  while (i !== -1 && printed < 5) {
    const start = Math.max(0, i - 100);
    const end = Math.min(html.length, i + 100);
    Logger.log('  CTX[' + printed + '] @' + i + ': ...' + html.substring(start, end).replace(/\s+/g, ' ') + '...');
    i = lower.indexOf('sold', i + 4);
    printed++;
  }
  if (printed === 0) Logger.log('  KHÔNG có chữ "sold" trong HTML — chắc bị block/captcha');
}

function fetchEbayAll() {
  // ===== Config (local, không leak ra global) =====
  const TAB_SHOPS = 'shops_ebay';
  const TAB_DATA  = 'Data';

  const PATTERN_PRIMARY = /"text"\s*:\s*"([\d,]+)"\s*,\s*"styles"\s*:\s*\["BOLD"\]\s*\}\s*,\s*\{\s*"_type"\s*:\s*"TextSpan"\s*,\s*"text"\s*:\s*"\s*items sold"/;
  const PATTERN_FALLBACK = /([\d,]+)\s*items sold/i;
  const INACTIVE_MARKERS = [
    'No active listings',
    '"totalFeedback":0',
    '"totalFeedback":"0"',
  ];
  const TRUTHY = ['TRUE', '1', 'YES', 'Y', 'T', 'X', '✓'];

  // ===== Helpers (local closures) =====
  const isTruthy = function(v) {
    const s = String(v == null ? '' : v).trim().toUpperCase();
    if (!s) return true;  // ô trống = active
    return TRUTHY.indexOf(s) !== -1;
  };

  const fetchHtml = function(url) {
    const r = UrlFetchApp.fetch(url, {
      muteHttpExceptions: true,
      followRedirects: true,
      headers: {
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
      },
    });
    const code = r.getResponseCode();
    if (code >= 400) throw new Error('HTTP ' + code);
    return r.getContentText();
  };

  const parseEbaySales = function(html) {
    let m = html.match(PATTERN_PRIMARY);
    if (m) return parseInt(m[1].replace(/,/g, ''), 10);
    m = html.match(PATTERN_FALLBACK);
    if (m) return parseInt(m[1].replace(/,/g, ''), 10);
    return null;
  };

  const isInactiveSeller = function(html) {
    if (html.indexOf('PRESENCE_INFORMATION_MODULE') === -1 &&
        html.indexOf('profileModule') === -1) {
      return false;
    }
    for (let i = 0; i < INACTIVE_MARKERS.length; i++) {
      if (html.indexOf(INACTIVE_MARKERS[i]) !== -1) return true;
    }
    return false;
  };

  const lastTotalsByShop = function(dataSheet, idxShop, idxTotal, idxStatus) {
    const last = {};
    const values = dataSheet.getDataRange().getValues();
    for (let r = 1; r < values.length; r++) {
      const shop = String(values[r][idxShop] || '').trim();
      const totalRaw = values[r][idxTotal];
      const status = String(values[r][idxStatus] || '').trim();
      if (!shop) continue;
      if (status && status.indexOf('OK') !== 0) continue;  // skip lỗi
      const total = Number(totalRaw);
      if (!isNaN(total) && totalRaw !== '' && totalRaw !== null) {
        last[shop] = total;
      }
    }
    return last;
  };

  // ===== Main =====
  const ss = SpreadsheetApp.getActive();
  const shopsSheet = ss.getSheetByName(TAB_SHOPS);
  const dataSheet  = ss.getSheetByName(TAB_DATA);
  if (!shopsSheet) throw new Error('Tab "' + TAB_SHOPS + '" không tồn tại');
  if (!dataSheet)  throw new Error('Tab "' + TAB_DATA + '" không tồn tại');

  const today = Utilities.formatDate(new Date(), 'GMT+7', 'yyyy-MM-dd');

  const shopRows = shopsSheet.getDataRange().getValues();
  const shopHeader = shopRows[0].map(function(h) { return String(h).trim().toLowerCase(); });
  const colShop   = shopHeader.indexOf('shop');
  const colUrl    = shopHeader.indexOf('url');
  const colActive = shopHeader.indexOf('active');
  if (colShop === -1 || colUrl === -1) {
    throw new Error('Tab shops_ebay thiếu cột Shop hoặc URL');
  }

  const dataHeaderRow = dataSheet.getRange(1, 1, 1, dataSheet.getLastColumn()).getValues()[0];
  const dataHeader = dataHeaderRow.map(function(h) { return String(h).trim().toLowerCase(); });
  const idxShop   = dataHeader.indexOf('shop');
  const idxDate   = dataHeader.indexOf('date');
  const idxTotal  = dataHeader.indexOf('sales_total');
  const idxDaily  = dataHeader.indexOf('sales_daily');
  const idxStatus = dataHeader.indexOf('fetch_status');
  if (idxShop === -1 || idxDate === -1 || idxTotal === -1 || idxStatus === -1) {
    throw new Error('Tab Data thiếu cột Shop/Date/Sales_Total/Fetch_Status');
  }

  const lastTotals = lastTotalsByShop(dataSheet, idxShop, idxTotal, idxStatus);

  let ok = 0, fail = 0;
  for (let r = 1; r < shopRows.length; r++) {
    const shop = String(shopRows[r][colShop] || '').trim();
    const url  = String(shopRows[r][colUrl]  || '').trim();
    const active = colActive === -1 ? true : isTruthy(shopRows[r][colActive]);
    if (!shop || !url || !active) continue;

    let total = null, status = '';
    try {
      const html = fetchHtml(url);
      total = parseEbaySales(html);
      if (total === null) {
        if (isInactiveSeller(html)) {
          total = 0;
          status = 'OK 200 (inactive)';
        } else {
          status = 'parse failed';
        }
      } else {
        status = 'OK 200';
      }
    } catch (e) {
      status = ('ERR ' + e.message).substring(0, 80);
    }

    const prev = lastTotals[shop];
    let daily = '';
    if (total !== null && typeof prev === 'number') daily = total - prev;

    const newRow = new Array(dataHeader.length).fill('');
    newRow[idxShop] = shop;
    newRow[idxDate] = today;
    if (total !== null) newRow[idxTotal] = total;
    if (idxDaily !== -1 && daily !== '') newRow[idxDaily] = daily;
    newRow[idxStatus] = status;
    dataSheet.appendRow(newRow);

    if (status.indexOf('OK') === 0) ok++; else fail++;
    Utilities.sleep(1500);  // polite delay giữa các shop
  }

  Logger.log('eBay fetch xong: ' + ok + ' ok, ' + fail + ' fail');
}
