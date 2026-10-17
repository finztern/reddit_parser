"""scripts/install_systemd.sh: отрисовка шаблонов юнитов без root и systemctl (--out)."""
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "scripts" / "install_systemd.sh"


def _run(*args):
    return subprocess.run(["bash", str(INSTALLER), *args], capture_output=True, text=True, timeout=30)


def test_renders_units_for_given_user_and_root():
    if not shutil.which("bash"):
        return
    out, proj = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
    try:
        r = _run("--user", "alice", "--root", str(proj), "--out", str(out))
        assert r.returncode == 0, r.stdout + r.stderr
        rendered = {p.name: p.read_text(encoding="utf-8") for p in out.iterdir()}
        # логин-only парк: гостевой харвест по умолчанию НЕ ставится
        assert set(rendered) == {"backup-sessions.service", "backup-sessions.timer",
                                 "refresh-cookies.service", "refresh-cookies.timer",
                                 "filter-nodes.service", "filter-nodes.timer",
                                 "rotation-daemon.service"}
        for name, text in rendered.items():
            assert "@USER@" not in text and "@ROOT@" not in text and "@GROUP@" not in text, name
        d = rendered["rotation-daemon.service"]
        assert "User=alice" in d and f"WorkingDirectory={proj}" in d
        assert f"{proj}/venv/bin/python3 scripts/rotation_daemon.py --no-headless" in d
        f = rendered["filter-nodes.service"]
        assert f"{proj}/venv/bin/python3 scripts/filter_nodes.py --write" in f and "User=alice" in f
        assert "OnUnitActiveSec=24h" in rendered["filter-nodes.timer"]

        out2 = Path(tempfile.mkdtemp())
        try:
            r = _run("--user", "alice", "--root", str(proj), "--out", str(out2), "--with-guest")
            assert r.returncode == 0, r.stdout + r.stderr
            g = (out2 / "refresh-guest-cookies.service").read_text(encoding="utf-8")
            assert f"{proj}/venv/bin/python3 scripts/refresh_guest_cookies.py" in g and "flock" in g
            assert (out2 / "refresh-guest-cookies.timer").exists()
        finally:
            shutil.rmtree(out2, ignore_errors=True)
    finally:
        shutil.rmtree(out, ignore_errors=True)
        shutil.rmtree(proj, ignore_errors=True)


def test_no_login_skips_login_units_and_bad_root_fails():
    if not shutil.which("bash"):
        return
    out = Path(tempfile.mkdtemp())
    try:
        r = _run("--no-login", "--with-guest", "--out", str(out))
        assert r.returncode == 0, r.stdout + r.stderr
        assert not (out / "refresh-cookies.service").exists()
        assert (out / "refresh-guest-cookies.timer").exists()
        assert (out / "filter-nodes.timer").exists()
        bad = _run("--root", "/definitely/not/here", "--out", str(out))
        assert bad.returncode != 0 and "нет каталога" in bad.stderr
    finally:
        shutil.rmtree(out, ignore_errors=True)
