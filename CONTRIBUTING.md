# Contributing

Thanks for your interest in `vooi-funding-arb-bot`. This is real-money trading software — contributions are welcome, but the bar for changes that touch order placement, sizing, or safety is high.

## Quick start

```bash
git clone https://github.com/your-org/vooi-funding-arb-bot
cd vooi-funding-arb-bot
brew install uv          # or: curl -LsSf https://astral.sh/uv/install.sh | sh
cp .env.example .env     # fill in VOOI_BEARER_TOKEN
uv sync --extra dev
uv run pytest -q
```

## Branches and PRs

- Branch off `main`. Name branches descriptively (`fix-lighter-bracket-race`, not `patch-1`).
- One concern per PR. Split refactors from feature changes.
- Reference an issue in the PR body if one exists.
- All PRs run CI (ruff, mypy, pytest). Green required to merge.

## What we welcome

- Bug fixes with a regression test.
- New venues (the Aster integration is the reference template — add it the same way).
- Improvements to filters, close criteria, or reporting that come with a justification: at least 7 days of dry-run data showing the change is positive or neutral.
- Documentation improvements — STRATEGY.md is meant to stay current.
- Operational scripts in `scripts/` for common workflows.

## What needs a heavier conversation first

Open an issue and let's discuss before sending the PR:

- New default values for any threshold in `.env.example`.
- Changes to the close-criteria priority list.
- New outbound HTTP endpoints.
- Anything that increases capital at risk per cycle.
- Removing safety gates (`BOT_MIN_HOLD_HOURS`, `BOT_PAIR_COOLDOWN_*`, etc.).

## Code style

- Python 3.11+, type-checked with mypy `--strict`.
- `ruff check .` clean.
- `Decimal` for money, never `float`.
- One event per significant action via `NDJsonLog.emit({"event": "...", ...})`. Don't add free-form prints — they break log parsers downstream.
- Tests live next to source they exercise: `tests/test_<module>.py`.

## Testing

```bash
uv run pytest -q                  # unit tests only
uv run pytest -m integration      # live API tests (needs token + small balance)
uv run mypy fundbot probe
uv run ruff check .
```

Integration tests place real orders at the minimum allowed size. Don't run them blindly.

## Disclosure / security

If you find a security issue (auth leak, order-tampering vector, etc.) email the maintainers privately rather than opening a public issue. See the contact in the project Homepage URL.

## License

By contributing you agree your changes are licensed under MIT (see `LICENSE`).
