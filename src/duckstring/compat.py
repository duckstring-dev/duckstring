"""Checking a Pond's code against the APIs a Duckstring release removed (``duckstring pond check``).

A Catchment runs one Duckstring and every Pond runs on it, so upgrading the Catchment upgrades every Pond, and code
calling an API the new version removed fails when its Ripple runs. This names each such call before then, with its
replacement: a static scan (the code is parsed, never imported, so a Catchment can run it on any Pond), run by
``pond check`` (``--fix`` rewrites the calls it can decide), at deploy (as a warning) and by the Catchment over every
deployed Pond (``/api/status``'s per-Pond ``compat``).

:data:`REMOVED` grows with each release's breaking changes (the CHANGELOG's "breaking" sections).
"""

from __future__ import annotations

import ast
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Removed:
    """A keyword argument removed from the methods named ``methods``. ``replace`` maps a literal value to the
    keyword argument that replaces it, as source."""

    methods: tuple[str, ...]
    keyword: str
    since: str
    replace: dict
    note: str


REMOVED: tuple[Removed, ...] = (
    Removed(("append_table",), "fail_on_conflict", "0.9.0",
            {False: 'on_conflict="skip_check"', True: 'on_conflict="fail"'},
            'on_conflict: "skip_check" (appends every row), "keep_prior" or "fail"'),
    Removed(("append",), "fail_on_conflict", "0.9.0",  # the builder's .append(); a list's never has it
            {False: 'on_conflict="keep_prior"', True: 'on_conflict="fail"'},
            'on_conflict: "keep_prior" (drops rows already there), "skip_check" or "fail"'),
)


@dataclass
class Finding:
    path: str  # relative to the project
    line: int
    col: int
    message: str
    fix: tuple[int, int, int, str] | None = None  # (line, start col, end col, replacement) on one line

    def as_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if k != "fix"}


def _method(call: ast.Call) -> str | None:
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None


def check_source(source: str, path: str = "<code>") -> list[Finding]:
    """The removed APIs ``source`` uses. A file that doesn't parse has none (the deploy reports it)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _method(node)
        for rule in REMOVED:
            if name not in rule.methods:
                continue
            for kw in node.keywords:
                if kw.arg != rule.keyword:
                    continue
                value = kw.value.value if isinstance(kw.value, ast.Constant) else None
                new = rule.replace.get(value) if isinstance(value, bool) else None
                message = (f"{name}({rule.keyword}=...) was removed in Duckstring {rule.since}: "
                           + (f"use {new}" if new else f"use {rule.note}"))
                fix = None
                if new and kw.lineno == kw.end_lineno:
                    fix = (kw.lineno, kw.col_offset, kw.end_col_offset, new)
                out.append(Finding(path, kw.lineno, kw.col_offset + 1, message, fix))
    return sorted(out, key=lambda f: (f.path, f.line, f.col))


def _files(root: Path) -> list[Path]:
    """The project's Python files a deploy would include (``.pondignore`` or its defaults)."""
    from .pondignore import deployed_files

    return [p for p in deployed_files(root) if p.suffix == ".py"]


def check_project(root: str | Path) -> list[Finding]:
    """Every removed API the Pond project at ``root`` uses."""
    root = Path(root)
    out = []
    for rel in _files(root):
        try:
            source = (root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        out += check_source(source, rel.as_posix())
    return out


def fix_project(root: str | Path) -> int:
    """Rewrite each use whose replacement is certain (a literal value, on one line). Returns how many."""
    root = Path(root)
    fixed = 0
    by_file: dict[str, list[Finding]] = {}
    for f in check_project(root):
        if f.fix is not None:
            by_file.setdefault(f.path, []).append(f)
    for path, findings in by_file.items():
        file = root / path
        lines = file.read_text(encoding="utf-8").splitlines(keepends=True)
        # Right to left on each line, so earlier offsets still hold. AST offsets are UTF-8 bytes.
        for f in sorted(findings, key=lambda f: (f.fix[0], -f.fix[1])):
            line, start, end, new = f.fix
            raw = lines[line - 1].encode("utf-8")
            lines[line - 1] = (raw[:start] + new.encode("utf-8") + raw[end:]).decode("utf-8")
            fixed += 1
        file.write_text("".join(lines), encoding="utf-8")
    return fixed


__all__ = ["REMOVED", "Removed", "Finding", "check_source", "check_project", "fix_project"]
