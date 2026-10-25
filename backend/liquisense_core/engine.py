"""Transparent cash-flow calculations for LiquiSense's hackathon prototype."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import pandas as pd


@dataclass
class ForecastResult:
    daily: pd.DataFrame
    receivables: pd.DataFrame
    payables: pd.DataFrame
    reserve_breach_date: date | None
    negative_balance_days: int
    minimum_balance: float
    minimum_balance_date: date
    ending_balance: float


def _parse_dates(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce").dt.date


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if pd.isna(value):
        return False
    return str(value).strip().lower() in {"true", "1", "yes", "y", "flexible"}


def clean_receivables(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize receivables CSV. Required columns: customer, amount, due_date."""
    expected = ["customer", "invoice_id", "amount", "due_date", "status", "avg_delay_days"]
    df = df.copy()
    for col in expected:
        if col not in df.columns:
            df[col] = None
    df["customer"] = df["customer"].fillna("").astype(str).str.strip()
    df["invoice_id"] = df["invoice_id"].fillna("").astype(str).str.strip()
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    df["due_date"] = _parse_dates(df["due_date"])
    df["status"] = df["status"].fillna("outstanding").astype(str).str.strip().str.lower()
    df["avg_delay_days"] = pd.to_numeric(df["avg_delay_days"], errors="coerce")
    df["avg_delay_days"] = df["avg_delay_days"].clip(lower=0, upper=90)
    # Ignore only rows explicitly marked as paid/settled; retain incomplete rows for data-quality warnings.
    df = df[~df["status"].isin({"paid", "settled", "closed", "received"})].reset_index(drop=True)
    return df


def clean_payables(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize payables CSV. Required columns: vendor, amount, due_date."""
    expected = ["vendor", "bill_id", "amount", "due_date", "category", "flexible", "status"]
    df = df.copy()
    for col in expected:
        if col not in df.columns:
            df[col] = None
    df["vendor"] = df["vendor"].fillna("").astype(str).str.strip()
    df["bill_id"] = df["bill_id"].fillna("").astype(str).str.strip()
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    df["due_date"] = _parse_dates(df["due_date"])
    df["category"] = df["category"].fillna("Other").astype(str).str.strip()
    df["flexible"] = df["flexible"].map(_to_bool)
    df["status"] = df["status"].fillna("unpaid").astype(str).str.strip().str.lower()
    df = df[~df["status"].isin({"paid", "settled", "closed"})].reset_index(drop=True)
    return df


def clean_bank_transactions(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize a bank transactions CSV for reconciliation/data-quality checks."""
    df = df.copy()
    expected = ["date", "description", "direction", "amount"]
    for col in expected:
        if col not in df.columns:
            df[col] = None
    df["date"] = _parse_dates(df["date"])
    df["description"] = df["description"].fillna("").astype(str).str.strip()
    df["direction"] = df["direction"].fillna("").astype(str).str.strip().str.lower()
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    return df


def build_forecast(
    opening_cash: float,
    receivables: pd.DataFrame,
    payables: pd.DataFrame,
    start_date: date | None = None,
    horizon_days: int = 30,
    reserve_threshold: float = 10000,
    customer_delay_shock_days: int = 0,
    sales_drop_pct: float = 0,
    emergency_expense: float = 0,
    emergency_expense_day: int = 7,
    flexible_payables_shift_days: int = 0,
) -> ForecastResult:
    """Calculate a dated daily balance forecast. All figures are illustrative estimates, not guarantees."""
    start_date = start_date or date.today()
    horizon_days = max(1, min(int(horizon_days), 90))
    sales_drop_pct = max(0.0, min(100.0, float(sales_drop_pct)))
    delay_shock = max(0, min(90, int(customer_delay_shock_days)))
    flexible_shift = max(0, min(30, int(flexible_payables_shift_days)))

    rec = clean_receivables(receivables)
    pay = clean_payables(payables)

    rec_valid = rec[rec["amount"].notna() & rec["due_date"].notna()].copy()
    rec_valid["avg_delay_days"] = rec_valid["avg_delay_days"].fillna(0)
    if rec_valid.empty:
        rec_valid["expected_date"] = pd.Series(dtype="object")
        rec_valid["forecast_amount"] = pd.Series(dtype="float64")
    else:
        rec_valid["expected_date"] = rec_valid.apply(
            lambda r: max(start_date, r["due_date"] + timedelta(days=int(r["avg_delay_days"] or 0) + delay_shock)), axis=1
        )
        rec_valid["forecast_amount"] = rec_valid["amount"] * (1 - sales_drop_pct / 100.0)

    pay_valid = pay[pay["amount"].notna() & pay["due_date"].notna()].copy()
    if pay_valid.empty:
        pay_valid["forecast_date"] = pd.Series(dtype="object")
    else:
        pay_valid["forecast_date"] = pay_valid.apply(
            lambda r: max(start_date, r["due_date"] + timedelta(days=flexible_shift if r["flexible"] else 0)), axis=1
        )

    dates = [start_date + timedelta(days=i) for i in range(horizon_days + 1)]
    rows: list[dict[str, Any]] = []
    balance = float(opening_cash)
    rec_by_date = rec_valid.groupby("expected_date")["forecast_amount"].sum().to_dict() if not rec_valid.empty else {}
    pay_by_date = pay_valid.groupby("forecast_date")["amount"].sum().to_dict() if not pay_valid.empty else {}

    for idx, day in enumerate(dates):
        # Each plotted date shows the projected end-of-day balance, including events scheduled for that day.
        inflow = float(rec_by_date.get(day, 0.0))
        outflow = float(pay_by_date.get(day, 0.0))
        emergency_today = float(emergency_expense) if int(emergency_expense_day) == idx else 0.0
        balance = balance + inflow - outflow - emergency_today
        rows.append({
            "date": day,
            "inflows": inflow,
            "outflows": outflow,
            "emergency_expense": emergency_today,
            "projected_balance": balance,
            "reserve_threshold": float(reserve_threshold),
            "below_reserve": balance < float(reserve_threshold),
            "negative_balance": balance < 0,
        })

    daily = pd.DataFrame(rows)
    breach_rows = daily[daily["projected_balance"] < float(reserve_threshold)]
    min_idx = daily["projected_balance"].idxmin()
    return ForecastResult(
        daily=daily,
        receivables=rec_valid,
        payables=pay_valid,
        reserve_breach_date=breach_rows.iloc[0]["date"] if not breach_rows.empty else None,
        negative_balance_days=int(daily["negative_balance"].sum()),
        minimum_balance=float(daily.loc[min_idx, "projected_balance"]),
        minimum_balance_date=daily.loc[min_idx, "date"],
        ending_balance=float(daily.iloc[-1]["projected_balance"]),
    )


def data_quality_checks(bank: pd.DataFrame, receivables: pd.DataFrame, payables: pd.DataFrame, today: date | None = None) -> list[dict[str, str]]:
    """Provide transparent checks. These checks can find some gaps; they cannot prove all activity is recorded."""
    today = today or date.today()
    issues: list[dict[str, str]] = []
    rec = receivables.copy()
    pay = payables.copy()
    txn = bank.copy()

    rec_missing = int(rec["amount"].isna().sum() + rec["due_date"].isna().sum()) if not rec.empty else 0
    pay_missing = int(pay["amount"].isna().sum() + pay["due_date"].isna().sum()) if not pay.empty else 0
    if rec_missing:
        issues.append({"severity": "Review", "title": "Incomplete receivable records", "detail": f"Found {rec_missing} missing amount/date field(s). These invoices were excluded from the forecast until corrected."})
    if pay_missing:
        issues.append({"severity": "Review", "title": "Incomplete payable records", "detail": f"Found {pay_missing} missing amount/date field(s). These bills were excluded from the forecast until corrected."})
    for frame, name_column, label in ((rec, "customer", "receipt"), (pay, "vendor", "bill")):
        if not frame.empty and {name_column, "amount", "due_date"}.issubset(frame.columns):
            valid=frame[frame[name_column].notna() & frame["amount"].notna() & frame["due_date"].notna()].copy()
            if not valid.empty:
                valid["_key_name"]=valid[name_column].astype(str).str.strip().str.casefold()
                valid["_key_amount"]=pd.to_numeric(valid["amount"],errors="coerce").round(2)
                valid["_key_date"]=valid["due_date"].astype(str)
                duplicate_count=int(valid.duplicated(subset=["_key_name","_key_amount","_key_date"]).sum())
                if duplicate_count:
                    issues.append({"severity":"Review","title":f"Possible duplicate {label}s","detail":f"Found {duplicate_count} row(s) with the same name, date, and amount. Review before using these totals."})
    overdue_receipts=int((rec["due_date"].notna() & (rec["due_date"] < today)).sum()) if not rec.empty and "due_date" in rec.columns else 0
    overdue_bills=int((pay["due_date"].notna() & (pay["due_date"] < today)).sum()) if not pay.empty and "due_date" in pay.columns else 0
    if overdue_receipts or overdue_bills:
        issues.append({"severity":"Review","title":"Unpaid past-due items","detail":f"{overdue_receipts} receipt(s) and {overdue_bills} bill(s) are past due. Confirm their current status and expected payment dates."})

    if not rec.empty and "avg_delay_days" in rec.columns:
        missing_delay = int(rec[rec["amount"].notna() & rec["due_date"].notna()]["avg_delay_days"].isna().sum())
        if missing_delay:
            issues.append({"severity": "Info", "title": "Payment history unavailable", "detail": f"{missing_delay} receivable(s) have no historical delay estimate. Forecast uses the due date as a starting assumption; actual timing may differ."})

    if not txn.empty:
        valid_dates = txn["date"].dropna()
        if valid_dates.empty:
            issues.append({"severity": "Review", "title": "Bank transaction dates unavailable", "detail": "Could not parse dates in the bank transaction file. Check the CSV date format."})
        else:
            latest = max(valid_dates)
            if (today - latest).days > 3:
                issues.append({"severity": "Review", "title": "Bank data may be stale", "detail": f"Latest imported bank transaction is {latest}. Confirm whether newer activity exists."})
        duplicate_cols = [c for c in ["date", "description", "direction", "amount"] if c in txn.columns]
        if duplicate_cols and txn.duplicated(subset=duplicate_cols).any():
            dup_count = int(txn.duplicated(subset=duplicate_cols).sum())
            issues.append({"severity": "Review", "title": "Possible duplicate transactions", "detail": f"Found {dup_count} exact duplicate row(s). Review before using imported totals."})
    else:
        issues.append({"severity": "Info", "title": "No bank transaction file supplied", "detail": "The prototype can still forecast from the entered opening balance, invoices and bills. Import recent transactions for reconciliation checks."})

    if not issues:
        issues.append({"severity": "OK", "title": "Basic data checks passed", "detail": "No obvious missing amounts/dates or duplicate bank rows were detected. This does not guarantee every transaction has been recorded."})
    return issues


def build_cash_alerts(
    daily: pd.DataFrame,
    alert_threshold: float,
    reserve_threshold: float,
) -> pd.DataFrame:
    """Return the first forecast date each cash-risk threshold is crossed.

    The early-warning threshold is inclusive (cash <= threshold); reserve breach
    and negative-balance alerts use strict less-than comparisons.
    """
    columns = ["alert_type", "severity", "date", "projected_balance", "trigger_amount", "message"]
    if daily.empty or "projected_balance" not in daily.columns or "date" not in daily.columns:
        return pd.DataFrame(columns=columns)

    rules = [
        (
            "Early cash warning",
            "Warning",
            float(alert_threshold),
            daily["projected_balance"] <= float(alert_threshold),
            "Projected cash reaches or falls below the configured early-warning limit.",
        ),
        (
            "Safety reserve breach",
            "High",
            float(reserve_threshold),
            daily["projected_balance"] < float(reserve_threshold),
            "Projected cash falls below the minimum safety reserve.",
        ),
        (
            "Negative projected cash",
            "Critical",
            0.0,
            daily["projected_balance"] < 0,
            "Projected cash becomes negative in this scenario.",
        ),
    ]
    alerts = []
    for alert_type, severity, trigger_amount, mask, message in rules:
        crossed = daily.loc[mask]
        if crossed.empty:
            continue
        first = crossed.iloc[0]
        alerts.append({
            "alert_type": alert_type,
            "severity": severity,
            "date": first["date"],
            "projected_balance": float(first["projected_balance"]),
            "trigger_amount": trigger_amount,
            "message": message,
        })
    return pd.DataFrame(alerts, columns=columns).sort_values("date", kind="stable").reset_index(drop=True)
