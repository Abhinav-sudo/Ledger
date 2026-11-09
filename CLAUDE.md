Ledger is a multi-agent accounts-payable exception-handling system on LangGraph. Its core
claim: the system can be killed at any point and never pays an invoice twice.

## Invariants

These are non-negotiable. A change that breaks one is wrong even if the tests pass.

1. Single write path. Analyst agents (variance, duplicate, bank-change, missing-PO) are bound
   to read-only tools only. Only the `payment_intent` node creates payments, and it writes an
   outbox row. It never calls the bank directly.
2. Replay rule. A LangGraph node re-runs from its start when the graph resumes after
   `interrupt()`. Every side-effecting node must be idempotent. The approval node writes
   nothing before it calls `interrupt()`.
3. Outbox. The payment node writes the state update and a `payment_intents` row in one
   Postgres transaction.
   - `idempotency_key` is UNIQUE and equals sha256(invoice_id | amount | vendor_account).
   - A unique-constraint violation on re-run counts as success.
   - A separate dispatcher claims rows with `SELECT ... FOR UPDATE SKIP LOCKED` and sends
     the `Idempotency-Key` header.
4. No generated amounts. Amounts come only from validated extracted fields. LLMs choose from
   enumerated actions. All arithmetic is in Python using `Decimal`, never `float`.
5. Policy is code. The approval matrix is deterministic Python rules. An LLM may explain a
   decision but never makes it. Bank-detail changes always go to a human.
6. Free tier only. Everything runs locally in Docker Compose. LLM access goes through one
   provider-agnostic `ModelRouter` with an OpenAI-compatible interface.
   - Providers, in order: Ollama (local), Gemini free tier (vision extraction), Groq free
     tier, OpenRouter `:free` models.
   - Each role in config gets one primary model and two fallbacks.
   - A circuit breaker trips a provider after repeated 429 errors.
   - Never hard-code a model name outside the config.
7. Ground truth. The generator is seeded and writes a label for every invoice. The dev seed
   and the held-out seed are committed (`world/generator.py`). Never tune on held-out data.

## Stack

Python 3.12, uv, LangGraph 1.x with PostgresSaver, Pydantic v2, FastAPI, Postgres 16,
Redis + arq, Mailpit, ReportLab, pytest + Hypothesis, OpenTelemetry -> Langfuse.

## Conventions

Typed everywhere, ruff, small modules. Tests ship in the same commit as the code that needs them.

## Commands

- `uv run pytest`
- `uv run ruff check . && uv run ruff format --check .`
- `docker compose up` (postgres, redis, mailpit, mock-bank)
- `uv run python -m world.generator --n 500 --seed 42` writes to `out/seed-42/`

## Git discipline

- The working tree must be clean at the end of every task. Never stop with uncommitted
  or unstaged changes unless I explicitly say "don't commit".
- Run `git status` at the start and end of every task. Report anything unexpected
  instead of silently stashing, discarding, or reverting it.
- Stage files explicitly by path. Never `git add -A` or `git add .`.
- One logical change per commit. If a task touched three unrelated things, make three commits.
- Commit subjects: imperative mood, lowercase after the type prefix, under 60 chars,
  no trailing period. Conventional prefixes (feat/fix/refactor/chore/test/docs).
  Body only when the "why" isn't obvious from the diff.
- Never add "Generated with Claude Code", "Co-Authored-By: Claude", emoji, or any
  attribution trailer to commits or PR descriptions.
- Work on feature branches. Never commit directly to main. Never force-push to main.
  Never amend or rebase a commit that is already pushed.
- Never commit: .env files, credentials, API keys, data dumps, build artifacts,
  lockfile churn unrelated to the task, or debug scratch files.
- If you create temporary scripts or scratch files while working, delete them before
  you finish. They never get committed.

## Code style

Write code the way an experienced engineer on a small team would write it. Specifically:

- Comments explain *why*, never *what*. If the comment restates the line below it, delete it.
  A typical file should have very few comments — often zero.
- No banner/divider comments, no section headers like `# ===== Helpers =====`,
  no `# Step 1:` narration above obvious code.
- No docstrings on trivial or self-evident functions. Docstring only public APIs with
  non-obvious contracts, and keep them to one or two lines.
- No emoji anywhere — code, comments, commit messages, README, log output, CLI output.
- No defensive try/except or null-checks around things that can't realistically fail.
  Let errors propagate unless there's a specific recovery.
- No premature abstraction: no base classes, interfaces, factories, or config layers
  with a single implementation. Write the concrete thing first.
- Short, plain names. `users`, not `user_data_list`. `parse`, not `process_and_handle_input`.
- No redundant `else` after `return`. No wrapper functions that just call one other function.
- Don't sprinkle logging everywhere. Log at boundaries and on failure, not line by line.
- Match the style already present in a file. Don't reformat or "improve" untouched code
  as part of an unrelated change.
- README: short. What it is, how to run it, how to test it. No badge walls, no emoji
  headers, no feature tables, no "🚀 Getting Started".
- No `TODO` / `FIXME` placeholders or stubbed functions left behind. Either implement it
  or tell me it's out of scope.

## Before you finish any task

- Run the linter/formatter and the tests. Don't report done if either fails.
- Re-read your own diff and strip anything that reads as filler: redundant comments,
  unused imports, dead code, over-eager error handling.
- Confirm `git status` is clean.
