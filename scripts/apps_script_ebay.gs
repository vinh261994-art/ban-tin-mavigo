/**
 * eBay sales fetcher — chạy trong Google Sheets qua Apps Script trigger.
 *
 * Đọc shop list từ tab `shops_ebay`, fetch từng /usr/ hoặc /str/ page bằng
 * UrlFetchApp (Google IP, US-region — không bị Akamai), trích "items sold"
 * cumulative, append vào tab `Data` cùng schema với Etsy fetcher để Python
 * bulletin đọc cả Etsy + eBay đồng bộ.
 *
 * SETUP:
 *   1. Mở Sheet → Extensions → Apps Script
 *   2. Tạo file mới `ebay.gs`, paste toàn bộ nội dung này vào
 *   3. Save (Ctrl+S), chọn function `fetchEbayAll` → click Run
 *      → Cấp quyền `UrlFetchApp` khi Google hỏi
 *   4. Triggers (icon đồng hồ bên trái) → Add Trigger:
 *        - Function: fetchEbayAll
 *        - Event source: Time-driven
 *        - Type: Day timer
 *        - Time: 6am-7am (GMT+7) — chạy trước khi Python đọc lúc 8:13 VN
 *
 * SCHEMA tab `Data` (phải khớp với Etsy fetcher hiện có):
 *   Cột bắt buộc: Shop | Date | Sales_Total | Sales_Daily | Fetch_Status
 *   (các cột khác tuỳ Etsy script đã có — script này chỉ ghi 5 cột trên,
 *   các cột còn lại để trống)
 *
 * SCHEMA tab `shops_ebay`:
 *   Shop | URL | Active?
 *   (Active để trống = active; "FALSE"/"NO"/"0" = bỏ qua)
 */

const SHEET_TAB_SHOPS = 'shops_ebay';
const SHEET_TAB_DATA  = 'Data';

// Khớp với scripts/shop_tracker.py cũ — bold number ngay trước "items sold"
const EBAY_PATTERN_PRIMARY = /"text"\s*:\s*"([\d,]+)"\s*,\s*"styles"\s*:\s*\["BOLD"\]\s*\}\s*,\s*\{\s*"_type"\s*:\s*"TextSpan"\s*,\s*"text"\s*:\s*"\s*items sold"/;
const EBAY_PATTERN_FALLBACK = /([\d,]+)\s*items sold/i;

// Marker cho seller chưa từng bán → ghi 0 thay vì "parse failed"
const EBAY_INACTIVE_MARKERS = [
  'No active listings',
  '"totalFeedback":0',
  '"totalFeedback":"0"',
];

function fetchEbayAll() {
  const ss = SpreadsheetApp.getActive();
  const shopsSheet = ss.getSheetByName(SHEET_TAB_SHOPS);
  const dataSheet  = ss.getSheetByName(SHEET_TAB_DATA);
  if (!shopsSheet) throw new Error('Tab "' + SHEET_TAB_SHOPS + '" không tồn tại');
  if (!dataSheet)  throw new Error('Tab "' + SHEET_TAB_DATA + '" không tồn tại');

  const today = Utilities.formatDate(new Date(), 'GMT+7', 'yyyy-MM-dd');

  // Đọc shop list
  const shopRows = shopsSheet.getDataRange().getValues();
  const shopHeader = shopRows[0].map(function(h) { return String(h).trim().toLowerCase(); });
  const colShop   = shopHeader.indexOf('shop');
  const colUrl    = shopHeader.indexOf('url');
  const colActive = shopHeader.indexOf('active');
  if (colShop === -1 || colUrl === -1) {
    throw new Error('Tab shops_ebay thiếu cột Shop hoặc URL');
  }

  // Đọc header tab Data để biết index từng cột
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

  // Build last-known total per eBay shop để tính daily delta
  const lastTotals = lastTotalsByShop_(dataSheet, idxShop, idxTotal, idxStatus);

  let ok = 0, fail = 0;
  for (let r = 1; r < shopRows.length; r++) {
    const shop = String(shopRows[r][colShop] || '').trim();
    const url  = String(shopRows[r][colUrl]  || '').trim();
    const active = colActive === -1 ? true : isTruthy_(shopRows[r][colActive]);
    if (!shop || !url || !active) continue;

    let total = null, status = '';
    try {
      const html = fetchHtml_(url);
      total = parseEbaySales_(html);
      if (total === null) {
        if (isInactiveSeller_(html)) {
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
    if (total !== null && typeof prev === 'number') {
      daily = total - prev;
    }

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

function fetchHtml_(url) {
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
}

function parseEbaySales_(html) {
  let m = html.match(EBAY_PATTERN_PRIMARY);
  if (m) return parseInt(m[1].replace(/,/g, ''), 10);
  m = html.match(EBAY_PATTERN_FALLBACK);
  if (m) return parseInt(m[1].replace(/,/g, ''), 10);
  return null;
}

function isInactiveSeller_(html) {
  // Page render xong nhưng seller chưa từng bán → dùng marker thay vì "parse failed"
  if (html.indexOf('PRESENCE_INFORMATION_MODULE') === -1 &&
      html.indexOf('profileModule') === -1) {
    return false;
  }
  for (let i = 0; i < EBAY_INACTIVE_MARKERS.length; i++) {
    if (html.indexOf(EBAY_INACTIVE_MARKERS[i]) !== -1) return true;
  }
  return false;
}

function isTruthy_(v) {
  const s = String(v == null ? '' : v).trim().toUpperCase();
  if (!s) return true;
  return ['TRUE', '1', 'YES', 'Y', 'T', 'X', '✓'].indexOf(s) !== -1;
}

function lastTotalsByShop_(dataSheet, idxShop, idxTotal, idxStatus) {
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
      last[shop] = total;  // overwrite — row sau ghi đè row trước (chronological)
    }
  }
  return last;
}
