# LiquiSense dashboard

Run the connected frontend and backend on Windows:

1. Open `backend` and double-click `run.bat`.
2. Wait for the server to report that it is running.
3. Open **http://127.0.0.1:8000** (do not open `index.html` as a `file:///` URL).

The dashboard preserves the clean white UI and now reads forecasts, transactions, data-quality issues, Ripple Lab simulations, and Rescue Planner results from a local FastAPI service. Forecast/ripple logic is reused from the supplied LiquiSense project. Demo invoice, bill, and business-network inputs are fictional. Added transactions/imported CSV rows are held in a local SQLite database; future-dated entries are included in the cash forecast.

Backend API reference: `http://127.0.0.1:8000/docs` after startup. More setup details are in `backend/README.md`.

On the dashboard, use **Edit Cash & Reserve** to set opening cash and the safety reserve, **Manage Receipts & Bills** to change/add/remove scheduled invoices and bills, or **Add Upcoming Item** for a one-off receipt/payment. Future-dated entries update the forecast. Cash settings are stored in SQLite; the scheduled invoice and bill lists are saved as local CSV files.

The dashboard also has a visible **Forecast Inputs** form: choose Expected income or Upcoming expense, enter a name, amount, and date, then select **Add to forecast**. Use **Delete** beside a scheduled item to remove it. The totals and chart recalculate after each change.

Records import supports CSV plus PDF and common image formats. PDF extraction and local OCR create candidate rows; review and correct them before confirming import. Uploaded files are processed in memory, and duplicate transaction rows are skipped.
