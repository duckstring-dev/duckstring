# Release: Duckstring 0.8.0

Status: **started 2026-11-11.** The `apps` branch carries the release (Apps, Azure and Google compute, Databricks
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
