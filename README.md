# Central GitHub cleanup

One central workflow cleans **many** GitHub repositories. It does not use a
hand-written repository list: every run **discovers** the repositories the
cleanup token can manage, reports them, and only then starts working.

* **discovers** `/user/repos` plus every account in `config/accounts.txt`
  (organisation and user accounts), with full pagination and de-duplication;
* keeps the newest **20 completed GitHub Actions runs per repository** and
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
   that difference instead of pretending the account is empty). Write access on
   a repository is proven per repository before anything is deleted:
   **Actions: Read and write**, **Contents: Read and write**, **Pull requests:
   Read**.
3. Run the workflow manually with `mode = dry-run` and review the discovery list
   and the per-repository reports.
4. Only then run `mode = live` with `confirm_live = DELETE`.

Scheduled runs (02:00 UTC daily) are **always** a dry run.

## Safety model

| Guard | Effect |
|---|---|
| `schedule` trigger | forces `dry-run`; live mode is rejected in both the workflow and the Python CLI |
| `mode=live` | requires `confirm_live=DELETE`, otherwise the run aborts before any API call |
| read-only workflow token | deletions are performed with `CLEANUP_TOKEN` only; the `github.token` never has write scopes |
| default-branch guard | the cleanup job cannot run from a pull request or feature ref |
| incomplete run listing | if any page fails, `total_count` disagrees, or an entry is unparsable, **nothing** is deleted for that repository |
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
keep  -> first 20
delete -> the rest
```

Example: repository A with 75 runs keeps 20 and deletes 55; B with 32 keeps 20
and deletes 12; C with 8 keeps all 8.

## Reports

`diagnostics/` (uploaded as a workflow artifact, also appended to the job
summary):

* `summary.json` / `REPORT.md` — the discovery table plus an aggregate report;
* `reports/<owner>__<repo>.json|.md` — per-repository report with counters,
  token-preflight results, failures, and the reason for every branch decision.

## Local usage

```bash
export CLEANUP_TOKEN=...            # never commit this
python3 tool/central_cleanup.py --discover-only            # discovery test only
python3 tool/central_cleanup.py --mode dry-run             # every discovered repo
python3 tool/central_cleanup.py --mode dry-run --repository owner/name
python3 tool/central_cleanup.py --mode live --confirm-live DELETE   # real deletion
```

Exit codes: `0` success, `1` at least one repository failed, `2` invalid
input/configuration.

## Development

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest -q
python3 tool/check_workflow_permissions.py .github/workflows
```

The test suite (200 tests) never touches the network and never deletes
anything: the GitHub API is replaced by in-memory fakes and the branch
algorithm is exercised against local git repositories.

See [`docs/CENTRAL_CLEANUP.md`](docs/CENTRAL_CLEANUP.md) for the architecture,
the diagnosis of the previous single-repository design, and the exact list of
changes.
