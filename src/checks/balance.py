"""
Balance reconciliation check.

Verifies that: opening_balance + sum(credits) - sum(debits) == closing_balance

A mismatch is a strong tampering signal: someone edited a debit, credit, or
balance cell in the PDF without recomputing the running column.
"""

from __future__ import annotations


def reconcile_balance(transactions: list[dict]) -> dict:
    """
    Returns a dict with:
        reconciled (bool or None if not enough data)
        expected_closing (float)
        actual_closing (float)
        difference (float)
        missing_balances (int) — rows without a balance value
    """
    if not transactions:
        return {
            "reconciled": None,
            "expected_closing": None,
            "actual_closing": None,
            "difference": None,
            "missing_balances": 0,
        }

    # Opening balance = balance of first row minus the movement of that row
    first = transactions[0]
    opening = None
    if first.get("balance") is not None:
        opening = first["balance"] - (first.get("credit") or 0) + (first.get("debit") or 0)

    total_credits = sum(t.get("credit") or 0 for t in transactions)
    total_debits = sum(t.get("debit") or 0 for t in transactions)

    missing = sum(1 for t in transactions if t.get("balance") is None)

    # Actual closing = last row that has a balance
    actual_closing = None
    for t in reversed(transactions):
        if t.get("balance") is not None:
            actual_closing = t["balance"]
            break

    if opening is None or actual_closing is None:
        return {
            "reconciled": None,
            "expected_closing": None,
            "actual_closing": actual_closing,
            "difference": None,
            "missing_balances": missing,
        }

    expected_closing = opening + total_credits - total_debits
    difference = round(expected_closing - actual_closing, 2)

    return {
        "reconciled": abs(difference) < 0.01,
        "expected_closing": round(expected_closing, 2),
        "actual_closing": round(actual_closing, 2),
        "difference": difference,
        "missing_balances": missing,
    }
