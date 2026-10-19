"""
Bank statement PDF parser.

Layer 1 approach: use pdfplumber to extract tables from digital PDFs.
Falls back to text-line regex parsing if table extraction yields nothing.

Returns a list of transaction dicts with keys:
    date (str, YYYY-MM-DD where possible)
    description (str)
    debit (float or None)
    credit (float or None)
    balance (float or None)
    raw (str) — the original text line, for debugging
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pdfplumber

# Nigerian bank statement date formats we'll try, in order.
_DATE_FORMATS = [
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d/%m/%y",
    "%d-%m-%y",
    "%d %b %Y",
    "%d %B %Y",
    "%Y-%m-%d",
]

# Regex to spot a currency amount: optional N, ₦, NGN prefix, digits with
# thousands separators, optional 2-decimal kobo. Handles (1,234.56) as negative.
_AMOUNT_RE = re.compile(
    r"(?P<paren>\()?\s*(?:N|₦|NGN)?\s*(?P<num>\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)\s*(?P<paren_close>\))?"
)


def _parse_date(cell: str) -> str | None:
    """Try to normalize a date cell to YYYY-MM-DD. Return None if unparseable."""
    if not cell:
        return None
    cell = cell.strip()
    # Strip trailing time if present
    cell = re.split(r"\s+", cell)[0]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cell, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _to_amount(cell: str) -> float | None:
    """Parse a numeric cell like '1,234.56' or '(1,234.56)' or '₦ 1,234.56' into a float."""
    if cell is None:
        return None
    cell = str(cell).strip()
    if not cell or cell in {"-", "—", ""}:
        return None
    # Detect parentheses = negative
    negative = cell.startswith("(") and cell.endswith(")")
    match = _AMOUNT_RE.search(cell)
    if not match:
        return None
    try:
        value = float(match.group("num").replace(",", ""))
    except ValueError:
        return None
    return -value if negative else value


def _looks_like_header(row: list[str]) -> bool:
    joined = " ".join(str(c).lower() for c in row if c)
    has_date = "date" in joined
    has_amt = any(
        kw in joined for kw in ("debit", "credit", "balance", "amount", "withdrawal", "deposit")
    )
    return has_date and has_amt


def _normalize_row(row: list[str]) -> dict | None:
    """
    Turn a raw table row into a transaction dict.
    Heuristic: first cell that parses as a date is the date.
    Amounts go to debit/credit/balance based on count and order.
    """
    if not row:
        return None
    cells = [str(c).strip() if c is not None else "" for c in row]
    if not any(cells):
        return None

    date_idx = None
    for i, c in enumerate(cells):
        if _parse_date(c):
            date_idx = i
            break
    if date_idx is None:
        return None

    date_val = _parse_date(cells[date_idx])
    # Description = concatenation of cells between date and the first amount
    amount_cells = []
    desc_parts = []
    for i, c in enumerate(cells):
        if i == date_idx:
            continue
        amt = _to_amount(c)
        # Only treat as amount if the cell is mostly numeric (avoid
        # description text that happens to contain digits)
        if amt is not None and re.fullmatch(r"[\sN₦NGN\(\)0-9,.\-]*", c or ""):
            amount_cells.append(amt)
        else:
            if not amount_cells:
                desc_parts.append(c)

    description = " ".join(p for p in desc_parts if p).strip()

    debit = credit = balance = None
    if len(amount_cells) >= 3:
        # Common layout: Debit | Credit | Balance  — but blank debit for credit rows
        # and blank credit for debit rows means the count varies. Handle:
        # If both debit and credit columns are present as separate cells we
        # can't tell from amounts alone. Best effort:
        debit, credit, balance = amount_cells[0], amount_cells[1], amount_cells[2]
    elif len(amount_cells) == 2:
        # Likely (amount, balance). Decide debit vs credit by sign or context.
        amt, bal = amount_cells
        if amt < 0:
            debit, balance = -amt, bal
        else:
            credit, balance = amt, bal
    elif len(amount_cells) == 1:
        # Only one amount — could be balance only (skip) or amount with no balance
        # Treat as credit if description suggests inflow, else debit.
        amt = amount_cells[0]
        d = description.lower()
        if any(k in d for k in ("credit", "deposit", "inflow", "trf in", "nip in")):
            credit = amt
        else:
            debit = amt

    return {
        "date": date_val,
        "description": description,
        "debit": debit,
        "credit": credit,
        "balance": balance,
        "raw": " | ".join(cells),
    }


def _parse_via_tables(pdf_path: Path) -> list[dict]:
    """Try structured table extraction first (works for most digital bank statements)."""
    transactions: list[dict] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            for table in tables:
                if not table:
                    continue
                # Skip header rows
                for row in table:
                    if _looks_like_header(row):
                        continue
                    tx = _normalize_row(row)
                    if tx:
                        transactions.append(tx)
    return transactions


def _parse_via_text(pdf_path: Path) -> list[dict]:
    """
    Fallback: extract plain text and use regex to find lines that look like
    transactions. Used when tables aren't detected (scanned or odd layouts).
    """
    transactions: list[dict] = []
    line_re = re.compile(
        r"^(?P<date>\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})\s+(?P<rest>.+)$"
    )
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
                amounts = [
                    _to_amount(tok)
                    for tok in rest.split()
                    if _to_amount(tok) is not None
                ]
                # Filter None
                amounts = [a for a in amounts if a is not None]
                if not amounts:
                    continue
                # Guess: last amount is balance, second-to-last is the transaction amount
                balance = amounts[-1] if len(amounts) >= 2 else None
                tx_amount = amounts[-2] if len(amounts) >= 2 else amounts[-1]
                # Description = text before the first amount token
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
