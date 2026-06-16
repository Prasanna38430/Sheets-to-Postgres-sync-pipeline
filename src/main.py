"""
FastAPI entrypoint. Ties the extractor and loader together and exposes a small
API to verify syncs, query data, and kick off manual runs.

A background scheduler runs the full pipeline once a day at 02:00 UTC. One sync
also runs on startup so the database isn't empty right after a deploy.
"""

import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException, Query
from sqlalchemy import desc, select

from src import extractor, loader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)

# in-memory view of the last run, for /api/status. resets on restart, which is
# fine because the durable record lives in the database.
sync_state = {
    "last_sync_time": None,
    "last_run_status": "never_run",
    "last_error": None,
}

scheduler = BackgroundScheduler(timezone="UTC")
_engine = None


def _get_engine():
    # built lazily so the test suite can mock it without a real database.
    global _engine
    if _engine is None:
        _engine = loader.get_engine()
        loader.ensure_table_exists(_engine)
    return _engine


def run_sync_pipeline():
    """
    Run the full extract -> clean -> load pipeline once.

    Records the outcome in sync_state so a failed scheduled run is still
    visible on /api/status instead of vanishing. Re-raises on failure so the
    manual trigger endpoint can return a 500.

    Returns a stats dict: rows_extracted, rows_after_cleaning,
    rows_inserted_or_updated, duration_seconds.
    """
    start = time.monotonic()
    try:
        raw_df = extractor.get_sheet_data()
        clean_df, clean_stats = extractor.clean_data(raw_df)
        load_stats = loader.upsert_records(clean_df, _get_engine())

        sync_state["last_sync_time"] = datetime.now(timezone.utc).isoformat()
        sync_state["last_run_status"] = "success"
        sync_state["last_error"] = None

        return {
            "rows_extracted": clean_stats["rows_before"],
            "rows_after_cleaning": clean_stats["rows_after"],
            "rows_inserted_or_updated": load_stats["total_processed"],
            "duration_seconds": round(time.monotonic() - start, 2),
        }
    except Exception as exc:
        sync_state["last_sync_time"] = datetime.now(timezone.utc).isoformat()
        sync_state["last_run_status"] = "failed"
        sync_state["last_error"] = str(exc)
        logger.exception("sync pipeline failed")
        raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    # start the daily job and run one sync now so there's data to look at.
    scheduler.add_job(
        run_sync_pipeline,
        trigger=CronTrigger(hour=2, minute=0),
        id="daily_sync",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler started. Running first sync now.")
    try:
        run_sync_pipeline()
    except Exception:
        # already logged. don't block startup; /api/status will show the error.
        logger.warning("initial sync failed; the API will still start")

    yield

    if scheduler.running:
        scheduler.shutdown(wait=False)


app = FastAPI(
    title="Sheets to Postgres Sync",
    description="Daily Google Sheets to PostgreSQL sync with a verification API.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/api/status")
def get_status():
    """
    Snapshot of the most recent sync. Good as a smoke test after deploying.

    Returns 200 even when the last run failed; the failure shows up in
    last_run_status and last_error. Response fields: last_sync_time,
    rows_in_db, last_run_status, last_error.
    """
    try:
        stats = loader.get_sync_stats(_get_engine())
        rows_in_db = stats["total_rows_in_db"]
    except Exception as exc:
        logger.exception("status endpoint failed to read sync stats")
        raise HTTPException(
            status_code=500,
            detail="Could not read database stats. Check the logs.",
        ) from exc

    return {
        "last_sync_time": sync_state["last_sync_time"],
        "rows_in_db": rows_in_db,
        "last_run_status": sync_state["last_run_status"],
        "last_error": sync_state["last_error"],
    }


@app.get("/api/data")
def get_data(limit: int = Query(100, ge=1, le=500)):
    """
    Return recently-synced rows, newest first.

    limit defaults to 100 and is capped at 500 so a request can't pull the
    whole table by accident. Returns 500 if the database can't be queried.
    """
    try:
        engine = _get_engine()
        with engine.connect() as conn:
            stmt = (
                select(loader.synced_records)
                .order_by(desc(loader.synced_records.c.synced_at))
                .limit(limit)
            )
            rows = conn.execute(stmt).mappings().all()
    except Exception as exc:
        logger.exception("data endpoint failed")
        raise HTTPException(
            status_code=500,
            detail="Could not read records. Check the logs.",
        ) from exc

    return {"count": len(rows), "rows": [dict(r) for r in rows]}


@app.post("/api/sync/trigger")
def trigger_sync():
    """
    Run the sync now instead of waiting for the daily job. Useful right after
    editing the sheet. Returns the same stats as a scheduled run, or 500 with
    the error message if it fails.
    """
    try:
        return run_sync_pipeline()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Sync failed: {exc}") from exc
