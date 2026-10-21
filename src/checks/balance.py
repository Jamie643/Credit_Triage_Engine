"""
Balance reconciliation check.

Instead of relying on the overall totals (which can be skewed by missing 
opening balances or out-of-period transactions), we validate the running 
balance row-by-row.

Logic:
    previous_balance - current_debit + current_credit == current_balance

This catches:
    - Deleted rows (the balance will jump unexpectedly)
    - Tampered amounts (changing a debit will break the next row's balance)
    - Parser errors (if an amount is misread, the math breaks)
"""

from __future__ import annotations


def reconcile_balance(transactions: list[dict]) -> dict:
    """
    Validates the running balance row-by-row.
    
    Returns a dict with:
        reconciled (bool or None if not enough data)
        expected_closing (float)
        actual_closing (float)
        difference (float)
        missing_balances (int) — rows without a balance value
        failed_rows (list) — indexes of rows where the math broke
    """
    if not transactions:
        return {
            "reconciled": None,
            "expected_closing": None,
            "actual_closing": None,
            "difference": None,
            "missing_balances": 0,
            "failed_rows": [],
        }

    # 1. Count how many rows are missing a balance entirely
    missing = sum(1 for t in transactions if t.get("balance") is None)

    # 2. Back-calculate the Opening Balance using the first valid row
    opening_balance = None
    start_index = 0
    
    for i, t in enumerate(transactions):
        if t.get("balance") is not None:
            # If the first row has a balance but no debit/credit, it IS the opening balance
            if not t.get("debit") and not t.get("credit"):
                opening_balance = t["balance"]
                start_index = i + 1
            else:
                # Otherwise, back-calculate it
                opening_balance = t["balance"] - (t.get("credit") or 0) + (t.get("debit") or 0)
                start_index = i
            break

    if opening_balance is None:
        return {
            "reconciled": None,
            "expected_closing": None,
            "actual_closing": None,
            "difference": None,
            "missing_balances": missing,
            "failed_rows": [],
        }

    # 3. Row-by-Row Validation
    running_balance = opening_balance
    failed_rows = []
    
    # We start checking from the first actual transaction
    for i in range(start_index, len(transactions)):
        t = transactions[i]
        
        # Apply the transaction to the running total
        debit = t.get("debit") or 0
        credit = t.get("credit") or 0
        running_balance = running_balance - debit + credit
        
        # Compare to what the bank printed
        actual_balance = t.get("balance")
        if actual_balance is not None:
            # Allow a tiny margin of error (0.01) for floating point weirdness
            if abs(running_balance - actual_balance) > 0.01:
                failed_rows.append({
                    "row_index": i,
                    "date": t.get("date"),
                    "expected_balance": round(running_balance, 2),
                    "actual_balance": round(actual_balance, 2),
                    "difference": round(running_balance - actual_balance, 2)
                })
                # Reset our running balance to the bank's printed balance 
                # so we can continue checking the rest of the document
                running_balance = actual_balance

    # 4. Calculate Final Metrics
    actual_closing = None
    for t in reversed(transactions):
        if t.get("balance") is not None:
            actual_closing = t["balance"]
            break

    # If no rows failed, it's perfectly reconciled
    is_reconciled = (len(failed_rows) == 0 and actual_closing is not None)

    return {
        "reconciled": is_reconciled,
        "expected_closing": round(running_balance, 2),
        "actual_closing": round(actual_closing, 2) if actual_closing else None,
        "difference": round(running_balance - actual_closing, 2) if actual_closing else None,
        "missing_balances": missing,
        "failed_rows": failed_rows,
    }
