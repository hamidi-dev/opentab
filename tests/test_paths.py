"""XDG base-dir resolution and the config->state/data migration (opentab.paths)."""

import os
import tempfile

import opentab as ot
from opentab import paths, sources


def _set(**env):
    # Point the given XDG_* vars at test values; return a restore() to undo it.
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)

    def restore():
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    return restore


def test_each_root_honors_its_env_then_falls_back_to_the_spec_default():
    restore = _set(
        XDG_CONFIG_HOME="/x/cfg",
        XDG_STATE_HOME="/x/st",
        XDG_DATA_HOME="/x/dat",
        XDG_CACHE_HOME="/x/ch",
    )
    try:
        assert paths.config_dir() == "/x/cfg/opentab"
        assert paths.state_dir() == "/x/st/opentab"
        assert paths.data_dir() == "/x/dat/opentab"
        assert paths.cache_dir() == "/x/ch/opentab"
    finally:
        restore()
    # Empty (or unset) -> the spec defaults under $HOME, each a distinct base dir.
    restore = _set(XDG_CONFIG_HOME="", XDG_STATE_HOME="", XDG_DATA_HOME="", XDG_CACHE_HOME="")
    try:
        home = os.path.expanduser("~")
        assert paths.config_dir() == os.path.join(home, ".config", "opentab")
        assert paths.state_dir() == os.path.join(home, ".local", "state", "opentab")
        assert paths.data_dir() == os.path.join(home, ".local", "share", "opentab")
        assert paths.cache_dir() == os.path.join(home, ".cache", "opentab")
    finally:
        restore()


def test_a_relative_xdg_value_is_ignored_per_the_spec():
    # The XDG spec: a non-absolute $XDG_*_HOME "should be considered invalid and
    # ignored" -- never resolved against the CWD (which would write into the repo).
    home = os.path.expanduser("~")
    restore = _set(XDG_STATE_HOME="relative-not-absolute", XDG_CACHE_HOME=".cache")
    try:
        assert paths.state_dir() == os.path.join(home, ".local", "state", "opentab")
        assert paths.cache_dir() == os.path.join(home, ".cache", "opentab")
    finally:
        restore()


def test_cross_device_migration_copies_atomically_and_leaves_no_temp():
    # When os.replace can't rename across filesystems, migration falls back to an atomic
    # copy: the destination ends up complete and no half-written temp is left behind.
    with tempfile.TemporaryDirectory() as home:
        cfg, state = os.path.join(home, "config"), os.path.join(home, "state")
        os.makedirs(os.path.join(cfg, "opentab"))
        legacy = os.path.join(cfg, "opentab", "state.json")
        with open(legacy, "w") as fh:
            fh.write('{"range": "7d"}')
        restore = _set(XDG_CONFIG_HOME=cfg, XDG_STATE_HOME=state)
        real_replace = os.replace

        def fake_replace(src, dst):  # only the legacy->new rename "crosses devices"
            if os.path.abspath(src) == os.path.abspath(legacy):
                raise OSError(18, "Invalid cross-device link")
            return real_replace(src, dst)

        paths.os.replace = fake_replace
        try:
            p = ot.state_path()
            assert p == os.path.join(state, "opentab", "state.json")
            with open(p) as fh:
                assert '"7d"' in fh.read()  # content copied intact
            leftovers = [n for n in os.listdir(os.path.dirname(p)) if n.startswith(".migrate-")]
            assert leftovers == []  # the atomic-copy temp was cleaned up
        finally:
            paths.os.replace = real_replace
            restore()


def test_atomic_copy_refuses_to_clobber_an_existing_destination():
    # The os.link publish is exclusive: if a racing migrator (or a note authored right
    # after the winner published) already created dst, the copy is dropped, not
    # overwritten -- so a slow cross-device migration can't lose a just-authored note.
    with tempfile.TemporaryDirectory() as d:
        src, dst = os.path.join(d, "legacy.json"), os.path.join(d, "current.json")
        with open(src, "w") as fh:
            fh.write("stale-legacy-snapshot")
        with open(dst, "w") as fh:
            fh.write("authored-after-migration")
        paths._atomic_copy(src, dst)
        with open(dst) as fh:
            assert fh.read() == "authored-after-migration"  # winner's content untouched
        assert [n for n in os.listdir(d) if n.startswith(".migrate-")] == []  # temp cleaned


def test_state_json_migrates_out_of_the_old_config_dir_on_read():
    # A pre-split user's state.json sits in the config dir; the first state_path() call
    # relocates it to the XDG state dir, moving (not copying) the file, and is a no-op
    # afterwards. This is what stops an upgrade from silently resetting saved prefs.
    with tempfile.TemporaryDirectory() as home:
        cfg, state = os.path.join(home, "config"), os.path.join(home, "state")
        os.makedirs(os.path.join(cfg, "opentab"))
        legacy = os.path.join(cfg, "opentab", "state.json")
        with open(legacy, "w") as fh:
            fh.write('{"range": "30d"}')
        restore = _set(XDG_CONFIG_HOME=cfg, XDG_STATE_HOME=state)
        try:
            p = ot.state_path()
            assert p == os.path.join(state, "opentab", "state.json")
            assert os.path.exists(p) and not os.path.exists(legacy)  # moved, not copied
            assert ot.load_state() == {"range": "30d"}
            assert ot.state_path() == p  # idempotent: nothing left to migrate
        finally:
            restore()


def test_notes_json_migrates_out_of_the_old_config_dir_on_read():
    # The authored notes file is the one thing a lost migration would truly cost; it
    # moves from config to the XDG data dir the same way.
    with tempfile.TemporaryDirectory() as home:
        cfg, data = os.path.join(home, "config"), os.path.join(home, "data")
        os.makedirs(os.path.join(cfg, "opentab"))
        legacy = os.path.join(cfg, "opentab", "notes.json")
        with open(legacy, "w") as fh:
            fh.write('{"version": 1, "notes": {"s1": "kept across the upgrade"}}')
        restore = _set(XDG_CONFIG_HOME=cfg, XDG_DATA_HOME=data)
        try:
            p = ot.notes_path()
            assert p == os.path.join(data, "opentab", "notes.json")
            assert not os.path.exists(legacy)
            assert ot.load_notes() == {"s1": "kept across the upgrade"}
        finally:
            restore()


def test_migration_never_clobbers_an_existing_new_file():
    # If both locations hold a file (e.g. a downgrade-then-upgrade), the new one is
    # authoritative and the stale legacy copy is left untouched, never merged in.
    with tempfile.TemporaryDirectory() as home:
        cfg, state = os.path.join(home, "config"), os.path.join(home, "state")
        os.makedirs(os.path.join(cfg, "opentab"))
        os.makedirs(os.path.join(state, "opentab"))
        legacy = os.path.join(cfg, "opentab", "state.json")
        with open(legacy, "w") as fh:
            fh.write('{"range": "legacy"}')
        with open(os.path.join(state, "opentab", "state.json"), "w") as fh:
            fh.write('{"range": "current"}')
        restore = _set(XDG_CONFIG_HOME=cfg, XDG_STATE_HOME=state)
        try:
            assert ot.load_state() == {"range": "current"}
            assert os.path.exists(legacy)  # left in place, not consumed
        finally:
            restore()


def test_legacy_caches_move_to_the_cache_dir_when_the_new_location_is_empty():
    # A fresh upgrade: the regenerable caches (warm-start cache/, prices.json, pulled
    # remotes/) move out of config into the XDG cache dir, preserving their contents, and
    # the stale notes.json.lock is swept.
    with tempfile.TemporaryDirectory() as home:
        cfg, cache = os.path.join(home, "config"), os.path.join(home, "cache")
        os.makedirs(os.path.join(cfg, "opentab", "cache"))
        os.makedirs(os.path.join(cfg, "opentab", "remotes"))
        with open(os.path.join(cfg, "opentab", "cache", "opencode-x.json"), "w") as fh:
            fh.write("warm")
        with open(os.path.join(cfg, "opentab", "prices.json"), "w") as fh:
            fh.write("prices")
        with open(os.path.join(cfg, "opentab", "remotes", "box.json"), "w") as fh:
            fh.write("summary")
        open(os.path.join(cfg, "opentab", "notes.json.lock"), "w").close()
        restore = _set(XDG_CONFIG_HOME=cfg, XDG_CACHE_HOME=cache)
        try:
            paths.migrate_legacy_caches()
            base = os.path.join(cache, "opentab")
            assert open(os.path.join(base, "cache", "opencode-x.json")).read() == "warm"
            assert open(os.path.join(base, "prices.json")).read() == "prices"
            assert open(os.path.join(base, "remotes", "box.json")).read() == "summary"
            # Nothing regenerable is left cluttering config, lock included.
            leftover = os.listdir(os.path.join(cfg, "opentab"))
            assert leftover == [] or leftover == [".DS_Store"]
        finally:
            restore()


def test_legacy_caches_are_dropped_when_the_new_location_already_exists():
    # A machine that already ran the new version owns the cache; the pre-split copy is a
    # stale orphan and is removed rather than moved over the live one.
    with tempfile.TemporaryDirectory() as home:
        cfg, cache = os.path.join(home, "config"), os.path.join(home, "cache")
        os.makedirs(os.path.join(cfg, "opentab", "cache"))
        with open(os.path.join(cfg, "opentab", "cache", "old.json"), "w") as fh:
            fh.write("stale")
        os.makedirs(os.path.join(cache, "opentab", "cache"))
        with open(os.path.join(cache, "opentab", "cache", "live.json"), "w") as fh:
            fh.write("fresh")
        restore = _set(XDG_CONFIG_HOME=cfg, XDG_CACHE_HOME=cache)
        try:
            paths.migrate_legacy_caches()
            assert not os.path.exists(os.path.join(cfg, "opentab", "cache"))  # orphan gone
            live = os.path.join(cache, "opentab", "cache")
            assert os.listdir(live) == ["live.json"]  # the live cache is untouched
        finally:
            restore()


def test_a_superseded_state_json_orphan_is_swept_from_config():
    # An old opentab run during the upgrade window can rewrite state.json into config after
    # the real one already moved to the state dir; that orphan is cleaned, the state-dir
    # copy left authoritative.
    with tempfile.TemporaryDirectory() as home:
        cfg, cache, state = (os.path.join(home, x) for x in ("config", "cache", "state"))
        os.makedirs(os.path.join(cfg, "opentab"))
        os.makedirs(os.path.join(state, "opentab"))
        with open(os.path.join(cfg, "opentab", "state.json"), "w") as fh:
            fh.write('{"range": "orphan"}')
        with open(os.path.join(state, "opentab", "state.json"), "w") as fh:
            fh.write('{"range": "authoritative"}')
        restore = _set(XDG_CONFIG_HOME=cfg, XDG_CACHE_HOME=cache, XDG_STATE_HOME=state)
        try:
            paths.migrate_legacy_caches()
            assert not os.path.exists(os.path.join(cfg, "opentab", "state.json"))  # swept
            assert ot.load_state() == {"range": "authoritative"}  # state dir wins
        finally:
            restore()


def test_an_unmigrated_state_json_is_never_deleted_by_the_cache_tidy():
    # With no state-dir copy yet the config state.json IS the real data; the tidy must not
    # delete it -- migrated() moves it lazily on first read instead.
    with tempfile.TemporaryDirectory() as home:
        cfg, cache, state = (os.path.join(home, x) for x in ("config", "cache", "state"))
        os.makedirs(os.path.join(cfg, "opentab"))
        with open(os.path.join(cfg, "opentab", "state.json"), "w") as fh:
            fh.write('{"range": "the only copy"}')
        restore = _set(XDG_CONFIG_HOME=cfg, XDG_CACHE_HOME=cache, XDG_STATE_HOME=state)
        try:
            paths.migrate_legacy_caches()
            assert os.path.exists(os.path.join(cfg, "opentab", "state.json"))  # preserved
            assert ot.state_path() == os.path.join(state, "opentab", "state.json")  # then moved
            assert not os.path.exists(os.path.join(cfg, "opentab", "state.json"))
        finally:
            restore()


def test_requests_default_prefers_data_but_still_finds_a_legacy_config_file():
    # requests.csv/jsonl auto-discovery: the canonical home is the XDG data dir, but a
    # file a pre-split user left in the config dir keeps being discovered.
    with tempfile.TemporaryDirectory() as home:
        cfg, data = os.path.join(home, "config"), os.path.join(home, "data")
        restore = _set(XDG_CONFIG_HOME=cfg, XDG_DATA_HOME=data)
        try:
            in_data = os.path.join(data, "opentab", "requests.csv")
            in_cfg = os.path.join(cfg, "opentab", "requests.csv")
            assert sources._default_requests_path("requests.csv") == in_data  # nothing on disk
            os.makedirs(os.path.join(cfg, "opentab"))
            open(in_cfg, "w").close()
            assert sources._default_requests_path("requests.csv") == in_cfg  # legacy discovered
            os.makedirs(os.path.join(data, "opentab"))
            open(in_data, "w").close()
            assert sources._default_requests_path("requests.csv") == in_data  # data wins once present
        finally:
            restore()
