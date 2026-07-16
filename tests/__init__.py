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

import opentab as ot  # noqa: E402  (must follow the sys.path shim above)

# Isolate the whole suite from the developer's real ~/.config: point XDG at an
# empty temp dir so model_price() reads the *embedded* price table (not a local
# models.dev cache a `r`/--refresh-models run may have written) and no test reads
# or writes the real prefs/cache. Without this, the price assertions pass on
# CI (no cache) but fail on a machine that has refreshed prices. The dir lives for
# the process; the held TemporaryDirectory cleans it up at exit.
_ISOLATED_CONFIG = tempfile.TemporaryDirectory(prefix="opentab-test-config-")
os.environ["XDG_CONFIG_HOME"] = _ISOLATED_CONFIG.name
ot.invalidate_price_cache()
