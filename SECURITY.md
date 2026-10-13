# Security and trust boundaries

This is a local, single-user development application, not a hosted multi-tenant
service. Bind to `127.0.0.1`, use one server worker and protect the local machine.
The default CLI enforces this bind address. Host/origin checks are defense in depth,
not authentication. A local process can access the API.

- Tools only list, search and read configured source files; they cannot run a
  shell, import target code, alter files, or access the network.
- Traversal, symlinks, Windows junctions/reparse points, hidden/sensitive names,
  binary files and oversized reads are denied. Scans and iterations have limits.
- These checks are **not an OS sandbox**. Do not point the app at adversarially
  changing filesystems. Hard links, source-embedded secrets and concurrent parent
  directory changes cannot be fully contained by a portable Python path policy.
- Prompt-injection defenses are instructions plus capability restrictions and
  citation checks, not a guarantee the model cannot be misled.
- Live mode sends the issue and selected source excerpts to the configured model
  endpoint. File-name filtering is not secret detection. Inspect code before using
  a third-party model with confidential repositories.
- SQLite journals include issues and source excerpts, stored unencrypted in the
  configured data directory (default `.triageflow/`, overridden by `TRIAGEFLOW_DATA_DIR`).
  Delete or protect that directory according to your requirements.
- Provider errors omit response bodies and credentials. Keys stay in environment
  variables and never go to the browser. Avoid putting secrets in issue text.
- Citation verification confirms that source lines exist and were read; it does
  not establish semantic correctness or prove the proposed repair works.

For suspected vulnerabilities, avoid including real credentials or private source
in public issues. Use the repository owner's private security reporting channel
when enabled; otherwise open a minimal issue requesting a private contact route.
