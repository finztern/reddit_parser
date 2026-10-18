from scraper.sub_rotation import pick_subs

SUBS = [f"s{i}" for i in range(20)]


def test_disabled_returns_all():
    assert pick_subs(SUBS, 0, "a", 0) == SUBS
    assert pick_subs(SUBS, 20, "a", 5) == SUBS
    assert pick_subs(["all"], 3, "a", 1) == ["all"]


def test_window_size_and_determinism():
    w = pick_subs(SUBS, 3, "acc1", 4)
    assert len(w) == 3 and len(set(w)) == 3
    assert w == pick_subs(list(reversed(SUBS)), 3, "acc1", 4)


def test_accounts_differ_and_window_moves():
    assert pick_subs(SUBS, 3, "acc1", 0) != pick_subs(SUBS, 3, "acc2", 0)
    assert pick_subs(SUBS, 3, "acc1", 0) != pick_subs(SUBS, 3, "acc1", 1)


def test_full_cycle_covers_all():
    seen = set()
    for i in range(7):  # ceil(20/3)
        seen |= set(pick_subs(SUBS, 3, "acc1", i))
    assert seen == set(SUBS)
