# Google Sheets → PostgreSQL Sync Pipeline

A Python service that syncs data from any Google Sheet to a PostgreSQL database automatically every day. Includes a FastAPI layer so you can verify syncs, trigger manual runs, and query the data via API.

I built this for a client who had their sales records in Google Sheets but needed them in a proper database for SQL reporting. The sync runs at 2 AM daily and takes about 3 seconds for a 10,000-row sheet.

## What it does

The pipeline pulls every row out of a configured Google Sheet, runs it through a pandas cleaning step (trimming whitespace, deduplicating on the id column, coercing junk numbers to 0.0, parsing dates, and filtering out rows with statuses we don't recognise), and upserts the result into a PostgreSQL table. The upsert uses Postgres' native `INSERT ... ON CONFLICT` so existing rows get updated in place — we don't delete-and-reinsert, because that would lose the per-row `synced_at` timestamp and make it impossible to tell what actually changed in a given run. APScheduler kicks the whole thing off at 02:00 UTC every day, and a small FastAPI layer sits on top of the database so a client can hit `/api/status` to confirm the last run worked, browse recent rows through `/api/data`, or trigger a sync on demand via `/api/sync/trigger` whenever they've just updated the sheet and don't want to wait for the next scheduled run.

## Architecture

```
  ┌────────────────┐      ┌──────────────────┐      ┌──────────────────┐
  │ Google Sheets  │─────▶│  extractor.py    │─────▶│   clean_data()   │
  └────────────────┘      │  get_sheet_data  │      │   (pandas)       │
                          └──────────────────┘      └────────┬─────────┘
                                                             │
                                                             ▼
  ┌────────────────┐      ┌──────────────────┐      ┌──────────────────┐
  │     Client     │◀─────│   FastAPI app    │◀────▶│   loader.py      │
  │ (curl/browser) │      │  (src/main.py)   │      │  upsert_records  │
  └────────────────┘      └──────────────────┘      └────────┬─────────┘
                                                             │
                                                             ▼
                                                  ┌──────────────────┐
                                                  │   PostgreSQL     │
                                                  │  synced_records  │
                                                  └──────────────────┘
```

## Quickstart

### Prerequisites

- Docker and Docker Compose installed
- A Google Cloud service account with the Sheets API enabled — Google's walkthrough at https://developers.google.com/workspace/guides/create-credentials covers it end-to-end
- A Google Sheet shared with your service account email (the one ending in `@*.iam.gserviceaccount.com`) with at least Viewer access

### 1. Clone and configure

```bash
git clone <your-repo-url>
cd sheets-to-postgres-pipeline
cp .env.example .env
```

Then open `.env` and fill in your `SPREADSHEET_ID` and `DATABASE_URL`. The defaults work for the bundled Postgres container, so for a local run you usually only need to set the spreadsheet ID.

### 2. Add your credentials

Drop the service account JSON key file at the project root and name it `credentials.json`. The compose file mounts that path straight into the app container — the sync needs it to authenticate with Google's API.

### 3. Start everything

```bash
docker compose up --build
```

The first build takes a minute. After that, Postgres comes up, waits for a healthcheck, then the app boots and immediately runs a first sync so the database isn't empty.

### 4. Verify it's working

```bash
# is the last sync healthy?
curl http://localhost:8000/api/status

# what's actually in the database?
curl http://localhost:8000/api/data?limit=5

# force a fresh sync right now
curl -X POST http://localhost:8000/api/sync/trigger
```

A healthy `/api/status` looks like this:

```json
{
  "last_sync_time": "2026-05-29T08:14:22.103+00:00",
  "rows_in_db": 1247,
  "last_run_status": "success",
  "last_error": null
}
```

## API reference

| Method | Path | What it does | Example response |
|--------|------|--------------|------------------|
| GET | `/api/status` | Snapshot of the last sync run | `{"last_sync_time": "...", "rows_in_db": 1247, "last_run_status": "success", "last_error": null}` |
| GET | `/api/data?limit=100` | Most recently-synced rows (limit 1–500, default 100) | `{"count": 100, "rows": [{"id": "...", "customer_name": "...", ...}]}` |
| POST | `/api/sync/trigger` | Runs the full pipeline immediately | `{"rows_extracted": 1247, "rows_after_cleaning": 1244, "rows_inserted_or_updated": 1244, "duration_seconds": 2.91}` |

## Configuration

| Variable | Required | Description | Example value |
|----------|----------|-------------|---------------|
| `SPREADSHEET_ID` | yes | The ID from your sheet URL (the part between `/d/` and `/edit`) | `1A2B3C4D5E6F7G8H9I0J` |
| `GOOGLE_CREDENTIALS_PATH` | yes | Path to the service account JSON file inside the container | `credentials.json` |
| `DATABASE_URL` | yes | Full SQLAlchemy-style Postgres URL | `postgresql://postgres:password@db:5432/sheetsync` |
| `SHEET_NAME` | no | Tab name to read from. Defaults to `Sheet1` | `Orders` |

## Adapting this to your use case

The two files you'll touch most are `src/extractor.py` and `src/loader.py`. If your sheet has different columns, update `EXPECTED_COLUMNS` and `VALID_STATUSES` in `extractor.py` and adjust the `clean_data()` body — every cleaning rule there is independent so you can drop or add steps without untangling anything. If your destination table needs different columns or types, edit the `synced_records` Table definition in `loader.py` and the dict-build inside `upsert_records()` to match. To change the schedule, look for the `CronTrigger(hour=2, minute=0)` line in `src/main.py` and put whatever cron parts you want — twice a day, every 15 minutes, whatever fits.

## Running tests

```bash
pip install -r requirements.txt pytest
pytest tests/ -v
```

The unit tests mock out Sheets and the database so they run on a clean machine without any external setup.

## Tech stack

Python 3.11 · gspread 6.x · pandas 2.x · SQLAlchemy 2.x · FastAPI · APScheduler · PostgreSQL 15 · Docker
