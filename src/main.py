"""
FastAPI entrypoint — ties the extractor and loader together and exposes a
small API so clients can verify syncs, query data, and kick off manual runs.

APScheduler runs the full pipeline once a day at 02:00 UTC. We also fire one
sync on startup so the database isn't empty the first time someone hits the
API after a fresh deploy.
"""

import logging
import time
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException, Query
from sqlalchemy import desc, select

from src import extractor, loader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Sheets → Postgres Sync",
    description="Daily Google Sheets to PostgreSQL sync with a verification API.",
    version="1.0.0",
)

# in-memory state for the /status endpoint. survives between requests but
# resets on restart — that's fine, the loader stores the durable bits.
sync_state = {
    "last_sync_time": None,
    "last_run_status": "never_run",
    "last_error": None,
}

scheduler = BackgroundScheduler(timezone="UTC")
_engine = None


def _get_engine():
    """Lazy engine accessor so tests can mock it without a real DB."""
    global _engine
    if _engine is None:
        _engine = loader.get_engine()
        loader.ensure_table_exists(_engine)
    return _engine


def run_sync_pipeline():
    """
    Runs the full extract → clean → load pipeline once and updates sync_state.

    Wrapped in a broad try/except on purpose: this runs from the scheduler,
    and a thrown exception there would kill the job silently. Instead we
    record the failure in sync_state so /api/status can surface it.

    Returns a stats dict combining counts from the cleaner and the loader,
    plus the duration in seconds. Re-raises on failure so the manual trigger
    endpoint can return a 500 to the caller.
    """
    start = time.monotonic()
    try:
        raw_df = extractor.get_sheet_data()
        clean_df, clean_stats = extractor.clean_data(raw_df)
        load_stats = loader.upsert_records(clean_df, _get_engine())

        duration = round(time.monotonic() - start, 2)
        sync_state["last_sync_time"] = datetime.now(timezone.utc).isoformat()
        sync_state["last_run_status"] = "success"
        sync_state["last_error"] = None

        return {
            "rows_extracted": clean_stats["rows_before"],
            "rows_after_cleaning": clean_stats["rows_after"],
            "rows_inserted_or_updated": load_stats["total_processed"],
            "duration_seconds": duration,
        }
    except Exception as exc:
        sync_state["last_sync_time"] = datetime.now(timezone.utc).isoformat()
        sync_state["last_run_status"] = "failed"
        sync_state["last_error"] = str(exc)
        logger.exception("sync pipeline failed")
        raise


@app.on_event("startup")
def _on_startup():
    """Schedule the daily sync and fire an initial one so the DB isn't empty."""
    scheduler.add_job(
        run_sync_pipeline,
        trigger=CronTrigger(hour=2, minute=0),
        id="daily_sync",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler started. First sync running now...")
    try:
        run_sync_pipeline()
    except Exception:
        # already logged inside run_sync_pipeline. don't block startup —
        # the /status endpoint will show the failure so it's visible.
        logger.warning("initial sync failed; API will still come up")


@app.on_event("shutdown")
def _on_shutdown():
    """Clean scheduler shutdown so we don't leave a thread hanging."""
    if scheduler.running:
        scheduler.shutdown(wait=False)


@app.get("/api/status")
def get_status():
    """
    Returns a snapshot of the most recent sync.

    Useful as a smoke test after deploying — if last_run_status is success
    and last_sync_time is recent, you're good. Returns 200 even if the last
    run failed; the failure shows up in last_run_status and last_error.

    Response shape:
      last_sync_time: ISO timestamp string or null
      rows_in_db: int
      last_run_status: "success" | "failed" | "never_run"
      last_error: string or null
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
    Returns recently-synced rows from synced_records.

    Query params:
      limit: how many rows to return. Defaults to 100, capped at 500 so a
             curious user can't accidentally pull a million-row dump.

    Rows come back ordered by synced_at descending — so the freshest writes
    show up first, which is what you usually want when debugging a sync.

    Returns 500 if the database can't be queried.
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
    Runs the sync pipeline right now, synchronously.

    Handy when a client just updated the sheet and doesn't want to wait
    until 2 AM to see the change land in the database. Returns the same
    stats shape the scheduled run produces.

    Returns 500 if the sync fails — the body includes the error message so
    you don't have to dig through logs to know what went wrong.
    """
    try:
        return run_sync_pipeline()
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Sync failed: {exc}",
        ) from exc
