"""Transparent supply-chain payment-ripple simulation for LiquiSense.

This is a deterministic stress-test, not a prediction of what real counterparties will do.
It assumes incoming transfers become available to recipients at end of day and that a payer
cannot make a whole invoice payment if it would drive its cash balance below zero. Unpaid
obligations are retried daily. Such delays are simulated assumptions, not observed defaults.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import networkx as nx
import pandas as pd


@dataclass
class RippleResult:
    daily: pd.DataFrame
    businesses: pd.DataFrame
    payments: pd.DataFrame
    chunks: pd.DataFrame


def sample_network(start_date: date) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return clearly fictional data designed to demonstrate payment-delay propagation."""
    businesses = pd.DataFrame([
        {"business_id": "BUYER", "business": "Anchor Retail Group", "role": "Large buyer", "opening_cash": 500000.0, "reserve": 100000.0},
        {"business_id": "MSME", "business": "Sahyadri Distribution", "role": "MSME distributor", "opening_cash": 60000.0, "reserve": 40000.0},
        {"business_id": "PACK", "business": "PackRight Packaging", "role": "Packaging supplier", "opening_cash": 40000.0, "reserve": 20000.0},
        {"business_id": "RAW", "business": "Prime Raw Materials", "role": "Raw-material supplier", "opening_cash": 30000.0, "reserve": 15000.0},
        {"business_id": "PROCESS", "business": "Local Processing Unit", "role": "Downstream processor", "opening_cash": 25000.0, "reserve": 10000.0},
        {"business_id": "TRANS", "business": "Swift Transport Partner", "role": "Transport partner", "opening_cash": 35000.0, "reserve": 18000.0},
    ])
    payments = pd.DataFrame([
        {"payment_id": "PAY-01", "payer_id": "BUYER", "payee_id": "MSME", "amount": 200000.0, "due_date": start_date + timedelta(days=5), "description": "Retail invoice INV-201"},
        {"payment_id": "PAY-02", "payer_id": "MSME", "payee_id": "PACK", "amount": 120000.0, "due_date": start_date + timedelta(days=7), "description": "Packaging bill PK-105"},
        {"payment_id": "PAY-03", "payer_id": "PACK", "payee_id": "RAW", "amount": 70000.0, "due_date": start_date + timedelta(days=9), "description": "Material bill RM-88"},
        {"payment_id": "PAY-04", "payer_id": "RAW", "payee_id": "PROCESS", "amount": 45000.0, "due_date": start_date + timedelta(days=11), "description": "Processing order PO-41"},
        {"payment_id": "PAY-05", "payer_id": "MSME", "payee_id": "TRANS", "amount": 25000.0, "due_date": start_date + timedelta(days=8), "description": "Transport bill TR-18"},
    ])
    return businesses, payments


def build_payment_graph(businesses: pd.DataFrame, payments: pd.DataFrame) -> nx.DiGraph:
    """Build a directed payment dependency graph; an edge means payer -> payee."""
    graph = nx.DiGraph()
    for row in businesses.to_dict("records"):
        graph.add_node(row["business_id"], label=row["business"], role=row.get("role", ""))
    for row in payments.to_dict("records"):
        graph.add_edge(row["payer_id"], row["payee_id"], payment_id=row["payment_id"], amount=float(row["amount"]))
    return graph


def downstream_payment_ids(businesses: pd.DataFrame, payments: pd.DataFrame, payment_id: str) -> set[str]:
    """Return payments downstream of the selected payment's recipient."""
    selected = payments[payments["payment_id"].astype(str) == str(payment_id)]
    if selected.empty:
        return set()
    graph = build_payment_graph(businesses, payments)
    recipient = str(selected.iloc[0]["payee_id"])
    downstream_businesses = {recipient} | nx.descendants(graph, recipient)
    return set(payments.loc[
        payments["payer_id"].astype(str).isin(downstream_businesses)
        & (payments["payment_id"].astype(str) != str(payment_id)),
        "payment_id",
    ].astype(str))


def _as_date(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, pd.Timestamp):
        return value
    return pd.to_datetime(value, errors="raise").date()


def _prepare_chunks(
    payments: pd.DataFrame,
    start_date: date,
    shock_payment_id: str | None,
    delay_days: int,
    early_collection_pct: int,
    early_discount_pct: float = 0.0,
) -> pd.DataFrame:
    """Split an invoice into dated face-value pieces and actual cash transfers.

    If an early partial payment is negotiated at a discount, the invoice can be
    settled for its face value while the actual cash transfer is lower. Keeping
    both values separate prevents a discount from being misreported as unpaid debt.
    """
    chunks: list[dict[str, Any]] = []
    delay_days = max(0, min(90, int(delay_days)))
    early_pct = max(0, min(100, int(early_collection_pct)))
    early_discount_pct = max(0.0, min(20.0, float(early_discount_pct)))

    for row in payments.to_dict("records"):
        payment_id = str(row["payment_id"])
        amount = float(row["amount"])
        original_due = _as_date(row["due_date"])
        is_shocked = payment_id == str(shock_payment_id) and delay_days > 0
        common = {
            "payment_id": payment_id,
            "payer_id": str(row["payer_id"]),
            "payee_id": str(row["payee_id"]),
            "original_due_date": original_due,
            "description": str(row.get("description", payment_id)),
            "shock_payment": bool(is_shocked),
        }
        if is_shocked and early_pct > 0:
            early_amount = round(amount * early_pct / 100.0, 2)  # face value settled early
            late_amount = round(amount - early_amount, 2)
            early_cash = round(early_amount * (1.0 - early_discount_pct / 100.0), 2)
            discount_cost = round(early_amount - early_cash, 2)
            if early_amount > 0:
                chunks.append({**common, "chunk_id": f"{payment_id}-EARLY", "amount": early_amount, "cash_amount": early_cash, "discount_amount": discount_cost, "scheduled_date": max(start_date, original_due), "piece": "Early partial collection"})
            if late_amount > 0:
                chunks.append({**common, "chunk_id": f"{payment_id}-DELAYED", "amount": late_amount, "cash_amount": late_amount, "discount_amount": 0.0, "scheduled_date": max(start_date, original_due + timedelta(days=delay_days)), "piece": "Delayed remainder"})
        else:
            scheduled_date = max(start_date, original_due + timedelta(days=delay_days if is_shocked else 0))
            chunks.append({**common, "chunk_id": f"{payment_id}-FULL", "amount": amount, "cash_amount": amount, "discount_amount": 0.0, "scheduled_date": scheduled_date, "piece": "Full payment"})

    chunk_df = pd.DataFrame(chunks)
    if chunk_df.empty:
        return pd.DataFrame(columns=["chunk_id", "payment_id", "payer_id", "payee_id", "amount", "cash_amount", "discount_amount", "original_due_date", "scheduled_date", "description", "shock_payment", "piece", "paid_date", "cash_constrained"])
    chunk_df["paid_date"] = None
    chunk_df["cash_constrained"] = False
    return chunk_df


def simulate_network(
    businesses: pd.DataFrame,
    payments: pd.DataFrame,
    start_date: date | None = None,
    horizon_days: int = 30,
    shock_payment_id: str | None = None,
    delay_days: int = 0,
    early_collection_pct: int = 0,
    early_discount_pct: float = 0.0,
) -> RippleResult:
    """Simulate cross-business cash transfers and daily cash-constrained payment deferrals.

    Transfer policy: outgoing invoice payments are attempted at start-of-day using cash then
    available. Receipts arrive to the recipient at end-of-day and can be used the next day.
    A payment is made in full only if it does not drive payer cash below zero; otherwise it is
    retried the next day. The model does not infer actual default or legal payment rights.
    """
    start_date = start_date or date.today()
    horizon_days = max(1, min(90, int(horizon_days)))
    biz = businesses.copy().reset_index(drop=True)
    pay = payments.copy().reset_index(drop=True)
    required_business_cols = {"business_id", "business", "opening_cash", "reserve"}
    required_payment_cols = {"payment_id", "payer_id", "payee_id", "amount", "due_date"}
    if not required_business_cols.issubset(biz.columns):
        raise ValueError(f"Businesses must include: {sorted(required_business_cols)}")
    if not required_payment_cols.issubset(pay.columns):
        raise ValueError(f"Payments must include: {sorted(required_payment_cols)}")
    if biz["business_id"].astype(str).duplicated().any():
        raise ValueError("Business IDs must be unique.")
    if pay["payment_id"].astype(str).duplicated().any():
        raise ValueError("Payment IDs must be unique.")
    known_ids = set(biz["business_id"].astype(str))
    if not set(pay["payer_id"].astype(str)).issubset(known_ids) or not set(pay["payee_id"].astype(str)).issubset(known_ids):
        raise ValueError("Every payment payer and payee must exist in the business table.")
    amounts = pd.to_numeric(pay["amount"], errors="coerce")
    if amounts.isna().any() or (amounts <= 0).any():
        raise ValueError("Payment amounts must be positive numbers.")

    biz["business_id"] = biz["business_id"].astype(str)
    biz["opening_cash"] = pd.to_numeric(biz["opening_cash"], errors="raise").astype(float)
    biz["reserve"] = pd.to_numeric(biz["reserve"], errors="raise").astype(float)
    pay["payment_id"] = pay["payment_id"].astype(str)
    pay["payer_id"] = pay["payer_id"].astype(str)
    pay["payee_id"] = pay["payee_id"].astype(str)
    pay["amount"] = amounts.astype(float)
    pay["due_date"] = pay["due_date"].apply(_as_date)

    early_discount_pct = max(0.0, min(20.0, float(early_discount_pct)))
    chunks = _prepare_chunks(pay, start_date, shock_payment_id, delay_days, early_collection_pct, early_discount_pct)
    balances = dict(zip(biz["business_id"], biz["opening_cash"]))
    business_name = dict(zip(biz["business_id"], biz["business"]))
    reserve_map = dict(zip(biz["business_id"], biz["reserve"]))
    daily_rows: list[dict[str, Any]] = []
    chunk_paid_dates: dict[str, date | None] = {str(cid): None for cid in chunks.get("chunk_id", pd.Series(dtype=str)).tolist()}
    chunk_cash_constrained: set[str] = set()

    for day_index in range(horizon_days + 1):
        day = start_date + timedelta(days=day_index)
        cash_working = balances.copy()  # Receipts on this day are not spendable until end of day.
        incoming_end_of_day = {business_id: 0.0 for business_id in balances}
        outgoing_today = {business_id: 0.0 for business_id in balances}

        due_indices = chunks.index[
            chunks["paid_date"].isna() & chunks["scheduled_date"].apply(lambda d: d <= day)
        ].tolist()
        due_indices.sort(key=lambda idx: (
            chunks.at[idx, "scheduled_date"],
            chunks.at[idx, "original_due_date"],
            chunks.at[idx, "payment_id"],
            chunks.at[idx, "chunk_id"],
        ))

        for idx in due_indices:
            amount = float(chunks.at[idx, "amount"])  # invoice face value settled
            cash_amount = float(chunks.at[idx, "cash_amount"])  # actual cash transferred after any discount
            payer = str(chunks.at[idx, "payer_id"])
            payee = str(chunks.at[idx, "payee_id"])
            chunk_id = str(chunks.at[idx, "chunk_id"])
            if cash_working[payer] + 1e-9 >= cash_amount:
                cash_working[payer] -= cash_amount
                outgoing_today[payer] += cash_amount
                incoming_end_of_day[payee] += cash_amount
                chunks.at[idx, "paid_date"] = day
                chunk_paid_dates[chunk_id] = day
            else:
                chunk_cash_constrained.add(chunk_id)
                chunks.at[idx, "cash_constrained"] = True

        # Receipts are posted after all payers have attempted their own due payments.
        balances = {bid: cash_working[bid] + incoming_end_of_day[bid] for bid in cash_working}
        for bid in balances:
            daily_rows.append({
                "date": day,
                "business_id": bid,
                "business": business_name[bid],
                "opening_cash": float(cash_working[bid] + outgoing_today[bid]),
                "inflows": float(incoming_end_of_day[bid]),
                "outflows": float(outgoing_today[bid]),
                "projected_cash": float(balances[bid]),
                "reserve": float(reserve_map[bid]),
                "below_reserve": bool(balances[bid] < float(reserve_map[bid])),
                "negative_cash": bool(balances[bid] < 0),
            })

    daily = pd.DataFrame(daily_rows)
    horizon_end = start_date + timedelta(days=horizon_days)
    payment_rows: list[dict[str, Any]] = []
    original_payments = pay.set_index("payment_id").to_dict("index")
    for payment_id, original in original_payments.items():
        pieces = chunks[chunks["payment_id"] == payment_id]
        amount = float(original["amount"])
        due = _as_date(original["due_date"])
        paid = pieces[pieces["paid_date"].notna()].copy()
        unpaid = pieces[pieces["paid_date"].isna()].copy()
        paid_amount = float(paid["amount"].sum()) if not paid.empty else 0.0
        on_time_amount = float(paid.loc[paid["paid_date"].apply(lambda d: d <= due), "amount"].sum()) if not paid.empty else 0.0
        late_paid_amount = float(paid.loc[paid["paid_date"].apply(lambda d: d > due), "amount"].sum()) if not paid.empty else 0.0
        unpaid_amount = float(unpaid["amount"].sum()) if not unpaid.empty else 0.0
        late_amount = late_paid_amount + (unpaid_amount if horizon_end >= due else 0.0)
        last_paid_date = max(paid["paid_date"].tolist()) if not paid.empty else None
        paid_late = late_paid_amount > 0.0 or unpaid_amount > 0.0 and horizon_end >= due
        if unpaid_amount > 0 and horizon_end >= due:
            status = "Overdue / partly unpaid" if paid_amount > 0 else "Overdue"
        elif unpaid_amount > 0:
            status = "Partially paid / pending" if paid_amount > 0 else "Upcoming"
        elif late_paid_amount > 0 and on_time_amount > 0:
            status = "Partially late"
        elif late_paid_amount > 0:
            status = "Paid late"
        else:
            status = "Paid on time" if paid_amount >= amount - 0.01 else "Upcoming"
        days_late = 0
        if paid_late:
            # If any amount remains unpaid at the end of the forecast, report lateness as of
            # the forecast end even when an earlier partial payment was received on time.
            if unpaid_amount > 0 and horizon_end >= due:
                reference_date = horizon_end
            else:
                reference_date = last_paid_date if last_paid_date is not None else horizon_end
            days_late = max(0, (reference_date - due).days)
        original_chunk_rows = chunks[chunks["payment_id"] == payment_id]
        cash_constrained = bool(original_chunk_rows["cash_constrained"].any())
        payment_rows.append({
            "payment_id": payment_id,
            "description": original.get("description", payment_id),
            "payer_id": str(original["payer_id"]),
            "payee_id": str(original["payee_id"]),
            "amount": amount,
            "due_date": due,
            "paid_amount": paid_amount,
            "discount_amount": float(paid["discount_amount"].sum()) if not paid.empty and "discount_amount" in paid.columns else 0.0,
            "on_time_amount": on_time_amount,
            "late_paid_amount": late_paid_amount,
            "unpaid_amount": unpaid_amount,
            "late_amount": late_amount,
            "last_paid_date": last_paid_date,
            "days_late": days_late,
            "status": status,
            "cash_constrained": cash_constrained,
            "shock_payment": bool(original_chunk_rows["shock_payment"].any()),
        })
    payment_summary = pd.DataFrame(payment_rows)

    business_rows: list[dict[str, Any]] = []
    graph = build_payment_graph(biz, pay)
    for row in biz.to_dict("records"):
        bid = str(row["business_id"])
        timeline = daily[daily["business_id"] == bid]
        min_idx = timeline["projected_cash"].idxmin()
        min_row = daily.loc[min_idx]
        incoming_payments = payment_summary[payment_summary["payee_id"] == bid]
        outgoing_payments = payment_summary[payment_summary["payer_id"] == bid]
        late_incoming_count = int((incoming_payments["late_amount"] > 0).sum())
        cash_constrained_outgoing_count = int(outgoing_payments["cash_constrained"].sum())
        delayed_outgoing_count = int((outgoing_payments["late_amount"] > 0).sum())
        reserve_days = int(timeline["below_reserve"].sum())
        reserve_breached = reserve_days > 0
        if reserve_breached or cash_constrained_outgoing_count > 0:
            status = "Cash-constrained"
        elif delayed_outgoing_count > 0:
            status = "Delay trigger"
        elif late_incoming_count > 0:
            status = "Late receipt"
        else:
            status = "Stable"
        business_rows.append({
            "business_id": bid,
            "business": row["business"],
            "role": row.get("role", ""),
            "opening_cash": float(row["opening_cash"]),
            "reserve": float(row["reserve"]),
            "minimum_cash": float(min_row["projected_cash"]),
            "minimum_date": min_row["date"],
            "ending_cash": float(timeline.iloc[-1]["projected_cash"]),
            "reserve_breach_days": reserve_days,
            "reserve_breached": reserve_breached,
            "late_incoming_count": late_incoming_count,
            "delayed_outgoing_count": delayed_outgoing_count,
            "cash_constrained_outgoing_count": cash_constrained_outgoing_count,
            "status": status,
        })
    business_summary = pd.DataFrame(business_rows)

    return RippleResult(daily=daily, businesses=business_summary, payments=payment_summary, chunks=chunks)


def rescue_comparison(
    businesses: pd.DataFrame,
    payments: pd.DataFrame,
    start_date: date,
    horizon_days: int,
    shock_payment_id: str,
    delay_days: int,
    candidate_percentages: tuple[int, ...] = (0, 25, 50, 75, 100),
    early_discount_pct: float = 0.0,
) -> tuple[pd.DataFrame, int | None]:
    """Find the smallest tested early-payment share that protects downstream obligations and reserves.

    Every row is a hypothetical agreement: the buyer pays a portion on the original due date and
    the remainder after the configured delay. No agreement or financing is assumed to exist.
    """
    down_ids = downstream_payment_ids(businesses, payments, shock_payment_id)
    focus = payments[payments["payment_id"].astype(str) == str(shock_payment_id)]
    if focus.empty:
        raise ValueError(f"Unknown shock payment ID: {shock_payment_id}")
    focus_amount = float(focus.iloc[0]["amount"])
    focus_payee = str(focus.iloc[0]["payee_id"])
    graph = build_payment_graph(businesses, payments)
    affected_business_ids = {focus_payee} | nx.descendants(graph, focus_payee)
    rows: list[dict[str, Any]] = []

    for pct in candidate_percentages:
        result = simulate_network(
            businesses, payments, start_date=start_date, horizon_days=horizon_days,
            shock_payment_id=shock_payment_id, delay_days=delay_days, early_collection_pct=pct,
            early_discount_pct=early_discount_pct,
        )
        late_downstream = result.payments[
            result.payments["payment_id"].astype(str).isin(down_ids) & (result.payments["late_amount"] > 0)
        ]
        affected_biz = result.businesses[result.businesses["business_id"].astype(str).isin(affected_business_ids)]
        reserve_days = int(affected_biz["reserve_breach_days"].sum())
        reserve_breaches = int(affected_biz["reserve_breached"].sum())
        downstream_late_value = float(late_downstream["late_amount"].sum())
        protected = len(late_downstream) == 0 and reserve_days == 0
        rows.append({
            "early_pct": int(pct),
            "early_amount": round(focus_amount * int(pct) / 100.0, 2),
            "downstream_late_count": int(len(late_downstream)),
            "downstream_late_amount": downstream_late_value,
            "affected_reserve_breaches": reserve_breaches,
            "affected_reserve_breach_days": reserve_days,
            "total_late_amount": float(result.payments["late_amount"].sum()),
            "protects_chain": protected,
        })

    comparison = pd.DataFrame(rows).sort_values("early_pct").reset_index(drop=True)
    eligible = comparison[comparison["protects_chain"]]
    recommended = int(eligible.iloc[0]["early_pct"]) if not eligible.empty else None
    return comparison, recommended


def optimize_rescue_plan(
    businesses: pd.DataFrame,
    payments: pd.DataFrame,
    start_date: date,
    horizon_days: int,
    shock_payment_id: str,
    delay_days: int,
    early_discount_pct: float = 2.0,
    extension_payment_id: str | None = None,
    extension_days_options: tuple[int, ...] = (0, 3, 7),
    extension_fee_pct_per_7_days: float = 0.5,
    candidate_percentages: tuple[int, ...] = (0, 25, 50, 75, 100),
) -> tuple[pd.DataFrame, dict[str, Any] | None, bool]:
    """Search combinations of a discounted early receipt and one agreed bill extension.

    This is a deterministic brute-force optimizer over a transparent, small action grid.
    A plan is considered feasible only if no affected business breaches its reserve and
    no downstream invoice is late under the scenario's explicitly revised terms.
    The extension and discount are hypothetical negotiated terms, never automatic actions.
    """
    if delay_days <= 0:
        raise ValueError("A positive payment delay is required to optimize a rescue plan.")

    early_discount_pct = max(0.0, min(20.0, float(early_discount_pct)))
    extension_fee_pct_per_7_days = max(0.0, min(10.0, float(extension_fee_pct_per_7_days)))
    percentages = tuple(sorted({max(0, min(100, int(p))) for p in candidate_percentages}))
    extension_days_grid = tuple(sorted({max(0, min(30, int(d))) for d in extension_days_options}))
    if not extension_days_grid:
        extension_days_grid = (0,)
    if extension_payment_id is None:
        extension_days_grid = (0,)

    selected = payments[payments["payment_id"].astype(str) == str(shock_payment_id)]
    if selected.empty:
        raise ValueError(f"Unknown shock payment ID: {shock_payment_id}")
    downstream_ids = downstream_payment_ids(businesses, payments, shock_payment_id)
    if extension_payment_id is not None and str(extension_payment_id) not in downstream_ids:
        raise ValueError("The extension action must target a modeled downstream payment.")

    focus_payee = str(selected.iloc[0]["payee_id"])
    graph = build_payment_graph(businesses, payments)
    affected_ids = {focus_payee} | nx.descendants(graph, focus_payee)
    base_extension_amount = 0.0
    if extension_payment_id is not None:
        ext = payments[payments["payment_id"].astype(str) == str(extension_payment_id)]
        if ext.empty:
            raise ValueError(f"Unknown extension payment ID: {extension_payment_id}")
        base_extension_amount = float(ext.iloc[0]["amount"])

    rows: list[dict[str, Any]] = []
    for early_pct in percentages:
        for shift_days in extension_days_grid:
            scenario_payments = payments.copy(deep=True)
            extension_fee = 0.0
            if extension_payment_id is not None and shift_days > 0:
                ext_mask = scenario_payments["payment_id"].astype(str) == str(extension_payment_id)
                extension_fee = round(base_extension_amount * (extension_fee_pct_per_7_days / 100.0) * (shift_days / 7.0), 2)
                original_amount = float(scenario_payments.loc[ext_mask, "amount"].iloc[0])
                original_due = _as_date(scenario_payments.loc[ext_mask, "due_date"].iloc[0])
                # The optional fee is modeled as part of the revised supplier settlement.
                scenario_payments.loc[ext_mask, "amount"] = original_amount + extension_fee
                scenario_payments.loc[ext_mask, "due_date"] = original_due + timedelta(days=shift_days)

            result = simulate_network(
                businesses,
                scenario_payments,
                start_date=start_date,
                horizon_days=horizon_days,
                shock_payment_id=shock_payment_id,
                delay_days=delay_days,
                early_collection_pct=early_pct,
                early_discount_pct=early_discount_pct,
            )
            late_downstream = result.payments[
                result.payments["payment_id"].astype(str).isin(downstream_ids)
                & (result.payments["late_amount"] > 0)
            ]
            affected = result.businesses[result.businesses["business_id"].astype(str).isin(affected_ids)]
            reserve_days = int(affected["reserve_breach_days"].sum())
            reserve_breaches = int(affected["reserve_breached"].sum())
            late_value = float(late_downstream["late_amount"].sum())
            discount_row = result.payments[result.payments["payment_id"].astype(str) == str(shock_payment_id)]
            realized_discount = float(discount_row["discount_amount"].iloc[0]) if not discount_row.empty else 0.0
            trigger_late_amount = float(discount_row["late_amount"].iloc[0]) if not discount_row.empty else 0.0
            estimated_cost = round(realized_discount + extension_fee, 2)
            protects_chain = len(late_downstream) == 0 and reserve_days == 0
            rows.append({
                "early_pct": int(early_pct),
                "early_face_amount": round(float(selected.iloc[0]["amount"]) * early_pct / 100.0, 2),
                "early_discount_pct": early_discount_pct if early_pct > 0 else 0.0,
                "realized_discount_cost": round(realized_discount, 2),
                "trigger_invoice_late_amount": round(trigger_late_amount, 2),
                "extension_payment_id": str(extension_payment_id) if extension_payment_id and shift_days else "None",
                "extension_days": int(shift_days),
                "extension_fee": round(extension_fee, 2),
                "estimated_total_cost": estimated_cost,
                "downstream_late_count": int(len(late_downstream)),
                "downstream_late_amount": round(late_value, 2),
                "affected_reserve_breaches": reserve_breaches,
                "affected_reserve_breach_days": reserve_days,
                "minimum_affected_cash": float(affected["minimum_cash"].min()) if not affected.empty else 0.0,
                "total_late_amount": float(result.payments["late_amount"].sum()),
                "protects_chain": bool(protects_chain),
            })

    comparison = pd.DataFrame(rows)
    if comparison.empty:
        return comparison, None, False
    has_feasible = bool(comparison["protects_chain"].any())
    if has_feasible:
        comparison = comparison.sort_values(
            ["protects_chain", "estimated_total_cost", "affected_reserve_breach_days", "downstream_late_amount", "early_pct", "extension_days"],
            ascending=[False, True, True, True, True, True],
        )
    else:
        comparison = comparison.sort_values(
            ["affected_reserve_breach_days", "downstream_late_amount", "downstream_late_count", "estimated_total_cost", "early_pct", "extension_days"],
            ascending=[True, True, True, True, True, True],
        )
    comparison = comparison.reset_index(drop=True)
    best = comparison.iloc[0].to_dict()
    return comparison, best, has_feasible
