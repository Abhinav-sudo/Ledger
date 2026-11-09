# Exactly-once payments

Ledger's claim is narrow: you can SIGKILL any process at any moment, and no invoice is paid
twice. This page is the argument for that claim, followed by the measurement.

## The shape

```
graph worker ──(one Postgres transaction)──▶ payment_intents row + graph checkpoint
                                                     │
dispatcher ──SELECT … FOR UPDATE SKIP LOCKED──▶ POST /payments  (Idempotency-Key: k)
                                                     │
reconciler ──GET /statement──▶ mark confirmed ──▶ resume the invoice's thread
```

The graph never calls the bank. Its `payment_intent` node only returns an intent. That intent
holds the amount and account taken from validated fields, plus the key
`k = sha256(invoice_id | amount | account)`. The amount is normalised to cents, so 100 and
100.00 produce the same key.

Three independent defenses stack:

1. **One intent per invoice, enforced by the database.** `idempotency_key` and `invoice_id`
   are both UNIQUE. A re-run that computes the same key counts as success. A re-run that
   computes a different amount or account is refused, and the thread goes to a human.
2. **One dispatcher per intent at a time.** A dispatcher holds the row lock from the moment it
   claims the row until it records the outcome. `SKIP LOCKED` makes other dispatchers pass
   over the row.
3. **One payment per key at the bank.** The bank honours `Idempotency-Key`: a repeated key
   returns the original payment instead of paying again.

## Why the intent and the state commit together

A LangGraph node can't open the checkpointer's transaction. The framework persists what a node
returns after the node finishes, through `put_writes` and then `put`, each on its own
autocommit connection.

`OutboxSaver` (in `ledger/side_effects/outbox.py`) subclasses `PostgresSaver`. When either call
carries an intent, it wraps that call in one transaction, which inserts:
- the intent row,
- an audit row,
- the checkpoint data.

The node itself writes nothing.

Both calls are wrapped because a replay test found a real gap. If `put_writes` raises,
LangGraph still applies the node's writes and saves the next checkpoint. Without
`put` also inserting the intent, a rolled-back intent could be checkpointed as recorded and
never paid.

## The four crash windows

| Window | What is durable at the moment of death | What happens next | Why there is no second payment |
|---|---|---|---|
| 1. Before the intent commits | Nothing from `payment_intent`; the checkpoint still says "run payment_intent" | The job lease expires, a worker re-claims it and re-runs the node | The bank was never called; the re-run inserts the intent once |
| 2. After commit, before dispatch | Intent `pending`, plus the node's writes | A dispatcher claims the row; the worker continues the thread from its writes | Re-running the node hits the unique key, which counts as success. A dispatcher that dies before sending releases its lock |
| 3. After the bank call, before `sent` | Intent still `pending`; the bank has paid (or paid and then dropped its response) | The next claim re-sends with the same key | The bank returns the original payment for a repeated key |
| 4. After `sent`, before reconcile | Intent `sent`; the thread waits at `reconcile` | The reconciler matches the statement, marks the intent `confirmed`, and resumes the thread | Confirming is an idempotent update. Resuming only happens while the thread waits, and the reconcile node writes nothing before its interrupt |

**Window 1.** Postgres rolls back the open transaction, so no intent and no checkpoint write
survive. The thread's latest durable checkpoint still has `payment_intent` to run. Only the
dispatcher calls the bank, and it only sees committed rows, so nothing outside the database
knows the attempt happened.

**Window 2.** The intent and the node's writes are durable together. LangGraph sees the
committed writes and doesn't re-run the node. It applies them and moves on to `reconcile`.
(The crash suite found a worker bug here: a re-claimed job treated "writes committed but
checkpoint not advanced" as finished. That is fixed and covered by a test.) Even if something
did re-run the node, for example two workers after a lease expiry, the unique key turns the
insert into a no-op.

**Window 3.** This is the window that matters most, and the guarantee here lives at the bank.
The outbox's contribution is that the key is stable and durable. Whoever retries, after
whatever crash, sends the same `Idempotency-Key`. The same holds when the row has been
dead-lettered after too many attempts: the reconciler confirms it if the bank in fact paid,
and re-driving it reuses the key.

**Window 4.** The money has moved, and only the bookkeeping is behind. The reconciler compares
the bank statement with the intents and flags three things:
- intents with no payment;
- payments with no intent;
- any invoice paid more than once.

Then it resumes threads waiting at `reconcile`. If it dies between confirming and resuming,
the next pass resumes them.

## Measured

`evals/crash_suite.py` uses fresh databases, 4 runs, and dev-seed invoices with ground-truth
extraction (the suite measures payments, not models). It runs these as separate processes,
all of which get killed:
- the API,
- one graph worker,
- two dispatchers,
- the reconciler.

Kills come from two sources:
- chaos points weighted toward the payment window, where a process SIGKILLs itself;
- the supervisor, which kills a random process every 2 to 6 seconds.

For the middle third of each run, the bank pays and then drops 30% of its responses. Then the
chaos stops, everything drains, and the statement is reconciled.

To reproduce, run `docker compose up -d postgres`, then
`uv run python -m evals.crash_suite --outbox on` (also `off` and `off-keyed`). It takes about
20 minutes per mode on a laptop.

### `LEDGER_OUTBOX=on` (the design)

| run | kills | invoices | bank payments | duplicate payments | orphaned intents | payments with no intent | stuck threads |
|---|---|---|---|---|---|---|---|
| 1 | 30 | 60 | 49 | 0 | 0 | 0 | 0 |
| 2 | 57 | 63 | 55 | 0 | 0 | 0 | 0 |
| 3 | 35 | 60 | 50 | 0 | 0 | 0 | 0 |
| 4 | 43 | 67 | 53 | 0 | 0 | 0 | 0 |
| **all** | **165** | **250** | **207** | **0** | 0 | 0 | 0 |

### `LEDGER_OUTBOX=off` (the node pays inline, with a fresh request id per attempt)

| run | kills | invoices | bank payments | duplicate payments | orphaned intents | payments with no intent | stuck threads |
|---|---|---|---|---|---|---|---|
| 1 | 19 | 60 | 61 | 12 | 0 | 61 | 0 |
| 2 | 19 | 63 | 76 | 21 | 0 | 76 | 0 |
| 3 | 25 | 60 | 67 | 17 | 0 | 67 | 0 |
| 4 | 17 | 67 | 64 | 11 | 0 | 64 | 0 |
| **all** | **80** | **250** | **268** | **61** | 0 | 268 | 0 |

### `LEDGER_OUTBOX=off-keyed` (the node pays inline, reusing the deterministic key)

| run | kills | invoices | bank payments | duplicate payments | orphaned intents | payments with no intent | stuck threads |
|---|---|---|---|---|---|---|---|
| 1 | 18 | 60 | 49 | 0 | 0 | 49 | 0 |
| 2 | 20 | 63 | 55 | 0 | 0 | 55 | 0 |
| 3 | 27 | 60 | 50 | 0 | 0 | 50 | 0 |
| 4 | 20 | 67 | 53 | 0 | 0 | 53 | 0 |
| **all** | **85** | **250** | **207** | **0** | 0 | 207 | 0 |

"Invoices" counts each run's slice plus the originals of any duplicates in it. Not every
invoice should be paid: held, rejected and credit-note invoices aren't. That is why the
design pays 207 of 250. No invoice labelled as a duplicate was paid in any mode.

**How to read this.**
- **The outbox design** survived 165 kills and a bank dropping responses, with zero duplicate
  payments. Every payment has an intent, and every thread finished.
- **Paying inline with a fresh request id** made 61 duplicate payments, with some invoices
  paid four times. Most come from the drop window and from kills just after the bank call:
  the retry looks like a new payment to the bank. This mode also logs fewer kills, because the
  dispatchers have no work and their chaos points never fire.
- **Paying inline but reusing the stable key** also produced zero duplicates. Against a bank
  that honours idempotency keys, the key is what stops the double payment. What the outbox
  adds is everything around it:
  - the intent is durable before any money moves, so reconciliation can account for every
    payment (207 payments with no intent here, 0 with the outbox);
  - retries, backoff and dead-lettering happen away from the graph;
  - the graph never blocks on, or fails because of, the bank.

  Inline-keyed is not unsafe against this bank, but it can't tell you what it paid.

## What this does not cover

- **A bank that ignores idempotency keys.** Window 3 depends on it. Against such a bank you
  need its own status endpoint, queried before any retry.
- **Money moved outside the system**, or a statement the bank gets wrong.
- **Two different invoice ids for one bill.** That is a business duplicate, not a retry.
  It is caught by duplicate detection, not by this mechanism. Duplicate detection compares
  against invoices already recorded, so two copies processed concurrently can miss each
  other (see ADR 0002). The suite runs one graph worker for this reason.
- **Version coupling.** `OutboxSaver` relies on `PostgresSaver` internals, so the
  `langgraph-checkpoint-postgres` version is pinned. `tests/test_outbox.py` checks atomicity on
  every upgrade.
