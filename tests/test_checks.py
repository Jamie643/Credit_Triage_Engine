"""Unit tests for the check modules — no PDFs required."""

from src.checks.balance import reconcile_balance
from src.checks.circular import detect_circular_transfers
from src.checks.recurring import detect_recurring_debts


def test_balance_reconciles():
    tx = [
        {"date": "2025-01-01", "description": "opening", "debit": None, "credit": 10000.0, "balance": 10000.0},
        {"date": "2025-01-05", "description": "transfer out", "debit": 3000.0, "credit": None, "balance": 7000.0},
        {"date": "2025-01-10", "description": "inflow", "debit": None, "credit": 5000.0, "balance": 12000.0},
    ]
    result = reconcile_balance(tx)
    assert result["reconciled"] is True
    assert result["difference"] == 0.0


def test_balance_tampered():
    # Balance column is wrong on purpose — simulates a doctored statement
    tx = [
        {"date": "2025-01-01", "description": "opening", "debit": None, "credit": 10000.0, "balance": 10000.0},
        {"date": "2025-01-05", "description": "transfer out", "debit": 3000.0, "credit": None, "balance": 9000.0},
    ]
    result = reconcile_balance(tx)
    assert result["reconciled"] is False
        assert result["difference"] == -2000.0

def test_circular_transfer_detected():
    tx = [
        {"date": "2025-02-01", "description": "TRF OUT", "debit": 50000.0, "credit": None, "balance": 0.0},
        {"date": "2025-02-03", "description": "TRF IN", "debit": None, "credit": 50000.0, "balance": 50000.0},
    ]
    flags = detect_circular_transfers(tx)
    assert len(flags) == 1
    assert flags[0]["amount"] == 50000.0
    assert flags[0]["delta_days"] == 2


def test_circular_no_false_positive_when_amounts_differ():
    tx = [
        {"date": "2025-02-01", "description": "TRF OUT", "debit": 50000.0, "credit": None, "balance": 0.0},
        {"date": "2025-02-03", "description": "TRF IN", "debit": None, "credit": 47000.0, "balance": 47000.0},
    ]
    flags = detect_circular_transfers(tx, tolerance_pct=2.0)
    assert len(flags) == 0


def test_recurring_debt_keyword_hit():
    tx = [
        {"date": "2025-01-05", "description": "FAIRMONEY LOAN REPAY", "debit": 15000.0, "credit": None, "balance": 0.0},
        {"date": "2025-02-05", "description": "FAIRMONEY LOAN REPAY", "debit": 15000.0, "credit": None, "balance": 0.0},
        {"date": "2025-03-05", "description": "FAIRMONEY LOAN REPAY", "debit": 15000.0, "credit": None, "balance": 0.0},
    ]
    patterns = detect_recurring_debts(tx)
    assert len(patterns) == 1
    assert patterns[0]["keyword_match"] is True
    assert patterns[0]["monthly_burden"] == 15000.0


def test_recurring_no_false_positive_on_one_off():
    tx = [
        {"date": "2025-01-05", "description": "SHOP RENT", "debit": 200000.0, "credit": None, "balance": 0.0},
    ]
    patterns = detect_recurring_debts(tx)
    assert patterns == []
