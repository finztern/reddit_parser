from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.check_docs import check_code_map, check_markdown_links  # noqa: E402


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
