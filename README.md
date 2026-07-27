# 52-Week High/Low Stock Collector

Collects the current Barchart 52-week high and low stock lists, enriches them with Yahoo Finance data, stores them in SQLite, and generates two sortable HTML pages.

## Setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
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

Use `--render-only` to rebuild `public/` pages from the existing SQLite data without scraping again:

```bash
python collect_52wk.py --render-only --date 2026-07-25
```

## Deployment

For Amazon Linux 2 deployment instructions using `/home/choochoo/52wk` and `/usr/share/nginx/html/choo-choo-train/52wk`, see `DEPLOY_AWS_LINUX_2.md`.
