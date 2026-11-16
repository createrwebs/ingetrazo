# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Marco Sumari Tellez and IngeTrazo contributors.
"""Audit a translation catalog against the tr() strings in the source.

English is the source language (core/i18n.py): every literal passed to
tr() should have an entry in i18n/<lang>.json, or the interface shows
English there. This walks the AST of views/, core/, plugins/, tools/ and
main.py — adjacent string literals concatenate exactly as Python does,
so a multi-line tr("..." "...") call is audited as the ONE string it is
(a regex audit cuts those in half and reports false gaps).

Usage:  python scripts/i18n_audit.py [--lang th] [--verbose]
Exit code 1 when untranslated strings remain (the allowlist excepted).
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Source strings that need no translation: symbols and proper names.
ALLOWLIST = {"+", "⧉", "IngeTrazo", "Wayland"}

_DIRS = ("views", "core", "plugins", "tools")


def _literal(node):
    """The constant string of an expression, joining «a» «b» as Python does."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal(node.left), _literal(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def source_strings(root: Path = ROOT) -> dict[str, str]:
    """Every literal tr() source string → the file it was found in."""
    files = [root / "main.py"]
    for d in _DIRS:
        files.extend(sorted((root / d).glob("*.py")))
    found: dict[str, str] = {}
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8",
                                            errors="ignore"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "tr" and node.args):
                text = _literal(node.args[0])
                if text is not None:
                    found.setdefault(text, str(path.relative_to(root)))
    return found


def missing_strings(lang: str, root: Path = ROOT) -> list[tuple[str, str]]:
    """(source string, file) for every tr() literal the catalog lacks."""
    catalog = json.loads(
        (root / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
    return sorted((s, f) for s, f in source_strings(root).items()
                  if s not in catalog and s not in ALLOWLIST)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Audit i18n/<lang>.json coverage")
    ap.add_argument("--lang", default="th")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)
    strings = source_strings()
    missing = missing_strings(args.lang)
    print(f"{len(strings)} tr() source strings; "
          f"{len(missing)} missing from i18n/{args.lang}.json "
          f"(allowlist: {len(ALLOWLIST)} symbols/names)")
    if args.verbose or missing:
        for text, where in missing:
            print(f"  {where}: {text[:100]!r}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())