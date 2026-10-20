"""
Bank statement PDF parser.

Strategy (in priority order):
    1. Camelot 'lattice' mode — for PDFs with visible ruling lines (UBA, GTBank, Zenith).
    2. Camelot 'stream' mode — for PDFs with whitespace-aligned columns.
    3. pdfplumber text — fallback for simple layouts.

Post-processing handles:
    - Multi-line dates ('01-Aug \\n2026' → '2026-08-01')
    - Debit vs credit from column assignment
    - Zenith-specific column mapping (handles '0.00' placeholders and blank columns)
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
    "%d-%b-%Y %H:%M %p",  # Zenith format: 01-Sep-2021 08:57 AM
]

_DATE_TOKEN_RE = re.compile(r"^(\d{1,2})[-/ ]([A-Za-z]{3,9})[-/ ]?(\d{2,4})?$")

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_date(cell: str) -> str | None:
    """Parse a date cell that may span multiple lines or contain timestamps."""
    if not cell:
        return None
    # Collapse whitespace and newlines
    cleaned = re.sub(r"\s+", " ", str(cell)).strip()
    
    # Try full string first (handles '01-Sep-2021 08:57 AM')
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue

    # Try just the first token (handles '01-Aug 2026')
    token = cleaned.split()[0] if cleaned.split() else ""
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(token, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
            
    # Handle 'DD-Mon' + year on second token
    parts = cleaned.split()
    if len(parts) >= 2 and _DATE_TOKEN_RE.match(parts[0] + " " + parts[1]):
        combined = parts[0] + "-" + parts[1] if "-" not in parts[0] else parts[0] + parts[1]
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(combined, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
                
    return None


def _to_amount(cell: str) -> float | None:
    """Parse a numeric cell like '1,234.56', '(1,234.56)', or '.75' into a float."""
    if cell is None:
        return None
    s = str(cell).strip()
    if not s or s in {"-", "—", "--"}:
        return None
        
    s = re.sub(r"\s+", "", s)
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1]
    s = re.sub(r"^(?:N|NGN|₦)", "", s, flags=re.IGNORECASE)
    
    # Allow .75 (no leading zero)
    if not re.fullmatch(r"\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\.\d{1,2}|\d+(?:\.\d{1,2})?", s):
        return None
    try:
        v = float(s.replace(",", ""))
    except ValueError:
        return None
    return -v if negative else v


def _clean_description(cell: str) -> str:
    """Collapse multi-line narration into a single clean string."""
    if not cell:
        return ""
    return re.sub(r"\s+", " ", str(cell)).strip()


def _is_header_row(row_values: list[str]) -> bool:
    joined = " ".join((v or "").lower() for v in row_values)
    return "date" in joined and ("balance" in joined or "narration" in joined or "description" in joined)


def _is_footer_row(row_values: list[str]) -> bool:
    joined = " ".join((v or "").lower() for v in row_values)
    return any(
        kw in joined
        for kw in ("download app", "head office", "privacy policy", "africa's global", "customer service")
    )


def _find_column_indices(header_row: list[str]) -> dict[str, int] | None:
    roles: dict[str, int] = {}
    for idx, cell in enumerate(header_row):
        if not cell:
            continue
        c = re.sub(r"\s+", " ", str(cell)).strip().lower()
        if idx in roles.values():
            continue
        if "trans date" in c or (c == "date" and "date" not in roles):
            roles["date"] = idx
        elif "value date" in c:
            roles["value_date"] = idx
        elif "narration" in c or "description" in c or "remarks" in c:
            roles["description"] = idx
        elif "chq" in c:
            roles["chq"] = idx
        elif "debit" in c or "withdrawal" in c:
            roles["debit"] = idx
        elif "credit" in c or "deposit" in c:
            roles["credit"] = idx
        elif "balance" in c:
            roles["balance"] = idx
            
    if "date" not in roles or ("debit" not in roles and "credit" not in roles):
        return None
    return roles


def _parse_with_camelot(pdf_path: Path, flavor: str) -> list[dict]:
    try:
        import camelot
    except ImportError:
        return []

    try:
        tables = camelot.read_pdf(str(pdf_path), flavor=flavor, pages="all")
    except Exception:
        return []

    transactions: list[dict] = []
    
    for table in tables:
        df = table.df
        columns: dict[str, int] | None = None
        
        # --- ZENITH SPECIFIC LAYOUT ---
        # Zenith has 'WITHDRAWALS' but often misses the 'DEBITS' header text.
        # It also has a blank column between Amounts and Balance.
        is_zenith = any("WITHDRAWALS" in str(v).upper() for v in df.values.flatten())
        
        if is_zenith:
            # We bypass the standard header detection for Zenith
            for _, row in df.iterrows():
                vals = [str(v).strip() for v in row.tolist()]
                if len(vals) < 6 or not vals[0] or "DATE" in vals[0].upper() or "BALANCE" in vals[1].upper():
                    continue
                
                date_val = _parse_date(vals[0])
                if not date_val:
                    continue
                
                # Zenith mock columns:
                # Index 0: Date, 1: Description, 2: Debit, 3: Credit, 4: Value Date, 5: Balance
                debit = _to_amount(vals[2])
                credit = _to_amount(vals[3])
                
                if debit == 0.0: debit = None
                if credit == 0.0: credit = None
                
                transactions.append({
                    "date": date_val,
                    "description": _clean_description(vals[1]),
                    "debit": debit,
                    "credit": credit,
                    "balance": _to_amount(vals[5]),
                    "raw": " | ".join(vals),
                })
            continue # Move to next table
            
        # --- STANDARD LAYOUT (UBA, GTBank, Synthetic) ---
        for _, row in df.iterrows():
            values = [str(v) for v in row.tolist()]
            if _is_header_row(values):
                columns = _find_column_indices(values)
                continue
            if columns is None:
                continue
            if _is_footer_row(values):
                break

            def get(role: str) -> str:
                idx = columns.get(role)
                if idx is None or idx >= len(values):
                    return ""
                return values[idx]

            date_val = _parse_date(get("date"))
            if date_val is None:
                continue

            description = _clean_description(get("description"))
            debit = _to_amount(get("debit"))
            credit = _to_amount(get("credit"))
            balance = _to_amount(get("balance"))

            transactions.append(
                {
                    "date": date_val,
                    "description": description,
                    "debit": debit,
                    "credit": credit,
                    "balance": balance,
                    "raw": " | ".join(values),
                }
            )
            
    return transactions


def _parse_via_text(pdf_path: Path) -> list[dict]:
    transactions: list[dict] = []
    line_re = re.compile(r"^(?P<date>\d{1,2}[/\-][A-Za-z0-9]{2,9}[/\-]\d{2,4})\s+(?P<rest>.+)$")
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
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"No such file: {pdf_path}")

    tx = _parse_with_camelot(pdf_path, flavor="lattice")
    if tx:
        return tx

    tx = _parse_with_camelot(pdf_path, flavor="stream")
    if tx:
        return tx

    return _parse_via_text(pdf_path)
