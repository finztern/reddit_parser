#!/usr/bin/env python3
"""Validate local Markdown links and paths in the assistant code map."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml

LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
CODE_PATH_RE = re.compile(r"`((?:main\.py|scraper/|scripts/|tests/)[^`]*)`")
CODE_MAP_START = "## Code map"
CODE_MAP_END = "## Runtime flow and ownership"
CONFIG_KEYS_START = "## `config.yaml` key inventory"
CONFIG_KEYS_END = "## When changing configuration"
CODE_TOKEN_RE = re.compile(r"`([^`]+)`")
CONFIG_KEY_RE = re.compile(r"^[a-z][a-z0-9_.]*$")
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
        elif path_text.startswith("tests/") and path.suffix != ".py":
            errors.append(f"CLAUDE.md: test map target {path_text!r} is not a Python test file")
    return errors


def _config_keys(data, prefix: str = "") -> set[str]:
    keys = set()
    if not isinstance(data, dict):
        return keys
    for key, value in data.items():
        name = str(key)
        dotted = f"{prefix}.{name}" if prefix else name
        if isinstance(value, dict):
            keys.update(_config_keys(value, dotted))
        else:
            keys.add(dotted)
    return keys


def check_configuration_map(root: Path) -> list[str]:
    config_path = root / "config.yaml"
    doc_path = root / "docs" / "configuration-map.md"
    if not config_path.exists() or not doc_path.exists():
        missing = []
        if not config_path.exists():
            missing.append("config.yaml")
        if not doc_path.exists():
            missing.append("docs/configuration-map.md")
        return [f"configuration map check requires {', '.join(missing)}"]
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        return [f"config.yaml: cannot parse YAML ({error})"]
    doc = doc_path.read_text(encoding="utf-8")
    documented = set(CODE_TOKEN_RE.findall(doc))
    start = doc.find(CONFIG_KEYS_START)
    end = doc.find(CONFIG_KEYS_END, start + len(CONFIG_KEYS_START))
    if start < 0 or end < 0:
        return ["docs/configuration-map.md: missing bounded config.yaml key inventory"]
    inventory = doc[start:end]
    documented.update(
        line.strip()
        for line in inventory.splitlines()
        if CONFIG_KEY_RE.fullmatch(line.strip())
    )
    missing = sorted(key for key in _config_keys(data) if key not in documented)
    if missing:
        return [f"docs/configuration-map.md: undocumented config.yaml keys: {', '.join(missing)}"]
    return []


def validate(root: Path) -> list[str]:
    files = markdown_files(root)
    return (
        check_markdown_links(root, files)
        + check_code_map(root)
        + check_configuration_map(root)
    )


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
