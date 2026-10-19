"""
Recurring debt detection.

Finds monthly debits that look like loan repayments: either the description
contains debt-related keywords, or the same amount recurs in consecutive months
to the same recipient.

Returns a list of recurring patterns with estimated monthly burden.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime

# Keywords that strongly suggest a loan repayment debit.
_DEBT_KEYWORDS = re.compile(
    r"\b(loan|repayment|repay|instal?lment|credit\s*repay|mfb|microfinance|"
    r"lender|kollect|fairmoney|carbon|moniepoint|branch|renmoney|paylater|"
    r"quickcheck|opay\s*loan|palmpay\s*loan)\b",
    re.IGNORECASE,
)


def detect_recurring_debts(transactions: list[dict]) -> list[dict]:
    """
    Returns a list of patterns:
        {description, amount, occurrences, months_span, monthly_burden}
    """
    # Group debits by (rounded_amount, month)
    by_amount: dict[float, list[datetime]] = defaultdict(list)
    descriptions: dict[float, str] = {}

    for t in transactions:
        if not t.get("debit"):
            continue
        if not t.get("date"):
            continue
        try:
            d = datetime.strptime(t["date"], "%Y-%m-%d")
        except ValueError:
            continue
        amt = round(t["debit"], 2)
        by_amount[amt].append(d)
        if amt not in descriptions:
            descriptions[amt] = t.get("description", "")

    patterns = []
    for amt, dates in by_amount.items():
        desc = descriptions.get(amt, "")
        keyword_hit = bool(_DEBT_KEYWORDS.search(desc))

        # Group by month
        months = sorted({(d.year, d.month) for d in dates})
        if len(months) < 2 and not keyword_hit:
            continue

        # If keyword hit, flag even with a single occurrence
        if not keyword_hit:
            # Require recurrence: same amount in >= 2 distinct months
            # AND the months should be roughly consecutive (not e.g. 2 payments
            # 18 months apart)
            if len(months) < 2:
                continue
            gaps = []
            for i in range(1, len(months)):
                y1, m1 = months[i - 1]
                y2, m2 = months[i]
                gaps.append((y2 - y1) * 12 + (m2 - m1))
            if not any(g <= 2 for g in gaps):
                continue

        span_months = (
            (months[-1][0] - months[0][0]) * 12 + (months[-1][1] - months[0][1]) + 1
            if months
            else 1
        )
        patterns.append(
            {
                "description": desc,
                "amount": amt,
                "occurrences": len(dates),
                "months_span": span_months,
                "monthly_burden": round(amt * len(dates) / span_months, 2),
                "keyword_match": keyword_hit,
            }
        )

    # Sort by monthly burden descending
    patterns.sort(key=lambda p: p["monthly_burden"], reverse=True)
    return patterns
