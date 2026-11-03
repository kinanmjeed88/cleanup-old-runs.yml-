# Central GitHub cleanup

One central workflow cleans **many** GitHub repositories. It does not use a
hand-written repository list: every run **discovers** the repositories the
cleanup token can manage, reports them, and only then starts working.

* **discovers** `/user/repos` plus every account in `config/accounts.txt`
  (organisation and user accounts), with full pagination and de-duplication;
* keeps the newest **50 completed GitHub Actions runs per repository** and
  deletes the older ones (count based, every workflow, paginated, safe);
* deletes **merged** branches using the project's existing proof-of-merge
  algorithm — target repositories need no workflow or Python files of their own.

All privileged access uses the repository secret **`CLEANUP_TOKEN`**. Only an
environment variable of the same name is read; the value is never written to
disk, embedded in a URL, printed, or stored in a report.

## Quick start

1. List the accounts whose repositories should be cleaned in
   [`config/accounts.txt`](config/accounts.txt) (one login per line; `org:` /
   `user:` prefixes are optional). No repository list and no code change are
   needed — repositories are discovered on every run.
2. Make sure `CLEANUP_TOKEN` exists as a repository secret with **Metadata:
   Read** and is allowed to *all repositories* of those accounts (a fine-grained
   token limited to "selected repositories" hides the others; the run reports
   that difference instead of pretending the account is empty). Configure
   **Actions: Read and write**, **Contents: Read and write**, **Pull requests:
   Read** and **Metadata: Read**. Repository read probes check coarse write
   access and Actions read; they cannot prove the Actions write grant. Confirm
   that grant in the token settings; the first live deletion enforces it.
3. Run the workflow manually with `mode = dry-run` and review the discovery list
   and the per-repository reports.
4. Only then run `mode = live` with `confirm_live = DELETE`.

Scheduled runs (02:00 UTC daily) are **always** a dry run.

### Manual LIVE (only after reviewing a fresh dry run)

In **Actions → Central Cleanup → Run workflow**, select the default branch and:

| Input | Value for a runs-only cleanup of one repository |
|---|---|
| `mode` | `live` |
| `confirm_live` | `DELETE` (exact, uppercase) |
| `repository` | the discovered `owner/name`, e.g. `kinanmjeed88/Scan-id` |
| `keep_latest` | `50` |
| `discovery_mode` | `auto` |
| `discovery_only` | `false` |
| `skip_branches` | `true` |

`live` is passed to the Python CLI as `--mode live --confirm-live DELETE`.
Without the exact confirmation the workflow stops before Python starts.
Leaving `skip_branches=false` also enables the existing merged-branch cleanup;
set it to `true` when you only authorised workflow-run deletion.

## Safety model

| Guard | Effect |
|---|---|
| `schedule` trigger | forces `dry-run`; live mode is rejected in both the workflow and the Python CLI |
| `mode=live` | requires `confirm_live=DELETE`, otherwise the run aborts before any API call |
| read-only workflow token | deletions are performed with `CLEANUP_TOKEN` only; the `github.token` never has write scopes |
| default-branch guard | the cleanup job cannot run from a pull request or feature ref |
| incomplete run listing | if any page fails, `total_count` changes or disagrees, ids duplicate, or an entry is unparsable, **nothing** is deleted for that repository |
| deletion order | candidates are deleted oldest-first, so a mid-run stop leaves the newest runs intact |
| already-deleted runs | HTTP 404 on a candidate is recorded as already absent, not as a failure |
| stop-on-risk errors | HTTP 401/403/429/5xx or a transport failure stops that repository's deletions; 422 continues |
| post-execution verification | after a live mutation the repository is re-listed; resolved ids must be gone and protected ids must remain, otherwise the report fails |
| conservative live gate | live mutation requires an observed repository write indicator; `Actions: Write` is reported as **unverified** until the API itself proves it |
| active runs | queued / in progress / waiting / pending / requested runs are never deleted |
| current run | the run executing the cleanup is always excluded |
| branch deletion | unchanged `evaluate_branch` decision order; anything that cannot be *proved* merged is kept |
| isolated git | each repository is cloned to its own temporary full clone; central refs are never visible |
| failure isolation | one inaccessible or failing repository never stops the others |
| discovery first | the discovered repository list is printed and written to `diagnostics/summary.json` before any repository is touched |
| no invented targets | `--repository` accepts only a repository that discovery actually returned |
| incomplete discovery | live deletion is refused when a discovery source failed (a partial list could hide repositories) |
| read-only repositories | analysed and reported as `SKIPPED`, never mutated; the branch phase is not even attempted |
| archived repositories | always `SKIPPED` — nothing is deleted there |
| central repository | excluded from branch cleanup, its runs are still cleaned |

## Retention rule

Per repository, independently:

```
sort(completed runs, by created_at desc, tie-break run id desc)
keep  -> first 50
delete -> the rest
```

Example: repository A with 75 completed runs keeps 50 and deletes 25; B with 52
keeps 50 and deletes 2; C with 8 keeps all 8. Active/current/unverifiable runs
are excluded before the count is applied. The workflow input, the shell
fallback, the Python CLI and the retention policy all default to **50**; an
explicit `keep_latest` value still overrides the default.

## Reports

`diagnostics/` (uploaded as a workflow artifact, also appended to the job
summary):

* `summary.json` / `REPORT.md` — the discovery table plus an aggregate report;
* `reports/<owner>__<repo>.json|.md` — per-repository report with counters,
  token-preflight results, failures, verification state, remaining counts, and
  the reason for every branch decision.

Per-repository statuses: `DISCOVERED`, `DRY_RUN_SUCCESS`, `LIVE_SUCCESS`,
`PARTIAL_FAILURE`, `FAILED`, `SKIPPED`. `PARTIAL_FAILURE` and `FAILED` make the
process exit `1`; a dry run never mutates anything.

## Local usage

```bash
export CLEANUP_TOKEN=...            # never commit this
python3 tool/central_cleanup.py --discover-only            # discovery test only
python3 tool/central_cleanup.py --mode dry-run             # every discovered repo
python3 tool/central_cleanup.py --mode dry-run --repository owner/name --keep-latest 50
# After approval, runs-only live cleanup of one repository:
python3 tool/central_cleanup.py --mode live --confirm-live DELETE --repository owner/name --keep-latest 50 --skip-branches
```

Exit codes: `0` success, `1` at least one repository failed or partially failed,
`2` invalid input/configuration.

## Development

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest -q
python3 tool/check_workflow_permissions.py .github/workflows
python3 -m bandit -r tool -ll -q
```

The test suite never touches the network and never deletes anything real:
the GitHub API is replaced by in-memory fakes and the branch algorithm is
exercised against local git repositories. `tests/test_workflow_execution.py`
executes the workflow's Bash steps and the real Python CLI with a fake GitHub
transport, including manual live deletion and a first-delete HTTP 403.

See [`docs/CENTRAL_CLEANUP.md`](docs/CENTRAL_CLEANUP.md) for the architecture
and [`docs/AUDIT_2026-11-03_AR.md`](docs/AUDIT_2026-11-03_AR.md) for the latest
Arabic audit, execution trace, permission limitations and test results.
