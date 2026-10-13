# v0.1.0 validation record

Verified locally on 2026-10-12, Windows / Python 3.12.4. These are development
checks, not production performance or real-world model accuracy claims.

| Check | Observed result |
| --- | --- |
| `python -m pytest -q` | 123 passed; 69 additional parameter subtests passed |
| `python -m ruff check src tests` | Passed |
| `python -m ruff format --check src tests` | 22 Python files already formatted |
| `node --check src/triageflow/web/app.js` | Passed |
| `triageflow eval` | 8/8 synthetic fixture cases passed, demo mode |
| `triageflow demo --case coupon --output ...` | Generated Markdown with exact source evidence |
| `python -m build` | sdist and wheel built; dashboard and fixtures present in wheel |
| Browser walkthrough | Selected coupon example, submitted, verified report, expanded tool call |
| Browser persistence | Restarted server, reloaded page, reopened completed report from history |
| Browser error log | No JavaScript warning/error observed during walkthrough |

The test run emitted one upstream deprecation warning: Starlette's TestClient
currently accepts `httpx` but recommends `httpx2` for future versions. This did not
fail tests; revisit when updating the development dependency snapshot.

Not verified here: Linux execution, GitHub-hosted CI, paid/live provider calls,
real-world diagnosis quality, mobile-device interaction or production deployment.
The CI matrix is configured for Windows/Ubuntu and Python 3.11/3.12; confirm its
results after publishing the repository.

Screenshot: [dashboard](assets/dashboard.jpg). Example: [generated report](examples/coupon-report.md).
