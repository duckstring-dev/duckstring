# Catchment Ponds CLI

This document describes `duckstring catchment ponds ...`.

## V1 Constraints

- Local execution only
- Pond code sources may be local filesystem or git
- Runtime staging target is always `<root_dir>/ponds/<pond>/<version>`

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

## ponds list-pulled

Show all pulled pond versions currently present under `<root_dir>/ponds`.

```bash
duckstring catchment ponds list-pulled [-f|--file <path>]
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
- `--force` (skip confirmations and auto-overwrite conflicts)
- `-i|--interactive`
- `-f|--file <path>`

Git repo values accept HTTPS URLs and SSH scp-style addresses (for example `git@github.com:org/repo.git`).

Monorepo notes:

- `git/catalog` with `--repo-structure monorepo` accepts `--root` (default `.`).
- Monorepo layout must be `{root}/{pond}/{version}` with semver version directories.

### Conflict / warning behavior on add

Before writing:

- exact duplicate source entries may be detected as conflicts
- duplicate single `pond@version` entries are conflicts
- catalog/single overlaps emit warnings
- catalog/catalog overlaps emit warnings
- presence of monorepo catalog sources emits warnings

Without `--force`, conflicts and final writes are confirmed interactively.
With `--force`, conflicts are auto-overwritten and confirmation prompts are skipped.

## ponds pull

Pull all configured `pond_sources` into `<root_dir>/ponds` and refresh the runtime pond catalog used by basin hydration.

```bash
duckstring catchment ponds pull [-f|--file <path>]
```

Behavior:

- Materializes local and git sources into `<root_dir>/ponds/<pond>/<version>`.
- Updates the runtime catalog used by basin hydration and resolution.
- Prints a summary count of pulled pond versions.

Selection behavior:

- Sources are processed in `pond_sources` order.
- For the same `pond@version`, later discovered entries overwrite earlier ones.

Source-specific discovery:

- `local/catalog`: discovers `<root>/<pond>/<version>` where `<version>` is semver and `pond.py` exists.
- `local/single`: uses explicit `pond`, `version`, `path`.
- `git/single`: checks out `repo@ref`, then uses explicit `pond`, `version`.
- `git/catalog` (versioned): maps matching refs from `ref_pattern` to `<pond>@<version>`.
- `git/catalog` (monorepo): checks out fixed `ref_pattern`, then discovers `<root>/<pond>/<version>`.

## ponds remove

Remove by pond source `id`.

```bash
duckstring catchment ponds remove <source_id> [-f|--file <path>]
```

Behavior:

- Removes matching `pond_sources` entry with the given `id`.
- Tab completion on `<source_id>` includes source details to help selection.
