# Contributing

Thanks for helping with Klepa. The project keeps a family's documents, so correctness and privacy come before speed.

## Setup

You need Python 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

## Before you open a pull request

```bash
uv run pytest
uv run ruff check
uv run ruff format --check
uv run mypy
```

CI runs the same checks, with tests on macOS and Linux.

## How we work

- **Issue first.** One issue, one branch, one pull request. The PR description says `Closes #N`.
- **Tests first.** Write the failing test, watch it fail, then make it pass. Anything that touches intake, storage or delivery also gets an acceptance test against the fake Telegram (`tests/faketg.py`).
- **Linear history.** Rebase on `main`; pull requests are merged by fast-forward.
- **Commits.** Use [Conventional Commits](https://www.conventionalcommits.org/) (`feat`, `fix`, `docs`, `test`, `refactor`, `style`, `build`, `ci`, `chore`). The subject is imperative and at most 72 characters; the body explains why.

## Style

- ruff decides formatting (120 columns) and lint. mypy runs in strict mode over `core/klepa_core`.
- Everything is in English: code, comments, docs, issues, commit messages.
- Code carries no other languages. User-facing texts live in [`core/klepa_core/locales/`](core/klepa_core/locales/). The only test module with non-English text is `tests/test_languages.py`, because it checks that data.

## Data rules

- Never commit installation data: tokens, keys, databases, documents, real names, Telegram IDs or locations.
- Tests use synthetic members only (`tests/helpers.py`).
- Keep real data out of issues and logs you paste, too.

## Adding a language

1. Copy `core/klepa_core/locales/en.toml` to `<code>.toml`, where `<code>` is the ISO 639-1 code.
2. Pick the plural rule that matches the language: `one-other`, `one-few-many` or `one-few-other`. If none fits, add a rule to `PLURAL_RULES` in `locale.py`.
3. Translate the texts, fill in the plural forms and choose the private keywords. Avoid keywords that are part of common document names; in some languages the word for "personal" is part of the name of an ID card.
4. Add receipt and keyword checks to `tests/test_languages.py`.
