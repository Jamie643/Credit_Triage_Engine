import json

def parse_ai_output(raw_ai_json: str) -> list[dict]:
    """
    Takes the raw JSON string from the Vision AI, converts the "IN"/"OUT"
    format into standard "debit"/"credit" keys, and automatically fixes
    cases where the AI got the direction wrong but the math is fixable.
    """
    try:
        raw_txs = json.loads(raw_ai_json)
    except json.JSONDecodeError:
        raise ValueError("AI did not return valid JSON.")

    # 1. Initial Translation
    txs = []
    for t in raw_txs:
        debit = t.get("amount") if t.get("type") == "OUT" else None
        credit = t.get("amount") if t.get("type") == "IN" else None
        txs.append({
            "date": t.get("date"),
            "description": t.get("description"),
            "debit": debit,
            "credit": credit,
            "balance": t.get("balance")
        })

    if not txs:
        return txs

    # 2. Back-calculate opening balance
    first = txs[0]
    running_balance = first["balance"] - (first.get("credit") or 0) + (first.get("debit") or 0)

    # 3. Smart Correction (Auto-fix IN/OUT flips)
    for i, t in enumerate(txs):
        debit = t.get("debit") or 0
        credit = t.get("credit") or 0
        actual_balance = t.get("balance")

        # What does the math say the balance should be?
        expected_balance = running_balance - debit + credit

        # If the math is wrong, try flipping the Debit and Credit
        if actual_balance is not None and abs(expected_balance - actual_balance) > 0.01:
            temp_debit = credit
            temp_credit = debit
            
            # If flipping it makes the math perfect, keep the flip!
            if abs((running_balance - temp_debit + temp_credit) - actual_balance) < 0.01:
                t["debit"] = temp_debit
                t["credit"] = temp_credit
                debit = temp_debit
                credit = temp_credit
                
        # Update running balance
        running_balance = running_balance - debit + credit

        # If it's still broken, reset to the bank's printed balance
        # so we can continue checking the rest of the document
        if actual_balance is not None and abs(running_balance - actual_balance) > 0.01:
            running_balance = actual_balance

    return txs


def run_math_gate(txs: list[dict]) -> dict:
    """
    Checks the running balance row-by-row.
    Returns a dictionary with the risk level and details.
    """
    if not txs:
        return {"risk_level": "RED", "reason": "No transactions found."}

    first = txs[0]
    opening_balance = first["balance"] - (first.get("credit") or 0) + (first.get("debit") or 0)
    running_balance = opening_balance
    failed_rows = []

    for i, t in enumerate(txs):
        running_balance = running_balance - (t.get("debit") or 0) + (t.get("credit") or 0)
        actual_balance = t.get("balance")

        if actual_balance is not None and abs(running_balance - actual_balance) > 0.01:
            failed_rows.append({
                "row": i,
                "date": t.get("date"),
                "expected": round(running_balance, 2),
                "actual": actual_balance,
                "difference": round(running_balance - actual_balance, 2)
            })
            # Reset running balance to the actual balance to keep checking
            running_balance = actual_balance

    if len(failed_rows) == 0:
        return {
            "risk_level": "GREEN",
            "reason": "Statement math reconciles perfectly.",
            "failures": []
        }
    else:
        total_discrepancy = sum(abs(f["difference"]) for f in failed_rows)
        if total_discrepancy < 500:
            return {
                "risk_level": "YELLOW",
                "reason": f"Minor discrepancy ({total_discrepancy} NGN). Likely unlisted bank charges.",
                "failures": failed_rows
            }
        else:
            return {
                "risk_level": "RED",
                "reason": f"High risk: {len(failed_rows)} rows failed the math check. Likely tampering or missing pages.",
                "failures": failed_rows
            }
