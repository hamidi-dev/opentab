"""Deterministic anonymisation for --demo."""
from __future__ import annotations

import math
import os
import random
import zlib

from opentab.models import Workflow
from opentab.pricing import is_local_provider

# --- Demo categories: what --demo actually scrambles, so a demo can be partial.
# `titles` hides identity (session/prompt/subagent titles, project paths, model and
# machine names); `turns` hides the expandable full prompt text; `spend` hides the
# money and token magnitudes (the hidden scale factor and the synthetic prices that
# backfill unpriced rows). Default is all three -- exactly the original behaviour.
DEMO_CATEGORIES = ("titles", "turns", "spend")
DEMO_ALL = frozenset(DEMO_CATEGORIES)


def parse_demo_cats(spec) -> frozenset:
    # Resolve a --demo value (or a set/list, or a bare on flag) to the category set.
    # None / True / "" / "all" -> everything; "titles,spend" -> that subset; an
    # unknown name is dropped, and an empty result falls back to all (an on-but-nothing
    # demo makes no sense -- the caller expresses "off" with the demo flag itself).
    if spec in (None, True, False, "", "all"):
        return DEMO_ALL
    names = spec if isinstance(spec, (set, frozenset, list, tuple)) else str(spec).split(",")
    picked = frozenset(n.strip().lower() for n in names) & DEMO_ALL
    return picked or DEMO_ALL


def demo_config(args) -> tuple[bool, float, frozenset]:
    # The demo state every store shares: (enabled, hidden magnitude scale, categories).
    # `args.demo` is a bool (tests) or the --demo value carrying the categories. The
    # scale is drawn once per store, and stays identity (1.0) unless spend is scrambled
    # -- so turning spend off shows real dollars and tokens without touching call sites.
    raw = getattr(args, "demo", False)
    enabled = bool(raw)
    cats = parse_demo_cats(raw) if enabled else DEMO_ALL
    scale = _demo_scale() if (enabled and "spend" in cats) else 1.0
    return enabled, scale, cats


def _demo_scale() -> float:
    # The hidden magnitude multiplier. Random per store by default (so token×list-price
    # can't recover real dollars), BUT pinnable with $OPENTAB_DEMO_SCALE to a fixed value
    # so a multi-launch capture (a chaptered video, a set of screenshots) shows ONE
    # consistent scale -- otherwise every launch, and even a --goto vs a plain launch,
    # draws its own factor because each store build consumes the RNG differently. A
    # malformed or non-positive override falls back to the random draw rather than
    # showing real ($0-scale) magnitudes.
    override = os.environ.get("OPENTAB_DEMO_SCALE")
    if override:
        try:
            value = float(override)
            if math.isfinite(value) and value > 0:  # reject inf/nan: they overflow tokens*scale
                return value
        except ValueError:
            pass
    return 3.0 ** random.uniform(-1.0, 1.0)


def scramble_workflow(
    w: Workflow, scale: float, cats: frozenset, *, guard_root: bool = False
) -> Workflow:
    # Apply the selected scrambles to a session row in place, shared by every store so
    # the category gating lives in one spot. `guard_root` keeps OpenCode's rule of only
    # backfilling root_cost when it was $0 (its root is genuinely priced, unlike the
    # all-unpriced backends). With every category on and a random scale this is byte-for-
    # byte the old per-store _demo_workflow.
    if "titles" in cats:
        w.title = demo_title(w.id)
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
    # The subagent-node twin of scramble_workflow, in place. `seed` defaults to the
    # node's own id but can be supplied (the remote export's nodes carry no stable id,
    # so they seed off session id + position). Token fields absent from a given
    # backend's node dict are simply skipped.
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


# --- Demo mode: anonymize titles/paths, backfill synthetic prices for "$0.00 /
# unpriced" gaps, and scale every cost/token by one hidden per-process factor so a
# live demo (or a README screenshot) never leaks real session titles, work repo
# paths, or actual spend -- tokens x list price would otherwise recover the dollars.
# What stays real is the *shape*: relative proportions between sessions/months and
# the model mix (which models, in what ratio). Labels are seeded for stability across
# redraws; the scale factor (Store.demo_scale) is drawn once per run, not seeded.
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
# Blended $/token used to price sessions OpenCode recorded with no cost
# (e.g. credit-based providers). Tuned so a few-million-token session lands in a
# believable single-digit-dollar range.
DEMO_RATE = 1.6e-6
DEMO_MODEL_POOL = (
    "anthropic/claude-opus-4.6",
    "anthropic/claude-sonnet-4.5",
    "openai/gpt-5.5",
    "openai/gpt-5-mini",
    "google/gemini-2.5-pro",
    "anthropic/claude-haiku-4.5",
)
# Fake hostnames for --demo's fleet view. A machine name is a real hostname (it can be
# a work box, a personal handle) so the consolidated view anonymises it exactly like a
# title or a path -- deterministically, so the same box keeps one fake across redraws and
# the grouping stays 1:1 (the whole point of the Machines mode is which box spent what).
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
    jitter = 0.85 + (_seed(seed) % 31) / 100.0  # 0.85 .. 1.15, stable per seed
    return round(max(0.0, float(tokens)) * DEMO_RATE * jitter, 4)


def demo_model(name: str) -> str:
    # Remap local-model names to a stable cloud model; leave cloud models as-is.
    if is_local_provider(name):
        return DEMO_MODEL_POOL[_seed(name) % len(DEMO_MODEL_POOL)]
    return name


def demo_machine(name: str) -> str:
    # Stable fake hostname for a machine label, so --demo's Machines mode/column/tabs
    # never leak a real box name. Deterministic per name; "" (local, untagged) stays "".
    # A machine name, unlike a title or a path, must NOT collide: two real boxes folding
    # onto one fake would merge their spend (distorting the very machine-ratio the demo
    # is meant to keep real) and could even hide the whole Machines view (machines_present
    # needs >=2). A pool of ten names collides for any handful of boxes, so the FULL crc32
    # rides in the suffix -- the whole hash, not a truncation (a truncation would collapse
    # the space, since the pool index is already h % 10). Distinct names then stay distinct
    # unless their crc32 genuinely clashes (~1 in 4.3e9), not merely agree on a few digits.
    if not name:
        return name
    h = _seed(name)
    return f"{DEMO_MACHINES[h % len(DEMO_MACHINES)]}-{h:08x}"
