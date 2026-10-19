"""
Bank statement PDF parser.

Layer 1 approach: use pdfplumber to extract tables from digital PDFs.
Falls back to text-line regex parsing if table extraction yields nothing.

Strategy:
    1. Find the header row in each table and record which column index
       corresponds to Date, Description, Debit, Credit, Balance.
    2. Apply that column map to every data row below the header.
    3. If a statement collapses Debit/Credit into a single "Amount" column,
       infer direction from the sign or from DR/CR markers.

Returns a list of transaction dicts with keys:
    date (str, YYYY-MM-DD where possible)
    description (str)
    debit (float or None)
    credit (float or None)
    balance (float or None)
    raw (str) — the original row joined with pipes, for debugging
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pdfplumber

_DATE_FORMATS = [
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d/%m/%y",
    "%d-%m-%y",
    "%d %b %Y",
    "%d %B %Y",
    "%Y-%m-%d",
]

# Column role keywords. Matched against the lowercase header text.
_COL_KEYWORDS = {
    "date": ["date", "trans date", "value date", "posting date"],
    "description": ["description", "narration", "details", "particulars", "remarks", "transaction"],
    "debit": ["debit", "withdrawal", "dr", "money out", "outflow"],
    "credit": ["credit", "deposit", "cr", "money in", "inflow"],
    "balance": ["balance", "running balance", "closing balance"],
    "amount": ["amount", "transaction amount", "value"],  # for combined debit/credit layouts
}


def _parse_date(cell: str) -> str | None:
    """Try to normalize a date cell to YYYY-MM-DD. Return None if unparseable."""
    if not cell:
        return None
    cell = str(cell).strip()
    # Take the first whitespace-delimited token in case there's trailing time
    token = re.split(r"\s+", cell)[0]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(token, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _to_amount(cell: str) -> float | None:
    """Parse a numeric cell like '1,234.56', '(1,234.56)', '1,234.56 DR' into a float.
    Returns None for empty cells, dashes, and pure text."""
    if cell is None:
        return None
    cell = str(cell).strip()
    if not cell or cell in {"-", "—", "--"}:
        return None

    negative = False
    s = cell

    # Parentheses = negative
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1].strip()

    # DR / CR suffix markers
    upper = s.upper()
    if upper.endswith("DR"):
        negative = True
        s = s[:-2].strip()
    elif upper.endswith("CR"):
        s = s[:-2].strip()

    # Leading minus
    if s.startswith("-"):
        negative = True
        s = s[1:].strip()

    # Strip currency symbols/prefixes
    s = re.sub(r"^(?:N|NGN|₦)\s*", "", s, flags=re.IGNORECASE)

    # Must look like a number now: digits, commas, at most one dot
    if not re.fullmatch(r"\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?", s):
        return None

    try:
        value = float(s.replace(",", ""))
    except ValueError:
        return None
    return -value if negative else value


def _detect_columns(header_row: list[str]) -> dict[str, int] | None:
    """
    Map role -> column index from a header row.
    Returns None if we can't identify at least Date and one of (Debit/Credit/Amount).
    """
    if not header_row:
        return None
    normalized = [(str(c) or "").strip().lower() for c in header_row]

    found: dict[str, int] = {}
    for role, keywords in _COL_KEYWORDS.items():
        for idx, cell in enumerate(normalized):
            if not cell:
                continue
            # Skip if this column is already assigned
            if idx in found.values():
                continue
            if any(kw == cell or kw in cell for kw in keywords):
                found[role] = idx
                break

    # Minimum viable: need a date column and something that carries amounts
    if "date" not in found:
        return None
    if not any(r in found for r in ("debit", "credit", "amount")):
        return None
    return found


def _looks_like_header(row: list[str]) -> bool:
    """Row looks like a header if it contains a date-ish keyword AND an amount-ish keyword."""
    if not row:
        return False
    joined = " ".join((str(c) or "").lower() for c in row)
    has_date = any(k in joined for k in _COL_KEYWORDS["date"])
    has_amt = any(
        k in joined
        for k in _COL_KEYWORDS["debit"]
        + _COL_KEYWORDS["credit"]
        + _COL_KEYWORDS["balance"]
        + _COL_KEYWORDS["amount"]
    )
    return has_date and has_amt


def _extract_by_columns(row: list[str], columns: dict[str, int]) -> dict | None:
    """Apply a column map to a data row. Returns transaction dict or None."""
    if not row or not any(str(c or "").strip() for c in row):
        return None

    def get(role: str) -> str:
        idx = columns.get(role)
        if idx is None or idx >= len(row):
            return ""
        return str(row[idx] or "").strip()

    date_val = _parse_date(get("date"))
    if date_val is None:
        return None

    description = get("description")
    debit = _to_amount(get("debit")) if "debit" in columns else None
    credit = _to_amount(get("credit")) if "credit" in columns else None
    balance = _to_amount(get("balance")) if "balance" in columns else None

    # Combined amount column: infer direction
    if "amount" in columns and debit is None and credit is None:
        amt = _to_amount(get("amount"))
        if amt is not None:
            if amt < 0:
                debit = -amt
            else:
                # Direction by marker in description
                d = description.lower()
                if re.search(r"\b(dr|debit|withdrawal|pos|trf out|nip out)\b", d):
                    debit = amt
                elif re.search(r"\b(cr|credit|deposit|trf in|nip in|inflow)\b", d):
                    credit = amt
                else:
                    # Fallback: compare this row's balance with previous is not
                    # available here — assume credit (inflows dominate statements)
                    credit = amt

    return {
        "date": date_val,
        "description": description,
        "debit": debit,
        "credit": credit,
        "balance": balance,
        "raw": " | ".join(str(c or "") for c in row),
    }


def _parse_via_tables(pdf_path: Path) -> list[dict]:
    """
    Walk each page, find the header row, and extract data rows using the
    detected column map. Handles headers appearing mid-page (common when
    pdfplumber splits long statements across tables).
    """
    transactions: list[dict] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            for table in tables:
                if not table:
                    continue
                columns: dict[str, int] | None = None
                for row in table:
                    if _looks_like_header(row):
                        detected = _detect_columns(row)
                        if detected:
                            columns = detected
                            continue
                    if columns is None:
                        continue
                    tx = _extract_by_columns(row, columns)
                    if tx:
                        transactions.append(tx)
    return transactions


def _parse_via_text(pdf_path: Path) -> list[dict]:
    """
    Fallback for scanned or non-tabular PDFs.
    This is deliberately conservative: only picks up lines beginning with a date
    followed by at least two numeric tokens (amount and balance).
    """
    transactions: list[dict] = []
    line_re = re.compile(r"^(?P<date>\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})\s+(?P<rest>.+)$")
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            for line in text.splitlines():
                m = line_re.match(line.strip())
                if not m:
                    continue
                date_val = _parse_date(m.group("date"))
                if not date_val:
                    continue
                rest = m.group("rest")
                amounts = [_to_amount(tok) for tok in rest.split()]
                amounts = [a for a in amounts if a is not None]
                if len(amounts) < 2:
                    continue
                balance = amounts[-1]
                tx_amount = amounts[-2]
                desc = rest
                for tok in rest.split():
                    if _to_amount(tok) is not None:
                        desc = rest.split(tok)[0].strip()
                        break
                transactions.append(
                    {
                        "date": date_val,
                        "description": desc,
                        "debit": None,
                        "credit": tx_amount if tx_amount >= 0 else None,
                        "balance": balance,
                        "raw": line,
                    }
                )
    return transactions


def parse_bank_statement(pdf_path: str | Path) -> list[dict]:
    """
    Entry point. Returns a list of transaction dicts.
    Tries table extraction first; if empty, falls back to text parsing.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"No such file: {pdf_path}")

    tx = _parse_via_tables(pdf_path)
    if not tx:
        tx = _parse_via_text(pdf_path)
    return tx
