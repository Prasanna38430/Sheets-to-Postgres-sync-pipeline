# Google Sheets → PostgreSQL Sync

[![CI](https://github.com/Prasanna38430/Sheets-to-Postgres-sync-pipeline/actions/workflows/ci.yml/badge.svg)](https://github.com/Prasanna38430/Sheets-to-Postgres-sync-pipeline/actions/workflows/ci.yml)

Keep a PostgreSQL database automatically in sync with a Google Sheet, so your
team can keep entering data in the spreadsheet they already use while you get
clean, query-ready data in a real database.

## What you get

- **Your spreadsheet data in PostgreSQL, updated every day** with no manual
  exports. The sync runs on its own at 2 AM, and you can also trigger it any
  time with one request.
- **Clean data, not a copy of the mess.** Duplicate rows, blank rows, bad
  dates, and junk numbers are handled before anything reaches the database, so
  your reports aren't full of surprises.
- **No double-counting.** Editing a row in the sheet updates that same row in
  the database instead of creating a duplicate.
- **A simple way to check it's working.** Three HTTP endpoints let you confirm
  the last sync succeeded, see the data, or run a sync on demand, without
  opening the database.
- **One command to run the whole thing.** `docker compose up` starts the
  database, the sync service, and a database admin UI together.

## See it working

The [`examples/`](examples/) folder has real output captured from a run against
the included sample sheet (27 rows in the sheet, 24 after cleaning):

```
Pulled 27 rows from sheet 'sample_sheet'
Cleaned sheet: 27 → 24 rows (1 dupes, 1 bad dates)
Upsert done: 24 inserted, 0 updated (processed 24)
```

```jsonc
// GET /api/status
{ "last_sync_time": "2026-05-29T11:03:14+00:00", "rows_in_db": 24,
  "last_run_status": "success", "last_error": null }
```

## How the cleaning works

Spreadsheets collect messy data over time. Before anything is written to the
database, each row goes through these rules:

| Problem in the sheet | What the pipeline does |
|----------------------|------------------------|
| Same id entered twice | Keeps the first, drops the rest |
| Empty rows between sections | Dropped |
| Text like `N/A` in an amount | Becomes `0.0` |
| A date in an odd format | Stored as empty instead of breaking the row |
| An unexpected status value | Row is left out |
| Extra spaces around text | Trimmed |
| A missing email | Row is kept (only fully blank rows are dropped) |

## How it fits together

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

- Docker and Docker Compose
- A Google Cloud service account with the Sheets API and Drive API enabled
  ([Google's setup guide](https://developers.google.com/workspace/guides/create-credentials))
- A Google Sheet shared with your service account email (Viewer is enough)

### 1. Clone and configure

```bash
git clone https://github.com/Prasanna38430/Sheets-to-Postgres-sync-pipeline.git
cd Sheets-to-Postgres-sync-pipeline
cp .env.example .env
```

Open `.env` and set `SPREADSHEET_ID` and `SHEET_NAME`. The database defaults
work as-is for a local run, so the spreadsheet ID is usually the only thing you
need to change.

### 2. Add your credentials

Put your service account key file at the project root as `credentials.json`.
Docker mounts it into the container at runtime; it is never copied into the
image (see `.dockerignore`). To try it without your own data, import the
included `sample_sheet.csv` into a Google Sheet and share it with your service
account.

### 3. Start everything

```bash
docker compose up --build
```

This starts three containers. The app runs one sync on startup, then daily at
2 AM UTC.

| Service | URL | Notes |
|---------|-----|-------|
| Sync API | http://localhost:8000 | The endpoints below |
| API docs | http://localhost:8000/docs | Interactive, auto-generated |
| pgAdmin | http://localhost:5050 | Login `admin@admin.com` / `admin`; DB pre-registered |
| PostgreSQL | `localhost:5433` | For psql, DBeaver, etc. |

### 4. Check it

```bash
curl http://localhost:8000/api/status
curl "http://localhost:8000/api/data?limit=5"
curl -X POST http://localhost:8000/api/sync/trigger
```

## API reference

| Method | Path | What it does |
|--------|------|--------------|
| GET | `/api/status` | Result of the last sync (time, row count, success/failure) |
| GET | `/api/data?limit=100` | Most recent rows, newest first (limit 1–500) |
| POST | `/api/sync/trigger` | Runs a sync immediately and returns the counts |

Full example responses are in [`examples/`](examples/).

## Configuration

| Variable | Required | Description | Example |
|----------|----------|-------------|---------|
| `SPREADSHEET_ID` | yes | The id from the sheet URL (between `/d/` and `/edit`) | `1A2B3C...` |
| `GOOGLE_CREDENTIALS_PATH` | yes | Path to the service account file in the container | `credentials.json` |
| `DATABASE_URL` | yes | PostgreSQL connection string | `postgresql://postgres:password@db:5432/sheetsync` |
| `SHEET_NAME` | no | Tab to read (defaults to `Sheet1`) | `sample_sheet` |

## Adapting it to your data

Most changes are in two files. For different columns, update `EXPECTED_COLUMNS`
and `VALID_STATUSES` in `src/extractor.py` and adjust the steps in
`clean_data()` (each rule is independent). For a different table shape, edit the
`synced_records` definition and the record dict in `src/loader.py`. To change
the schedule, edit the `CronTrigger(hour=2, minute=0)` line in `src/main.py`.

## Running tests

```bash
pip install -r requirements-dev.txt
pytest
```

The tests mock out Google Sheets and the database, so they run on a clean
machine with no external setup. CI runs them on Python 3.11, 3.12, and 3.13.

## Project structure

```
src/extractor.py   read the sheet and clean the rows
src/loader.py      upsert the rows into PostgreSQL
src/main.py        FastAPI app + daily scheduler
tests/             unit tests for the cleaning rules and the API
examples/          real captured output
scripts/           sample-data generator
```

## Built with

Python, pandas, gspread (Google Sheets API), SQLAlchemy, FastAPI, APScheduler,
PostgreSQL, Docker. This is an API-based data pipeline (ETL); it reads through
the Google Sheets API, not by scraping a web page.

## License

MIT — see [LICENSE](LICENSE).
