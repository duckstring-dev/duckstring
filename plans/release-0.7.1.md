# Release: Trickle 0.3.0 and Duckstring 0.7.1, then the pipeline engine post

Status: **prepared 2026-10-27, nothing pushed.** The `performance` branch carries the release; the post is on
`blog-duckdb-pipeline-engine` (branched from it). Every push, tag and PyPI approval below needs the author's
explicit go-ahead for this release.

## What ships

- **Trickle 0.3.0** (`packages/trickle`): faster incremental builder runs (lazy current states, one-pass joins
  against unchanged sides, the insert-only fast path, aggregate rescans only when an extreme may have gone, a
  windowed delta read once, NULL-safe key matching fixes). `Context.apply_zset` gains `insert_only`, and the
  builder passes it for an insert-only change.
- **Duckstring 0.7.1**: the fixes, memory slots and performance work in `CHANGELOG.md` "Unreleased (0.7.1)".
  Migration `033` (a nullable column, `ripple.memory_bytes`).

### Why Trickle is 0.3.0, not 0.2.2

Duckstring 0.7.0 requires `duckstring-trickle>=0.2.1,<0.3`, and its `Pond.apply_zset` has no `insert_only`
parameter. A Trickle 0.2.2 carrying the new builder would install under 0.7.0 and fail with a `TypeError` on the
first insert-only change (any append Trickle feeding a builder). As 0.3.0 it can't: 0.7.0 stays on 0.2.1, and
0.7.1 requires `>=0.3.0,<0.4`. Trickle's own standalone host takes `**kw`, so standalone users are unaffected.

## Order

1. **Review** (author): `CHANGELOG.md`'s entry, the post branch (post, figures, `bench/tpcds_chain`).
2. **PR `performance` into `main`.** Push the branch, open the PR, let CI run (all jobs, including the standalone
   `trickle` job, `duckdb-floor`, `postgres-egress` and `object-store`), merge. Checked locally: the full suite,
   `ruff check .`, and Trickle 0.3.0's suite from its wheel with Duckstring absent (191 passed).
3. **Trickle 0.3.0.** Tag `trickle-v0.3.0` on the merge commit; `release-trickle.yml` builds and waits on the
   `pypi` environment; approve. Verify `pip install duckstring-trickle==0.3.0` in a clean venv without Duckstring.
   Nothing picks it up yet: 0.7.0 is pinned below it.
4. **Duckstring 0.7.1rc1.** On `main`, set the version to `0.7.1rc1` (`pyproject.toml`, `uv lock`), tag
   `v0.7.1rc1`; `release.yml` publishes to PyPI (approve) and builds `ghcr.io/duckstring-dev/duckstring:0.7.1rc1`.
   Verify a clean `pip install duckstring==0.7.1rc1` resolves Trickle 0.3.0 and DuckDB 2.0 and serves the web UI.
5. **Smoke test the RC** as 0.7.0's RCs were (an EC2 Catchment with Fargate Ducks and an S3 data root),
   plus the two things this release changes most:
   - the read snapshots on a real S3 data root: a continuous Refresh over the Trickle demo for a few minutes, Sinks
     verified (the benchmark's continuous runs already did this on the Catchment's own machine);
   - memory slots on the Catchment's machine: `/api/status` shows `memory` per Pond, and a third Pond waits with
     `DUCKSTRING_RUN_SLOTS=2` (as `tests/test_memory_slots_runtime.py`, but from the published wheel).
   About $1 of AWS. An RC is worth it here: the last release's RC smoke tests found two bugs that only showed on
   fresh remote Ducks.
6. **Duckstring 0.7.1.** "Release 0.7.1" commit: version `0.7.1`, the changelog heading to `0.7.1` and the date,
   `uv lock`; tag `v0.7.1`; approve. Verify `pip install duckstring` gives 0.7.1 with Trickle 0.3.0.
7. **The post.** Rebase `blog-duckdb-pipeline-engine` on `main`; set the publish date (file name and front
   matter `date`); PR and merge (CI builds the docs). The author deploys the site (the Vercel fork sync). Then the
   share image (`media/duckdb-pipeline-engine-share.png` in the blog repo) for Reddit.
8. **After.** Delete the post and its images from the private blog repo (the public repo is the source from then
   on); keep `bench/tpcds_chain` only here.

## The post's claims that depend on this release

- Its details say the Duckstring runs were on 0.7.1. They ran on `performance` at `9e0235fb` with the harness at
  the blog repo's `e23e282`; 0.7.1 must ship that code (steps 2 to 6 release exactly `performance`).
- The 1 TB runs used six memory slots (`DUCKSTRING_RUN_SLOTS=6`); the default is four. The post says six.
- It links `github.com/duckstring-dev/duckstring/tree/main/bench/tpcds_chain`, which exists once step 7 merges.
