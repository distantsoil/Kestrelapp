# Dependency security updates

Branch: `deferred/dependency-alerts`, based on redesign commit `25fbe9f`.
Decision: D-UI-175. Changed only dependency declarations, the lockfile and project records.
No application behavior, prompt, schema, migration or test assertion changes.

## Alert inventory

The private repository's live Dependabot API reported 55 open alerts on 2026-10-25.
The inventory is a snapshot of the default branch; publishing this branch does not itself
close default-branch alerts.

| Package | Starting version | Patched minimum | Final lock | Alert count |
| --- | --- | --- | --- | --- |
| Pillow | 12.3.0 | 13.0.0 | 13.0.0 | 41 |
| filelock | 4.2.0 | 4.2.1 | 4.2.4 | 6 |
| pypdf | 6.21.1 | 6.21.2 | 6.21.2 | 3 |
| fastmcp | 3.4.5 | 3.4.10 | 3.4.10 | 1 |
| fastmcp-slim | 3.4.5 | 3.4.10 | 3.4.10 | 1 |
| setuptools | 83.0.0 | 84.1.0 | 84.2.1 | 1 |
| opentelemetry-api | 1.44.0 | 1.45.1 | 1.45.1 | 1 |
| certifi | 2026.7.22 | 2026.10.19 | 2026.10.23 | 1 |

## Validation

The owner intentionally stopped the first live-browser full check and instructed that the
full suite must not be rerun for this item. Run only relevant existing test files after each
package update, then commit and push; the full suite runs once after merge. FastMCP's
exact-version slim dependency must update with it. Targeted results will be recorded below.
Third-party notices are generated from installed distributions by the existing packaging
code and verified by the notices tests.

The interrupted check passed Ruff and reached approximately 8% of pytest before the owner's
stop. It produced no pytest summary and no `Ready to push.` result. Its last five lines were:

```text
........................................................................ [  1%]
........................................................................ [  3%]
........................................................................ [  5%]
........................................................................ [  7%]
........................................................................ [  8%]
```

Log: `%TEMP%\kestrel-dependency-alerts-01-pillow-check.txt`.

## Package results

1. Pillow: 12.3.0 to 13.0.0, direct minimum `pillow>=13.0.0`.
   `uv lock --upgrade-package pillow`; `uv sync --extra dev`.
   `uv run --extra dev pytest tests/test_packaging.py tests/test_notices.py tests/test_privacy_review.py tests/test_close_to_tray.py -q`:
   **92 passed in 85.64s**. Covers icon decoding, photo processing, desktop tray, packaging
   and generated notices. No application or notices source changes were needed.

2. filelock: 4.2.0 to 4.2.4, transitive constraint `filelock>=4.2.1`.
   `uv lock --upgrade-package filelock`; `uv sync --extra dev`.
   `uv run --extra dev pytest tests/test_inprocess_embed.py tests/test_notices.py -q`:
   **38 passed, 1 skipped in 33.33s**. The existing real-embedding test requires uninstalled
   bundled model weights; the deterministic tokenizer/session tests and notices tests pass.

3. pypdf: 6.21.1 to 6.21.2, direct minimum `pypdf>=6.21.2`.
   `uv lock --upgrade-package pypdf`; `uv sync --extra dev`.
   `uv run --extra dev pytest tests/test_docs.py tests/test_docs_convert.py tests/test_notices.py -q`:
   **46 passed, 2 skipped in 96.26s**. Both skips are existing POSIX-shell stand-ins for
   LibreOffice, disabled on Windows. PDF rendering, text and metadata checks ran successfully.

4. FastMCP and fastmcp-slim: 3.4.5 to 3.4.10, direct minimum `fastmcp>=3.4.10,<4`.
   `uv lock --upgrade-package fastmcp==3.4.10`; `uv sync --extra dev`.
   FastMCP pins its slim distribution to exactly the same release, so they move together.
   The 3.4.10 patch preserves MCP 1.30.0 and permits certifi to be updated independently;
   3.4.11 requires patched certifi, and FastMCP 4 also brings a major MCP upgrade.
   `uv run --extra dev pytest tests/test_mcp.py tests/test_mcp_actions.py tests/test_mcp_lan_key.py tests/test_mcp_switch.py tests/test_confidential_api_mcp.py tests/test_privacy_review_contacts.py tests/test_notices.py -q`:
   **68 passed in 104.61s**. Covers tool access, LAN authentication, Host/Origin protections,
   live server lifecycle, confidential data and notices.

5. setuptools: 83.0.0 to 84.2.1, transitive constraint `setuptools>=84.1.0`.
   `uv lock --upgrade-package setuptools`; `uv sync --extra dev`.
   `uv run --extra dev pytest tests/test_packaging.py tests/test_notices.py -q`:
   **57 passed in 28.47s**. Setuptools is present through the development packaging tool
   PyInstaller; the alert API labels it runtime, but `uv tree` identifies its actual dependency
   path. It remains outside the application's runtime notices closure.

6. opentelemetry-api: 1.44.0 to 1.45.1, transitive constraint `opentelemetry-api>=1.45.1`.
   `uv lock --upgrade-package opentelemetry-api`; `uv sync --extra dev`.
   `uv run --extra dev pytest tests/test_mcp.py tests/test_mcp_actions.py tests/test_notices.py -q`:
   **27 passed in 47.96s**. MCP read/action tool calls exercise the dependency's importing
   library; no live external service is called.

7. certifi: 2026.7.22 to 2026.10.23, transitive constraint `certifi>=2026.10.19`.
   `uv lock --upgrade-package certifi`; `uv sync --extra dev`.
   `uv run --extra dev pytest tests/test_http_client.py tests/test_llm_client.py tests/test_notices.py -q`:
   **26 passed in 17.24s**. Ingestion politeness and model-client request handling stay covered
   with mocked HTTP services. No real job board, model provider or ATS is contacted.

## Final checks and omissions

- `uv lock --check`: resolved 170 packages successfully.
- `uv pip check`: checked 156 installed packages, all compatible.
- `uv run --extra dev ruff check .`: `All checks passed!`.
- Compared the lockfile's eight affected versions against every first-patched version in the
  live 55-alert snapshot and against installed distribution versions: all passed.
- All affected packages could be upgraded. FastMCP 4 is intentionally outside this security
  update; 3.4.10 fixes its reported alert without a major MCP migration. FastMCP's exact slim
  dependency is the only paired package update.
- Generated notices tests pass after every update. The existing metadata-based generator
  picks up patched versions and their bundled licenses; no notices or license source edits
  were required. Setuptools remains a development dependency through PyInstaller.
- No UI changes or new browser parity tests. All existing tests and assertions are unchanged.
- The complete suite was intentionally stopped by the owner and is deferred to one run after
  merge. This branch does not claim a successful full check or `Ready to push.`.
- The original checkout, its branch, files and stash were preserved. No pull request is opened.

## Outside this item

The owner reports three or four known Windows onboarding overflow failures at
`tests/test_onboarding_browser.py:201`, in
`test_no_overlap_no_wrapping_no_sideways_scroll`, to be fixed by the owner's integrator on the
base branch. The exact three or four parameter variants were not supplied, and this item's
interrupted check did not reach those cases, so no variants are claimed as observed here.
These failures are outside this item. No onboarding code or tests were changed. The owner
explicitly authorizes pushing this item with the scoped validation above.

## Integration handoff

Item 12 is complete for the owner's requested scoped validation. Each dependency step is saved
in its own local commit, followed by the final `feat: patch vulnerable dependencies` commit.
After merging, sync the merged lockfile with `uv sync --extra dev` and run the owner's single
full-suite check. Default-branch Dependabot alerts can be reassessed after that merge; no alert
was dismissed or manually marked fixed by this work.
