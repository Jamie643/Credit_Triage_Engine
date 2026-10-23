# Credit Triage Engine

An AI-powered risk intelligence layer for Nigerian Microfinance Banks. 

This engine takes unstructured loan documents (bank statements, guarantor forms, collateral docs) and turns them into a structured risk assessment for credit officers.

## Architecture

1. **Vision Layer (AI):** Uses a fine-tuned Vision Language Model (Qwen2-VL) to visually read blurry screenshots, photos, and handwritten forms, extracting data into JSON.
2. **Translation Layer:** Converts the AI's simple "IN/OUT" labels into standard banking Debits/Credits.
3. **Math Gate (The Bouncer):** Validates the math row-by-row (`Prev Balance - Debit + Credit = Curr Balance`). If the AI hallucinates or a page is missing, the math will not add up, and the system flags the file as `RED` (Tampering Risk).
4. **Document Parser (PDFs):** Uses Camelot to extract clean tables from digital PDFs (UBA, GTBank, Zenith).

## Current Status

- [x] Math Gate Engine (`core/engine.py`) built and tested.
- [x] Auto-correction logic for AI column mix-ups.
- [x] PDF Parsing pipeline proven with Camelot.
- [ ] Vision Model GPU integration (Pending hardware access).
- [ ] Next.js Web Dashboard.
