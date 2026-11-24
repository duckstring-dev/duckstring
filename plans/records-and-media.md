# Records and Media: Catchment-held App data

Status: **built 2026-11-24** on branch `records-media` for 0.9.0 (see "As built"). Settled with the author
2026-11-23/24. Supersedes the paragraph in
`plans/apps.md` "State" that says an App's data reaches a pipeline only through an API an Inlet reads.

## Why

Duckstring is to become a data app factory: most teams run one Catchment with many Apps deployed to it.
Apps need durable data that outlives an instance, can be shared by several instances, can be read by
Ponds (so App writes feed a pipeline whose output updates the App, usually through the App's own Tap), and
can be explored like Pond output. Today an App has only a private directory (`[app] state = "local"`) that
nothing else can see.

## Concepts

- **Records**: an App line's relational data, held in Postgres. Postgres only (the author's call): the
  engine is visible in App SQL, so starting on SQLite and scaling later means a rewrite. An App wanting
  SQLite keeps it in its private state directory, as today; Ponds and the viewer never see that.
- **Media**: an App line's files (uploads, generated images, documents). One directory or object-store
  prefix per App line.
- Exactly one Records and one Media per Catchment, each split per App line. Separation between Apps is a
  namespace (and in Postgres a role), so an App is only ever handed its own. It stops accidents; it does not
  sandbox a hostile App, which already runs code on the Catchment's machine.
- Both are split by **major** (`name@major`). A major bump is how an App declares a breaking Records change.
- An App opts in: `[app] records = true`, `[app] media = true`. Without them nothing is provisioned.
- `[app] state = "local" | "none"` is unchanged (the App's private directory).

## Catchment configuration

- `records`: `managed` (default: a Postgres Duckstring runs on the Catchment's machine) or a
  `postgres://` URI (RDS, Lakebase, any Postgres; credentials as `${secret:}`/`${env:}`, as for Spouts). The
  external credential needs CREATEROLE and CREATE on the database.
- `media`: a local directory under the Catchment root (default) or `s3://`, `gs://`, `az://`/`abfss://`,
  `/Volumes/...`, through the data root's storage layer (`storage.py`), not the Spout drivers (Media needs
  read, list and delete).
- Neither setting changes after first use (as `data_root` can't), except through a move that copies
  everything across (`duckstring catchment records move URI`, `... media move URI`: dump and restore every
  schema, copy every file, then switch), so nothing is stranded.
- Hosts that serve the Catchment at one HTTP address (Posit Connect, Databricks Apps, Cloud Run) can't
  expose a Postgres port: there, Records is external if anything off the Catchment's machine needs it.

## Managed Postgres

- Started when the first App line with `records = true` needs it (an instance starting, a Pond reading,
  the viewer); kept running while the Catchment runs. Supervised like an App instance (own process group,
  log under the Catchment root, restart counted, failure alerts).
- Memory: a Catchment setting (default 512 MB) set aside while it runs, as `Driver.app_reserve` does for
  an App, so the memory slots divide what is left; refused past the same share an App may take. Postgres
  settings derive from it: `shared_buffers` about a quarter, modest `max_connections`, small `work_mem`. The
  reservation is an estimate, as an App's declared memory is.
- Binaries: the operating system's official PostgreSQL packages (the author's call, 2026-11-24): PGDG apt or
  yum on Linux, Homebrew `postgresql@18` or Postgres.app on macOS, EDB's installer on Windows. Installed by
  `duckstring install postgres` (see below). The Catchment uses the recorded `pg_ctl` (or
  `DUCKSTRING_PG_BIN`, else the PATH) and, when none is found, says to run that command. The Postgres major
  is pinned per Catchment and recorded at `initdb`.
- Considered and rejected: a pip wheel of binaries. `postgresql-binaries` (leontrolski) repackages
  theseus-rs/postgresql-binaries' builds, so two third parties sit between the PostgreSQL source and PyPI.
  It worked (measured 2026-11-24 on macOS arm64, 18.6.0: `initdb`, start, an App role writing 100k rows into
  its own schema, `pg_dump -n`, a DuckDB read-only `ATTACH ... (TYPE postgres)`; idle RSS about 51 MB over 9
  processes, an overestimate; a fresh data directory 60 MB). `pgserver` (last release 2024-06) and
  `embedded-postgres` (4 releases) likewise. An own wheel built from source was also rejected (the author's
  call, 2026-11-24): it would make Duckstring answer for every PostgreSQL minor and OpenSSL fix.
- A Unix socket path is limited to 103 bytes, which a Catchment root can exceed (measured): connect over
  localhost TCP, or use a short socket directory.
- Backups: `pg_dump` into `catchment download` and the state backup (`checkpoint_full`). Postgres major
  upgrades by dump and restore (sizes are small).
- Network: binds localhost unless something off the machine needs it (remote App instances or Ducks),
  then the Catchment's address with TLS. The auto-relay would need to forward it too.

## `duckstring install`

A top-level command for machine setup a pip install can't do (agreed 2026-11-24): `duckstring install
COMPONENT... [--yes] [--dry-run]`.

- Installs only from official sources and never downloads a binary itself. It prints the commands it will
  run and runs them after a yes (or at once with `--yes`; `--dry-run` only prints). It never calls sudo: on
  Linux run it as root or under sudo; Homebrew needs neither.
- With no component, it lists every component with its state (installed and where, missing, not needed):
  a setup check.
- Components:
  - `postgres [--version 18]`: a `pg_ctl` of that major already present is only recorded; otherwise PGDG
    apt (signing key fingerprint pinned) on Debian/Ubuntu, PGDG dnf on the RHEL family, `brew install
    postgresql@18` on macOS; on Windows it prints the EDB installer link. Records the path in the CLI's
    config.
  - `extensions [NAME...]`: DuckDB's extensions fetched into the user's extension directory, for machines
    without internet access. Takes over `docker/duckdb_extensions.py`.
  - `aws`, `azure`, `gcp`, `dbt`, `lineage`: the extra installed into the environment the CLI runs from,
    pinned to the running Duckstring (`uv pip install 'duckstring[aws]==X.Y.Z'`). Missing-extra errors name
    this command instead of a pip line.
- The Dockerfile and VM userdata call `duckstring install postgres extensions --yes` in place of their own
  steps, so the image, a VM and a laptop set up the same way.
- `install` over `setup`: `setup` reads as configuring a Catchment (`catchment init`'s job); Ponds are
  deployed, never installed, so `install` stays unambiguous.
- The extensions and extras components are useful without Records and can be built first.

## Namespaces and access

- Postgres: per App line a schema `{app}_v{major}` (the catalog's naming) owned by a role of the same name
  with a generated password (secret store). Every App role gets read on every Records schema (cross-App
  reads are allowed, the author's call); Ponds read with one read-only role.
- Media: `media/{app}/m{major}/` (or the same under the prefix).
- Apps get env vars, the contract for any language: `DUCKSTRING_RECORDS_URL` (a standard libpq URL with
  `search_path` set to its schema) and `DUCKSTRING_MEDIA_URL`. Python helpers wrap them: `app.records()`
  returns a connection, `app.media()` an fsspec filesystem rooted at the line's Media.
- Off the Catchment's machine with local Media, `app.media()` talks to the Catchment's Media HTTP API (the
  `VolumeFileSystem` pattern over Databricks' Files API). On an object store it goes direct.

## Ponds reading Records and Media

- `pond.read_records("feedback@1.ratings")` returns a DuckDB relation (the `postgres` extension,
  read-only). The major is always explicit: a bare name following the live major would change schema under
  a Pond at promotion.
- `pond.media("feedback@1", "uploads/")` returns an Input like `databricks.files()`, with `added()` for
  files new since the Pond's last Run (position kept as for Databricks inputs).
- A Records or Media read is a foreign read: no engine change. Inlets always run; a non-Inlet reading
  Records uses `@ripple(always_run=True)`, as for any outside source. `merge_table` over a Records read gives
  an incremental Trickle with a change log.
- Ponds never write Records or Media.
- Remote Ducks reach Records through Postgres directly (needs reachability) and Media as Apps do.

## External source nodes (canvas only)

- `[pond] external = ["GDELT events", "app:feedback@1"]`: each entry is a node with an edge only into the
  Pond. Ponds naming the same entry share a node. Optional and visual only; nothing is drawn automatically
  (the author's call: declared or not shown).
- The node looks like an App node, marked as external. An `app:name@major` entry is that App line's data:
  labelled with the line, with a table icon at the top right opening the viewer on its Records and Media.
  Never an edge back to the App itself. An entry naming a line not deployed is drawn as a plain name.
- An App node with Records or Media carries the same table icon, which also tells stateful Apps from
  stateless ones at a glance. Data no Pond declares is reached from the App node or the Catalog.
- `_check_apps_terminal` is unchanged: an App is still never a Source.

## Majors: trial, live, retired

An App line with Records or Media has one live major at a time (two would take writes in two places).
Apps without either keep concurrent majors.

- **Trial**: a new major before promotion. Manage access only can open it (the author's call), against a
  trial migration; anything
  written there is discarded by the final migration. A trial migration can be re-run at will.
- **Live**: the major `/apps/{name}` serves (`pond promote`).
- **Retired**: the previous major after promotion. Instances stopped, its address redirects to the live
  major, Records and Media frozen (read-only) until explicitly deleted.

**Migration**: by default the previous major's Records schema (dump and restore under the new name) and
Media (real copies locally, since a hardlink would let an App appending in place change a frozen line's
file too; server-side copies on an object store) are copied whole, then `[app] migrate`
(a command, like `[app] download`) runs over the copy with the previous major readable. Most migrations
are a few `ALTER` statements. A move from the private SQLite directory into Records is a `migrate` that
reads the previous major's state directory.

**Cutover** (`pond promote` on such an App):
1. Stop the live major's instances; the proxy answers 503 with `Retry-After` ("upgrading").
2. Run the migration (copy, then `migrate`).
3. Start the new major, switch the address, mark the old major retired.
4. On failure: restart the old major unchanged, report the error.

The window is the migration's length.

**Deletion**: Records and Media are kept until explicitly deleted (the author's call). `pond remove` on
an App line keeps them. `duckstring app purge APP@MAJOR` (proposed; the `app` group already holds
`instances|logs|restart`) drops the line's schema and role and deletes its Media. Full access, the
Catchment's name as confirmation (as reset, wipe and remove ask), refused on the live line. Purged data is
gone from backups taken afterwards only.

## Viewer, catalog and auth

Anything in the Catchment is available to anyone authenticated to it; an App that wants its data more
public serves it itself.

- Read: browse Records tables and Media, download Media, query Records through `/api/query` (attached
  read-only). The catalog list gains a Records section, one schema per App line.
- Manage (full): upload and delete Media. Not lower, so Media can't be used as a drop box. Objects did not
  gain upload (2026-11-24, while building): an Object is a Pond's published output, so an uploaded one would
  be replaced at the Pond's next Run and would skip the version contract; the viewer keeps listing,
  downloading and deleting them.
- Records are read-only in the UI: only the App knows its rules.

## Later

- A Spout destination `records:` writing Pond output into a read-only `duckstring` schema every App can
  read (fast lookups and concurrent reads in Apps; would have replaced the attention App's `db_lock`).
- A cheap change signal for Records so a reading Pond can skip its read.


## As built (2026-11-24)

All the above, with these specifics and differences:

- `duckstring install postgres` measured on macOS (Homebrew 18.7) and in `python:3.13-slim` (PGDG 18.7: the
  postgresql-common script adds the repository with the key it ships; `create_main_cluster = false` before the server
  package, so no cluster starts). The image installs it (`POSTGRES` build arg, empty to leave it out; about 30 MB).
- The managed cluster never uses Unix sockets (a long Catchment root overflows the 103-byte path), adopts a
  postmaster a previous Catchment process left running, and keeps a stable port (re-chosen only when taken).
- The reader URL rides each job built from settings alone, so a dispatch never starts PostgreSQL; the App
  supervisor's loop keeps it running while any line holds Records.
- The trial's first start runs the migration; a previous major with only a private state directory also hands over
  (its line is recorded retired with no Records or Media).
- Records copies go through a scratch database (`pg_dump`, rename the schema, `REASSIGN OWNED`, `pg_dump`, restore),
  so the source line keeps serving during a trial migration.
- The serving catalog attaches Records as `records`; DuckDB's sandbox (`enable_external_access = false`) still
  reads an already-attached PostgreSQL (measured).
- Backups dump Records (`records/records.dump`), restored by a fresh managed cluster after it makes the line roles;
  the state backup carries Media up to 256 MB.

Added the same day, before merging (the author asked for them):

- **TLS.** A managed PostgreSQL listening beyond its machine (`catchment records --listen`) accepts other machines
  only over TLS (`hostssl` lines only), with the operator's certificate (`--tls-cert/--tls-key`) or a self-signed
  one made with `openssl`; loopback stays plain. `--host` names the reachable address, `--reader-url` prints a
  read-only URL for a BI tool (`sslmode=require`), status shows the certificate's SHA-256 fingerprint. Measured:
  from the machine's LAN address a plain connection is refused, a TLS one accepted.
- **Ducks on other machines** read Records and Media (on the Catchment's disk) through the Duck channel, which they
  already dial back on: no new port, and it works through the relay and platform hosts. Decided per Pond by the
  launcher (`on_this_machine`: a remote backend, not a `local` Pool). Media in an object store and an external
  PostgreSQL are read directly.
- **Several instances of an App** on the Catchment's machine: `[app] instances = N` (with `state = "none"`); one
  starts on the first request, another once every ready one has 4 requests in flight; least-busy routing with a
  sticky cookie; memory per instance; each stops when idle. This is the "most teams run one machine" scaling: an
  App outgrowing one Python process. App instances on other machines are not built (the launchers would have to run
  long-lived services that take inbound traffic, validated on each cloud).
- **Spouts into Records**: `records:` delivers into `{pond}_v{major}`, readable by every App.

- **Moves** (the author's calls: a downtime window for Records; a move deletes the old store; PostgreSQL major
  upgrades are the same mechanism): `catchment records move managed|postgres://... [--postgres-version N]` and
  `catchment media move local|s3://...`, in `catchment/moves.py`. Records: check the target (CREATEROLE and
  CREATEDB, none of this Catchment's schemas or roles there), the window (Apps with Records 503, `records:` Spouts
  wait, writing roles frozen), one `pg_dump` restored after the roles are made, row counts compared, switch, old
  store deleted. A version move builds `records/pg.new` with the new major beside the old and swaps it in (a real
  17 to 18 move is tested). Media: a first copy while Apps run, the changes in the window, listings compared,
  switch, old files deleted. A failure before the switch undoes the target. While a move runs, `keep_alive` is held
  off: it restarted the old cluster mid-swap in the first test run.

Open: App instances on other machines, and the purge command's final name (`app purge` for now).
