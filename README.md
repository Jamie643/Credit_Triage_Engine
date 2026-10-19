# Credit Triage Engine

AI-powered credit risk triage for Nigerian Microfinance Banks (MFBs).

Layer 1: Document intelligence engine that parses bank statement PDFs, runs integrity and cash-flow checks, and emits a preliminary risk signal.

## Status

Pre-alpha. Building layer 1 (PDF parsing + fraud checks) locally before any integration or deployment.

## Structure

- `src/parsers/` — PDF → structured transaction list
- `src/checks/` — balance reconciliation, circular transfer detection, recurring debt detection
- `src/main.py` — CLI entry point
- `data/samples/` — redacted bank statement PDFs used for local testing (never committed)
- `tests/` — unit tests

## Local setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python -m src.main data/samples/sample.pdf
