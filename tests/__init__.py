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

# The exporter override otherwise imports real Copilot usage into every synthetic
# backend fixture. Tests that exercise the override set their own temporary file.
os.environ.pop("COPILOT_OTEL_FILE_EXPORTER_PATH", None)

# Ambient multiplexer markers would make terminal tests host-dependent.
for _var in (
    "TMUX",
    "TMUX_PANE",
    "HERDR_ENV",
    "HERDR_BIN_PATH",
    "HERDR_PANE_ID",
    "HERDR_WORKSPACE_ID",
    "OPENTAB_LAUNCHER",
    "OPENTAB_DIFF_PAGER",
):
    os.environ.pop(_var, None)

# argparse reads the terminal width even when help is captured into a StringIO.
# Keep structural help assertions independent of the caller's tmux pane size;
# width-specific tests override COLUMNS explicitly.
os.environ["COLUMNS"] = "80"

# Python 3.14 argparse colors direct format_help() on a TTY, but not help
# captured into StringIO. Keep both plain, including under forced-color shells.
# PYTHON_COLORS takes precedence over NO_COLOR in Python's color detection.
os.environ["NO_COLOR"] = "1"
os.environ["PYTHON_COLORS"] = "0"

import opentab as ot  # noqa: E402  (must follow the sys.path shim and XDG isolation above)

ot.invalidate_price_cache()
