"""
Takes a cleaned DataFrame and pushes it into PostgreSQL using an upsert.

We deliberately don't use the ORM here — the table is tiny and flat, and going
through Core keeps the SQL we generate easy to reason about (and easy to
explain to a client when they ask "what does this actually run?").
"""

import logging
import os
from datetime import datetime, timezone

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import (
    Column,
    Date,
    Float,
    MetaData,
    String,
    Table,
    TIMESTAMP,
    create_engine,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError

load_dotenv()

logger = logging.getLogger(__name__)

metadata = MetaData()

synced_records = Table(
    "synced_records",
    metadata,
    Column("id", String, primary_key=True),
    Column("customer_name", String),
    Column("email", String),
    Column("amount", Float),
    Column("order_date", Date),
    Column("status", String),
    Column("synced_at", TIMESTAMP(timezone=True)),
)


def get_engine():
    """
    Builds a SQLAlchemy engine from the DATABASE_URL env var.

    We pool_pre_ping so a long-idle connection doesn't blow up the first sync
    of the day with a "server closed the connection unexpectedly" error —
    cheap insurance when the API has been running but the sheet hasn't changed.

    Returns a SQLAlchemy Engine. Raises RuntimeError if the URL is missing or
    the database isn't reachable, with a message that points at the most
    likely fix.
    """
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError(
            "DATABASE_URL is not set — check your DATABASE_URL in .env."
        )

    try:
        engine = create_engine(database_url, pool_pre_ping=True, future=True)
        # cheap sanity check so failures surface here instead of inside the
        # first real query, which is much harder to debug.
        with engine.connect() as conn:
            conn.execute(select(1))
        return engine
    except SQLAlchemyError as exc:
        logger.exception("could not connect to postgres")
        raise RuntimeError(
            "Could not connect to PostgreSQL — check your DATABASE_URL in .env, "
            "and make sure the database is running and reachable."
        ) from exc


def ensure_table_exists(engine):
    """
    Creates the synced_records table if it doesn't already exist.

    Safe to call on every startup. SQLAlchemy's create_all is a no-op when
    the table is already there, so we don't need a separate migration step
    for a project this small.
    """
    metadata.create_all(engine, tables=[synced_records])
    logger.info("Ensured synced_records table exists")


def _count_rows(engine):
    """Returns the current row count of synced_records. Used for upsert stats."""
    with engine.connect() as conn:
        result = conn.execute(select(func.count()).select_from(synced_records))
        return int(result.scalar() or 0)


def upsert_records(df, engine):
    """
    Loads a cleaned DataFrame into synced_records using INSERT ... ON CONFLICT.

    Why upsert instead of delete-then-insert? Two reasons. First, we want to
    preserve synced_at history per row — a delete+insert would reset every
    row's synced_at on every run, hiding which records actually changed.
    Second, an upsert is atomic at the row level: if the run dies halfway,
    the table is never in an empty/partial state, which matters because the
    FastAPI layer is reading from the same table at the same time.

    The caller must pass a DataFrame that's already been through clean_data().
    No defensive cleaning here on purpose — if something junky reaches the
    loader we want it to surface as a database error, not silently get
    swallowed.

    Returns a dict with rows_inserted, rows_updated, total_processed.
    """
    if df.empty:
        logger.info("upsert called with empty DataFrame — nothing to do")
        return {"rows_inserted": 0, "rows_updated": 0, "total_processed": 0}

    now = datetime.now(timezone.utc)
    rows_before = _count_rows(engine)

    # build the list of dicts SQLAlchemy expects. order_date arrives as a
    # pandas Timestamp — postgres is fine with that but we convert to date
    # to match the column type and avoid timezone confusion on the date.
    records = []
    for row in df.to_dict(orient="records"):
        order_date = row.get("order_date")
        if isinstance(order_date, pd.Timestamp) and not pd.isna(order_date):
            order_date = order_date.date()
        elif pd.isna(order_date):
            order_date = None

        records.append({
            "id": str(row["id"]),
            "customer_name": row.get("customer_name"),
            "email": row.get("email"),
            "amount": float(row.get("amount", 0.0)),
            "order_date": order_date,
            "status": row.get("status"),
            "synced_at": now,
        })

    stmt = pg_insert(synced_records).values(records)
    update_cols = {
        "customer_name": stmt.excluded.customer_name,
        "email": stmt.excluded.email,
        "amount": stmt.excluded.amount,
        "order_date": stmt.excluded.order_date,
        "status": stmt.excluded.status,
        "synced_at": stmt.excluded.synced_at,
    }
    stmt = stmt.on_conflict_do_update(index_elements=["id"], set_=update_cols)

    with engine.begin() as conn:
        conn.execute(stmt)

    rows_after = _count_rows(engine)
    rows_inserted = rows_after - rows_before
    rows_updated = len(records) - rows_inserted

    logger.info(
        "Upsert done: %d inserted, %d updated (processed %d)",
        rows_inserted,
        rows_updated,
        len(records),
    )
    return {
        "rows_inserted": rows_inserted,
        "rows_updated": rows_updated,
        "total_processed": len(records),
    }


def get_sync_stats(engine):
    """
    Returns a snapshot of what's currently in the database.

    Used by the /api/status endpoint so a client can confirm a sync ran
    without having to query the database themselves.

    Keys in the returned dict:
      total_rows_in_db, last_synced_at, most_recent_order_date.
    Any of the timestamp fields can be None if the table is empty.
    """
    with engine.connect() as conn:
        total = conn.execute(
            select(func.count()).select_from(synced_records)
        ).scalar() or 0
        last_synced = conn.execute(
            select(func.max(synced_records.c.synced_at))
        ).scalar()
        most_recent_order = conn.execute(
            select(func.max(synced_records.c.order_date))
        ).scalar()

    return {
        "total_rows_in_db": int(total),
        "last_synced_at": last_synced.isoformat() if last_synced else None,
        "most_recent_order_date": (
            most_recent_order.isoformat() if most_recent_order else None
        ),
    }
