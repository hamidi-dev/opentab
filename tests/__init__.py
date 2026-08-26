"""Isolate tests before importing the src-layout package."""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

# Defaults and price caches resolve at import time, so isolate every XDG root first.
_ISOLATED_HOME = tempfile.TemporaryDirectory(prefix="opentab-test-home-")
for _var, _sub in (
    ("XDG_CONFIG_HOME", "config"),
    ("XDG_STATE_HOME", "state"),
    ("XDG_DATA_HOME", "data"),
    ("XDG_CACHE_HOME", "cache"),
):
    os.environ[_var] = os.path.join(_ISOLATED_HOME.name, _sub)

# Ambient multiplexer markers would make terminal tests host-dependent.
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
