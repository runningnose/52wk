# 52-Week High/Low Stock Collector

Collects the current Barchart 52-week high and low stock lists, enriches them with Yahoo Finance data, stores them in SQLite, and generates two sortable HTML pages.

## Setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

## Run

```bash
python collect_52wk.py
```

Generated files:

- `data/52wk.sqlite3`
- `public/index.html` - archive page linking to every collected date
- `public/YYYY-MM-DD.html` - daily summary page
- `public/YYYY-MM-DD-highs.html`
- `public/YYYY-MM-DD-lows.html`
- `public/highs.html` and `public/lows.html` - latest-run convenience aliases

The SQLite table has a unique key on `(date, ticker, type)`.

The script avoids newer SQLite upsert syntax and works with SQLite 3.7.x.

## Options

```bash
python collect_52wk.py --date 2026-07-25 --db data/52wk.sqlite3 --out public
```

Use `--skip-yahoo` to test Barchart collection without Yahoo enrichment.

Use `--random-delay` to wait a randomly selected 1 to 59 minutes before the
script starts:

```bash
python collect_52wk.py --random-delay
```

Barchart collection uses the anonymous Playwright browser by default and does
not call the direct HTTP API. No Barchart login is required. To try the direct
API first and automatically fall back to the browser on HTTP 401 or 403, use:

```bash
python collect_52wk.py --try-barchart-api
```

The older `--barchart-source auto|http|browser` option remains available for
explicit source selection and backward compatibility.

If Chrome or Chromium is installed outside a standard location, set its path:

```bash
export BARCHART_BROWSER_EXECUTABLE=/path/to/google-chrome
```

Use `--render-only` to rebuild `public/` pages from the existing SQLite data without scraping again:

```bash
python collect_52wk.py --render-only --date 2026-07-25
```

## Deployment

For Amazon Linux 2 deployment instructions using `/home/choochoo/52wk` and `/usr/share/nginx/html/choo-choo-train/52wk`, see `DEPLOY_AWS_LINUX_2.md`.
