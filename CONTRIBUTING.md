# Contributing

Use Python 3.11+ and `python -m pip install -e ".[dev]"` in a virtual environment.

```bash
python -m ruff check src tests
python -m ruff format --check src tests
python -m pytest -q
triageflow eval
```

Keep repository tools read-only and bounded. Add regression tests for changes to
path policy, tool protocols, checkpoints, budgets and evidence validation. Do not
add a shell tool or automatic code execution without a separate sandbox design.
Never commit keys, local repository excerpts, `.env`, or run databases.

Demo results are deterministic fixture regressions. Do not describe them as LLM
diagnosis accuracy. Report model, dataset, configuration and failures when adding
a live evaluation. Changes should remain runnable without provider credentials.

Follow the ownership and workflow overview in [architecture](docs/architecture.md).
