# Catchment Ponds CLI

This document describes `duckstring catchment ponds ...`.

## Command Group

- `duckstring catchment ponds list-sources`
- `duckstring catchment ponds list-pulled`
- `duckstring catchment ponds pull`
- `duckstring catchment ponds add`
- `duckstring catchment ponds remove`

All commands accept `-f|--file <path>` (default: `catchment.json`) where applicable.

## ponds list-sources

Show all `pond_sources` entries with source `id`.

```bash
duckstring catchment ponds list-sources [-f|--file <path>]
```

Example output:

```text
1  -- git_catalog/versioned pond=ingest repo=https://git.com ref_type=branch pattern=release/{version}
2  -- git_catalog/versioned pond=enriched repo=https://git.com ref_type=branch pattern=release/{version}
```

## ponds list-pulled

Show all pulled pond versions currently present under `<root_dir>/ponds`.

```bash
duckstring catchment ponds list-pulled [-f|--file <path>]
```

Example output:

```text
Catchment Root: /abs/path/to/.duckstring/

aggregated@1.0.0
```

## ponds add

`ponds add` supports two modes:

- direct (all options supplied on CLI), or
- interactive (`-i|--interactive`).

### Interactive mode

```bash
duckstring catchment ponds add -i [-f|--file <path>]
```

Interactive flow:

1. Source type: `local` or `git`
2. Scope: `single` or `catalog`
3. Source-specific prompts
4. Conflict checks / warnings
5. Final confirmation

Interactive branches:

- Local single: writes `pond_sources` entry `{type: local, structure: single, pond, version, path, entrypoint}`
- Local catalog: writes `{type: local, structure: catalog, root, entrypoint}`
- Git single: writes `{type: git, structure: single, repo, ref_type, ref, pond, version, entrypoint}`
- Git catalog:
  - Versioned: `{type: git, structure: catalog, repo_structure: versioned, repo, ref_type, ref_pattern, pond, entrypoint}`
  - Monorepo: `{type: git, structure: catalog, repo_structure: monorepo, repo, ref_type, ref_pattern, pond: null, entrypoint}`

Notes:

- `pond.py` is fixed as entrypoint.
- URL prompts require valid URLs.
- For git catalog monorepo, `ref_pattern` stores the fixed ref value.

### Direct mode

```bash
duckstring catchment ponds add [options]
```

Direct options:

- `--source-type <local|git>`
- `--scope <single|catalog>`
- `--pond <name>`
- `-p|--path <path>`
- `--root <path>`
- `-v|--version <x.y.z>`
- `--repo <url>`
- `--repo-structure <versioned|monorepo>` (default: `versioned`)
- `--ref-type <branch|tag|commit>` (allowed values depend on source/scope)
- `--ref <value>`
- `--ref-pattern <value>`
- `-i|--interactive`
- `-f|--file <path>`

### Conflict / warning behavior on add

Before writing:

- exact duplicate source => conflict
- duplicate single `pond@version` => conflict
- catalog/single overlaps => warnings
- catalog/catalog overlaps => warnings
- monorepo source present => warning

If conflicts are found, CLI prompts to overwrite conflicting sources.
Warnings are printed before final confirmation.

## ponds pull

Pull all configured `pond_sources` into `<root_dir>/ponds` and refresh the runtime pond catalog used by basin hydration.

```bash
duckstring catchment ponds pull [-f|--file <path>]
```

Behavior:

- Materializes local and git sources into `<root_dir>/ponds/<pond>/<version>`.
- Prints a summary count of pulled pond versions.

Resolution policy (conflicts, precedence, populate/skip):

1. Sources are evaluated in `pond_sources` order.
2. Each discovered pond candidate is keyed by `pond@version`.
3. Priority by source scope:
   - `single` entries have higher priority than `catalog` entries.
   - `catalog` entries are lower priority than any `single` for the same `pond@version`.
4. Populate/skip rules:
   - If a `single` discovers a `pond@version` not yet selected, populate it.
   - If a `single` discovers a `pond@version` already selected from a `catalog`, replace it with the `single`.
   - If a `catalog` discovers a `pond@version` already selected, skip it.
5. Same-priority ties:
   - `single` vs `single`: keep the earlier `pond_sources` entry and skip later duplicates.
   - `catalog` vs `catalog`: keep the earlier `pond_sources` entry and skip later duplicates.
6. This precedence model matches `ponds add` semantics:
   - duplicate explicit `single pond@version` is treated as a conflict at add time.
   - `single` can override catalog content.
   - overlapping catalogs are allowed, with ordering determining which source is selected.

Source-specific discovery:

- `local/catalog`: discovers `<root>/<pond>/<version>` where `<version>` is semver and `pond.py` exists.
- `local/single`: uses explicit `pond`, `version`, `path`.
- `git/single`: checks out `repo@ref`, then uses explicit `pond`, `version`.
- `git/catalog` (versioned): for matching refs from `ref_pattern`, maps to `<pond>@<version>`.
- `git/catalog` (monorepo): checks out fixed ref, then discovers `<root>/<pond>/<version>` like local catalog.

## ponds remove

Remove by pond source `id`.

```bash
duckstring catchment ponds remove <source_id> [-f|--file <path>]
```

Behavior:

- Removes matching `pond_sources` entry with the given `id`.
- Tab completion on `<source_id>` includes source details to help selection.
