# Scaling document extraction beyond one pass

**Status: proposal. Nothing here is implemented.** Written after the Experian
failure diagnosed in `scripts/diagnose_report.py`.

## The constraint

One `responses.parse` call must emit the entire report as one JSON object, and
`max_output_tokens` caps reasoning *plus* the answer. Our own extractor prompt
demands the expensive parts: the month-by-month grid "cell by cell", and
per-field evidence excerpts.

Measured against the current schema (`ExtractedTradeline` with 25 months of
history and 8 evidence items — a normal Experian tradeline):

| Tradelines | Output tokens | % of the 32,000 budget | Headroom for reasoning |
|---|---|---|---|
| 5 | ~10,500 | 33% | 21,500 |
| 10 | ~21,000 | 66% | 11,000 |
| 12 | ~25,100 | 79% | 6,900 |
| **15** | **~31,400** | **98%** | **600** |
| 20 | ~41,800 | 131% | none |

**~2,100 output tokens per fully-detailed tradeline.** A 15-tradeline Experian
disclosure needs essentially the whole budget before the model thinks at all.
TransUnion and Equifax succeed because they are under the line, not because
anything about them is easier.

Raising `max_output_tokens` is the wrong fix twice over. It buys maybe five
more tradelines before the same wall, and it raises the price of *every*
successful extraction — including the ones that already work.

## The rule the design has to satisfy

> A failure on account/page N must not require paying to regenerate accounts
> 1 … N−1.

Today one truncation discards everything. The checkpointing added for async
processing already gives us the machinery — it just operates at the wrong
granularity. The unit of checkpointing is currently "the whole extraction";
it needs to be "one batch of tradelines".

## Recommendation: index pass, then account batches

Three stages, each independently checkpointed.

### Stage 1 — index (cheap, one call)

Read the whole PDF and return **only the report-level facts and a tradeline
index**: bureau, document date, score and type, and for each tradeline its
creditor name, masked account number, and the pages it occupies. No balances,
no payment history, no evidence.

~80 tokens per tradeline — a 15-account report indexes in **~1,500 output
tokens**, 5% of the current budget. This can run at `detail=low`; locating a
tradeline heading does not need full-resolution rendering.

The index is also the count the auditor is checked against, which strengthens
the existing "audit counted N, extraction has M" gate: today both numbers come
from a model that saw everything at once.

### Stage 2 — batches (the expensive work, parallelisable)

For each batch of **4 tradelines** (~8,500 output tokens, 26% of budget —
roughly 3× headroom for reasoning), send the same original PDF plus the index
entries for that batch, and ask for those tradelines only, in full detail.

A 15-account report becomes 4 batch calls. Each lands in its own checkpoint
slot the moment it succeeds:

```
extraction_checkpoint = {
  "index": {...},                     # stage 1, banked
  "batches": {
    "0": {"accounts": [...]},         # banked
    "1": {"accounts": [...]},         # banked
    "2": null,                        # failed — only this one is re-bought
    "3": {"accounts": [...]}          # banked
  }
}
```

Batch 2 failing re-runs batch 2. That is the rule, satisfied directly.

Why 4 and not 1: input tokens dominate. The PDF is re-sent with every batch —
~94,000 input tokens in the failed run — so the batch size trades output-budget
headroom against re-sending the document. Batches of 4 keep the call count
low while leaving ample room for reasoning. **The real number should come from
the benchmark, not from this estimate.** Prompt caching on the document prefix
would change the calculus substantially and is worth measuring first.

### Stage 3 — merge (deterministic, no model)

Concatenate the batches in index order into one `CreditReportExtraction`,
attach the stage-1 report-level fields, and run the existing duplicate
detection over the result. Purely local; free to re-run.

The auditor then works unchanged on the merged extraction — and can itself be
batched the same way if it starts approaching the budget, which it will on a
large report, since it receives the extraction as prompt input.

### Alternatives considered

- **Deterministic page segmentation first.** Split the PDF by page ranges
  locally, then extract per chunk. Cheaper (each call carries fewer input
  tokens), but it reintroduces exactly what the AI-native rewrite removed: a
  local heuristic deciding what the model is allowed to see. A tradeline
  spanning a page break, or an Experian two-column layout, would be silently
  cut. Viable *later* as an input-cost optimisation, using the stage-1 index's
  page numbers — which the model produced — rather than a regex.
- **Smaller schema.** Drop `field_evidence`, or store payment history as a
  compact string. Halves the output, but provenance is the thing that makes an
  extraction auditable, and the compact form would have to be re-parsed
  locally. Rejected on those grounds — though trimming evidence to the fields
  that actually get disputed is worth revisiting.
- **Streaming with incremental parse.** Does not help: the cap applies to the
  generated tokens regardless of how they are delivered.

## Sequencing

1. **Land the failure taxonomy** (done) so a truncation is never again
   mistaken for an outage or silently re-bought.
2. **Confirm the diagnosis** on the real failed report with
   `scripts/diagnose_report.py`, and against the provider's usage dashboard.
3. **Measure the index pass alone** on the real Experian PDF — one cheap call.
   If it returns 15 tradelines with page numbers, the design is sound and the
   rest follows.
4. **Build stages 1–3** behind a setting, defaulting off.
5. **Benchmark** batched-Sol against single-pass Sol on the reports that
   currently succeed, to confirm batching does not cost quality. Only then
   consider the A/B/C model comparison — cheaper models are a separate
   question from a working extraction shape.

## What this changes about cost

The failed run billed ~94,300 input + 32,000 output tokens for nothing.
Batching re-sends the document per batch, so input tokens rise; output tokens
stay roughly the same in total but become *recoverable*. The honest summary:
batching probably costs more on a successful extraction and enormously less on
a failing one, because a failure stops discarding everything that preceded it.
Prompt caching is the lever that would make it cheaper on both, and should be
measured as part of step 5.
