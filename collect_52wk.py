#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import html
import math
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

try:
    import yfinance as yf
except ImportError:  # pragma: no cover
    yf = None


BARCHART_BASE = "https://www.barchart.com"
BARCHART_API = f"{BARCHART_BASE}/proxies/core-api/v1/quotes/get"

PAGES = {
    "high": f"{BARCHART_BASE}/stocks/highs-lows/highs",
    "low": f"{BARCHART_BASE}/stocks/highs-lows/lows",
}

# Barchart serves these pages from a dynamic quotes endpoint. The list names are
# kept in one place so they are easy to update if Barchart renames them.
BARCHART_LISTS = {
    "high": "stocks.us.new_highs_lows.highs.overall.1y",
    "low": "stocks.us.new_highs_lows.lows.overall.1y",
}

BARCHART_FIELDS = ",".join(
    [
        "symbol",
        "symbolName",
        "lastPrice",
        "priceChange",
        "percentChange",
        "volume",
        "highHits1y",
        "highPercent1y",
        "lowPercent1y",
        "tradeTime",
        "symbolCode",
        "symbolType",
        "hasOptions",
    ]
)

COLUMNS = [
    "date",
    "ticker",
    "company_name",
    "latest_price",
    "percent_change",
    "volume",
    "fifty_two_week_percent_high",
    "fifty_two_week_percent_low",
    "market_cap",
    "pe",
    "dividend_yield",
    "sector",
    "earnings_date",
    "type",
]


@dataclass
class BarchartRow:
    date: str
    ticker: str
    company_name: str | None
    latest_price: float | None
    percent_change: float | None
    volume: int | None
    fifty_two_week_percent_high: float | None
    fifty_two_week_percent_low: float | None
    row_type: str


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect Barchart 52-week highs/lows and enrich with Yahoo Finance."
    )
    parser.add_argument("--date", default=dt.date.today().isoformat())
    parser.add_argument("--db", default="data/52wk.sqlite3")
    parser.add_argument("--out", default="public")
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--skip-yahoo", action="store_true")
    parser.add_argument(
        "--render-only",
        action="store_true",
        help="Regenerate HTML from the existing SQLite data without scraping.",
    )
    args = parser.parse_args()

    if yf is None and not args.skip_yahoo and not args.render_only:
        raise SystemExit("yfinance is not installed. Run: pip install -r requirements.txt")

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        init_db(conn)
        if not args.render_only:
            rows: list[dict[str, Any]] = []
            session = barchart_session()
            for row_type in ("high", "low"):
                barchart_rows = fetch_barchart_rows(session, row_type, args.date, args.limit)
                print(f"Fetched {len(barchart_rows)} Barchart {row_type} rows")
                enriched = enrich_rows(barchart_rows, skip_yahoo=args.skip_yahoo)
                rows.extend(enriched)
            upsert_rows(conn, rows)
        archive_rows = get_archive_summary(conn)
        rows_by_date = {
            row["date"]: {
                "high": get_rows(conn, row["date"], "high"),
                "low": get_rows(conn, row["date"], "low"),
            }
            for row in archive_rows
        }

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for archive in archive_rows:
        collection_date = archive["date"]
        high_rows = rows_by_date[collection_date]["high"]
        low_rows = rows_by_date[collection_date]["low"]
        write_page(
            out_dir / f"{collection_date}-highs.html",
            collection_date,
            "New 52-Week Highs",
            high_rows,
            "high",
        )
        write_page(
            out_dir / f"{collection_date}-lows.html",
            collection_date,
            "New 52-Week Lows",
            low_rows,
            "low",
        )
        write_daily_summary(out_dir / f"{collection_date}.html", collection_date, len(high_rows), len(low_rows))

    current_rows = rows_by_date.get(args.date, {"high": [], "low": []})
    write_page(out_dir / "highs.html", args.date, "New 52-Week Highs", current_rows["high"], "high")
    write_page(out_dir / "lows.html", args.date, "New 52-Week Lows", current_rows["low"], "low")
    write_index(out_dir / "index.html", archive_rows)

    print(f"Wrote {db_path}")
    print(f"Rendered {len(archive_rows)} archived date(s)")
    print(f"Wrote {out_dir / 'index.html'}")
    return 0


def barchart_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
        }
    )
    response = session.get(PAGES["high"], timeout=30)
    response.raise_for_status()
    xsrf = session.cookies.get("XSRF-TOKEN")
    if xsrf:
        session.headers["X-XSRF-TOKEN"] = requests.utils.unquote(xsrf)
    return session


def fetch_barchart_rows(
    session: requests.Session, row_type: str, collection_date: str, limit: int
) -> list[BarchartRow]:
    rows: list[BarchartRow] = []
    page = 1
    page_size = min(max(limit, 1), 1000)
    referer = PAGES[row_type]

    while len(rows) < limit:
        params = {
            "lists": BARCHART_LISTS[row_type],
            "fields": BARCHART_FIELDS,
            "orderBy": "symbol",
            "orderDir": "asc",
            "meta": "field.shortName,field.type,field.description,lists.lastUpdate",
            "hasOptions": "true",
            "page": page,
            "limit": page_size,
            "raw": 1,
        }
        response = session.get(
            BARCHART_API,
            params=params,
            headers={"Referer": referer},
            timeout=45,
        )
        if response.status_code in {401, 403}:
            raise RuntimeError(
                f"Barchart rejected the {row_type} request with HTTP {response.status_code}. "
                "The public endpoint may require a current browser token."
            )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") or []
        if not data:
            break

        for item in data:
            rows.append(normalize_barchart_item(item, row_type, collection_date))
            if len(rows) >= limit:
                break

        total = int(payload.get("total") or payload.get("count") or len(rows))
        if len(rows) >= total or len(data) < page_size:
            break
        page += 1
        time.sleep(0.2)

    if not rows:
        raise RuntimeError(
            f"No Barchart rows returned for {row_type}. Check BARCHART_LISTS in collect_52wk.py."
        )
    return rows


def normalize_barchart_item(item: dict[str, Any], row_type: str, collection_date: str) -> BarchartRow:
    raw = item.get("raw") or item
    return BarchartRow(
        date=collection_date,
        ticker=clean_ticker(raw.get("symbol") or item.get("symbol")),
        company_name=raw.get("symbolName") or item.get("symbolName") or item.get("name"),
        latest_price=to_float(field_value(item, raw, "lastPrice")),
        percent_change=to_barchart_percent(field_value(item, raw, "percentChange")),
        volume=to_int(field_value(item, raw, "volume")),
        fifty_two_week_percent_high=to_barchart_percent(
            first_field_value(item, raw, ["highPercent1y", "selectedPeriodHighPercent"])
        ),
        fifty_two_week_percent_low=to_barchart_percent(
            first_field_value(item, raw, ["lowPercent1y", "selectedPeriodLowPercent"])
        ),
        row_type=row_type,
    )


def enrich_rows(rows: list[BarchartRow], skip_yahoo: bool = False) -> list[dict[str, Any]]:
    tickers = [row.ticker for row in rows if row.ticker]
    yahoo_data: dict[str, dict[str, Any]] = {}
    if not skip_yahoo and tickers:
        yahoo_data = fetch_yahoo_data(tickers)

    enriched = []
    for row in rows:
        extra = yahoo_data.get(row.ticker, {})
        enriched.append(
            {
                "date": row.date,
                "ticker": row.ticker,
                "company_name": row.company_name,
                "latest_price": row.latest_price,
                "percent_change": row.percent_change,
                "volume": row.volume,
                "fifty_two_week_percent_high": row.fifty_two_week_percent_high,
                "fifty_two_week_percent_low": row.fifty_two_week_percent_low,
                "market_cap": extra.get("market_cap"),
                "pe": extra.get("pe"),
                "dividend_yield": extra.get("dividend_yield"),
                "sector": extra.get("sector"),
                "earnings_date": extra.get("earnings_date"),
                "type": row.row_type,
            }
        )
    return enriched


def fetch_yahoo_data(tickers: list[str]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for index, ticker in enumerate(tickers, 1):
        yahoo_symbol = ticker.replace(".", "-")
        try:
            stock = yf.Ticker(yahoo_symbol)
            info = stock.get_info()
            result[ticker] = {
                "market_cap": to_int(info.get("marketCap")),
                "pe": to_float(info.get("trailingPE") or info.get("forwardPE")),
                "dividend_yield": normalize_yield(
                    info.get("dividendYield") or info.get("trailingAnnualDividendYield")
                ),
                "sector": info.get("sector"),
                "earnings_date": get_earnings_date(stock, info),
            }
        except Exception as exc:
            print(f"Yahoo lookup failed for {ticker}: {exc}", file=sys.stderr)
            result[ticker] = {}

        if index % 25 == 0:
            time.sleep(1)
    return result


def get_earnings_date(stock: Any, info: dict[str, Any]) -> str | None:
    for key in ("earningsTimestamp", "earningsTimestampStart", "earningsTimestampEnd"):
        value = info.get(key)
        if value:
            try:
                return dt.datetime.fromtimestamp(int(value), tz=dt.UTC).date().isoformat()
            except (TypeError, ValueError, OSError):
                pass

    try:
        dates = stock.get_earnings_dates(limit=1)
        if dates is not None and not dates.empty:
            return dates.index[0].date().isoformat()
    except Exception:
        pass
    return None


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stocks_52wk (
            date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            company_name TEXT,
            latest_price REAL,
            percent_change REAL,
            volume INTEGER,
            fifty_two_week_percent_high REAL,
            fifty_two_week_percent_low REAL,
            market_cap INTEGER,
            pe REAL,
            dividend_yield REAL,
            sector TEXT,
            earnings_date TEXT,
            type TEXT NOT NULL CHECK (type IN ('high', 'low')),
            PRIMARY KEY (date, ticker, type)
        )
        """
    )


def upsert_rows(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    insert_sql = (
        f"INSERT INTO stocks_52wk ({','.join(COLUMNS)}) "
        f"VALUES ({','.join('?' for _ in COLUMNS)})"
    )
    update_columns = [column for column in COLUMNS if column not in {"date", "ticker", "type"}]
    update_sql = (
        f"UPDATE stocks_52wk SET {','.join(f'{column} = ?' for column in update_columns)} "
        "WHERE date = ? AND ticker = ? AND type = ?"
    )

    for row in rows:
        cursor = conn.execute(
            update_sql,
            [row.get(column) for column in update_columns]
            + [row.get("date"), row.get("ticker"), row.get("type")],
        )
        if cursor.rowcount == 0:
            conn.execute(insert_sql, [row.get(column) for column in COLUMNS])
    conn.commit()


def get_rows(conn: sqlite3.Connection, collection_date: str, row_type: str) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    cursor = conn.execute(
        f"SELECT {','.join(COLUMNS)} FROM stocks_52wk WHERE date = ? AND type = ? ORDER BY ticker",
        (collection_date, row_type),
    )
    return [dict(row) for row in cursor.fetchall()]


def get_archive_summary(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    cursor = conn.execute(
        """
        SELECT
            date,
            SUM(CASE WHEN type = 'high' THEN 1 ELSE 0 END) AS high_count,
            SUM(CASE WHEN type = 'low' THEN 1 ELSE 0 END) AS low_count
        FROM stocks_52wk
        GROUP BY date
        ORDER BY date DESC
        """
    )
    return [dict(row) for row in cursor.fetchall()]


def write_index(path: Path, archive_rows: list[dict[str, Any]]) -> None:
    latest = archive_rows[0]["date"] if archive_rows else "-"
    archive_body = "\n".join(archive_row(row) for row in archive_rows)
    path.write_text(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>52-Week Highs/Lows</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <main class="wrap">
    <h1>52-Week Highs/Lows</h1>
    <p class="meta">Latest run: {html.escape(str(latest))} · Archived days: {len(archive_rows)}</p>
    <div class="table-shell archive">
      <table>
        <thead>
          <tr><th>Date</th><th>Highs</th><th>Lows</th><th>Daily Page</th><th>Highs Page</th><th>Lows Page</th></tr>
        </thead>
        <tbody>
{archive_body}
        </tbody>
      </table>
    </div>
  </main>
</body>
</html>
""",
        encoding="utf-8",
    )
    write_css(path.parent / "style.css")


def write_daily_summary(path: Path, collection_date: str, high_count: int, low_count: int) -> None:
    path.write_text(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>52-Week Highs/Lows - {html.escape(collection_date)}</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <main class="wrap narrow">
    <div class="topbar">
      <div>
        <h1>52-Week Highs/Lows</h1>
        <p class="meta">Date: {html.escape(collection_date)}</p>
      </div>
      <nav><a href="index.html">Index</a></nav>
    </div>
    <nav class="links">
      <a href="{html.escape(collection_date)}-highs.html">New highs ({high_count})</a>
      <a href="{html.escape(collection_date)}-lows.html">New lows ({low_count})</a>
    </nav>
  </main>
</body>
</html>
""",
        encoding="utf-8",
    )
    write_css(path.parent / "style.css")


def archive_row(row: dict[str, Any]) -> str:
    collection_date = str(row.get("date") or "")
    high_count = int(row.get("high_count") or 0)
    low_count = int(row.get("low_count") or 0)
    escaped_date = html.escape(collection_date)
    return (
        "          <tr>"
        f"<td>{escaped_date}</td>"
        f"<td>{high_count:,}</td>"
        f"<td>{low_count:,}</td>"
        f'<td><a href="{escaped_date}.html">Open</a></td>'
        f'<td><a href="{escaped_date}-highs.html">Highs</a></td>'
        f'<td><a href="{escaped_date}-lows.html">Lows</a></td>'
        "</tr>"
    )


def write_page(
    path: Path,
    collection_date: str,
    title: str,
    rows: list[dict[str, Any]],
    page_type: str,
) -> None:
    write_css(path.parent / "style.css")
    rows = sorted(rows, key=market_cap_sort_value, reverse=True)
    headers = table_headers(page_type)
    body = "\n".join(table_row(row, page_type) for row in rows)
    path.write_text(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)} - {html.escape(collection_date)}</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <main class="wrap">
    <div class="topbar">
      <div>
        <h1>{html.escape(title)}</h1>
        <p class="meta">Date: {html.escape(collection_date)} · Rows: {len(rows)}</p>
      </div>
      <nav><a href="index.html">Index</a></nav>
    </div>
    <div class="table-shell">
      <table id="stock-table">
        <thead><tr>{"".join(f"<th>{html.escape(header)}</th>" for header in headers)}</tr></thead>
        <tbody>
{body}
        </tbody>
      </table>
    </div>
  </main>
  <script src="sort-table.js"></script>
</body>
</html>
""",
        encoding="utf-8",
    )
    write_sort_js(path.parent / "sort-table.js")


def table_headers(page_type: str) -> list[str]:
    if page_type == "low":
        return [
            "Market Cap",
            "Ticker",
            "Company Name",
            "Latest Price",
            "% Change",
            "52W %/High",
            "Volume",
            "P/E",
            "Dividend Yield",
            "Sector",
            "Earnings Date",
            "Type",
            "52W %/Low",
        ]
    return [
        "Market Cap",
        "Ticker",
        "Company Name",
        "Latest Price",
        "% Change",
        "52W %/Low",
        "Volume",
        "P/E",
        "Dividend Yield",
        "Sector",
        "Earnings Date",
        "Type",
        "52W %/High",
    ]


def table_row(row: dict[str, Any], page_type: str) -> str:
    ticker = row.get("ticker") or ""
    ticker_url = f"https://finance.yahoo.com/quote/{quote(ticker.replace('.', '-'))}/"
    if page_type == "low":
        cells = [
            fmt_market_cap(row.get("market_cap")),
            f'<a href="{ticker_url}" target="_blank" rel="noopener">{html.escape(ticker)}</a>',
            fmt(row.get("company_name")),
            fmt_number(row.get("latest_price")),
            fmt_percent(row.get("percent_change")),
            fmt_percent(row.get("fifty_two_week_percent_high")),
            fmt_volume(row.get("volume")),
            fmt_number(row.get("pe")),
            fmt_percent(row.get("dividend_yield")),
            fmt(row.get("sector")),
            fmt(row.get("earnings_date")),
            fmt(row.get("type")),
            fmt_percent(row.get("fifty_two_week_percent_low")),
        ]
        return "          <tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>"

    cells = [
        fmt_market_cap(row.get("market_cap")),
        f'<a href="{ticker_url}" target="_blank" rel="noopener">{html.escape(ticker)}</a>',
        fmt(row.get("company_name")),
        fmt_number(row.get("latest_price")),
        fmt_percent(row.get("percent_change")),
        fmt_percent(row.get("fifty_two_week_percent_low")),
        fmt_volume(row.get("volume")),
        fmt_number(row.get("pe")),
        fmt_percent(row.get("dividend_yield")),
        fmt(row.get("sector")),
        fmt(row.get("earnings_date")),
        fmt(row.get("type")),
        fmt_percent(row.get("fifty_two_week_percent_high")),
    ]
    return "          <tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>"


def market_cap_sort_value(row: dict[str, Any]) -> int:
    return to_int(row.get("market_cap")) or -1


def write_css(path: Path) -> None:
    path.write_text(
        """body {
  margin: 0;
  color: #1f2933;
  background: #f5f7fa;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
.wrap {
  max-width: 1440px;
  margin: 0 auto;
  padding: 28px;
}
.narrow {
  max-width: 720px;
}
.topbar {
  display: flex;
  align-items: end;
  justify-content: space-between;
  gap: 20px;
}
h1 {
  margin: 0 0 6px;
  font-size: 28px;
  font-weight: 700;
}
.meta {
  margin: 0;
  color: #64748b;
}
a {
  color: #0f5e9c;
  text-decoration: none;
}
a:hover {
  text-decoration: underline;
}
.links {
  display: flex;
  gap: 12px;
  margin-top: 24px;
}
.links a,
.topbar nav a {
  border: 1px solid #cbd5e1;
  border-radius: 6px;
  padding: 8px 12px;
  background: #fff;
}
.table-shell {
  margin-top: 22px;
  overflow: auto;
  border: 1px solid #d9e2ec;
  border-radius: 8px;
  background: #fff;
}
table {
  width: 100%;
  min-width: 1280px;
  border-collapse: collapse;
}
.archive table {
  min-width: 720px;
}
th,
td {
  padding: 9px 10px;
  border-bottom: 1px solid #e6edf3;
  text-align: left;
  white-space: nowrap;
  font-size: 13px;
}
.archive th,
.archive td {
  text-align: left;
}
.archive th:nth-child(2),
.archive th:nth-child(3),
.archive td:nth-child(2),
.archive td:nth-child(3) {
  text-align: right;
}
th {
  position: sticky;
  top: 0;
  z-index: 1;
  color: #334155;
  background: #eef3f8;
  cursor: pointer;
  user-select: none;
}
th::after {
  content: " ↕";
  color: #8796a8;
  font-size: 11px;
}
#stock-table td:nth-child(1),
#stock-table td:nth-child(4),
#stock-table td:nth-child(5),
#stock-table td:nth-child(6),
#stock-table td:nth-child(7),
#stock-table td:nth-child(8),
#stock-table td:nth-child(9),
#stock-table td:nth-child(13) {
  text-align: right;
}
tbody tr:hover {
  background: #f8fafc;
}
@media (max-width: 720px) {
  .wrap {
    padding: 18px;
  }
  .topbar {
    align-items: start;
    flex-direction: column;
  }
}
""",
        encoding="utf-8",
    )


def write_sort_js(path: Path) -> None:
    path.write_text(
        """const table = document.querySelector("#stock-table");
if (table) {
  const tbody = table.tBodies[0];
  table.querySelectorAll("th").forEach((th, index) => {
    th.addEventListener("click", () => {
      const direction = th.dataset.direction === "asc" ? "desc" : "asc";
      sortColumn(index, direction);
    });
  });
  sortColumn(0, "desc");
}

function sortColumn(index, direction) {
  table.querySelectorAll("th").forEach(header => delete header.dataset.direction);
  table.querySelectorAll("th")[index].dataset.direction = direction;
  const rows = Array.from(table.tBodies[0].rows);
  rows.sort((a, b) => compare(a.cells[index].innerText, b.cells[index].innerText, direction));
  rows.forEach(row => table.tBodies[0].appendChild(row));
}

function compare(left, right, direction) {
  const leftValue = parseValue(left);
  const rightValue = parseValue(right);
  let result;
  if (typeof leftValue === "number" && typeof rightValue === "number") {
    result = leftValue - rightValue;
  } else {
    result = String(leftValue).localeCompare(String(rightValue), undefined, {numeric: true});
  }
  return direction === "asc" ? result : -result;
}

function parseValue(value) {
  const trimmed = value.trim();
  if (!trimmed || trimmed === "-") return "";
  const suffixMatch = trimmed.match(/^([-+]?[\\d,.]+)\\s*([kKmMbBtT])$/);
  if (suffixMatch) {
    const multiplier = {k: 1_000, m: 1_000_000, b: 1_000_000_000, t: 1_000_000_000_000}[suffixMatch[2].toLowerCase()];
    return Number(suffixMatch[1].replace(/,/g, "")) * multiplier;
  }
  const numeric = Number(trimmed.replace(/[$,%]/g, "").replace(/,/g, ""));
  return Number.isFinite(numeric) ? numeric : trimmed.toLowerCase();
}
""",
        encoding="utf-8",
    )


def clean_ticker(value: Any) -> str:
    return str(value or "").strip().upper()


def to_float(value: Any) -> float | None:
    if value in (None, "", "N/A"):
        return None
    try:
        number = float(str(value).replace(",", "").replace("%", "").replace("$", "").replace("+", ""))
        if math.isnan(number) or math.isinf(number):
            return None
        return number
    except (TypeError, ValueError):
        return None


def to_int(value: Any) -> int | None:
    if value in (None, "", "N/A"):
        return None
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return None


def normalize_yield(value: Any) -> float | None:
    number = to_float(value)
    if number is None:
        return None
    return number * 100 if 0 < abs(number) <= 0.25 else number


def field_value(item: dict[str, Any], raw: dict[str, Any], key: str) -> Any:
    return item.get(key) if item.get(key) not in (None, "") else raw.get(key)


def first_field_value(item: dict[str, Any], raw: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        value = field_value(item, raw, key)
        if value not in (None, ""):
            return value
    return None


def to_barchart_percent(value: Any) -> float | None:
    number = to_float(value)
    if number is None:
        return None
    if isinstance(value, str) and "%" in value:
        return number
    return number * 100 if 0 < abs(number) <= 1 else number


def fmt(value: Any) -> str:
    return html.escape(str(value)) if value not in (None, "") else "-"


def fmt_number(value: Any) -> str:
    number = to_float(value)
    return f"{number:,.2f}" if number is not None else "-"


def fmt_percent(value: Any) -> str:
    number = to_float(value)
    return f"{number:,.2f}%" if number is not None else "-"


def fmt_int(value: Any) -> str:
    number = to_int(value)
    return f"{number:,}" if number is not None else "-"


def fmt_market_cap(value: Any) -> str:
    number = to_int(value)
    return f"{number // 1_000_000:,} M" if number is not None else "-"


def fmt_volume(value: Any) -> str:
    number = to_int(value)
    return f"{number // 1_000:,}k" if number is not None else "-"


if __name__ == "__main__":
    raise SystemExit(main())
