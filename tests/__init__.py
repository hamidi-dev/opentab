"""Test package for opentab.

Importing *any* test module runs this first, which is what the suite relies on:
opentab is a src-layout package, so src/ goes on sys.path here (no editable
install needed), and the config isolation below is guaranteed to be in place
before a single test — or `pytest tests/test_pricing.py` on its own — imports it.
"""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

# Isolate the whole suite from the developer's real home: point *every* XDG base dir
# opentab writes into (config/state/data/cache — see opentab.paths) at an empty temp
# dir, so model_price() reads the *embedded* price table (not a local models.dev cache a
# `r`/--refresh-models run may have written) and no test reads or writes the real
# prefs/notes/state/cache. Without this, the price assertions pass on CI (no cache) but
# fail on a machine that has refreshed prices. Distinct subdirs (not one shared root) so
# the suite exercises the real split. This must run *before* importing opentab: some
# defaults (sources.DEFAULT_CSV_PATH/JSONL) are resolved at import time and would
# otherwise capture the developer's real ~/.local/share. The dir lives for the process;
# the held TemporaryDirectory cleans it up at exit.
_ISOLATED_HOME = tempfile.TemporaryDirectory(prefix="opentab-test-home-")
for _var, _sub in (
    ("XDG_CONFIG_HOME", "config"),
    ("XDG_STATE_HOME", "state"),
    ("XDG_DATA_HOME", "data"),
    ("XDG_CACHE_HOME", "cache"),
):
    os.environ[_var] = os.path.join(_ISOLATED_HOME.name, _sub)

# Multiplexer markers describe the developer's terminal, not the isolated test process.
for _var in (
    "TMUX",
    "TMUX_PANE",
    "HERDR_ENV",
    "HERDR_BIN_PATH",
    "HERDR_PANE_ID",
    "HERDR_WORKSPACE_ID",
    "OPENTAB_LAUNCHER",
):
    os.environ.pop(_var, None)

import opentab as ot  # noqa: E402  (must follow the sys.path shim and XDG isolation above)

ot.invalidate_price_cache()
