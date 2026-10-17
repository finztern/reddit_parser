"""Модуль 04 — human_session: окно длительности, отсутствие записей, разброс старта, статусы.
Без браузера и сети: страница-заглушка, поддельные часы."""
import random
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import human_session as hs  # noqa: E402

CFG = {**hs.DEFAULTS, "min_session_seconds": 40, "max_session_seconds": 150}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeMouse:
    def __init__(self, page):
        self.page = page
        self.moves = 0
        self.wheel_calls = 0

    def move(self, x, y):
        self.moves += 1

    def wheel(self, dx, dy):
        self.wheel_calls += 1


class FakePage:
    def __init__(self, links=("/r/a/comments/1/x/", "/r/b/comments/2/y/", "/r/c/comments/3/z/"),
                 body="hello", url="https://www.reddit.com/", raise_on_goto=False):
        self.links, self.body, self.url, self.raise_on_goto = list(links), body, url, raise_on_goto
        self.mouse = FakeMouse(self)
        self.viewport_size = {"width": 1280, "height": 720}
        self.gotos = []
        self.requests = []   # методы «сетевых запросов», которые сделала бы страница

    def goto(self, url, **kw):
        if self.raise_on_goto:
            raise TimeoutError("nav")
        self.gotos.append(url)
        self.requests.append("GET")
        self.url = url

    def go_back(self, **kw):
        self.requests.append("GET")

    def inner_text(self, sel, **kw):
        return self.body

    def eval_on_selector_all(self, sel, js):
        return list(self.links)


def _run(page, seed=1, cfg=None):
    clk = Clock()
    res = hs.run_session(page, cfg or CFG, rng=random.Random(seed), clock=clk, sleep=clk.sleep)
    return res


def test_session_fits_window_for_many_seeds():
    for seed in range(60):
        res = _run(FakePage(), seed)
        assert res.status == hs.OK, (seed, res)
        assert res.duration <= CFG["max_session_seconds"], (seed, res.duration)
        assert res.duration >= CFG["min_session_seconds"], (seed, res.duration)


def test_durations_vary():
    ds = {round(_run(FakePage(), s).duration) for s in range(30)}
    assert len(ds) > 10


def test_pauses_within_1_and_lognormal_bounds():
    rng = random.Random(7)
    for prof in hs.PROFILES.values():
        vals = [hs.pick_pause(rng, prof) for _ in range(2000)]
        assert min(vals) >= 0.8 and max(vals) <= 6.0
        vals.sort()
        median = vals[len(vals) // 2]
        mean = sum(vals) / len(vals)
        assert mean > median   # правый «хвост» — не равномерное распределение


def test_session_reads_posts_and_goes_back_only_get():
    page = FakePage()
    res = _run(page, 3)
    assert "open_post" in res.actions and "scroll" in res.actions
    assert set(page.requests) == {"GET"}
    assert page.mouse.moves > 0


def test_human_never_clicks_or_posts():
    # у страницы-заглушки нет click/fill/keyboard — сессия не должна к ним обращаться
    page = FakePage()
    assert not hasattr(page, "click") and not hasattr(page, "fill")
    assert _run(page, 5).ok


def test_empty_feed_needs_attention():
    res = _run(FakePage(links=()), 1)
    assert res.status == hs.NEEDS_ATTENTION and "пуст" in res.detail


def test_captcha_needs_attention():
    res = _run(FakePage(body="Please complete the CAPTCHA"), 1)
    assert res.status == hs.NEEDS_ATTENTION and "капч" in res.detail


def test_login_redirect_needs_attention():
    class P(FakePage):
        def goto(self, url, **kw):
            super().goto(url, **kw)
            self.url = "https://www.reddit.com/login/"
    assert _run(P(), 1).status == hs.NEEDS_ATTENTION


def test_navigation_error_does_not_raise():
    res = _run(FakePage(raise_on_goto=True), 1)
    assert res.status == hs.NEEDS_ATTENTION and "TimeoutError" in res.detail


def test_calm_profile_runs():
    assert _run(FakePage(), 2, {**CFG, "profile": "calm"}).ok


# ---- WriteGuard: ни одной записи ----

def test_write_guard_blocks_all_unsafe_methods():
    g = hs.make_guard(CFG)
    for m in ("POST", "PUT", "PATCH", "DELETE"):
        assert g.decide(m, "https://www.reddit.com/api/vote", "dir=1") is False
    assert g.decide("POST", "https://oauth.reddit.com/api/comment", "x") is False
    assert g.decide("POST", "https://events.reddit.com/v2", "{}") is False
    assert len(g.blocked) == 6


def test_write_guard_allows_get_and_readonly_gql_only():
    g = hs.make_guard(CFG)
    assert g.decide("GET", "https://www.reddit.com/", None) is True
    assert g.decide("POST", "https://gql.reddit.com/", '{"operationName":"FeedQuery"}') is True
    assert g.decide("POST", "https://gql.reddit.com/", '{"operationName":"UpdatePostVote","query":"mutation"}') is False
    assert g.decide("POST", "https://gql.reddit.com/", '{"operationName":"SubscribeToSubreddit"}') is False
    assert [m for m, _ in g.blocked] == ["POST", "POST"]


def test_write_guard_route_handler_aborts_and_continues():
    class Route:
        def __init__(self): self.act = None
        def continue_(self): self.act = "continue"
        def abort(self, reason): self.act = f"abort:{reason}"

    class Req:
        def __init__(self, method, url, data=None): self.method, self.url, self.post_data = method, url, data

    g = hs.make_guard(CFG)
    r1, r2 = Route(), Route()
    g._handler(r1, Req("GET", "https://www.reddit.com/r/popular/"))
    g._handler(r2, Req("POST", "https://www.reddit.com/api/vote", "id=t3_x&dir=1"))
    assert r1.act == "continue" and r2.act.startswith("abort")


def test_guard_installs_route_on_context():
    calls = []

    class Ctx:
        def route(self, pattern, handler): calls.append(pattern)

    hs.make_guard(CFG).install(Ctx())
    assert calls == ["**/*"]


# ---- Частота и «нужно ли обновление» ----

def test_interval_gate_and_state():
    d = Path(tempfile.mkdtemp())
    now = 10_000_000.0
    assert hs.should_run("a1", CFG, None, [], now=now, state_dir=d) == (True, "нет сохранённого storage_state")
    hs.write_state("a1", {"next_due_ts": now + 3600}, d)
    ok, why = hs.should_run("a1", CFG, None, [], now=now, state_dir=d)
    assert not ok and "min_interval_hours" in why
    assert hs.should_run("a1", CFG, None, [], now=now + 3601, state_dir=d)[0]


def test_disabled_and_fresh_session_skip():
    d = Path(tempfile.mkdtemp())
    assert hs.should_run("a", {**CFG, "enabled": False}, None, [], state_dir=d)[0] is False
    ss = d / "ss.json"
    ss.write_text("{}")
    now = time.time()
    ok, why = hs.should_run("a", CFG, ss, [{"name": "token_v2", "expires": now + 7 * 86400}], now=now, state_dir=d)
    assert not ok and "свежая" in why


def test_expiring_token_or_old_state_triggers():
    d = Path(tempfile.mkdtemp())
    ss = d / "ss.json"
    ss.write_text("{}")
    now = time.time()
    ok, why = hs.should_run("a", CFG, ss, [{"name": "token_v2", "expires": now + 3600}], now=now, state_dir=d)
    assert ok and "token_v2" in why
    ok, why = hs.should_run("a", CFG, ss, [], now=now + 25 * 3600, state_dir=d)
    assert ok and "старше" in why


def test_next_due_has_jitter_and_respects_min_interval():
    rng = random.Random(1)
    now = 0.0
    vals = [hs.next_due_ts(now, CFG, rng) for _ in range(100)]
    lo = CFG["min_interval_hours"] * 3600
    assert all(lo <= v <= lo * (1 + CFG["interval_jitter"]) for v in vals)
    assert len({round(v) for v in vals}) > 50


def test_maintain_skips_without_touching_context_and_records_state():
    d = Path(tempfile.mkdtemp())
    routed = []

    class Ctx:
        def route(self, p, h): routed.append(p)

    res = hs.maintain("a", Ctx(), FakePage(), {**CFG, "enabled": False}, base_url="https://www.reddit.com",
                      storage_state_path=None, cookies=[], state_dir=d)
    assert res.status == hs.SKIPPED and not routed

    res = hs.maintain("a", Ctx(), FakePage(), {**CFG, "max_session_seconds": 41, "min_session_seconds": 40},
                      base_url="https://www.reddit.com", storage_state_path=None, cookies=[], state_dir=d,
                      rng=random.Random(1))
    # реальных часов нет -> page.wait_for_timeout у заглушки отсутствует => сессия упадёт в needs_attention, но
    # state обязан быть записан, а предохранитель — установлен
    assert routed == ["**/*"]
    st = hs.read_state("a", d)
    assert st["status"] == res.status and st["next_due_ts"] > time.time()


# ---- Планировщик: старты разнесены ----

def test_start_offsets_are_spread_and_shuffled():
    names = [f"account_{i}" for i in range(1, 11)]
    offs = hs.plan_start_offsets(names, random.Random(4), mean_gap=45)
    vals = sorted(offs.values())
    assert len(set(vals)) == len(names)                       # ни один не стартует одновременно
    gaps = [b - a for a, b in zip(vals, vals[1:])]
    assert min(gaps) >= 45 * 0.35 - 1e-6                      # минимальный зазор
    assert max(vals) - min(vals) >= 45 * 3                    # реальный разброс
    assert len({round(g) for g in gaps}) > 4                  # зазоры не одинаковые
    order = sorted(names, key=lambda n: offs[n])
    assert order != names or hs.plan_start_offsets(names, random.Random(5)) != offs


def test_start_offsets_differ_between_runs():
    names = [f"a{i}" for i in range(8)]
    assert hs.plan_start_offsets(names, random.Random(1)) != hs.plan_start_offsets(names, random.Random(2))


def test_load_cfg_defaults_and_override():
    d = Path(tempfile.mkdtemp())
    p = d / "c.yaml"
    p.write_text("human:\n  profile: calm\n  max_session_seconds: 100\n")
    cfg = hs.load_human_cfg(p)
    assert cfg["profile"] == "calm" and cfg["max_session_seconds"] == 100 and cfg["enabled"] is True
    p.write_text("human:\n  profile: weird\n")
    assert hs.load_human_cfg(p)["profile"] == "normal"
    assert hs.load_human_cfg(d / "none.yaml")["min_interval_hours"] == 24