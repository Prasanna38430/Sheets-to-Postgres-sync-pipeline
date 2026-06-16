"""
Takes a cleaned DataFrame and writes it into PostgreSQL using an upsert.

This uses SQLAlchemy Core rather than the ORM. The table is small and flat, so
Core keeps the generated SQL easy to read and reason about.
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
    Build a SQLAlchemy engine from the DATABASE_URL env var.

    Raises RuntimeError with a clear message if the URL is missing or the
    database can't be reached.
    """
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set. Check your DATABASE_URL in .env.")

    # pool_pre_ping avoids a stale-connection error on the first query after
    # the app has been idle for a while.
    try:
        engine = create_engine(database_url, pool_pre_ping=True, future=True)
        # connect once now so a bad URL fails here, not deep inside a later query.
        with engine.connect() as conn:
            conn.execute(select(1))
        return engine
    except SQLAlchemyError as exc:
        logger.exception("could not connect to postgres")
        raise RuntimeError(
            "Could not connect to PostgreSQL. Check your DATABASE_URL in .env "
            "and make sure the database is running."
        ) from exc


def ensure_table_exists(engine):
    # safe to call on every startup; create_all is a no-op if the table exists.
    metadata.create_all(engine, tables=[synced_records])
    logger.info("Ensured synced_records table exists")


def _count_rows(engine):
    with engine.connect() as conn:
        result = conn.execute(select(func.count()).select_from(synced_records))
        return int(result.scalar() or 0)


def upsert_records(df, engine):
    """
    Load a cleaned DataFrame into synced_records using INSERT ... ON CONFLICT.

    Upsert instead of delete-then-insert for two reasons: it keeps each row's
    synced_at so you can tell what actually changed, and it never leaves the
    table empty mid-run while the API is reading from it.

    Expects a DataFrame that already went through clean_data(). Returns a dict
    with rows_inserted, rows_updated, total_processed.
    """
    if df.empty:
        logger.info("upsert called with empty DataFrame — nothing to do")
        return {"rows_inserted": 0, "rows_updated": 0, "total_processed": 0}

    now = datetime.now(timezone.utc)
    rows_before = _count_rows(engine)

    # order_date comes in as a pandas Timestamp; convert to a plain date to
    # match the column type.
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
    Snapshot of the table for /api/status: total_rows_in_db, last_synced_at,
    most_recent_order_date. The timestamp fields are None if the table is empty.
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
