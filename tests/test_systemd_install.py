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
        assert set(rendered) == {"refresh-cookies.service", "refresh-cookies.timer",
                                 "refresh-guest-cookies.service", "refresh-guest-cookies.timer",
                                 "rotation-daemon.service"}
        for name, text in rendered.items():
            assert "@USER@" not in text and "@ROOT@" not in text and "@GROUP@" not in text, name
        d = rendered["rotation-daemon.service"]
        assert "User=alice" in d and f"WorkingDirectory={proj}" in d
        assert f"{proj}/venv/bin/python3 scripts/rotation_daemon.py --no-headless" in d
        g = rendered["refresh-guest-cookies.service"]
        assert f"{proj}/venv/bin/python3 scripts/refresh_guest_cookies.py" in g and "flock" in g
    finally:
        shutil.rmtree(out, ignore_errors=True)
        shutil.rmtree(proj, ignore_errors=True)


def test_no_login_skips_login_units_and_bad_root_fails():
    if not shutil.which("bash"):
        return
    out = Path(tempfile.mkdtemp())
    try:
        r = _run("--no-login", "--out", str(out))
        assert r.returncode == 0, r.stdout + r.stderr
        assert not (out / "refresh-cookies.service").exists()
        assert (out / "refresh-guest-cookies.timer").exists()
        bad = _run("--root", "/definitely/not/here", "--out", str(out))
        assert bad.returncode != 0 and "нет каталога" in bad.stderr
    finally:
        shutil.rmtree(out, ignore_errors=True)
