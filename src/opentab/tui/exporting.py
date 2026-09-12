"""TUI CSV datasets from selected, price-projected rows, plus safe serialization."""
from __future__ import annotations

import csv
from collections import defaultdict

from opentab.models import HarnessSummary, MachineSummary, ProjectSummary, Workflow
from opentab.pricing import family_label


def sessions_dataset(
    sessions: list[Workflow], notes: dict[str, str]
) -> tuple[str, list[str], list[list]]:
    header = [
        "id",
        "created_at",
        "title",
        "directory",
        "total_cost",
        "root_cost",
        "subagent_cost",
        "subagents",
        "models",
        "total_tokens",
        "unpriced_tokens",
        "note",
    ]
    rows = [
        [
            workflow.id,
            workflow.created_at,
            workflow.title,
            workflow.directory,
            workflow.total_cost,
            workflow.root_cost,
            round(workflow.total_cost - workflow.root_cost, 6),
            workflow.subagents,
            workflow.model_count,
            workflow.total_tokens,
            workflow.unpriced_tokens,
            notes.get(workflow.id, ""),
        ]
        for workflow in sessions
    ]
    return "sessions", header, rows


def model_sessions_dataset(
    sessions: list[tuple[Workflow, dict]], model: str
) -> tuple[str, list[str], list[list]]:
    header = [
        "id",
        "created_at",
        "title",
        "directory",
        "model",
        "model_list_cost",
        "model_cost_estimated",
        "model_tokens",
        "model_messages",
        "session_cost",
        "session_tokens",
    ]
    rows = [
        [
            workflow.id,
            workflow.created_at,
            workflow.title,
            workflow.directory,
            model,
            usage["list_cost"],
            usage["estimated"],
            usage["tokens"],
            usage["runs"],
            workflow.total_cost,
            workflow.total_tokens,
        ]
        for workflow, usage in sessions
    ]
    return "model-sessions", header, rows


def projects_dataset(projects: list[ProjectSummary]) -> tuple[str, list[str], list[list]]:
    header = ["directory", "cost", "tokens", "sessions", "subagents", "unpriced_tokens"]
    rows = [
        [
            project.directory,
            project.cost,
            project.tokens,
            project.workflows,
            project.subagents,
            project.unpriced_tokens,
        ]
        for project in projects
    ]
    return "projects", header, rows


def machines_dataset(machines: list[MachineSummary]) -> tuple[str, list[str], list[list]]:
    # Free-text names cannot identify the synthetic fleet total.
    header = [
        "machine",
        "live",
        "cost",
        "tokens",
        "sessions",
        "subagents",
        "exported_at",
        "fleet",
    ]
    rows = [
        [
            machine.name,
            machine.live,
            machine.cost,
            machine.tokens,
            machine.workflows,
            machine.subagents,
            machine.exported_at,
            machine.fleet,
        ]
        for machine in machines
    ]
    return "machines", header, rows


def harnesses_dataset(
    harnesses: list[HarnessSummary],
) -> tuple[str, list[str], list[list]]:
    header = ["harness", "cost", "tokens", "sessions", "subagents", "aggregate"]
    rows = [
        [
            harness.name,
            harness.cost,
            harness.tokens,
            harness.workflows,
            harness.subagents,
            harness.aggregate,
        ]
        for harness in harnesses
    ]
    return "harnesses", header, rows


def periods_dataset(scope: str, label: str, items: list) -> tuple[str, list[str], list[list]]:
    header = [label, "cost", "tokens", "sessions", "subagents", "unpriced_tokens"]
    rows = [
        [
            getattr(item, label),
            item.cost,
            item.tokens,
            item.workflows,
            item.subagents,
            item.unpriced_tokens,
        ]
        for item in items
    ]
    return scope, header, rows


def prices_dataset(entries: list) -> tuple[str, list[str], list[list]]:
    header = [
        "model",
        "family",
        "routes",
        "pinned",
        "share",
        "eff_usd_per_mtok",
        "eff_approx",
        "input",
        "output",
        "cache_read",
        "cache_write",
    ]
    rows = [
        [
            entry.bare,
            family_label(entry.family),
            " ".join(entry.routes),
            entry.pinned,
            round(entry.share, 4),
            round(entry.eff, 4),
            entry.approx,
            *entry.price,
        ]
        for entry in entries
    ]
    return "prices", header, rows


def sources_dataset(workflows: list[Workflow]) -> tuple[str, list[str], list[list]]:
    by_source: dict[str, dict[str, float | int]] = defaultdict(
        lambda: {"cost": 0.0, "tokens": 0, "sessions": 0}
    )
    for workflow in workflows:
        item = by_source[workflow.source or "unknown"]
        item["cost"] = float(item["cost"]) + workflow.total_cost
        item["tokens"] = int(item["tokens"]) + workflow.total_tokens
        item["sessions"] = int(item["sessions"]) + 1
    rows = sorted(
        by_source.items(),
        key=lambda item: (float(item[1]["cost"]), int(item[1]["tokens"])),
        reverse=True,
    )
    header = ["source", "cost", "tokens", "sessions"]
    return (
        "sources",
        header,
        [[source, item["cost"], item["tokens"], item["sessions"]] for source, item in rows],
    )


def machine_agg_dataset(rows: list[tuple[str, dict]]) -> tuple[str, list[str], list[list]]:
    header = ["machine", "cost", "tokens", "sessions"]
    return (
        "machines",
        header,
        [[machine, item["cost"], item["tokens"], item["sessions"]] for machine, item in rows],
    )


def models_dataset(rows: list[tuple[str, dict]]) -> tuple[str, list[str], list[list]]:
    header = ["model", "runs", "cost", "tokens", "cache_read", "cache_write", "output"]
    out = []
    for name, item in rows:
        tokens_total = item["tokens"] if "tokens" in item else item["tokens_total"]
        out.append(
            [
                name,
                item["runs"],
                item["cost"],
                tokens_total,
                item["cache_read"],
                item["cache_write"],
                item["output"],
            ]
        )
    return "models", header, out


def subagents_dataset(rows: list[dict]) -> tuple[str, list[str], list[list]]:
    header = ["date", "depth", "agent", "model", "cost", "tokens", "title"]
    return (
        "subagents",
        header,
        [
            [
                row.get("created_at", ""),
                row["depth"],
                row["agent"],
                row["model_name"],
                row["cost"],
                row["tokens_total"],
                row["title"],
            ]
            for row in rows
        ],
    )


def turns_dataset(rows: list[dict]) -> tuple[str, list[str], list[list]]:
    header = [
        "time",
        "agent",
        "depth",
        "model",
        "cost",
        "tokens",
        "input",
        "output",
        "cache_read",
        "cache_write",
        "prompt",
    ]
    return (
        "turns",
        header,
        [
            [
                row["time"],
                row["agent"] if row["depth"] else "-",
                row["depth"],
                row["model_name"],
                row["cost"],
                row["tokens_total"],
                row["input"],
                row["output"],
                row["cache_read"],
                row["cache_write"],
                (row.get("prompt_title") or "").strip(),
            ]
            for row in rows
        ],
    )


def tools_dataset(rows: list[dict]) -> tuple[str, list[str], list[list]]:
    header = [
        "tool",
        "model",
        "calls",
        "cost",
        "tokens",
        "input",
        "output",
        "cache_read",
        "cache_write",
    ]
    return (
        "tools",
        header,
        [
            [
                row["tool"],
                row["model_name"],
                row["calls"],
                row["cost"],
                row["tokens_total"],
                row["input"],
                row["output"],
                row["cache_read"],
                row["cache_write"],
            ]
            for row in rows
        ],
    )


def csv_safe(value):
    # Neutralize formula-prefixed text while preserving numeric strings and values.
    if not isinstance(value, str) or not value or value[0] not in "=+-@\t\r":
        return value
    try:
        float(value)
        return value
    except ValueError:
        return "'" + value


def write_csv(path: str, header: list[str], rows: list[list]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(header)
        writer.writerows([[csv_safe(cell) for cell in row] for row in rows])
