# Release: Duckstring 0.8.0

Status: **released 2026-11-11.** The `apps` branch carries the release (Apps, Azure and Google compute, Databricks
and BigQuery, the Npgsql wire, Inlet stamping). No Trickle release: since `trickle-v0.3.1` it has only a docstring
change, so 0.8.0 keeps `duckstring-trickle>=0.3.1,<0.4`. The author gave the go-ahead for this release's pushes,
tags and PyPI approvals on 2026-11-11.

## What ships

`CHANGELOG.md` "Unreleased (0.8.0)". Migrations `034` (`app_instance`, `pond_version.app`) and `035`
(`pond_tap.key_hash` nullable). First release to build the `-azure` and `-gcp` images (`release.yml`'s matrix).

## Order

1. PR `apps` into `main`, full CI, merge. PR #48 (`azure-gcs-storage`) closed as superseded.
2. `0.8.0rc1`: version in `pyproject.toml`, `uv lock`, tag `v0.8.0rc1`, approve the `pypi` environment.
3. Smoke tests (below). Fix on a branch, rc2, re-run only the tiers a fix touches.
4. "Release 0.8.0": version, changelog heading and date, `uv lock`, tag `v0.8.0`, approve, verify. The author
   deploys the Playground, then the docs.

## Smoke tests

The full runs on each cloud passed on 2026-11-07 and 2026-11-08 with branch builds. The RC tests aim at what only a
release exercises (published images and the default `pip_spec`), plus the AWS regression.

- **A. Package and images (local).** Clean venvs on 3.10 (DuckDB 1.5) and 3.13 (2.0): install, web UI served,
  extras `azure`/`gcp`/`databricks` resolve. Anonymous pulls of `:{rc}`, `-aws`, `-azure`, `-gcp` on amd64 and
  arm64; each image's DuckDB extensions load offline. Upgrade in place: a 0.7.1 Catchment with the Trickle demo,
  upgraded to the RC, restarted (migrations apply, pipeline still runs). From the wheel: a small App (start on
  request, idle stop, a keyless Tap), the Postgres wire with `psql`/pg8000, `--header-command`.
- **B. AWS regression.** `reference_aws_smoke_test` (EC2 Catchment, Fargate Ducks on S, S3 root, four rounds,
  queries, hard kill and orphan exit), updated for `/refresh` and `POND.TABLE`, plus an App on the Catchment,
  `refresh now --fresh-within` stopping short of Inlets, and Fargate pulling the bare-version tag.
- **C. Azure, default image and `pip_spec`.** Container Apps Ducks on `ghcr.io/...:{rc}-azure` (not ACR), and an
  `azurevm` Pool with the default `pip_spec`, on the `abfss://` test root. Teardown deletes the VMs; watch
  `release`'s stop-then-delete watcher (never seen live).
- **D. Google, default image path and `pip_spec`.** Cloud Run Ducks pulling `-gcp` through an Artifact Registry
  remote repository proxying ghcr.io (`DUCKSTRING_CLOUDRUN_REGISTRY`, never run), and a `gce` Pool with the default
  `pip_spec`, on the `gs://` test root.

Not covered: Power BI itself (Windows), Lakebase (deleted), the Databricks App with the released wheel (deferred
by the author's choice of scope), the Flock conformance suites (unchanged since measured).

## Progress

- 2026-11-11: PR #49 merged; CI found two bugs, both fixed before the RC: a never-published line's
  `retain_from` was None (keep only the newest), so overlapping first Runs deleted a version a Sink was reading;
  DuckDB 1.5's Delta reader can't read a version (Databricks tables go through the warehouse there).
  0.8.0rc1 published (`v0.8.0rc1`, run 42994632925).
- 0.8.0rc1 smoke. **A** passed: clean installs (3.10 → DuckDB 1.5.6, 3.13 → 2.0.0, Trickle 0.3.1, UI present), all
  four image tags anonymous on amd64/arm64, an upgrade in place from 0.7.1 (migrations to 35; Refresh, Tap,
  window and Spout kept; rounds and query clean), the App (started on request, keyless Tap refreshed `reports`,
  stopped when idle), the Postgres wire (pg8000, parameters, a 34-digit numeric), `--header-command` (both
  clouds). **C** (Azure) passed: Container Apps Ducks on the default `-azure` image, an `azurevm` Pool VM on the
  default `pip_spec`, three rounds; the dedicated-VM stop-then-delete watcher seen live for the first time (VM
  deleted about a minute after it stopped, with its disk, NIC and IP); deleting the Catchment deleted the Pool VM.
  **D** (Google) passed: the Catchment and Cloud Run Ducks on `-gcp` through the Artifact Registry remote
  repository, a `gce` Pool on the default `pip_spec`, the service's deletion deleted the VM. Its first round failed
  with a 403 on the Duck's artifact fetch: `run.invoker` granted under two minutes earlier, not yet propagated;
  rounds after a Clear passed. **B** (AWS) found a regression: a fresh Fargate Duck's `sales.price_tiers` failed
  in `duckdb_setup` ("failed to configure DuckDB S3 credentials"), from the per-Ripple secret rewrite added for
  Google and Azure tokens (`3aad674a`), run before every Ripple from concurrent pool threads on the one registry
  connection. Fixed: rewritten only when the credential generation changes, under a lock. Rounds 3 and 4, the App
  and the continuous check were blocked behind the failed `sales`; `--fresh-within` passed.
- Follow-ups (not blocking): after a Ripple fails, the Duck's watchdog reports the Run "stuck" 30 s later and
  that message replaces the Ripple's error on the Pond; a read of a merge main as of NEVER warns and reads the
  newest base (from 0.7.1's read snapshots); a pool VM's serial console shows the Duck token in the agent's
  command line.
- 2026-11-11: PR #50 merged; 0.8.0rc2 published (run 43003901556). AWS smoke re-run (run `20261111T104255Z`) passed:
  four rounds on fresh Fargate Ducks (the bare-version image), `--fresh-within` ran nothing already fresh, the App
  on the Catchment (started on request, its keyless Tap refreshed `reports` on S3, stopped when idle), two minutes
  of a continuous Refresh (every Pond 14 to 19 runs, nothing failed), both queries, and orphaned Ducks stopped
  3.4 minutes after the hard kill. The Azure and Google infrastructure recreated for C and D was deleted after.
  0.8.0 released.
