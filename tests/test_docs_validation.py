from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.check_docs import (  # noqa: E402
    check_code_map,
    check_configuration_map,
    check_markdown_links,
)


def test_markdown_link_checker_reports_missing_local_target(tmp_path):
    doc = tmp_path / "README.md"
    doc.write_text("[missing](./missing.md)\n[external](https://example.invalid)\n", encoding="utf-8")

    errors = check_markdown_links(tmp_path, [doc])

    assert errors == ["README.md: broken local link target './missing.md'"]


def test_code_map_checker_requires_existing_paths(tmp_path):
    (tmp_path / "scraper").mkdir()
    guide = tmp_path / "CLAUDE.md"
    guide.write_text(
        "## Code map\n| area | `scraper/` | `tests/not_here.py` |\n"
        "## Runtime flow and ownership\n",
        encoding="utf-8",
    )

    errors = check_code_map(tmp_path)

    assert errors == ["CLAUDE.md: code map target 'tests/not_here.py' does not exist"]


def test_configuration_map_requires_yaml_keys_to_be_documented(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "config.yaml").write_text(
        "stream_subs: [all]\nqueue_control:\n  enabled: true\n  stale_seconds: 30\n",
        encoding="utf-8",
    )
    doc = tmp_path / "docs" / "configuration-map.md"
    doc.write_text(
        "`stream_subs` and `queue_control.enabled`.\n"
        "## `config.yaml` key inventory\nstream_subs\nqueue_control.enabled\n"
        "## When changing configuration\n",
        encoding="utf-8",
    )

    errors = check_configuration_map(tmp_path)

    assert errors == ["docs/configuration-map.md: undocumented config.yaml keys: queue_control.stale_seconds"]


def test_configuration_map_passes_when_all_yaml_keys_are_listed(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "config.yaml").write_text(
        "stream_subs: [all]\nqueue_control:\n  enabled: true\n",
        encoding="utf-8",
    )
    (tmp_path / "docs" / "configuration-map.md").write_text(
        "`stream_subs`, `queue_control.enabled`.\n"
        "## `config.yaml` key inventory\nstream_subs\nqueue_control.enabled\n"
        "## When changing configuration\n",
        encoding="utf-8",
    )

    assert check_configuration_map(tmp_path) == []


def test_configuration_map_requires_key_inventory_section(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "config.yaml").write_text("stream_subs: [all]\n", encoding="utf-8")
    (tmp_path / "docs" / "configuration-map.md").write_text("`stream_subs`.\n", encoding="utf-8")

    assert check_configuration_map(tmp_path) == [
        "docs/configuration-map.md: missing bounded config.yaml key inventory",
    ]
