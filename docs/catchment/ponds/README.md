# Catchment Ponds CLI

This document describes `duckstring catchment ponds ...`.

## Command Group

- `duckstring catchment ponds list`
- `duckstring catchment ponds add`
- `duckstring catchment ponds remove`

All commands accept `-f|--file <path>` (default: `catchment.json`) where applicable.

## ponds list

Show both:

- legacy `ponds` entries (if any), and
- `pond_sources` entries (the current preferred model).

```bash
duckstring catchment ponds list [-f|--file <path>]
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
duckstring catchment ponds add [legacy_name] [options]
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

Backwards-compatible shorthand:

```bash
duckstring catchment ponds add <name> -p <path> -v <version>
```

This maps to `local/single`.

### Conflict / warning behavior on add

Before writing:

- exact duplicate source => conflict
- duplicate single `pond@version` => conflict
- catalog/single overlaps => warnings
- catalog/catalog overlaps => warnings
- monorepo source present => warning

If conflicts are found, CLI prompts to overwrite conflicting sources.
Warnings are printed before final confirmation.

## ponds remove

Remove by `pond@version`.

```bash
duckstring catchment ponds remove <name> -v|--version <version> [-f|--file <path>]
```

Behavior:

- Removes matching `pond_sources` single entries first.
- Falls back to removing legacy `ponds` entry if present.
