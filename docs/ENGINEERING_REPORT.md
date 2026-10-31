# Engineering report — central multi-repository cleanup

## 1. How the system worked before the change

| Area | Previous state |
|---|---|
| Entry point | `.github/workflows/cleanup-old-runs.yml`, single repository, bound to `github.repository` |
| Credentials | `${{ github.token }}` was mapped to `GH_TOKEN`/`GITHUB_TOKEN`; the workflow requested `actions: write` + `contents: write` for the central token |
| Workflow runs | `cleanup_runs()` in `tool/branch_cleanup.py` deleted runs **older than 24 h** (`--cutoff-hours`), page by page, with no global ordering and no per-repository count |
| Runs pagination safety | a failed page only printed a warning and the loop continued, so deletions could be planned from a partial view |
| Runs dry-run reporting | printed `Deleting run ...` and incremented `deleted` even in dry-run |
| Branches | `branch_cleanup.py` used the `origin` remote of the **checked-out** repository, so it could only ever inspect the repository it ran in |
| Target list | none — no configuration, no per-repository isolation, no per-repository report |
| Tests | none in the repository; `tool/check_workflow_permissions.py` did not exist |
| Stale config | `sparse-checkout` exclusions for `/Mostmskat/` and `/local-data/` (from another project) |

Consequences: the system could not clean more than one repository, could not
express "keep the newest 20 runs", and had no automated proof of its safety
properties.

## 2. Files changed and why

| File | Change | Reason |
|---|---|---|
| `.github/workflows/cleanup-old-runs.yml` | rewritten | central entry point; scheduled = dry run always; manual `dry-run`/`live` with `confirm_live=DELETE`; workflow token reduced to `contents: read`; uses `secrets.CLEANUP_TOKEN` |
| `.github/workflows/ci.yml` | new | syntax, least-privilege and test gates for every push/PR |
| `config/repositories.txt` | new | target list, no code change needed to add/remove a repository |
| `tool/branch_cleanup.py` | **additive only** (+70 / −0) | `--json-report` output and a deprecation warning for the legacy time-based run cleanup. `evaluate_branch`, `evaluate_all_branches`, the evidence functions and the delete path are byte-identical |
| `tool/github_client.py` | new | token handling (env only), `gh api` transport with rate-limit retries, redaction of every outbound string |
| `tool/run_retention.py` | new | pure "keep newest N completed runs" policy + fail-closed paginated listing + deletion executor |
| `tool/repositories.py` | new | strict `owner/name` validation, comments/blank lines, duplicate removal, path confinement, selection that can only narrow the list |
| `tool/git_workspace.py` | new | isolated throw-away full clone per target repository; credential helper reads `$CLEANUP_TOKEN` from the environment |
| `tool/reports.py` | new | per-repository + aggregate JSON/Markdown reports and job summary |
| `tool/central_cleanup.py` | new | orchestrator: token preflight, per-repository phases, failure isolation, CLI safety gates |
| `tool/check_workflow_permissions.py` | new | least-privilege linter for all workflows (8 rules) |
| `tests/*` (8 files, 167 tests) | new | retention, branch algorithm, orchestration, config, token safety, workflow contract, simulated dry run |
| `README.md`, `docs/CENTRAL_CLEANUP.md`, `requirements-dev.txt`, `pytest.ini`, `.gitignore` | new | usage, architecture, dev dependencies |

## 3. How the system now manages several repositories

```
workflow (central)  ->  tool/central_cleanup.py
                          ├─ config/repositories.txt  (validated, confined to the repo)
                          ├─ per repository: token preflight (read-only probes)
                          ├─ per repository: runs retention   -> tool/run_retention.py
                          └─ per repository: branch cleanup  -> tool/branch_cleanup.py
                                                  inside an isolated full clone
                          └─ reports: diagnostics/reports/<owner>__<repo>.{json,md}
                                      diagnostics/summary.json, diagnostics/REPORT.md
```

* Each target repository is processed inside its own `try/except`: one failure
  never stops the others.
* Each target repository gets its own temporary full clone, so
  `git fetch origin`, `git merge-base --is-ancestor origin/...` and
  `git diff origin/...` operate exclusively on that repository's refs. The
  central repository's branches can never be observed. The clone's
  `remote.origin.url` is additionally verified against the requested
  `owner/name` before any analysis, so a mis-clone aborts instead of comparing
  unrelated refs.
* The target repositories contain **no** cleanup workflow and **no** Python.
* The target set comes only from `config/repositories.txt`; the workflow input
  can narrow it (`--repository owner/name`) but never widen it.
* Token: `CLEANUP_TOKEN` is read from the environment, passed to children as
  `GH_TOKEN` *after removing any inherited* `GH_TOKEN`/`GITHUB_TOKEN`, so the
  central `github.token` can never be used as a fallback. Git authenticates via
  a credential helper that reads `$CLEANUP_TOKEN` from the environment (no token
  in URLs, arguments, config files or logs).

## 4. How "keep the newest 20" is applied

Per repository, independently:

1. `GET /repos/{owner}/{repo}/actions/runs?per_page=100&page=N` is paginated
   until a short/empty page. No workflow filter → every workflow is covered.
2. The listing is marked **incomplete** (and deletion is skipped entirely) if a
   page fails, an entry cannot be parsed, or fewer runs were collected than the
   API's `total_count`.
3. Excluded from deletion: the current cleanup run (only for the central
   repository), every non-`completed` run, and every run whose id/status/
   `created_at` cannot be parsed.
4. Remaining completed runs are sorted by `created_at` descending (tie-break:
   run id descending — a total order); the newest `keep_latest` (default 20) are
   kept, the rest are deletion candidates.
5. Counters are per repository; the `20` is never applied to a mixed pool.

There is no time-based cutoff anywhere in the active path.

## 5. Branch deletion logic is unchanged

`git diff --stat` for `tool/branch_cleanup.py`: **70 insertions, 0 deletions**.

The only additions are the `--json-report` argument, its writer, and a
deprecation warning printed by the legacy `cleanup_runs()`. Specifically
untouched: `evaluate_branch()` (decision order), `evaluate_all_branches()`,
`fetch_default_branch()`, `list_remote_branches_api()`,
`list_remote_branches_git_fallback()`, `list_open_pr_branches()`,
`list_closed_merged_prs()`, `check_protected_branches()`, `is_ancestor()`,
`diff_empty()`, `merge_commit_is_ancestor()`, `delete_branch_remote()` (API-only
deletion, no `git push` fallback), the final protection re-check, the
pre-delete re-verification, and the "keep on any doubt" defaults.

All protected rules are exercised by tests: default branch detection via API,
protected branches, open PRs, real merge proofs (`merge-base --is-ancestor`,
`git diff`), squash/rebase paths, unverifiable evidence → keep, branch moved
during the scan → keep, deletion via API only.

## 6. Retention test results

`tests/test_run_retention.py` — 30 tests, all passing. Covered:

* 100 completed runs → keep 20, delete 80;
* exactly 20 → delete 0; fewer than 20 → delete 0;
* in-progress/queued/waiting/pending/requested runs never deleted;
* current run never deleted and does not consume a retention slot;
* >100 results → pagination (250 runs over 3 pages, all considered);
* `total_count` mismatch, malformed entry, failed page → **no deletion**;
* per-repository independence (75/32/8 → 55/12/0 deletions);
* count-based, not age-based (30 runs older than a year still keep the newest 20);
* dry run performs no DELETE call; live run deletes exactly the plan;
* 403 on delete → repository aborted with a clear permission error.

## 7. Branch test results

`tests/test_branch_algorithm.py` — 23 tests. 16 pure decision tests plus 7
end-to-end tests that run `branch_cleanup.main()` against **real local git
repositories** with the GitHub API replaced by an in-process router:

* default branch, protected (flag and by name), open PR → keep;
* real merge commit → delete;
* unmerged branch with unique changes → keep;
* squash merge without merge-commit ancestry proof → keep (conservative, unchanged behaviour);
* merged PR with an ancestor merge commit → delete;
* branch listed by the API but unresolvable in git → keep;
* branch moved between evaluation and deletion → keep;
* protection endpoint 403 → keep;
* API delete failure → reported, no `git push` fallback;
* dry run issues no DELETE call;
* multi-repository runs produce identical decisions to standalone runs.

## 8. Permissions and security checks

* Token preflight per repository (read-only probes): `Metadata: Read`,
  `Contents: Read and write` (from the repository `permissions` object),
  `Pull requests: Read`, `Actions: Read`. Missing capabilities skip that phase
  and are reported per repository; they are never bypassed.
* `Actions: write` cannot be probed without deleting: it is reported as
  `unverified` and enforced at the first deletion — a 403 aborts that
  repository with an explicit "Actions: Read and write" message.
* The central workflow token is `contents: read` only; `actions: write` and
  `contents: write` were removed.
* `tool/check_workflow_permissions.py` enforces 8 least-privilege rules
  (explicit `permissions`, no `write-all`, valid scopes/values, write scopes
  require a default-branch guard, scheduled workflows read-only,
  `CLEANUP_TOKEN` workflows must not give the workflow token write scopes).
  Both workflows pass.
* Token never printed: `redact()` covers the literal token, base64 forms and
  GitHub token patterns; all logs/report writes pass through it. Tests assert
  no token appears in any report file or in `gh` error output.
* Config path is confined to the repository root; repository names are strictly
  validated, so a name can never be interpreted as a git option or URL.
* Clone origin verification (see §3) prevents cross-repository analysis.
* `git` runs with `GIT_TERMINAL_PROMPT=0`, `GCM_INTERACTIVE=never` and no
  interactive credential prompt; a failed clone is reported, never retried with
  another identity.

## 9. GitHub Actions status

* Commit `c77464c` (full change set) — **CI: success**. Job
  `Tests, syntax and workflow permissions` ran every gate green: dependency
  install, `compileall`, workflow least-privilege linter, YAML syntax, the full
  pytest suite and the mocked dry-run simulation.
* The `Central Cleanup` workflow is triggered by `schedule` and
  `workflow_dispatch`; both are confined to the default branch
  (`searchidf`) by the job guard, so it is intentionally *not* executed from
  this feature branch and no pull request can reach the cleanup token.
* The workflow definition itself was validated locally
  (`tool/check_workflow_permissions.py`, YAML parse, contract tests) and the
  identical command line was executed as a real dry run against
  `kinanmjeed88/cleanup-old-runs.yml-`: 4 runs examined / 4 retained,
  2 branches examined / 2 kept (`searchidf` = default branch, the unmerged
  feature branch kept), **0 deletions**, exit code 0, no token in any report.

### Post-merge fix (found during the first real dry run)

The first execution after the merge reported `branch cleanup produced no JSON
report`. Root cause: `--reports-dir diagnostics` was relative, and the branch
cleanup child process runs inside the throw-away clone, so the report was
written into the clone and deleted with it. Fix: the reports directory and the
per-repository branch report path are resolved to absolute paths before the
child is spawned (plus a regression test that runs a real child process from a
different working directory). After the fix the dry run is `ok` with
`branches_examined=2, kept=2`.

## 10. Known limitations

1. `Actions: write` cannot be verified non-destructively; a dry run reports it
   as `unverified` and a live run fails closed at the first deletion.
2. Squash/rebase-merged branches are kept unless an ancestor merge commit or an
   empty diff proves the merge. This is the **existing** algorithm behaviour and
   was deliberately not changed (safety-first requirement).
3. A repository with more than 20 000 runs exceeds the pagination guard
   (`MAX_PAGES = 200`); deletion is aborted rather than guessed.
4. The legacy `cleanup_runs()` (24 h cutoff) still exists for CLI
   compatibility and prints a deprecation warning; the central system never
   calls it (`--skip-runs` is always passed).
5. Each target repository is fully cloned for the branch proof, so runtime
   grows with repository size; the job timeout is 90 minutes.
6. The `Central Cleanup` workflow only runs from the default branch (by design).
   Therefore the first scheduled/manual clean execution happens after this
   branch is reviewed and merged into `searchidf`; it cannot be dispatched from
   a feature branch, and the sandbox credentials used during development do not
   carry `actions: write` to force a dispatch.
