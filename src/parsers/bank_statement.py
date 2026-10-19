"""
Bank statement PDF parser.

Primary strategy: positional parsing using pdfplumber's word coordinates.
Real Nigerian bank statement PDFs (e.g. UBA) do not have ruling lines and
do not survive extract_tables(). They also break extract_text() because
descriptions wrap across multiple visual lines while amounts do not.

But the PDF DOES preserve each word's (x0, top) position. So we:
    1. Extract words per page
    2. Group words into visual rows by 'top' coordinate
    3. Detect the header row to learn column x-bands
    4. Extract transactions row-by-row, carrying forward state for
       continuation lines (year fragments, description continuations)
    5. Infer debit vs credit from the running balance delta — the only
       robust signal given UBA's inconsistent amount x-positions

Fallback: text-line regex parser (kept for simple statements).

Returns a list of transaction dicts with keys:
    date, description, debit, credit, balance, raw
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pdfplumber

_DATE_FORMATS = [
    "%d-%b-%Y",
    "%d-%b-%y",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d/%m/%y",
    "%d-%m-%y",
    "%d %b %Y",
    "%d %B %Y",
    "%Y-%m-%d",
]

# Matches '14-Aug' or '12-Oct' (day-month token)
_DATE_TOKEN_RE = re.compile(r"^(\d{1,2})[-/]([A-Za-z]{3,9})(?:[-/](\d{2,4}))?$")
# Matches amounts like 6,400.00 or 28.56 or 865.49
_AMOUNT_RE = re.compile(r"^\(?-?(?:N|NGN|₦)?\d{1,3}(?:,\d{3})*(?:\.\d{2})?\)?$")

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_date_token(token: str, year_hint: str | None = None) -> str | None:
    """
    Parse '14-Aug' + optional year, or '14-Aug-2026', or '14/08/2026'.
    Returns 'YYYY-MM-DD' or None.
    """
    if not token:
        return None

    m = _DATE_TOKEN_RE.match(token.strip())
    if m:
        day, month_str, year = m.groups()
        month = _MONTHS.get(month_str.lower()[:3])
        if month is None:
            return None
        if not year:
            year = year_hint
        if not year or len(year) < 4:
            # Try to infer — but without context this is unsafe
            return None
        try:
            return datetime(int(year), month, int(day)).strftime("%Y-%m-%d")
        except ValueError:
            return None

    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(token.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _to_amount(cell: str) -> float | None:
    """Parse a numeric token into a float. Returns None for non-numbers."""
    if cell is None:
        return None
    s = str(cell).strip()
    if not s or s in {"-", "—", "--"}:
        return None
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1]
    s = re.sub(r"^(?:N|NGN|₦)\s*", "", s, flags=re.IGNORECASE)
    if not re.fullmatch(r"\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?", s):
        return None
    try:
        v = float(s.replace(",", ""))
    except ValueError:
        return None
    return -v if negative else v


def _group_words_into_rows(words: list[dict], tolerance: int = 3) -> list[dict]:
    """
    Group pdfplumber word dicts into visual rows by 'top' coordinate.
    Returns list of {'top': float, 'words': [sorted by x0]}.
    """
    rows_by_top: dict[int, list[dict]] = {}
    for w in words:
        key = round(w["top"] / tolerance) * tolerance
        rows_by_top.setdefault(key, []).append(w)
    rows = []
    for top in sorted(rows_by_top.keys()):
        row_words = sorted(rows_by_top[top], key=lambda w: w["x0"])
        rows.append({"top": float(top), "words": row_words})
    return rows


def _row_text(row: dict) -> str:
    return " ".join(w["text"] for w in row["words"])


def _find_header_row(rows: list[dict]) -> int | None:
    """Find the index of the header row (contains TRANS DATE and BALANCE)."""
    for i, row in enumerate(rows):
        joined = _row_text(row).upper()
        if "TRANS" in joined and "DATE" in joined and "BALANCE" in joined:
            return i
        if "BALANCE" in joined and "DEBIT" in joined and "CREDIT" in joined:
            return i
    return None


def _detect_balance_x(rows: list[dict], header_idx: int) -> float | None:
    """
    Find x0 of the balance column by looking for numeric tokens on data rows.
    The balance is typically the rightmost number on a line.
    """
    for row in rows[header_idx + 1 :]:
        numeric_xs = [
            w["x0"] for w in row["words"] if _AMOUNT_RE.match(w["text"])
        ]
        if numeric_xs:
            return max(numeric_xs)
    return None


def _parse_page_transactions(page, page_number: int) -> list[dict]:
    """
    Parse one page into a list of transaction dicts.
    """
    words = page.extract_words()
    if not words:
        return []

    rows = _group_words_into_rows(words)
    header_idx = _find_header_row(rows)
    if header_idx is None:
        return []

    # Establish footer cutoff — don't parse past it (footer repeats contact info)
    footer_idx = None
    for i in range(header_idx + 1, len(rows)):
        joined = _row_text(rows[i]).lower()
        if "download app" in joined or "head office" in joined or "privacy policy" in joined:
            footer_idx = i
            break

    data_rows = rows[header_idx + 1 : footer_idx] if footer_idx else rows[header_idx + 1 :]

    transactions: list[dict] = []
    current: dict | None = None
    last_date: str | None = None
    prev_balance: float | None = None

    for row in data_rows:
        words_in_row = row["words"]
        if not words_in_row:
            continue

        # Categorize each word by x-position
        date_x = None
        narration_x_range = None
        balance_x = None
        amount_tokens: list[tuple[float, str]] = []  # (x0, text)
        text_tokens: list[tuple[float, str]] = []

        # Determine date-column x from first word if it's a date-ish token
        first = words_in_row[0]
        first_is_date = bool(_DATE_TOKEN_RE.match(first["text"]))

        # Collect tokens by role (rough x-bands derived from observed layout)
        for w in words_in_row:
            txt = w["text"]
            x0 = w["x0"]
            if _AMOUNT_RE.match(txt) and any(ch.isdigit() for ch in txt):
                amount_tokens.append((x0, txt))
            else:
                text_tokens.append((x0, txt))

        # Split amount_tokens: rightmost is balance, second-rightmost is debit/credit
        amount_tokens.sort(key=lambda t: t[0])
        balance_val = None
        txn_amount = None
        if len(amount_tokens) >= 2:
            balance_val = _to_amount(amount_tokens[-1][1])
            txn_amount = _to_amount(amount_tokens[-2][1])
        elif len(amount_tokens) == 1:
            # Could be a balance-only row (like the Opening Balance row)
            balance_val = _to_amount(amount_tokens[-1][1])

        # Try to extract date from first two tokens
        date_val = None
        used_date_tokens = 0
        for token in words_in_row[:2]:
            if _DATE_TOKEN_RE.match(token["text"]):
                parsed = _parse_date_token(token["text"])
                if parsed:
                    date_val = parsed
                    used_date_tokens += 1

        # Detect continuation row: text like "2026 2026" on its own
        joined_row = _row_text(row).strip()
        is_year_only = bool(re.fullmatch(r"\d{4}\s+\d{4}", joined_row)) or bool(
            re.fullmatch(r"\d{4}", joined_row)
        )
        if is_year_only and current is not None:
            # Attach year to current transaction's date if we have one
            year_match = re.search(r"\d{4}", joined_row)
            if year_match and current.get("date_partial"):
                day_month = current["date_partial"]
                parsed = _parse_date_token(day_month, year_hint=year_match.group(0))
                if parsed:
                    current["date"] = parsed
                    last_date = parsed
            continue

        # Is this a new transaction? (starts with a date token)
        if first_is_date and date_val:
            # Flush previous
            if current is not None:
                transactions.append(current)

            # Extract narration: all text tokens after the date tokens and
            # before the first amount token. On a fresh row, dates are the
            # leftmost tokens, amounts are rightmost.
            narr_words = []
            for x0, txt in text_tokens:
                # Skip words that are date tokens (in date band, x0 < 160)
                if x0 < 160 and _DATE_TOKEN_RE.match(txt):
                    continue
                narr_words.append(txt)
            narration = " ".join(narr_words).strip()

            current = {
                "date": date_val,
                "description": narration,
                "debit": None,
                "credit": None,
                "balance": balance_val,
                "raw": joined_row,
            }
            # Remember partial date if year missing (e.g. '12-Oct-202')
            raw_date_token = words_in_row[0]["text"]
            if date_val is None and _DATE_TOKEN_RE.match(raw_date_token):
                current["date_partial"] = raw_date_token
            last_date = date_val
        elif current is not None:
            # Continuation of previous transaction: append narration text
            extra_narr = " ".join(txt for _, txt in text_tokens).strip()
            if extra_narr:
                if current["description"]:
                    current["description"] += " " + extra_narr
                else:
                    current["description"] = extra_narr
            # If this continuation row carries the amount, assign it
            if balance_val is not None and current["balance"] is None:
                current["balance"] = balance_val

        # Assign debit vs credit by comparing balance change
        if current is not None and current["balance"] is not None:
            delta = current["balance"] - (prev_balance if prev_balance is not None else 0)
            # If we don't have prev_balance, use opening inference later
            if txn_amount is not None and current["debit"] is None and current["credit"] is None:
                # Direction: positive delta = credit, negative = debit
                if delta > 0:
                    current["credit"] = txn_amount
                elif delta < 0:
                    current["debit"] = txn_amount
                # If delta == 0 (opening balance row), leave both None
            prev_balance = current["balance"]

    if current is not None:
        transactions.append(current)

    # Resolve any leftover 'date_partial' entries using last_date's year
    if last_date:
        year = last_date[:4]
        for tx in transactions:
            if tx.get("date") is None and tx.get("date_partial"):
                parsed = _parse_date_token(tx["date_partial"], year_hint=year)
                if parsed:
                    tx["date"] = parsed
            tx.pop("date_partial", None)

    # Drop transactions without a valid date (headers, footers, noise)
    transactions = [t for t in transactions if t.get("date")]

    return transactions


def _parse_via_positions(pdf_path: Path) -> list[dict]:
    """Primary parser using word coordinates."""
    all_tx: list[dict] = []
    with pdfplumber.open(pdf_path) as pdf:
        for pno, page in enumerate(pdf.pages):
            page_tx = _parse_page_transactions(page, pno + 1)
            all_tx.extend(page_tx)
    return all_tx


def _parse_via_text(pdf_path: Path) -> list[dict]:
    """
    Fallback parser for statements that resist positional parsing.
    Simple line-based, date at start of line, last two numeric tokens
    are amount and balance.
    """
    transactions: list[dict] = []
    line_re = re.compile(r"^(?P<date>\d{1,2}[/\-][A-Za-z0-9]{2,9}[/\-]\d{2,4})\s+(?P<rest>.+)$")
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            for line in text.splitlines():
                m = line_re.match(line.strip())
                if not m:
                    continue
                date_val = _parse_date_token(m.group("date"))
                if not date_val:
                    continue
                rest = m.group("rest")
                amounts = [_to_amount(tok) for tok in rest.split()]
                amounts = [a for a in amounts if a is not None]
                if len(amounts) < 2:
                    continue
                transactions.append(
                    {
                        "date": date_val,
                        "description": rest,
                        "debit": None,
                        "credit": amounts[-2],
                        "balance": amounts[-1],
                        "raw": line,
                    }
                )
    return transactions


def parse_bank_statement(pdf_path: str | Path) -> list[dict]:
    """
    Entry point. Tries positional parser first; falls back to text if empty.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"No such file: {pdf_path}")

    tx = _parse_via_positions(pdf_path)
    if not tx:
        tx = _parse_via_text(pdf_path)
    return tx
