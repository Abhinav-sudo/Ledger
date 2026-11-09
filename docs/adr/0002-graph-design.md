# 0002: The invoice graph

Status: accepted, 2026-11-09

## Context

Each invoice has to move from a PDF to a decision. Some get paid with no human involved, and
some wait for one, possibly for days. A worker may die at any point along the way. The graph
has to make the read-only boundary and the replay rule hold by construction, not by care.
This ADR covers the graph up to `payment_intent`, which is a stub until the outbox lands.

## Decision

One LangGraph thread per invoice, `thread_id = invoice_id`, checkpointed by `PostgresSaver`.

```
START -> extract -> match -> route --Send--> variance | duplicate_fraud | missing_po -> policy
                                   \------------------------ clean ------------------> policy
policy -> payment_intent             pay or pay_partial, no approval needed
policy -> approve -> payment_intent  approved and the action pays; otherwise END
policy -> END                        hold, reject, request_credit_note
extract | match -> END               needs_human
```

- **extract** renders page one and asks the `extraction` role for an `ExtractedInvoice`.
  Low confidence means either of two things:
  - a self-reported field confidence is below 0.85;
  - the line amounts don't sum to the total within a cent. This check is in Python with
    `Decimal`, so it doesn't rely on the model being honest.

  Low confidence moves to the next model in the chain, which is ordered weak to strong. If
  the last model is still low, the status is `needs_human`.
- **match** resolves the vendor from the printed name. It runs the deterministic matcher
  against the PO, the goods receipts, the account history and prior invoices. Then it
  records the invoice row (`ON CONFLICT DO NOTHING`, so a replay inserts nothing). The row
  is written here, not in extract, because `invoices.vendor_id` is required and only match
  knows it.
- **route** is a pure function of `MatchResult.exceptions` that returns `Send`s.
  - An LLM router would only add a way to be wrong: the matcher already knows which rule
    fired.
  - The `router` model role stays configured for when classification is genuinely fuzzy.
- **Analysts** (`variance`, `duplicate_fraud`, `missing_po`) are bound only to
  `ledger/tools/erp_queries.py`.
  - Their connection sets `default_transaction_read_only`, so Postgres rejects a write even
    if a query were changed to attempt one.
  - They return `Analysis` rows that are merged by an `operator.add` reducer.
  - If every analyst model fails, the analyst records that it was unavailable rather than
    failing the invoice.
- **policy** is the approval matrix, in Python. The amount is always one of the following,
  computed with `Decimal`:
  - the extracted total;
  - Σ min(invoiced, received) × price;
  - the sum of the credit lines;
  - zero.

  Analyses go into the rationale and nowhere else. No LLM output can change the action, the
  amount, or whether a human is needed.
- **approve** calls `interrupt()` as its first statement.
- **payment_intent** is a stub that records an audit event and writes nothing.

### Approval matrix

| Match outcome (first that applies) | Action | Amount |
|---|---|---|
| duplicate | reject | 0 |
| missing PO, or any price delta beyond 2% | hold | 0 |
| credit line on the invoice | request_credit_note | credit value |
| qty invoiced above qty received | pay_partial | received qty × price |
| otherwise, including price within 2% inclusive | pay | invoice total |

Any `bank_change` sets `requires_approval` with approver `treasury`, whatever the action is.
That is the only route to `approve` today. Everything else is settled by the rules.

### Replay rule

On resume, LangGraph re-runs the interrupted node from its first line. `approve` therefore
does nothing before `interrupt()`. The approval, the status and the audit event are all in
the node's return value, which is committed once with the checkpoint.

Every other side-effecting step is idempotent:
- the invoice insert ignores conflicts;
- the payment node will rely on the outbox's unique idempotency key.

`tests/test_graph_replay.py` covers this end to end:
1. It runs to the interrupt.
2. It closes the pool and rebuilds the graph.
3. It resumes.
4. It asserts that `approve` executed twice, while the approval, its audit event and the
   payment call each appear exactly once.

### Vendor email is data

The analyst sees email text inside `<email>` tags and is told it is untrusted. That
instruction isn't what makes it safe, though. A redirect request is caught by the matcher:
any account-shaped string in the email that isn't the account on file raises `bank_change`.
Policy then sends the invoice to a human. The payment account always comes from the
extracted invoice, never from the email.

## Consequences

- Duplicate detection compares against invoices already recorded. Two copies processed
  concurrently can each miss the other. The eval runner processes invoices in arrival order.
  A database constraint on (vendor, date, total), or a per-vendor lock, would close this gap
  when ingestion becomes concurrent.
- Approving or ingesting the same invoice twice at the same moment can run a node twice. That
  is safe only because the outbox key, which is the next milestone, dedupes the payment
  itself.
- `audit` lives in the checkpointed state. The `audit_log` table is filled when the outbox
  lands.
- A thread whose models all fail stays at its last checkpoint and can be resumed by invoking
  it again. Nothing re-runs it automatically yet.
