# LiquiSense local backend

This backend serves the preserved LiquiSense dashboard at `http://127.0.0.1:8000` and exposes a local FastAPI API. Forecast and ripple calculations reuse `engine.py` and `ripple_engine.py`. Demo inputs are in `data/`; captured/imported transactions are stored locally in SQLite.

## Start on Windows

1. Double-click `run.bat` (requires Python 3.12 and internet access for first-time dependency installation).
2. Open `http://127.0.0.1:8000`.
3. Keep the terminal window open while using the dashboard.

Open the `http://` address rather than the `file:///.../index.html` file. API docs are at `http://127.0.0.1:8000/docs`.

## Main endpoints

- `GET /api/health`
- `GET /api/dashboard`
- `GET, POST /api/transactions`
- `POST /api/import-csv` (CSV headers: `date,description,direction,amount`; direction is `inflow` or `outflow`)
- `POST /api/import-document?filename=...` (PDF and image extraction; returns candidate rows for review)
- `POST /api/import-document/confirm` (saves only user-confirmed rows and skips duplicates)
- `GET /api/forecast` (query controls: horizon, starting cash, reserve, delay, sales drop, emergency expense)
- `GET /api/ripple` and `GET /api/rescue`

All currency is INR. The supplied records and network are fictional demo data; forecasts and scenarios are estimates, not guarantees. Captured transactions are stored in `data/liquisense.sqlite3` on this computer.

On the dashboard, use **Edit Cash & Reserve** to set opening cash and the safety reserve, **Manage Receipts & Bills** to change/add/remove scheduled invoices and bills, or **Add Upcoming Item** for a one-off receipt/payment. Future-dated entries update the forecast. Cash settings are stored in SQLite; the scheduled invoice and bill lists are saved as local CSV files.

The dashboard also has a visible **Forecast Inputs** form: choose Expected income or Upcoming expense, enter a name, amount, and date, then select **Add to forecast**. Use **Delete** beside a scheduled item to remove it. The totals and chart recalculate after each change.


The Records page accepts CSV, text/scanned PDF, PNG, JPG/JPEG, WebP, BMP, TIF, and TIFF. PDF text extraction and OCR for scans/images run locally. Select **Import File**, choose a supported file, review or correct each detected row, then select **Import selected rows**. Nothing from a PDF or image is saved before confirmation. CSV rows are imported directly using the required headers above. Duplicate transaction rows are skipped. Check extracted dates, descriptions, direction, and amounts because document layouts and OCR can be ambiguous. Uploaded document contents are processed in memory and not retained by the importer. Files are limited to 20 MB and PDF imports to 20 pages.
