import os
import shutil
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _project():
    temp = tempfile.TemporaryDirectory()
    project = Path(temp.name) / "project"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "backup_sessions.sh", scripts)
    shutil.copy2(ROOT / "scripts" / "restore_sessions.sh", scripts)
    return temp, project


def _run(script, project, backup_root, *args):
    env = os.environ | {"BACKUP_ROOT": str(backup_root)}
    return subprocess.run(
        ["bash", str(project / "scripts" / script), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_backup_permissions_and_account_restore():
    temp, project = _project()
    try:
        backup_root = Path(temp.name) / "archive"
        sources = {
            "cookies/alice.json": "cookie-backup",
            "cookies/bob.json": "bob-cookie",
            "storage_state/alice.json": "storage-backup",
            "state/profiles/alice.json": "profile-backup",
        }
        for relative, content in sources.items():
            path = project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        nested = project / "state/profiles/.history"
        nested.mkdir()

        backup = _run("backup_sessions.sh", project, backup_root)
        assert backup.returncode == 0, backup.stderr
        backup_dir = next(backup_root.iterdir())
        assert backup_dir.parent == backup_root
        assert not (project / "backups").exists()
        for path in [backup_root, backup_dir, *backup_dir.rglob("*")]:
            expected_mode = 0o700 if path.is_dir() else 0o600
            assert path.stat().st_mode & 0o777 == expected_mode, path

        (project / "cookies/alice.json").write_text("changed", encoding="utf-8")
        (project / "cookies/bob.json").write_text("bob-unchanged", encoding="utf-8")
        (project / "storage_state/alice.json").write_text("changed", encoding="utf-8")
        (project / "state/profiles/alice.json").write_text("changed", encoding="utf-8")

        restored = _run("restore_sessions.sh", project, backup_root, backup_dir.name, "alice")
        assert restored.returncode == 0, restored.stderr
        assert (project / "cookies/alice.json").read_text(encoding="utf-8") == "cookie-backup"
        assert (project / "cookies/bob.json").read_text(encoding="utf-8") == "bob-unchanged"
        assert (project / "storage_state/alice.json").read_text(encoding="utf-8") == "storage-backup"
        assert (project / "state/profiles/alice.json").read_text(encoding="utf-8") == "profile-backup"

        missing = _run("restore_sessions.sh", project, backup_root, backup_dir.name, "unknown")
        assert missing.returncode != 0
        assert "no session files found" in missing.stderr
    finally:
        temp.cleanup()
