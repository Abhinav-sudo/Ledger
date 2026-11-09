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
