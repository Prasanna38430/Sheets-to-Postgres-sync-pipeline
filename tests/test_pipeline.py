"""
Unit tests for the cleaner and the FastAPI surface.

These run without a real Google Sheet or a real database — gspread is never
called and the loader is mocked at the boundary. That keeps the suite fast
and runnable on a laptop with nothing installed beyond pip dependencies.

What's not covered: an end-to-end integration test against a live Postgres
and Sheet. Those need the full Docker stack and real credentials, so they
belong in a separate manual / CI smoke test rather than this unit suite.
"""

from unittest.mock import patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from src import extractor, main


def test_clean_data_drops_duplicates():
    """Duplicate ids should be collapsed to a single row, keeping the first."""
    df = pd.DataFrame([
        {"id": "1", "customer_name": "Alice", "email": "a@x.com",
         "amount": "10", "order_date": "2025-01-01", "status": "active"},
        {"id": "1", "customer_name": "Alice dup", "email": "a@x.com",
         "amount": "10", "order_date": "2025-01-02", "status": "active"},
        {"id": "2", "customer_name": "Bob", "email": "b@x.com",
         "amount": "20", "order_date": "2025-01-03", "status": "active"},
    ])
    cleaned, stats = extractor.clean_data(df)
    assert stats["duplicates_removed"] == 1
    assert sorted(cleaned["id"].tolist()) == ["1", "2"]
    assert cleaned.loc[cleaned["id"] == "1", "customer_name"].iloc[0] == "Alice"


def test_clean_data_handles_null_emails():
    """A missing email shouldn't drop the row — only fully blank rows go."""
    df = pd.DataFrame([
        {"id": "1", "customer_name": "Alice", "email": None,
         "amount": "10", "order_date": "2025-01-01", "status": "active"},
    ])
    cleaned, _ = extractor.clean_data(df)
    assert len(cleaned) == 1
    assert cleaned.iloc[0]["id"] == "1"


def test_clean_data_filters_invalid_status():
    """Unknown status values should be filtered out of the result."""
    df = pd.DataFrame([
        {"id": "1", "customer_name": "A", "email": "a@x.com",
         "amount": "10", "order_date": "2025-01-01", "status": "active"},
        {"id": "2", "customer_name": "B", "email": "b@x.com",
         "amount": "20", "order_date": "2025-01-02", "status": "unknown"},
        {"id": "3", "customer_name": "C", "email": "c@x.com",
         "amount": "30", "order_date": "2025-01-03", "status": "deleted"},
    ])
    cleaned, _ = extractor.clean_data(df)
    assert cleaned["id"].tolist() == ["1"]


def test_clean_data_amount_coercion():
    """Junk in the amount column should be coerced to 0.0, not crash."""
    df = pd.DataFrame([
        {"id": "1", "customer_name": "A", "email": "a@x.com",
         "amount": "not_a_number", "order_date": "2025-01-01", "status": "active"},
    ])
    cleaned, _ = extractor.clean_data(df)
    assert cleaned.iloc[0]["amount"] == 0.0


def test_status_endpoint_returns_200():
    """The status endpoint should respond 200 with the expected fields."""
    fake_stats = {
        "total_rows_in_db": 42,
        "last_synced_at": "2026-05-29T10:00:00+00:00",
        "most_recent_order_date": "2026-05-28",
    }
    with patch("src.main.run_sync_pipeline"), \
         patch("src.main._get_engine", return_value=object()), \
         patch("src.main.loader.get_sync_stats", return_value=fake_stats):
        client = TestClient(main.app)
        response = client.get("/api/status")
    assert response.status_code == 200
    body = response.json()
    assert "last_sync_time" in body
    assert "rows_in_db" in body
    assert body["rows_in_db"] == 42


def test_trigger_endpoint_runs_pipeline():
    """POST /api/sync/trigger should call the pipeline and return its stats."""
    fake_result = {
        "rows_extracted": 10,
        "rows_after_cleaning": 9,
        "rows_inserted_or_updated": 9,
        "duration_seconds": 0.05,
    }
    with patch("src.main.run_sync_pipeline", return_value=fake_result):
        client = TestClient(main.app)
        response = client.post("/api/sync/trigger")
    assert response.status_code == 200
    body = response.json()
    assert "rows_extracted" in body
    assert body["rows_extracted"] == 10
