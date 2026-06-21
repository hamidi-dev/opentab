"""Deterministic anonymisation for --demo."""
from __future__ import annotations

import zlib

from opentab.pricing import is_local_provider

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
