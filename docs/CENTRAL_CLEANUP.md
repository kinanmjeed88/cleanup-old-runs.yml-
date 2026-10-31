# Central Cleanup System

One central repository that cleans **many** GitHub repositories using a dedicated
secret (`CLEANUP_TOKEN`). Target repositories never need their own workflow or
Python files.

## Diagnosis of the previous (single-repository) design

| # | Finding | Consequence |
|---|---------|---------|
| 1 | `.github/workflows/cleanup-old-runs.yml` was bound to `github.repository` and used `${{ github.token }}` | Could only ever clean the repository that hosted the workflow |
| 2 | `tool/branch_cleanup.py` resolved branches through the `origin` remote of the *checked-out* repository (`git fetch origin`, `git merge-base origin/...`) | Running it inside the central repository would compare central branches with target branches — the exact cross-contamination this task forbids |
| 3 | `cleanup_runs()` deleted by **age** (`--cutoff-hours 24`), page by page, with no global ordering and no per-repository count | The required retention rule is "newest 20 completed runs", which is impossible with a time cutoff |
| 4 | `cleanup_runs()` printed `Deleting run ...` and incremented `deleted` even in dry-run mode | Dry-run output was misleading |
| 5 | A failed runs page (`gh api` non-zero) only printed a warning and the loop continued | Deletion could proceed on a partially known run list — unsafe |
| 6 | No repository list, no per-repository isolation, no per-repository report | No way to drive multiple repositories or to audit a run |
| 7 | Workflow requested `actions: write` + `contents: write` for `github.token` | Deletion capability was granted to the central token instead of the dedicated one |
| 8 | No tests, no `tool/check_workflow_permissions.py`, no config directory | Nothing pinned the safety invariants |
| 9 | Stale `sparse-checkout` exclusions (`/Mostmskat/`, `/local-data/`) from another project | Dead configuration |

## Architecture after the change

```
.github/workflows/cleanup-old-runs.yml   central entry point (schedule = dry-run, dispatch = dry-run|live)
.github/workflows/ci.yml                 test + syntax + workflow-permission gates
config/accounts.txt                      accounts to discover (one login per line)
config/repositories.txt                  optional list, used only by discovery mode `list`/`union`
tool/branch_cleanup.py                   branch deletion algorithm — UNCHANGED decision logic
tool/github_client.py                    token handling + `gh api` transport + redaction
tool/run_retention.py                    "keep newest 20 completed runs" pure policy + executor
tool/discovery.py                        repository discovery (pagination, de-duplication)
tool/repositories.py                     config parsing / validation / selection
tool/git_workspace.py                    isolated full clone per target repository
tool/reports.py                          per-repository + aggregate reports
tool/central_cleanup.py                  orchestrator (multi-repository driver)
tool/check_workflow_permissions.py       workflow least-privilege linter
tests/                                   unit + integration tests (no network, no deletion)
```

### Repository discovery (no hand-written list)

Every execution starts with discovery; nothing is processed before the list has
been printed and written to `diagnostics/summary.json`.

| Source | Endpoint | Purpose |
|---|---|---|
| authenticated user | `GET /user/repos?affiliation=owner,collaborator,organization_member&visibility=all` | every repository the token can see, public and private |
| configured account (organisation) | `GET /orgs/{login}/repos?type=all` | repositories of an organisation |
| configured account (user) | `GET /users/{login}/repos?type=owner` | fallback when `/orgs/...` answers 404 (e.g. a personal account) |
| scoped-token diagnostic | `GET /users/{authenticated}/repos` | compares *visible* with *token-visible* repositories and warns when a fine-grained token is limited to selected repositories |

* All sources are paginated to the end (`per_page=100`); a short page ends a
  source, a failing page marks it incomplete and is reported.
* Entries are keyed by `full_name` (case-insensitive) and merged, so a
  repository found by several sources appears once with every source recorded.
* Each entry keeps `full_name`, owner login, owner type, default branch,
  visibility, archived/fork/disabled flags, token permissions, and its sources.
* An account that is neither a readable organisation nor a readable user
  (403/404 on both) is reported as a *skipped source*, not as an empty account.
* `DiscoveryResult.complete` is false when any source failed; live deletion is
  refused in that case, because a partial list could silently hide repositories.

### Per-repository statuses

| Status | Meaning |
|---|---|
| `DISCOVERED` | in the discovery list (before processing) |
| `ACCESSIBLE` | token verified, but no phase was executed |
| `DRY RUN` | retention and branch analysis executed; nothing deleted |
| `LIVE` | deletions executed (only with `--mode live --confirm-live DELETE`) |
| `SKIPPED` | deliberately not modified: archived, read-only token, or no access — the reason is recorded |
| `FAILED` | at least one operation or verification failed for that repository |

### Retaining the newest 20 completed workflow runs

1. `GET /repos/{owner}/{repo}/actions/runs?per_page=100&page=N` is paginated
   until an incomplete/empty page. No workflow filter is applied, so **every
   workflow** in the repository is covered.
2. If a page fails, the reported `total_count` is larger than what was collected,
   or an entry cannot be parsed, the repository is marked **incomplete** and the
   retention plan becomes "delete nothing".
3. Runs are classified: current cleanup run → excluded; status not `completed`
   (queued / in progress / waiting / pending / requested) → excluded; unknown
   status or unparseable `created_at` / `id` → excluded (never deleted).
4. The remaining completed runs are sorted by `created_at` descending
   (tie-break: run id descending). The newest `keep_latest` (default **20**) are
   retained; everything older is eligible for deletion.
5. Ordering is computed **per repository**, never across repositories.

### Branch cleanup reuse

`tool/branch_cleanup.py` is executed as a subprocess with its working directory
set to a temporary **full clone of the target repository** created by
`tool/git_workspace.py`. This keeps `git fetch origin`, `git merge-base
--is-ancestor origin/<branch> origin/<default>` and `git diff origin/<default>...origin/<branch>`
semantically identical to the original single-repository execution, while
guaranteeing that central-repository refs can never be observed. The only change
in that file is an additive `--json-report PATH` flag (plus its writer) so the
orchestrator can report the reason for every branch decision.

### Token handling

* The token is read from the `CLEANUP_TOKEN` environment variable at call time.
* It is never written to disk, never embedded in a git remote URL, never passed
  as a command-line argument and never printed. Git authenticates through a
  credential helper that reads `$CLEANUP_TOKEN` from the environment.
* Child processes get `GH_TOKEN=$CLEANUP_TOKEN`; inherited `GH_TOKEN` /
  `GITHUB_TOKEN` are removed so the central `github.token` can never be used as
  a fallback.
* Every string that reaches a log, exception or report passes through
  `redact()`.

### Protections applied to discovered repositories

* the **central repository** is excluded from branch cleanup (its runs are still
  cleaned) — `--central-repository`, defaulting to `$GITHUB_REPOSITORY`;
* **archived** repositories are skipped entirely;
* repositories where the token has no write access are analysed and reported as
  `SKIPPED`, never mutated, and their branch phase is not executed at all;
* a different owner is never a reason to skip: accessibility decides;
* a repository is never deleted, and no file inside it is ever written — the
  branch phase only issues `DELETE /repos/{repo}/git/refs/heads/{branch}` from
  inside a throw-away clone;
* `--repository` only accepts a repository that discovery returned.

### Running

```bash
# Discovery test: print and write the discovered list, clean nothing
python3 tool/central_cleanup.py --discover-only

# Preview every discovered repository (always the default and the only
# scheduled mode)
python3 tool/central_cleanup.py --mode dry-run

# Preview a single discovered repository
python3 tool/central_cleanup.py --mode dry-run --repository kinanmjeed88/Scan-id

# Restrict to the configured list, or extend discovery with it
python3 tool/central_cleanup.py --discovery-mode list|union

# Live deletion — requires an explicit confirmation token
python3 tool/central_cleanup.py --mode live --confirm-live DELETE
```

Exit codes: `0` all repositories processed, `1` at least one repository failed
or a phase could not be completed, `2` configuration/input error.
