"""probe_nodes передаёт expected=200 в delay-API mihomo; 403-нода считается негодной."""
import http.server
import sys
import threading
import types
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
if "curl_cffi" not in sys.modules:
    try:
        import curl_cffi  # noqa: F401
    except ImportError:
        m = types.ModuleType("curl_cffi")
        r = types.ModuleType("curl_cffi.requests")
        r.BrowserType = type("BrowserType", (), {})
        m.requests = r
        sys.modules["curl_cffi"], sys.modules["curl_cffi.requests"] = m, r

import add_guest_slots as ags  # noqa: E402
import rotation as rot  # noqa: E402

SEEN = []


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        SEEN.append(self.path)
        name = urllib.parse.unquote(self.path.split("/proxies/", 1)[1].split("/delay", 1)[0])
        banned = "banned" in name and "expected=200" in self.path
        code, body = (504, b'{"message":"err"}') if banned else (200, b'{"delay":50}')
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_probe_expected_status():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        api = f"http://127.0.0.1:{srv.server_address[1]}"
        mk = lambda n: ags.Free(rot.Node(key=n, name=n, uri=n), n, "de")
        nodes = [mk("de1-good"), mk("de2-banned")]
        ok, bad, _ = ags.probe_nodes(nodes, api, "", ags.REDDIT_PROBE_URL, 2000, 2, "200")
        assert [f.mihomo_name for f in ok] == ["de1-good"]
        assert [f.mihomo_name for f in bad] == ["de2-banned"]
        assert all("expected=200" in p for p in SEEN)
        SEEN.clear()
        ok, bad, _ = ags.probe_nodes(nodes, api, "", ags.DEFAULT_PROBE_URL, 2000, 2)
        assert len(ok) == 2 and not any("expected" in p for p in SEEN)
    finally:
        srv.shutdown()
