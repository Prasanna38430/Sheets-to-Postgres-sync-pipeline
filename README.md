# Google Sheets → PostgreSQL Sync Pipeline

[![CI](https://github.com/Prasanna38430/Sheets-to-Postgres-sync-pipeline/actions/workflows/ci.yml/badge.svg)](https://github.com/Prasanna38430/Sheets-to-Postgres-sync-pipeline/actions/workflows/ci.yml)

A Python service that syncs data from any Google Sheet into a PostgreSQL database on a daily schedule, with a FastAPI layer for verifying syncs, triggering manual runs, and querying the data over HTTP.

I built this as a portfolio project to show a production-shaped ETL pattern end to end: pull from a messy source, clean it, load it idempotently, and put a small API in front so the result is verifiable. A lot of teams keep operational data in spreadsheets but eventually need it in a real database for SQL reporting, and this is the shape that problem usually takes.

## What it does

The pipeline reads every row from a configured Google Sheet and runs it through a pandas cleaning step: it trims whitespace, drops fully-blank rows, deduplicates on the `id` column, coerces junk values in the amount column to `0.0`, parses dates (leaving unparseable ones as null), and filters out rows whose status isn't in a known set. The cleaned rows are then upserted into PostgreSQL.

The upsert uses Postgres' native `INSERT ... ON CONFLICT (id) DO UPDATE` rather than a delete-and-reinsert. That choice matters: a delete-and-reinsert would reset every row's `synced_at` timestamp on each run, so you'd lose the ability to tell which records actually changed. With an upsert, only the rows that were touched get a new timestamp.

APScheduler runs the whole thing at 02:00 UTC daily. On top of the database sits a FastAPI app with three endpoints, so you can confirm the last run succeeded (`/api/status`), browse recent rows (`/api/data`), or kick off a sync on demand (`/api/sync/trigger`) right after editing the sheet instead of waiting for the next scheduled run.

## Architecture

```
  Google Sheets ──▶ extractor.py ──▶ clean_data()  (pandas)
                    get_sheet_data         │
                                           ▼
        Client ◀──── FastAPI app ◀────▶ loader.py
     (curl / browser)  src/main.py     upsert_records
                                           │
                                           ▼
                                      PostgreSQL
                                     synced_records
```

## Quickstart

### Prerequisites

- Docker and Docker Compose installed
- A Google Cloud service account with the Sheets API and Drive API enabled (Google's setup walkthrough: https://developers.google.com/workspace/guides/create-credentials)
- A Google Sheet shared with your service account email (the one ending in `@*.iam.gserviceaccount.com`) with at least Viewer access

### 1. Clone and configure

```bash
git clone https://github.com/Prasanna38430/Sheets-to-Postgres-sync-pipeline.git
cd Sheets-to-Postgres-sync-pipeline
cp .env.example .env
```

Open `.env` and set your `SPREADSHEET_ID` and `SHEET_NAME`. The Postgres defaults work as-is for a local run, so the spreadsheet ID is usually the only value you have to change.

### 2. Add your credentials

Put your service account JSON key file at the project root and name it `credentials.json`. The compose file mounts it into the app container, where the sync uses it to authenticate with Google's API. (A sample sheet is included as `sample_sheet.csv` if you want to test without your own data — import it into a Google Sheet and share it with your service account.)

### 3. Start everything

```bash
docker compose up --build
```

This brings up three containers: Postgres, the sync API, and a pgAdmin instance for browsing the database. Postgres comes up first and waits for its healthcheck, then the app boots and runs an initial sync so the database isn't empty on first load.

Once it's running:

| Service | URL | Notes |
|---------|-----|-------|
| Sync API | http://localhost:8000 | The three endpoints below |
| API docs (Swagger) | http://localhost:8000/docs | Interactive, auto-generated |
| pgAdmin | http://localhost:5050 | Login `admin@admin.com` / `admin`; the DB is pre-registered |
| Postgres (direct) | `localhost:5433` | For DBeaver, psql, app code |

### 4. Verify it's working

```bash
# is the last sync healthy?
curl http://localhost:8000/api/status

# what's actually in the database?
curl "http://localhost:8000/api/data?limit=5"

# force a fresh sync right now
curl -X POST http://localhost:8000/api/sync/trigger
```

A healthy `/api/status` looks like this (numbers reflect the bundled sample sheet):

```json
{
  "last_sync_time": "2026-05-29T11:03:14.598+00:00",
  "rows_in_db": 24,
  "last_run_status": "success",
  "last_error": null
}
```

## API reference

| Method | Path | What it does | Example response |
|--------|------|--------------|------------------|
| GET | `/api/status` | Snapshot of the last sync run | `{"last_sync_time": "...", "rows_in_db": 24, "last_run_status": "success", "last_error": null}` |
| GET | `/api/data?limit=100` | Most recently-synced rows (limit 1–500, default 100) | `{"count": 24, "rows": [{"id": "1001", "customer_name": "...", ...}]}` |
| POST | `/api/sync/trigger` | Runs the full pipeline immediately | `{"rows_extracted": 27, "rows_after_cleaning": 24, "rows_inserted_or_updated": 24, "duration_seconds": 4.99}` |

## Configuration

| Variable | Required | Description | Example value |
|----------|----------|-------------|---------------|
| `SPREADSHEET_ID` | yes | The ID from your sheet URL (the part between `/d/` and `/edit`) | `1A2B3C4D5E6F7G8H9I0J` |
| `GOOGLE_CREDENTIALS_PATH` | yes | Path to the service account JSON file inside the container | `credentials.json` |
| `DATABASE_URL` | yes | Full SQLAlchemy-style Postgres URL | `postgresql://postgres:password@db:5432/sheetsync` |
| `SHEET_NAME` | no | Tab name to read from (defaults to `Sheet1`) | `sample_sheet` |

## Adapting this to your use case

The two files you'll touch most are `src/extractor.py` and `src/loader.py`. If your sheet has different columns, update `EXPECTED_COLUMNS` and `VALID_STATUSES` in `extractor.py` and adjust the body of `clean_data()`; each cleaning rule is independent, so you can add or remove steps without breaking the rest. If your destination table needs different columns or types, edit the `synced_records` table definition in `loader.py` and the matching dict built inside `upsert_records()`. To change the schedule, find the `CronTrigger(hour=2, minute=0)` line in `src/main.py` and set whatever cadence you need.

## Running tests

```bash
pip install -r requirements.txt pytest
pytest tests/ -v
```

The unit tests mock out Sheets and the database, so they run on a clean machine with no external setup. They cover the cleaning rules (dedupe, status filtering, amount coercion, null handling) and the API endpoints. End-to-end testing against a live sheet and database is done by running the full Docker stack.

## Tech stack

Python 3.11 · gspread 6.x · pandas 2.x · SQLAlchemy 2.x · FastAPI · APScheduler · PostgreSQL 15 · pgAdmin · Docker
