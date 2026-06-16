# Example output

These files are real output captured from a live run against the bundled
`sample_sheet.csv` (27 rows in the sheet, 24 after cleaning). They're here so
you can see what the service produces without setting anything up.

| File | What it is |
|------|-----------|
| `sync_log.txt` | The app log from one sync, showing extract → clean → load |
| `api_status.json` | Response from `GET /api/status` |
| `api_data.json` | Response from `GET /api/data?limit=3` |
| `api_sync_trigger.json` | Response from `POST /api/sync/trigger` |
