"""
Bank statement PDF parser.

Strategy:
    1. Camelot 'lattice' mode — for PDFs with visible ruling lines (UBA, GTBank).
    2. Camelot 'stream' mode — for PDFs without lines (Moniepoint, Opay, etc).
    3. pdfplumber text — fallback.

Post-processing handles:
    - Multi-line dates.
    - Zenith-specific column mapping.
    - Moniepoint-specific column mapping (handles 'NA' placeholders and 15 columns).
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pdfplumber

_DATE_FORMATS = [
    "%d-%b-%Y", "%d-%b-%y", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y",
    "%d %b %Y", "%d %B %Y", "%Y-%m-%d", "%d-%b-%Y %H:%M %p", "%Y-%m-%d %H:%M",
]

_DATE_TOKEN_RE = re.compile(r"^(\d{1,2})[-/ ]([A-Za-z]{3,9})[-/ ]?(\d{2,4})?$")

def _parse_date(cell: str) -> str | None:
    if not cell: return None
    cleaned = re.sub(r"\s+", " ", str(cell)).strip()
    # Strip time component if present (e.g. '2024-09-15 21:31' -> '2024-09-15')
    if " " in cleaned and ":" in cleaned:
        cleaned = cleaned.split(" ")[0]
        
    for fmt in _DATE_FORMATS:
        try: return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except ValueError: continue
        
    token = cleaned.split()[0] if cleaned.split() else ""
    for fmt in _DATE_FORMATS:
        try: return datetime.strptime(token, fmt).strftime("%Y-%m-%d")
        except ValueError: continue
    return None

def _to_amount(cell: str) -> float | None:
    if cell is None: return None
    s = str(cell).strip()
    if not s or s.upper() in {"-", "—", "--", "NA", "N/A", ""}: return None
    s = re.sub(r"\s+", "", s)
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1]
    s = re.sub(r"^(?:N|NGN|₦)", "", s, flags=re.IGNORECASE)
    if not re.fullmatch(r"\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\.\d{1,2}|\d+(?:\.\d{1,2})?", s):
        return None
    try: v = float(s.replace(",", ""))
    except ValueError: return None
    return -v if negative else v

def _clean_description(cell: str) -> str:
    if not cell: return ""
    return re.sub(r"\s+", " ", str(cell)).strip()

def _is_header_row(row_values: list[str]) -> bool:
    joined = " ".join((v or "").lower() for v in row_values)
    return "date" in joined and ("balance" in joined or "narration" in joined or "description" in joined)

def _is_footer_row(row_values: list[str]) -> bool:
    joined = " ".join((v or "").lower() for v in row_values)
    return any(kw in joined for kw in ("download app", "head office", "privacy policy", "customer service"))

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
        
        flat_values = [str(v).upper() for v in df.values.flatten()]

        # --- ZENITH SPECIFIC LAYOUT ---
        if any("WITHDRAWALS" in v for v in flat_values) and not any("MONIEPOINT" in v for v in flat_values):
            for _, row in df.iterrows():
                vals = [str(v).strip() for v in row.tolist()]
                if len(vals) < 6 or not vals[0] or "DATE" in vals[0].upper(): continue
                date_val = _parse_date(vals[0])
                if not date_val: continue
                
                debit = _to_amount(vals[2])
                credit = _to_amount(vals[3])
                if debit == 0.0: debit = None
                if credit == 0.0: credit = None
                
                transactions.append({
                    "date": date_val, "description": _clean_description(vals[1]),
                    "debit": debit, "credit": credit, "balance": _to_amount(vals[5]),
                    "raw": " | ".join(vals),
                })
            continue 

        # --- MONIEPOINT SPECIFIC LAYOUT ---
        # Moniepoint has a 'Transaction Amount' column (index 5) which is the real amount,
        # but also has separate 'Debit' (6), 'Credit' (7), and 'Balance' (8) columns.
        if any("MONIEPOINT" in v for v in flat_values) or any("TRANSACTION AMOUNT" in v for v in flat_values):
            # We know Moniepoint maps exactly to these indices based on our test
            for _, row in df.iterrows():
                vals = [str(v).strip() for v in row.tolist()]
                if len(vals) < 9 or not vals[0] or "DATE" in vals[0].upper(): continue
                
                # Moniepoint dates are at index 0 (e.g. 2024-09-15 21:31)
                date_val = _parse_date(vals[0])
                if not date_val: continue
                
                # Index 6 = Debit, 7 = Credit, 8 = Balance
                debit = _to_amount(vals[6])
                credit = _to_amount(vals[7])
                
                # Moniepoint sometimes uses '0.00' instead of empty strings
                if debit == 0.0: debit = None
                if credit == 0.0: credit = None
                
                transactions.append({
                    "date": date_val, 
                    "description": _clean_description(vals[9]), # Index 9 is Narration
                    "debit": debit, 
                    "credit": credit, 
                    "balance": _to_amount(vals[8]),
                    "raw": " | ".join(vals),
                })
            continue
        # --- END MONIEPOINT SPECIFIC LAYOUT ---

        # --- STANDARD LAYOUT (UBA, GTBank, Synthetic) ---
        for _, row in df.iterrows():
            values = [str(v) for v in row.tolist()]
            if _is_header_row(values):
                # Use our generic header matcher
                # (Which was previously in _find_column_indices)
                roles = {}
                for idx, cell in enumerate(values):
                    c = re.sub(r"\s+", " ", str(cell)).strip().lower()
                    if "date" in c and "date" not in roles: roles["date"] = idx
                    elif "narration" in c or "description" in c: roles["description"] = idx
                    elif "debit" in c or "withdrawal" in c: roles["debit"] = idx
                    elif "credit" in c or "deposit" in c: roles["credit"] = idx
                    elif "balance" in c: roles["balance"] = idx
                if "date" in roles: columns = roles
                continue
                
            if columns is None: continue
            if _is_footer_row(values): break

            def get(role: str) -> str:
                idx = columns.get(role)
                return values[idx] if idx is not None and idx < len(values) else ""

            date_val = _parse_date(get("date"))
            if date_val is None: continue

            transactions.append({
                "date": date_val,
                "description": _clean_description(get("description")),
                "debit": _to_amount(get("debit")),
                "credit": _to_amount(get("credit")),
                "balance": _to_amount(get("balance")),
                "raw": " | ".join(values),
            })
            
    return transactions

def _parse_via_text(pdf_path: Path) -> list[dict]:
    # (Fallback text parser kept exactly as it was)
    transactions: list[dict] = []
    line_re = re.compile(r"^(?P<date>\d{1,2}[/\-][A-Za-z0-9]{2,9}[/\-]\d{2,4})\s+(?P<rest>.+)$")
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            for line in text.splitlines():
                m = line_re.match(line.strip())
                if not m: continue
                date_val = _parse_date(m.group("date"))
                if not date_val: continue
                amounts = [_to_amount(tok) for tok in m.group("rest").split()]
                amounts = [a for a in amounts if a is not None]
                if len(amounts) < 2: continue
                transactions.append({
                    "date": date_val, "description": m.group("rest"), 
                    "debit": None, "credit": amounts[-2], "balance": amounts[-1], "raw": line
                })
    return transactions

def parse_bank_statement(pdf_path: str | Path) -> list[dict]:
    pdf_path = Path(pdf_path)
    if not pdf_path.exists(): raise FileNotFoundError(f"No such file: {pdf_path}")

    # 1. Try Lattice (Grid lines)
    tx = _parse_with_camelot(pdf_path, flavor="lattice")
    if tx: return tx

    # 2. Try Stream (No grid lines, like Moniepoint)
    tx = _parse_with_camelot(pdf_path, flavor="stream")
    if tx: return tx

    # 3. Fallback to raw text regex
    return _parse_via_text(pdf_path)
