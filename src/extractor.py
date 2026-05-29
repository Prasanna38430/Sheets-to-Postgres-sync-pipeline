"""
Pulls rows out of a Google Sheet and hands back a clean pandas DataFrame.

The extractor is intentionally dumb about what happens next — it just talks to
Sheets, normalises the messy stuff humans tend to leave in spreadsheets, and
returns something the loader can trust.
"""

import logging
import os

import gspread
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# the columns we expect in the sheet. if a client renames a column, this is
# the one place to update — everything downstream depends on these names.
EXPECTED_COLUMNS = ["id", "customer_name", "email", "amount", "order_date", "status"]

VALID_STATUSES = {"active", "inactive", "pending", "completed", "refunded"}


def get_sheet_data():
    """
    Connects to the configured Google Sheet and returns its rows as a DataFrame.

    Reads SPREADSHEET_ID, GOOGLE_CREDENTIALS_PATH, and SHEET_NAME from the
    environment. Uses a service account for auth — the sheet needs to be shared
    with the service account email, otherwise gspread will tell you it can't
    find the spreadsheet (the error message is misleading but that's almost
    always the reason).

    Returns a pandas DataFrame with whatever columns the sheet has. Cleaning
    happens later in clean_data() so we keep the two concerns separate — that
    way a caller can inspect the raw data if a sync looks weird.

    Raises a friendly RuntimeError if the credentials file is missing or the
    sheet can't be opened.
    """
    spreadsheet_id = os.getenv("SPREADSHEET_ID")
    credentials_path = os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json")
    sheet_name = os.getenv("SHEET_NAME", "Sheet1")

    if not spreadsheet_id:
        raise RuntimeError(
            "SPREADSHEET_ID is not set. Copy .env.example to .env and fill it in."
        )

    try:
        gc = gspread.service_account(filename=credentials_path)
    except FileNotFoundError:
        raise RuntimeError(
            f"Credentials file not found at '{credentials_path}'. "
            "Download a service account JSON key from Google Cloud Console "
            "(IAM & Admin → Service Accounts → Keys → Add Key) and save it at that path."
        )

    try:
        spreadsheet = gc.open_by_key(spreadsheet_id)
        worksheet = spreadsheet.worksheet(sheet_name)
        records = worksheet.get_all_records()
    except gspread.SpreadsheetNotFound:
        raise RuntimeError(
            f"Could not open spreadsheet '{spreadsheet_id}'. "
            "Double-check the ID and make sure the sheet is shared with your "
            "service account email (it ends in @*.iam.gserviceaccount.com)."
        )
    except gspread.WorksheetNotFound:
        raise RuntimeError(
            f"Worksheet tab '{sheet_name}' not found in the spreadsheet. "
            "Check the tab name at the bottom of the sheet."
        )
    except gspread.GSpreadException as exc:
        logger.exception("gspread error while reading sheet")
        raise RuntimeError(
            "Something went wrong talking to Google Sheets. "
            "Check the logs for details, then verify your credentials and quota."
        ) from exc

    df = pd.DataFrame(records)
    logger.info("Pulled %d rows from sheet '%s'", len(df), sheet_name)
    return df


def clean_data(df):
    """
    Takes a raw DataFrame from the sheet and returns a cleaned one + stats.

    Humans put weird stuff in spreadsheets: trailing spaces, blank rows between
    sections, duplicated IDs, "N/A" in numeric fields, dates in five formats.
    This function pushes all that into something a database will accept.

    What it actually does:
      - strips whitespace from string columns
      - drops fully-empty rows (Sheets sometimes exports trailing blanks)
      - drops duplicate IDs, keeping the first occurrence
      - coerces amount to float, replacing junk with 0.0
      - parses order_date to datetime, leaving bad dates as NaT
      - filters out rows whose status isn't in our known set

    Returns a tuple of (cleaned DataFrame, stats dict). The stats dict has:
      rows_before, rows_after, duplicates_removed, bad_dates.
    Callers (the FastAPI layer mainly) log these so we can spot a sheet
    that's degrading over time.
    """
    rows_before = len(df)

    if df.empty:
        return df, {
            "rows_before": 0,
            "rows_after": 0,
            "duplicates_removed": 0,
            "bad_dates": 0,
        }

    # strip whitespace on every string column. doing it column-by-column
    # because applymap is deprecated and gives a warning on pandas 2.x.
    for col in df.select_dtypes(include="object").columns:
        df[col] = df[col].astype(str).str.strip()
        # gspread gives back empty strings for blank cells — turn them into NaN
        # so downstream "all null" checks work as you'd expect.
        df[col] = df[col].replace({"": pd.NA, "nan": pd.NA, "None": pd.NA})

    df = df.dropna(how="all")

    before_dedupe = len(df)
    if "id" in df.columns:
        df = df.drop_duplicates(subset=["id"], keep="first")
    duplicates_removed = before_dedupe - len(df)

    if "amount" in df.columns:
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0.0)

    bad_dates = 0
    if "order_date" in df.columns:
        parsed = pd.to_datetime(df["order_date"], errors="coerce")
        bad_dates = int(parsed.isna().sum() - df["order_date"].isna().sum())
        df["order_date"] = parsed

    if "status" in df.columns:
        df["status"] = df["status"].astype(str).str.lower()
        df = df[df["status"].isin(VALID_STATUSES)]

    df = df.reset_index(drop=True)

    stats = {
        "rows_before": rows_before,
        "rows_after": len(df),
        "duplicates_removed": duplicates_removed,
        "bad_dates": bad_dates,
    }
    logger.info(
        "Cleaned sheet: %d → %d rows (%d dupes, %d bad dates)",
        stats["rows_before"],
        stats["rows_after"],
        stats["duplicates_removed"],
        stats["bad_dates"],
    )
    return df, stats
