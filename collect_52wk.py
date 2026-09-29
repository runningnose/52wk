#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
import os
import platform
import random
import shutil
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
BARCHART_BROWSER_EVALUATE_ATTEMPTS = 3
SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR / "env.txt"
DEFAULT_DB_PATH = SCRIPT_DIR / "data" / "52wk.sqlite3"

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


class BarchartAccessError(RuntimeError):
    """Raised when Barchart rejects an otherwise valid public-page request."""


def load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"{path}:{line_number}: expected KEY=VALUE")

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError(f"{path}:{line_number}: setting name cannot be empty")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def resolve_script_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else SCRIPT_DIR / path


def main() -> int:
    try:
        settings = load_env_file(ENV_FILE)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    configured_output = settings.get("HTML_OUTPUT_DIR", "public")
    if not configured_output:
        raise SystemExit(f"{ENV_FILE}: HTML_OUTPUT_DIR cannot be empty")

    parser = argparse.ArgumentParser(
        description="Collect Barchart 52-week highs/lows and enrich with Yahoo Finance."
    )
    parser.add_argument("--date", default=dt.date.today().isoformat())
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument(
        "--out",
        default=str(resolve_script_path(configured_output)),
        help="HTML output directory; overrides HTML_OUTPUT_DIR in env.txt.",
    )
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--skip-yahoo", action="store_true")
    parser.add_argument(
        "--random-delay",
        action="store_true",
        help="Wait a random 1 to 59 minutes before starting.",
    )
    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument(
        "--try-barchart-api",
        action="store_true",
        help=(
            "Try Barchart's direct HTTP API first, then fall back to the browser "
            "on HTTP 401/403. By default the direct API is skipped."
        ),
    )
    source_group.add_argument(
        "--barchart-source",
        choices=("auto", "http", "browser"),
        default=None,
        help=(
            "Explicit Barchart source selection retained for backward compatibility."
        ),
    )
    parser.add_argument(
        "--render-only",
        action="store_true",
        help="Regenerate HTML from the existing SQLite data without scraping.",
    )
    args = parser.parse_args()

    if args.random_delay:
        random_startup_delay()

    if yf is None and not args.skip_yahoo and not args.render_only:
        raise SystemExit("yfinance is not installed. Run: pip install -r requirements.txt")

    db_path = resolve_script_path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        init_db(conn)
        if not args.render_only:
            rows: list[dict[str, Any]] = []
            barchart_batches: dict[str, list[BarchartRow]] = {}
            session: requests.Session | None = None
            browser_client: BarchartBrowserClient | None = None
            active_source = args.barchart_source or (
                "auto" if args.try_barchart_api else "browser"
            )
            try:
                if active_source != "browser":
                    try:
                        session = barchart_session()
                    except BarchartAccessError:
                        if active_source == "http":
                            raise
                        print(
                            "Barchart rejected the HTTP page request; using anonymous browser fallback.",
                            file=sys.stderr,
                        )
                        active_source = "browser"

                for row_type in ("high", "low"):
                    if active_source == "browser":
                        if browser_client is None:
                            browser_client = BarchartBrowserClient()
                        barchart_rows = browser_client.fetch_rows(
                            row_type, args.date, args.limit
                        )
                    else:
                        assert session is not None
                        try:
                            barchart_rows = fetch_barchart_rows(
                                session, row_type, args.date, args.limit
                            )
                        except BarchartAccessError:
                            if active_source == "http":
                                raise
                            print(
                                "Barchart rejected the HTTP API request; using anonymous "
                                "browser fallback.",
                                file=sys.stderr,
                            )
                            active_source = "browser"
                            browser_client = BarchartBrowserClient()
                            barchart_rows = browser_client.fetch_rows(
                                row_type, args.date, args.limit
                            )

                    print(
                        f"Fetched {len(barchart_rows)} Barchart {row_type} rows "
                        f"via {active_source}"
                    )
                    barchart_batches[row_type] = barchart_rows
            finally:
                if browser_client is not None:
                    browser_client.close()

            # Fetch both Barchart lists before starting the slower Yahoo lookups.
            # This keeps the browser from sitting idle long enough for Barchart's
            # page scripts to redirect or reload it between the two lists.
            for row_type in ("high", "low"):
                enriched = enrich_rows(
                    barchart_batches[row_type], skip_yahoo=args.skip_yahoo
                )
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

    out_dir = resolve_script_path(args.out)
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


def random_startup_delay() -> None:
    delay_minutes = random.randint(1, 59)
    print(
        f"Delaying start for {delay_minutes} minute(s).",
        flush=True,
    )
    time.sleep(delay_minutes * 60)


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
    if response.status_code in {401, 403}:
        raise BarchartAccessError(
            f"Barchart rejected the public page request with HTTP {response.status_code}."
        )
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
        params = barchart_request_params(row_type, page, page_size)
        response = session.get(
            BARCHART_API,
            params=params,
            headers={"Referer": referer},
            timeout=45,
        )
        if response.status_code in {401, 403}:
            raise BarchartAccessError(
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


def barchart_request_params(row_type: str, page: int, page_size: int) -> dict[str, Any]:
    return {
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


class BarchartBrowserClient:
    """Fetch Barchart API data from an anonymous, JavaScript-capable browser context."""

    def __init__(self) -> None:
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "The Barchart browser fallback requires Playwright. Run: "
                "pip install -r requirements.txt"
            ) from exc

        executable = find_browser_executable()
        self._playwright_error = PlaywrightError
        self._playwright = sync_playwright().start()
        self._browser = None
        self._context = None

        launch_options: dict[str, Any] = {
            "headless": True,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if executable:
            launch_options["executable_path"] = executable

        try:
            self._browser = self._playwright.chromium.launch(**launch_options)
        except PlaywrightError as exc:
            self._playwright.stop()
            location_hint = (
                "Set BARCHART_BROWSER_EXECUTABLE to Chrome/Chromium, or run: "
                "python -m playwright install chromium"
            )
            raise RuntimeError(f"Could not launch a browser. {location_hint}") from exc

        user_agent = browser_user_agent(self._browser.version)
        self._context = self._browser.new_context(
            user_agent=user_agent,
            locale="en-US",
            viewport={"width": 1440, "height": 1000},
        )

    def fetch_rows(
        self, row_type: str, collection_date: str, limit: int
    ) -> list[BarchartRow]:
        assert self._context is not None
        page = self._context.new_page()
        referer = f"{PAGES[row_type]}?viewName=main"
        rows: list[BarchartRow] = []
        page_size = min(max(limit, 1), 1000)

        try:
            response = page.goto(referer, wait_until="domcontentloaded", timeout=60_000)
            if response is None or response.status in {401, 403}:
                status = response.status if response is not None else "unknown"
                raise BarchartAccessError(
                    f"Barchart rejected the anonymous browser page with HTTP {status}."
                )
            if not response.ok:
                raise RuntimeError(
                    f"Barchart page returned HTTP {response.status} in the browser fallback."
                )

            page_number = 1
            while len(rows) < limit:
                params = barchart_request_params(row_type, page_number, page_size)
                result = self._evaluate_api_request(page, params, row_type)

                status = int(result.get("status") or 0)
                if status in {401, 403}:
                    raise BarchartAccessError(
                        f"Barchart rejected the in-browser {row_type} API request with "
                        f"HTTP {status}."
                    )
                if not result.get("ok"):
                    raise RuntimeError(
                        f"Barchart in-browser {row_type} API request failed with HTTP {status}."
                    )

                payload = result.get("payload") or {}
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
                page_number += 1
                page.wait_for_timeout(200)
        except self._playwright_error as exc:
            raise RuntimeError(f"Barchart browser fallback failed: {exc}") from exc
        finally:
            page.close()

        if not rows:
            raise RuntimeError(
                f"No Barchart rows returned for {row_type} in the browser fallback."
            )
        return rows

    def _evaluate_api_request(
        self, page: Any, params: dict[str, Any], row_type: str
    ) -> dict[str, Any]:
        for attempt in range(1, BARCHART_BROWSER_EVALUATE_ATTEMPTS + 1):
            try:
                return page.evaluate(
                    """
                    async ({url, params}) => {
                      const query = new URLSearchParams();
                      for (const [key, value] of Object.entries(params)) {
                        query.set(key, String(value));
                      }
                      const response = await fetch(`${url}?${query.toString()}`, {
                        credentials: "include",
                        headers: {
                          "Accept": "application/json, text/plain, */*",
                          "X-Requested-With": "XMLHttpRequest"
                        }
                      });
                      let payload = null;
                      try {
                        payload = await response.json();
                      } catch (_) {
                        // The Python side will report the status and missing payload.
                      }
                      return {status: response.status, ok: response.ok, payload};
                    }
                    """,
                    {"url": BARCHART_API, "params": params},
                )
            except self._playwright_error as exc:
                if not is_transient_navigation_error(exc):
                    raise
                if attempt == BARCHART_BROWSER_EVALUATE_ATTEMPTS:
                    raise

                delay_ms = 500 * attempt
                print(
                    f"Barchart {row_type} page navigated during its API request; "
                    f"retrying ({attempt}/{BARCHART_BROWSER_EVALUATE_ATTEMPTS - 1}).",
                    file=sys.stderr,
                )
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=15_000)
                except self._playwright_error:
                    # A second navigation may supersede the first one; the delay
                    # below gives the replacement document a chance to settle.
                    pass
                page.wait_for_timeout(delay_ms)

        raise AssertionError("unreachable")

    def close(self) -> None:
        if self._context is not None:
            self._context.close()
            self._context = None
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None


def find_browser_executable() -> str | None:
    configured = os.environ.get("BARCHART_BROWSER_EXECUTABLE")
    if configured:
        path = Path(configured).expanduser()
        if not path.is_file():
            raise RuntimeError(
                f"BARCHART_BROWSER_EXECUTABLE does not point to a file: {path}"
            )
        return str(path)

    command_candidates = (
        "google-chrome-stable",
        "google-chrome",
        "chromium",
        "chromium-browser",
    )
    for command in command_candidates:
        executable = shutil.which(command)
        if executable:
            return executable

    app_candidates = (
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
    )
    for path in app_candidates:
        if path.is_file():
            return str(path)
    return None


def browser_user_agent(browser_version: str) -> str:
    system = platform.system()
    if system == "Darwin":
        platform_token = "Macintosh; Intel Mac OS X 10_15_7"
    elif system == "Windows":
        platform_token = "Windows NT 10.0; Win64; x64"
    else:
        platform_token = "X11; Linux x86_64"
    return (
        f"Mozilla/5.0 ({platform_token}) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{browser_version} Safari/537.36"
    )


def is_transient_navigation_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return (
        "execution context was destroyed" in message
        or "cannot find context with specified id" in message
    )


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
    chart_script = historical_sentiment_script(archive_rows)
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
    <p class="description">Tracks the daily number of stocks reaching new 52-week highs and lows. The high-to-low ratio offers a quick view of market sentiment: lower values suggest fear, while higher values suggest greed.</p>
    <p class="meta">Latest run: {html.escape(str(latest))} · Archived days: {len(archive_rows)}</p>
    <div class="temperature-legend">
      <span class="temperature-title">Market sentiment</span>
      <div class="temperature-key">
        <div class="temperature-labels" aria-hidden="true">
          <span>Fear</span>
          <span>Neutral</span>
          <span>Greed</span>
        </div>
        <div class="temperature-scale" role="img" aria-label="Market sentiment scale from cold blue for lower high-to-low ratios to fire red for higher ratios"></div>
        <canvas id="sentiment-chart" class="sentiment-chart" width="560" height="82" role="img" aria-label="Historical high-to-low ratio trend over time; fear is lower, neutral is centered, and greed is higher"></canvas>
      </div>
    </div>
    <div class="table-shell archive">
      <table>
        <colgroup>
          <col class="archive-date">
          <col class="archive-count">
          <col class="archive-count">
          <col class="archive-ratio">
        </colgroup>
        <thead>
          <tr><th>Date</th><th>Highs</th><th>Lows</th><th>High/Low Ratio</th></tr>
        </thead>
        <tbody>
{archive_body}
        </tbody>
      </table>
    </div>
  </main>
{chart_script}
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
        f'<td><a href="{escaped_date}.html">{escaped_date}</a></td>'
        f'<td><a href="{escaped_date}-highs.html">{high_count:,}</a></td>'
        f'<td><a href="{escaped_date}-lows.html">{low_count:,}</a></td>'
        f"<td>{archive_ratio_indicator(high_count, low_count)}</td>"
        "</tr>"
    )


def archive_ratio_indicator(high_count: int, low_count: int) -> str:
    total = high_count + low_count
    if total == 0:
        return '<span class="ratio-empty">—</span>'

    temperature = ratio_temperature_value(high_count, low_count)
    assert temperature is not None
    if low_count == 0:
        ratio_text = "∞"
    else:
        ratio = high_count / low_count
        ratio_text = f"{ratio:.2f}×"

    if temperature < 0.25:
        temperature_label = "cold"
    elif temperature < 0.5:
        temperature_label = "cool"
    elif temperature == 0.5:
        temperature_label = "balanced"
    elif temperature < 0.75:
        temperature_label = "warm"
    else:
        temperature_label = "hot"

    background_color = ratio_temperature_color(temperature)
    text_color = contrasting_text_color(background_color)
    accessible_label = html.escape(
        f"High to low ratio {ratio_text}; market temperature {temperature_label}",
        quote=True,
    )
    return (
        f'<span class="ratio-temperature" aria-label="{accessible_label}" '
        f'style="background-color:{background_color};color:{text_color}">'
        f"{html.escape(ratio_text)}</span>"
    )


def ratio_temperature_value(high_count: int, low_count: int) -> float | None:
    if high_count + low_count == 0:
        return None
    if low_count == 0:
        return 1.0

    ratio = high_count / low_count
    if ratio == 0:
        return 0.0

    # A logarithmic scale makes reciprocal ratios equally distant from
    # balanced: 0.5x and 2.0x receive symmetric temperatures. Values from
    # 0.125x through 8.0x span the full fear-to-greed scale.
    return min(max((math.log2(ratio) + 3) / 6, 0.0), 1.0)


def historical_sentiment_script(archive_rows: list[dict[str, Any]]) -> str:
    values = []
    for row in reversed(archive_rows):
        temperature = ratio_temperature_value(
            int(row.get("high_count") or 0), int(row.get("low_count") or 0)
        )
        if temperature is not None:
            values.append(round(temperature, 6))

    script = """  <script>
  (() => {
    const values = __SENTIMENT_VALUES__;
    const canvas = document.getElementById("sentiment-chart");
    if (!canvas || values.length === 0) return;

    const draw = () => {
      const width = Math.max(Math.round(canvas.clientWidth), 1);
      const height = Math.max(Math.round(canvas.clientHeight), 1);
      const scale = window.devicePixelRatio || 1;
      canvas.width = Math.round(width * scale);
      canvas.height = Math.round(height * scale);

      const context = canvas.getContext("2d");
      context.scale(scale, scale);
      context.clearRect(0, 0, width, height);

      const padding = 7;
      const chartWidth = width - padding * 2;
      const chartHeight = height - padding * 2;
      const points = values.map((value, index) => ({
        x: values.length === 1
          ? width / 2
          : padding + (index / (values.length - 1)) * chartWidth,
        y: padding + (1 - value) * chartHeight
      }));

      const neutralY = padding + chartHeight / 2;
      context.beginPath();
      context.setLineDash([4, 4]);
      context.moveTo(padding, neutralY);
      context.lineTo(width - padding, neutralY);
      context.strokeStyle = "rgba(100, 116, 139, 0.35)";
      context.lineWidth = 1;
      context.stroke();
      context.setLineDash([]);

      context.beginPath();
      context.moveTo(points[0].x, height - padding);
      points.forEach((point) => context.lineTo(point.x, point.y));
      context.lineTo(points[points.length - 1].x, height - padding);
      context.closePath();
      const area = context.createLinearGradient(0, padding, 0, height - padding);
      area.addColorStop(0, "rgba(220, 38, 38, 0.22)");
      area.addColorStop(0.5, "rgba(234, 179, 8, 0.09)");
      area.addColorStop(1, "rgba(37, 99, 235, 0.18)");
      context.fillStyle = area;
      context.fill();

      context.beginPath();
      context.moveTo(points[0].x, points[0].y);
      points.slice(1).forEach((point) => context.lineTo(point.x, point.y));
      const line = context.createLinearGradient(0, padding, 0, height - padding);
      line.addColorStop(0, "#dc2626");
      line.addColorStop(0.25, "#f97316");
      line.addColorStop(0.5, "#eab308");
      line.addColorStop(0.75, "#06b6d4");
      line.addColorStop(1, "#2563eb");
      context.strokeStyle = line;
      context.lineWidth = 2.5;
      context.lineJoin = "round";
      context.lineCap = "round";
      context.stroke();
    };

    draw();
    if ("ResizeObserver" in window) {
      new ResizeObserver(draw).observe(canvas);
    } else {
      window.addEventListener("resize", draw);
    }
  })();
  </script>"""
    return script.replace(
        "__SENTIMENT_VALUES__", json.dumps(values, separators=(",", ":"))
    )


def ratio_temperature_color(temperature: float) -> str:
    stops = (
        (0.00, (37, 99, 235)),
        (0.25, (6, 182, 212)),
        (0.50, (234, 179, 8)),
        (0.75, (249, 115, 22)),
        (1.00, (220, 38, 38)),
    )
    temperature = min(max(temperature, 0.0), 1.0)
    for (start_at, start_rgb), (end_at, end_rgb) in zip(stops, stops[1:]):
        if temperature <= end_at:
            progress = (temperature - start_at) / (end_at - start_at)
            rgb = tuple(
                round(start + (end - start) * progress)
                for start, end in zip(start_rgb, end_rgb)
            )
            return "#" + "".join(f"{channel:02x}" for channel in rgb)
    return "#dc2626"


def contrasting_text_color(background_color: str) -> str:
    channels = [
        int(background_color[index : index + 2], 16) / 255
        for index in (1, 3, 5)
    ]
    linear_channels = [
        channel / 12.92
        if channel <= 0.04045
        else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    luminance = (
        0.2126 * linear_channels[0]
        + 0.7152 * linear_channels[1]
        + 0.0722 * linear_channels[2]
    )
    white_contrast = 1.05 / (luminance + 0.05)
    black_contrast = (luminance + 0.05) / 0.05
    return "#ffffff" if white_contrast >= black_contrast else "#111827"


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
    <div class="table-shell stock-table-shell">
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
    else:
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

    # The labels are used by the responsive card layout, where the table header
    # is visually hidden and each value needs to remain self-describing.
    labeled_cells = "".join(
        f'<td data-label="{html.escape(header, quote=True)}">{cell}</td>'
        for header, cell in zip(table_headers(page_type), cells)
    )
    return f"          <tr>{labeled_cells}</tr>"


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
.description {
  max-width: 760px;
  margin: 0 0 8px;
  color: #334155;
  font-size: 14px;
  line-height: 1.5;
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
  min-width: 1120px;
  border-collapse: collapse;
}
.archive table {
  min-width: 0;
  table-layout: fixed;
}
.archive {
  max-width: 900px;
}
.archive-date {
  width: 26%;
}
.archive-count {
  width: 17%;
}
.archive-ratio {
  width: 40%;
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
  text-align: center;
}
.temperature-legend {
  display: flex;
  align-items: flex-start;
  gap: 14px;
  max-width: 720px;
  margin-top: 14px;
}
.temperature-title {
  padding-top: 20px;
  color: #334155;
  font-size: 13px;
  font-weight: 700;
  white-space: nowrap;
}
.temperature-key {
  flex: 1;
  min-width: 360px;
}
.temperature-labels {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 5px;
  color: #52606d;
  font-size: 12px;
}
.temperature-labels span:nth-child(2) {
  text-align: center;
}
.temperature-labels span:last-child {
  text-align: right;
}
.temperature-scale {
  height: 12px;
  border-radius: 999px;
  background: linear-gradient(90deg, #2563eb 0%, #06b6d4 25%, #eab308 50%, #f97316 75%, #dc2626 100%);
  box-shadow: inset 0 0 0 1px rgb(15 23 42 / 12%);
}
.sentiment-chart {
  display: block;
  width: 100%;
  height: 82px;
  margin-top: 8px;
  border: 1px solid #d9e2ec;
  border-radius: 6px;
  background: #fff;
}
.ratio-temperature {
  display: inline-block;
  min-width: 54px;
  padding: 5px 9px;
  border-radius: 5px;
  box-shadow: inset 0 0 0 1px rgb(15 23 42 / 10%);
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  text-align: center;
}
.ratio-empty {
  color: #8a94a3;
}
@media (max-width: 640px) {
  .temperature-legend {
    align-items: stretch;
    flex-direction: column;
    gap: 6px;
  }
  .temperature-title {
    padding-top: 0;
  }
  .temperature-key {
    min-width: 0;
  }
}
th {
  position: sticky;
  top: 0;
  z-index: 1;
  color: #334155;
  background: #eef3f8;
}
#stock-table th {
  cursor: pointer;
  user-select: none;
}
#stock-table th::after {
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
    padding: 14px 12px;
  }
  h1 {
    font-size: 23px;
  }
  .description {
    font-size: 13px;
  }
  .topbar {
    align-items: start;
    flex-direction: column;
    gap: 12px;
  }
  .links {
    flex-wrap: wrap;
    margin-top: 18px;
  }
  .archive {
    margin-top: 16px;
    overflow: hidden;
  }
  .archive-date {
    width: 30%;
  }
  .archive-count {
    width: 16%;
  }
  .archive-ratio {
    width: 38%;
  }
  .archive th,
  .archive td {
    padding: 8px 4px;
    font-size: 12px;
  }
  .archive th:last-child {
    font-size: 0;
  }
  .archive th:last-child::after {
    content: "Ratio";
    font-size: 12px;
  }
  .ratio-temperature {
    min-width: 44px;
    padding: 4px 6px;
  }

  /* The desktop stock grid has too many columns for a useful phone layout.
     On small screens, retain the table semantics but present each row as a
     compact, two-column card with an inline label for every value. */
  .stock-table-shell {
    overflow: visible;
    border: 0;
    background: transparent;
  }
  #stock-table {
    display: block;
    min-width: 0;
  }
  #stock-table thead {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }
  #stock-table tbody {
    display: grid;
    gap: 12px;
  }
  #stock-table tr {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    padding: 8px;
    border: 1px solid #d9e2ec;
    border-radius: 8px;
    background: #fff;
    box-shadow: 0 1px 2px rgb(15 23 42 / 5%);
  }
  #stock-table td {
    display: flex;
    min-width: 0;
    padding: 6px;
    border: 0;
    flex-direction: column;
    gap: 2px;
    overflow-wrap: anywhere;
    text-align: left;
    white-space: normal;
  }
  #stock-table td::before {
    content: attr(data-label);
    color: #64748b;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.04em;
    line-height: 1.2;
    text-transform: uppercase;
  }
  #stock-table td:nth-child(3) {
    grid-column: 1 / -1;
  }
  #stock-table td:nth-child(2) a {
    font-size: 16px;
    font-weight: 700;
  }
  #stock-table td:nth-child(1),
  #stock-table td:nth-child(4),
  #stock-table td:nth-child(5),
  #stock-table td:nth-child(6),
  #stock-table td:nth-child(7),
  #stock-table td:nth-child(8),
  #stock-table td:nth-child(9),
  #stock-table td:nth-child(13) {
    text-align: left;
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
