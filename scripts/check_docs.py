#!/usr/bin/env python3
"""Validate local Markdown links and paths in the assistant code map."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
CODE_PATH_RE = re.compile(r"`((?:main\.py|scraper/|scripts/|tests/)[^`]*)`")
CODE_MAP_START = "## Code map"
CODE_MAP_END = "## Runtime flow and ownership"
IGNORED_DIRS = {".git", ".pytest_cache", "__pycache__", "venv", ".venv"}


def markdown_files(root: Path) -> list[Path]:
    return sorted(
        path for path in root.rglob("*.md")
        if not any(part in IGNORED_DIRS or part.startswith(".") for part in path.relative_to(root).parts[:-1])
    )


def check_markdown_links(root: Path, files: list[Path]) -> list[str]:
    errors = []
    for doc in files:
        text = doc.read_text(encoding="utf-8")
        for match in LINK_RE.finditer(text):
            target = match.group(1).strip().split(maxsplit=1)[0].strip("<>")
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            path = (doc.parent / unquote(parsed.path)).resolve()
            if not path.exists():
                rel = doc.relative_to(root)
                errors.append(f"{rel}: broken local link target {parsed.path!r}")
    return errors


def check_code_map(root: Path) -> list[str]:
    guide = root / "CLAUDE.md"
    if not guide.exists():
        return ["CLAUDE.md: missing assistant navigation guide"]
    text = guide.read_text(encoding="utf-8")
    start = text.find(CODE_MAP_START)
    end = text.find(CODE_MAP_END, start + len(CODE_MAP_START))
    if start < 0 or end < 0:
        return ["CLAUDE.md: could not find bounded Code map section"]
    section = text[start:end]
    errors = []
    for value in CODE_PATH_RE.findall(section):
        path_text = value.rstrip(".,;:")
        path = root / path_text
        if not path.exists():
            errors.append(f"CLAUDE.md: code map target {path_text!r} does not exist")
    return errors


def validate(root: Path) -> list[str]:
    files = markdown_files(root)
    return check_markdown_links(root, files) + check_code_map(root)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    errors = validate(root)
    if errors:
        print("\n".join(f"ERROR: {error}" for error in errors), file=sys.stderr)
        return 1
    print(f"Documentation checks passed ({len(markdown_files(root))} Markdown files).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
