"""Deterministic anonymisation for --demo."""
from __future__ import annotations

import math
import os
import random
import zlib

from opentab.models import Workflow
from opentab.pricing import is_local_provider

# `titles` hides identity (session/prompt/subagent titles, model and machine names);
# `paths` hides the project directories; `turns` hides the expandable full prompt text;
# `spend` hides the money and token magnitudes. Default is all four. Paths are their own
# scope because a project tree is often the one label a demo WANTS real -- it is what
# makes a screenshot legible -- while the prompts inside it stay private.
DEMO_CATEGORIES = ("titles", "paths", "turns", "spend")
DEMO_ALL = frozenset(DEMO_CATEGORIES)


def parse_demo_cats(spec) -> frozenset:
    if spec in (None, True, False, "", "all"):
        return DEMO_ALL
    names = spec if isinstance(spec, (set, frozenset, list, tuple)) else str(spec).split(",")
    picked = frozenset(n.strip().lower() for n in names) & DEMO_ALL
    return picked or DEMO_ALL


def demo_config(args) -> tuple[bool, float, frozenset]:
    # Keep the scale at identity unless spend anonymisation is enabled.
    raw = getattr(args, "demo", False)
    enabled = bool(raw)
    cats = parse_demo_cats(raw) if enabled else DEMO_ALL
    scale = _demo_scale() if (enabled and "spend" in cats) else 1.0
    return enabled, scale, cats


def _demo_scale() -> float:
    # Random scaling prevents recovering spend from tokens and rates. The environment
    # override keeps multi-launch captures consistent; invalid values remain random.
    override = os.environ.get("OPENTAB_DEMO_SCALE")
    if override:
        try:
            value = float(override)
            if math.isfinite(value) and value > 0:
                return value
        except ValueError:
            pass
    return 3.0 ** random.uniform(-1.0, 1.0)


def scramble_workflow(
    w: Workflow, scale: float, cats: frozenset, *, guard_root: bool = False
) -> Workflow:
    # ``guard_root`` avoids backfilling an already priced OpenCode root.
    if "titles" in cats:
        w.title = demo_title(w.id)
    if "paths" in cats:
        w.directory = demo_dir(w.id)
    if w.unpriced_tokens > 0 and "spend" in cats:
        add = demo_cost(w.unpriced_tokens, w.id)
        w.total_cost += add
        if not guard_root or w.root_cost == 0:
            w.root_cost += add
        w.unpriced_tokens = 0
    w.total_cost = round(w.total_cost * scale, 4)
    w.root_cost = round(w.root_cost * scale, 4)
    w.total_tokens = int(round(w.total_tokens * scale))
    return w


_NODE_TOKEN_FIELDS = (
    "tokens_input",
    "tokens_output",
    "tokens_reasoning",
    "tokens_cache_read",
    "tokens_cache_write",
    "tokens_cache_write_1h",
    "tokens_total",
)


def scramble_node(n: dict, scale: float, cats: frozenset, *, seed: str | None = None) -> dict:
    # Remote export nodes can supply a stable session-and-position seed when they lack ids.
    key = n["id"] if seed is None else seed
    if "titles" in cats:
        n["title"] = demo_title(key)
        if n.get("model_name"):
            n["model_name"] = demo_model(n["model_name"])
    if float(n.get("cost") or 0.0) == 0 and "spend" in cats:
        n["cost"] = demo_cost(n.get("tokens_total") or 0, key)
    n["cost"] = round(float(n.get("cost") or 0.0) * scale, 4)
    for f in _NODE_TOKEN_FIELDS:
        if f in n:
            n[f] = int(round((n.get(f) or 0) * scale))
    return n


# Labels are deterministically anonymised; spend and tokens share one hidden scale so
# relative proportions survive without exposing recoverable magnitudes.
DEMO_VERBS = (
    "refactor",
    "fix",
    "implement",
    "debug",
    "optimize",
    "wire up",
    "rename",
    "document",
    "add tests for",
    "migrate",
    "polish",
    "investigate",
    "scaffold",
    "harden",
    "simplify",
    "profile",
    "rework",
    "ship",
)
DEMO_NOUNS = (
    "the auth middleware",
    "the snapshot harness",
    "the token parser",
    "the retry logic",
    "the config loader",
    "the CLI flags",
    "the cache layer",
    "the export pipeline",
    "the webhook handler",
    "the search index",
    "the rate limiter",
    "the migration script",
    "the date formatter",
    "the error boundary",
    "the settings panel",
    "the upload flow",
    "the pagination bug",
    "the flaky test",
    "the release script",
    "the metrics collector",
)
DEMO_REPOS = (
    "~/code/acme-api",
    "~/code/web-dashboard",
    "~/code/billing-svc",
    "~/code/mobile-app",
    "~/code/data-pipeline",
    "~/code/infra",
    "~/code/notes-app",
    "~/code/cli-tools",
    "~/work/internal-portal",
    "~/work/reporting",
)
# Blended synthetic rate for unpriced demo sessions.
DEMO_RATE = 1.6e-6
DEMO_MODEL_POOL = (
    "anthropic/claude-opus-4.6",
    "anthropic/claude-sonnet-4.5",
    "openai/gpt-5.5",
    "openai/gpt-5-mini",
    "google/gemini-2.5-pro",
    "anthropic/claude-haiku-4.5",
)
# Stable fake hostnames preserve fleet grouping without exposing machine identities.
DEMO_MACHINES = (
    "workstation",
    "laptop",
    "build-server",
    "dev-box",
    "homelab",
    "cloud-vm",
    "render-node",
    "sandbox",
    "the-nas",
    "jump-host",
)


def _seed(value: str) -> int:
    return zlib.crc32(str(value).encode())


def demo_title(seed: str) -> str:
    h = _seed(seed)
    return f"{DEMO_VERBS[h % len(DEMO_VERBS)]} {DEMO_NOUNS[(h // 7) % len(DEMO_NOUNS)]}"


def demo_dir(seed: str) -> str:
    return DEMO_REPOS[_seed(seed) % len(DEMO_REPOS)]


def demo_cost(tokens: float, seed: str) -> float:
    jitter = 0.85 + (_seed(seed) % 31) / 100.0
    return round(max(0.0, float(tokens)) * DEMO_RATE * jitter, 4)


def demo_model(name: str) -> str:
    if is_local_provider(name):
        return DEMO_MODEL_POOL[_seed(name) % len(DEMO_MODEL_POOL)]
    return name


def demo_machine(name: str) -> str:
    # The full checksum suffix prevents fake-name collisions from merging fleet spend.
    if not name:
        return name
    h = _seed(name)
    return f"{DEMO_MACHINES[h % len(DEMO_MACHINES)]}-{h:08x}"
