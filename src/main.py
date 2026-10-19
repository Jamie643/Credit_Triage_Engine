"""
Credit Triage Engine — Layer 1 CLI.

Usage:
    python -m src.main path/to/statement.pdf

Prints a JSON report with parsing stats, balance reconciliation,
circular transfer flags, recurring debt patterns, and a preliminary
risk verdict.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from src.parsers.bank_statement import parse_bank_statement
from src.checks.balance import reconcile_balance
from src.checks.circular import detect_circular_transfers
from src.checks.recurring import detect_recurring_debts


def _preliminary_risk(balance: dict, circular: list, recurring: list, tx_count: int) -> str:
    """
    GREEN  = clean balance, no circular, no suspicious debt patterns, parsed rows
    YELLOW = one soft issue (parse low, tiny discrepancy, one circular pair)
    RED    = balance mismatch, or 2+ circular pairs, or heavy recurring burden
    """
    if tx_count == 0:
        return "RED"  # couldn't parse anything — treat as failure
    if balance.get("reconciled") is False:
        return "RED"
    if len(circular) >= 2:
        return "RED"
    if len(circular) == 1:
        return "YELLOW"
    if balance.get("missing_balances", 0) > 0:
        return "YELLOW"
    if len(recurring) >= 3:
        return "YELLOW"
    if tx_count < 10:
        return "YELLOW"
    return "GREEN"


def run(pdf_path: str) -> dict:
    transactions = parse_bank_statement(pdf_path)
    balance = reconcile_balance(transactions)
    circular = detect_circular_transfers(transactions)
    recurring = detect_recurring_debts(transactions)

    total_credits = sum(t.get("credit") or 0 for t in transactions)
    total_debits = sum(t.get("debit") or 0 for t in transactions)

    return {
        "file": Path(pdf_path).name,
        "parsed_transactions": len(transactions),
        "total_credits": round(total_credits, 2),
        "total_debits": round(total_debits, 2),
        "net_flow": round(total_credits - total_debits, 2),
        "balance_check": balance,
        "circular_flags": circular,
        "circular_count": len(circular),
        "recurring_debts": recurring,
        "preliminary_risk": _preliminary_risk(
            balance, circular, recurring, len(transactions)
        ),
    }


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python -m src.main path/to/statement.pdf", file=sys.stderr)
        sys.exit(1)
    report = run(sys.argv[1])
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
