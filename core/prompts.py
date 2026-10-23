VISION_PROMPT = """
You are an expert financial document parser.
Extract the transactions from this bank statement image into a JSON array.

Follow these rules strictly:
1. Copy the numbers EXACTLY as printed. Do not calculate the balance yourself. If the printed balance is 354,697.87, write exactly 354697.87.
2. Do not skip decimals (e.g., copy 630.20, not 630).
3. Read the "Money in" and "Money out" columns.
   - If the number is under "Money in", the type is "IN".
   - If the number is under "Money out", the type is "OUT".
4. Read the date from the Date/Time column (format: YYYY-MM-DD).

Return ONLY a valid JSON array. Do not output any markdown or extra text.
Format:
[
  {"date": "YYYY-MM-DD", "description": "string", "amount": number, "type": "IN" or "OUT", "balance": number}
]
"""
