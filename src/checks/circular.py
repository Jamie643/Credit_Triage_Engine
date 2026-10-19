"""
Circular transfer detection.

Flags money that leaves and comes back — a common way to inflate apparent
turnover on a statement. Layer 1 heuristic:

  - Look for a debit and a credit of (nearly) the same amount
  - Within a configurable window (default 7 days)
  - Each pair counted once

Returns a list of flagged pairs with amounts and dates.
"""

from __future__ import annotations

from datetime import datetime


def _to_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return None


def detect_circular_transfers(
    transactions: list[dict], window_days: int = 7, tolerance_pct: float = 2.0
) -> list[dict]:
    """
    Returns a list of suspicious pairs: {out_date, in_date, amount, delta_days}
    """
    # Split into debits and credits with parsed dates
    debits = []
    credits = []
    for t in transactions:
        d = _to_date(t.get("date"))
        if d is None:
            continue
        if t.get("debit"):
            debits.append({"date": d, "amount": t["debit"], "desc": t.get("description", "")})
        if t.get("credit"):
            credits.append({"date": d, "amount": t["credit"], "desc": t.get("description", "")})

    flags = []
    used_credits: set[int] = set()

    for debit in debits:
        for i, credit in enumerate(credits):
            if i in used_credits:
                continue
            if debit["date"] > credit["date"]:
                continue
            delta = (credit["date"] - debit["date"]).days
            if delta > window_days:
                continue
            # Amount match within tolerance
            if debit["amount"] == 0:
                continue
            pct_diff = abs(credit["amount"] - debit["amount"]) / debit["amount"] * 100
            if pct_diff <= tolerance_pct:
                flags.append(
                    {
                        "out_date": debit["date"].strftime("%Y-%m-%d"),
                        "in_date": credit["date"].strftime("%Y-%m-%d"),
                        "amount": round(debit["amount"], 2),
                        "delta_days": delta,
                        "out_desc": debit["desc"],
                        "in_desc": credit["desc"],
                    }
                )
                used_credits.add(i)
                break

    return flags
