# TriageFlow

Evidence-first bug triage with bounded agents, durable checkpoints, and reproducible evaluation.

TriageFlow turns a bug description into a source-backed investigation report. A planner
builds the investigation plan, an investigator calls read-only repository tools, and a reviewer
checks the conclusion in a separate context. Deterministic validation ensures every citation
matches current source lines and was actually inspected by that run.

## Quick start

Python 3.11+ is required. In the repository:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
triageflow serve
```

On Windows, use `.venv\Scripts\python.exe` and `.venv\Scripts\triageflow.exe` directly.
Open http://127.0.0.1:8000. The interface and generated reports are Chinese-first.

Default **demo mode is deterministic and does not call an LLM**. It inspects real fixture
files for three deliberately seeded bugs. Unknown issues yield an explicit abstention.

```bash
triageflow demo --case coupon --output artifacts/report.md
triageflow eval
python -m pytest -q
```

## Live models

Copy `.env.example` to `.env`, set `TRIAGEFLOW_MODE=live`, `TRIAGEFLOW_API_KEY`,
`TRIAGEFLOW_MODEL`, `TRIAGEFLOW_BASE_URL`, and optionally `TRIAGEFLOW_REPO` to an absolute
repository path. Restart the server. Use a model supporting **Chat Completions function tools**;
Responses-only models are not supported by this adapter. Local unauthenticated servers may use
a placeholder API key. Live mode transmits selected source excerpts to the configured provider.

## Engineering features

- Bounded model/tool loops and transient-failure retries.
- Exact path/line/quote validation with per-run read provenance.
- SQLite checkpoints, atomic run claims, recovery with retained budgets.
- Action traces, reported usage, history and downloadable Markdown reports.
- Repository path, symlink/reparse, source-type and size policies.
- Python package with a static dashboard; no Node build required.
- Offline protocol, boundary, workflow and API tests; Windows/Linux CI configuration.

The eight evaluation cases are synthetic regression fixtures, **not a real-world model
accuracy benchmark**. Provider tests use mock responses; no live-provider quality claim is made.
Citation verification does not establish root-cause correctness, and test plans are not executed.

Designed for a local single user and one server process. No authentication, remote repository
cloning, code changes or target-code execution. See [architecture](architecture.md),
[security](../SECURITY.md), [contributing](../CONTRIBUTING.md), and [MIT license](../LICENSE).
