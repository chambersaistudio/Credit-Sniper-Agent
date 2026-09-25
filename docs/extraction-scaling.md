# Scaling document extraction beyond one pass

**Status: Stage 1 validated in production. Stage 2 implemented and awaiting
its first real batch. Stage 3 (merge) remains a proposal.** Written after the Experian failure
diagnosed in `scripts/diagnose_report.py`.

The failure is confirmed from production: two Sol attempts, both
`AIResponseError` — `Invalid JSON: EOF while parsing a string` at columns
112,636 and 110,739. The model produced ~111,000 characters of JSON and was
cut off mid-string. Nothing was banked; no audit pass ever ran. At ~$1 per
attempt for nothing.

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

### Stage 1 — index (cheap, one call) — IMPLEMENTED

Run it with `scripts/index_pass.py`; see "Validating Stage 1" below.


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

### Stage 2 — batches (the expensive work) — IMPLEMENTED

Run one batch with `scripts/extract_batch.py`; see "Running Stage 2" below.


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

Why 4 and not 1: it keeps detailed output at roughly a quarter of the budget,
leaving ample room for reasoning, without multiplying the call count.

**The document is no longer re-sent whole.** Each batch receives a transient
PDF containing only the pages the Stage-1 index placed its tradelines on,
plus one neighbouring page either side as a safety margin.

Measured on the real banked Experian index (28 pages, 15 tradelines):

| Batch | Indexed pages | Bundle | Padding | Page-sends |
|---|---|---|---|---|
| b0 | 3, 4, 5, 6 | 2–7 | 2, 7 | 6 |
| b1 | 7, 8, 9, 10 | 6–11 | 6, 11 | 6 |
| b2 | 11, 12, 13, 14 | 10–15 | 10, 15 | 6 |
| b3 | 15, 16, 17 | 14–18 | 14, 18 | 5 |
| | | | **total** | **23** |

Against 28 pages × 4 batches = 112 page-sends if each batch re-sent the whole
report, that is **89 fewer — a 79.5% reduction**.

### Stage 3 — merge (deterministic, no model)

Concatenate the batches in index order into one `CreditReportExtraction`,
attach the stage-1 report-level fields, and run the existing duplicate
detection over the result. Purely local; free to re-run.

The auditor then works unchanged on the merged extraction — and can itself be
batched the same way if it starts approaching the budget, which it will on a
large report, since it receives the extraction as prompt input.

### Alternatives considered

- **Deterministic page segmentation first.** Splitting the PDF by locally
  guessed page ranges would reintroduce exactly what the AI-native rewrite
  removed: a heuristic deciding what the model may see. This is why page
  selection is driven by the Stage-1 index — page numbers a model produced
  from the whole document — and never by a regex or a text search.
- **Smaller schema.** Drop `field_evidence`, or store payment history as a
  compact string. Halves the output, but provenance is the thing that makes an
  extraction auditable, and the compact form would have to be re-parsed
  locally. Rejected on those grounds — though trimming evidence to the fields
  that actually get disputed is worth revisiting.
- **Streaming with incremental parse.** Does not help: the cap applies to the
  generated tokens regardless of how they are delivered.

## Validating Stage 1

The index pass is a standalone job. It is deliberately NOT wired into the
upload worker: the full-report extraction it replaces still fails, so running
both would pay Sol to fail after the index had already succeeded.

    # against the stored original for the failed report
    DATABASE_URL=... OPENAI_API_KEY=... \
      python scripts/index_pass.py --report <report_id> --expect 15

One model call, `gpt-5.6-luna` at `detail=low`. It prints the tradelines it
found with their page locations — check those against the document by eye —
plus model, input/output/reasoning tokens, latency and estimated cost. Exit 0
if the index passes its quality gate, 1 if not.

A passing index is checkpointed at `extraction_checkpoint["index"]`, beside
(never replacing) the extraction checkpoint. A failing one is not banked: a
checkpoint promises the work behind it need not be repeated, and an index that
cannot account for every tradeline is not work to build on.

If Luna fails the gate, escalate the **index only**:

    python scripts/index_pass.py --report <id> --expect 15 --model gpt-5.6-terra

The script refuses a Sol model unless explicitly forced. Sol is the escalation
target for detailed extraction, not for indexing.

### What the gate checks

Beyond the confirmed count, an index is only trusted when every tradeline can
be accounted for and found again:

- every entry has a creditor name and at least one source page
- no entry is listed twice — where identity is (furnisher, last four digits,
  original creditor), so Navy Federal's two accounts and Jefferson Capital's
  two collections stay distinct rather than being merged
- the model's self-declared `tradeline_count` matches the list it returned.
  A listing that stops short of its own count is the signature of a truncated
  or abandoned response, and would silently drop accounts from every later
  batch — which is why the schema asks for the total separately
- the bureau is identified, and no page is reported unreadable

### Expected cost

~77 output tokens per entry; ~1,160 for a 15-tradeline report, about 7% of the
index tier's budget. On Luna that is roughly **$0.01–0.02 per index pass**,
against **$1.02 per failed Sol attempt** that banked nothing.

## Running these commands

The Railway image is built from `backend/` alone, so the repository's
`scripts/` directory does not exist in the container. The logic lives in
`app/operator/`, and `scripts/*.py` are thin wrappers around exactly those
modules — one implementation, two entry points, identical behaviour.

**Inside the container** (working directory `/app`):

    python -m app.operator.index_pass     --report <id> --expect 15
    python -m app.operator.extract_batch  --report <id> --batch b0
    python -m app.operator.batch_benchmark --report <id> --batch b0 --truth-inline '<json>'

**From a checkout:**

    python scripts/index_pass.py      --report <id> --expect 15
    python scripts/extract_batch.py   --report <id> --batch b0
    python scripts/benchmark_batch.py --report <id> --batch b0 --truth batch0.json

Nothing in the serving path imports `app.operator`, so shipping these commands
cannot change production behaviour.

## Running Stage 2

    # see the plan, spend nothing
    DATABASE_URL=... python -m app.operator.extract_batch --report <id> --plan

    # run ONE batch
    DATABASE_URL=... OPENAI_API_KEY=... \
      python -m app.operator.extract_batch --report <id> --batch b0

One batch per invocation, deliberately: there is no flag that runs them all,
so no accident spends four times what was asked for.

**Page bundling.** The bundle is built in memory from the banked index's
`source_pages`, handed to one model call, and dropped. The original in R2 is
read, never written. A tradeline the index placed on two pages contributes
both.

**Original page numbers survive.** The model sees a renumbered short document
and reports pages 1..N; those are translated back deterministically through
the bundle's page map, in all three places a page number appears — the
tradeline's `source_pages`, every `field_evidence` entry, and every month of
`payment_history`. A page reference the bundle cannot place is dropped and
reported rather than approximated: a wrong page is worse than none, being
indistinguishable from real provenance later. The manifest given to the model
deliberately contains no original page numbers, so it cannot be tempted to
report those instead of what it sees.

**Independent checkpoints.** A passing batch banks at
`extraction_checkpoint["batches"][batch_id]`. A batch that fails costs only
itself — rehearsed: with b0 banked, a provider failure on b1 left b0 and the
Stage-1 index untouched. A banked batch is reused rather than re-read unless
`--force`.

**The batch gate** requires the batch to answer exactly what it was asked:
every named tradeline returned, nothing extra (the bundle's pages hold other
accounts, and reporting one would become a duplicate at merge time), and every
page reference inside the supplied pages. Identity is matched in tiers —
exact, then name + masked digits, then name + original creditor, then name
alone *only when unique in the batch*. Two collections from one agency are
never paired by name, because that would silently swap their contents.

## Benchmarking a batch

    DATABASE_URL=... OPENAI_API_KEY=... \
      python -m app.operator.batch_benchmark --report <id> --batch b0 \
        --truth-inline '{"accounts": [ ... ]}'

Runs the same batch and the same bundle under A (luna), B (terra) and C (sol),
all at `detail=high`, and reports field accuracy, payment-history accuracy,
provenance accuracy against original page numbers, tokens, latency, cost and
the gate result.

It banks nothing — it reads through `extract_batch`, which touches no
database, never through `run_report_batch`, which is what banks — and it
proves that rather than promising it, by comparing the report's banked batches
before and after and saying so in its output. It changes no production
defaults, and only the batch named by `--batch` (default `b0`) runs: there is
no flag that sweeps them all.

Ground truth can be a file (`--truth batch0.json`), stdin (`--truth -`) or a
literal string (`--truth-inline '<json>'`), the last being the practical one
inside a container.

Record each field as the document prints it, in full. Bureaus put several
clauses in one field — Experian's Status reads "Voluntarily surrendered.
$7,684 past due as of Sep 2026." — and a truth file holding only the first
clause scores a correct extraction as a miss. Field comparison is exact on
purpose: accepting a superset would also accept a model that invented the
extra text.

### Payment-history detail

An accuracy percentage cannot tell two models apart, so the report names every
month that disagreed:

    payment history: 32/48 month(s) correct (66.7%)
      months expected 48, months extracted 44
      CREDIT ACCEPTANCE CORP    expected  12, extracted  11, correct   8   (4 wrong)
      misses:
        CREDIT ACCEPTANCE CORP  2026-03  expected `OK`  got `30`
        CREDIT ACCEPTANCE CORP  2026-12  expected `OK`  got MISSING

A month read wrong and a month never returned are different failures calling
for different fixes, so they are distinguished rather than both counted as
"incorrect". Every miss also reaches the `--json` output uncapped, which is
what makes "did Luna and Terra get the SAME cells wrong?" answerable.

## Sequencing

1. **Land the failure taxonomy** (done) so a truncation is never again
   mistaken for an outage or silently re-bought.
2. **Confirm the diagnosis** (done — production shows two `AIResponseError`
   truncations at ~111k characters).
3. **Validate the index pass** (done — Luna indexed all 15 tradelines,
   distinct, with page refs, in 10.5s for $0.0035; 12,486 input tokens at
   `detail=low`, well under the estimate).
4. **Build Stage 2** (done). Not wired into the upload worker: the operator
   runs one batch at a time.
5. **Benchmark the first batch** across luna/terra/sol at `detail=high`
   against confirmed ground truth, then choose the production model.
6. **Stage 3 (merge)** once a model is chosen: concatenate banked batches in
   index order, attach Stage 1's report-level fields, re-run duplicate
   detection, then the existing auditor over the merged extraction.

## What this changes about cost

The failed run billed ~94,300 input + 32,000 output tokens for nothing.

Two things changed the arithmetic since that was written. The Stage-1 index at
`detail=low` cost 12,486 input tokens — far less than the estimate, because
low-detail rendering is dramatically cheaper per page. And batches no longer
re-send the whole document: 23 page-sends instead of 112, a 79.5% reduction.

So the earlier caveat — "batching probably costs more on a successful
extraction" — no longer obviously holds, and should not be assumed either way
until the batch benchmark measures it. What is certain is the failure case: a
batch that fails costs one batch, not the whole report, and every batch that
already passed stays banked.

Prompt caching on the bundle prefix is the remaining lever and is worth
measuring once a model is chosen.
