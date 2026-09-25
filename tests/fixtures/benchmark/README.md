# Benchmark golden set

One JSON file per report, each pointing at its PDF. `experian_golden.json` and
`experian_golden.pdf` here are **synthetic** — the sanitized Sep 24 2026
Experian structure with invented account numbers and no consumer PII. They
exist so the harness and its scoring can be tested offline; they are not a
meaningful quality measurement, because the PDF does not actually contain the
accounts the truth file lists.

To benchmark for real, build a golden set outside this repository:

    golden/
      experian.json     experian.pdf
      transunion.json   transunion.pdf
      equifax.json      equifax.pdf

    python scripts/benchmark_extraction.py golden/*.json --out results/

**Never commit a real consumer report or its ground truth.** A truth file
records what the document says about tradelines — masked account numbers,
balances, dates. It must not record the consumer's name, address, SSN or date
of birth; none of those are scored.

Ground truth is partial-friendly: only the fields a file states are scored, so
a set can start with identity, balances, status and dates and grow.

    {
      "name": "experian-2026-09",
      "pdf": "experian.pdf",
      "bureau": "experian",
      "report_date": "Sep 24, 2026",
      "score": 580,
      "score_type": "FICO Score 8",
      "accounts": [
        {
          "creditor_name": "CAINE & WEINER",
          "original_creditor": "PROGRESSIVE",
          "account_number": "88XXXX2211",
          "open_closed": "Open",
          "status_raw": "Collection account",
          "balance": "$1,204",
          "date_opened": "Mar 3, 2024",
          "source_pages": [7],
          "payment_history": { "2026-05": "COL", "2026-04": "COL" }
        }
      ],
      "inquiries": [
        { "creditor_name": "CAPITAL ONE", "inquiry_type": "hard",
          "inquiry_category": "credit_application", "business_type": "Bank Credit Cards" }
      ]
    }
