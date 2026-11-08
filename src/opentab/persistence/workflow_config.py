"""The work-items switch and state vocabulary.

``$XDG_CONFIG_HOME/opentab/workflow.json`` turns work items on; they stay off unless
explicitly enabled, and any doubt about the file (unreadable, malformed, newer) means
off. The file is never rewritten here, so a correction plus reload always recovers.
Session tags are not configured here: their switch is ``tags_enabled`` in state.json.
A ``"tags"`` section left over from older builds is ignored.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass

from opentab.persistence import paths

CONFIG_VERSION = 1
_MAX_BYTES = 64 * 1024

STATE_COLORS = frozenset(("default", "red", "green", "yellow", "blue", "magenta", "cyan", "white"))


@dataclass(frozen=True)
class WorkState:
    id: str
    label: str
    short: str = ""
    color: str = "default"
    done: bool = False


DEFAULT_STATES = tuple(
    WorkState(name.lower(), name, name[:4], color, done)
    for name, color, done in (
        ("TODO", "default", False),
        ("NEXT", "cyan", False),
        ("PROGRESS", "yellow", False),
        ("WAITING", "magenta", False),
        ("REVIEW", "blue", False),
        ("DONE", "green", True),
        ("CANCELLED", "red", True),
    )
)


def _states(section: dict) -> tuple[WorkState, ...]:
    if "states" not in section:
        return DEFAULT_STATES
    entries = section["states"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= 32:
        raise ValueError("work_items.states needs 1–32 definitions")
    result = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("work_items.states definitions must be objects")
        ident, label = entry.get("id"), entry.get("label")
        short, color, done = (
            entry.get("short", ""),
            entry.get("color", "default"),
            entry.get("done", False),
        )
        if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", ident):
            raise ValueError("invalid work state ID")
        for text, limit, required in ((label, 40, True), (short, 12, False)):
            if (
                not isinstance(text, str)
                or len(text) > limit
                or (required and not text.strip())
                or any(unicodedata.category(ch).startswith("C") for ch in text)
            ):
                raise ValueError("invalid work state label")
        if not isinstance(color, str) or color not in STATE_COLORS or not isinstance(done, bool):
            raise ValueError("invalid work state color or done flag")
        if any(state.id == ident for state in result):
            raise ValueError("duplicate work state ID")
        result.append(WorkState(ident, label.strip(), short.strip(), color, done))
    return tuple(result)


@dataclass(frozen=True)
class WorkflowConfig:
    work_items: bool = False
    # Human-readable reason work items stayed off; empty when the file was fine.
    error: str = ""
    states: tuple[WorkState, ...] = ()


def config_path() -> str:
    return os.path.join(paths.config_dir(), "workflow.json")


def _enabled(payload: dict, section: str) -> tuple[bool, str]:
    value = payload.get(section)
    if value is None:
        return False, ""
    if not isinstance(value, dict):
        return False, f"{section} must be an object"
    flag = value.get("enabled", False)
    if not isinstance(flag, bool):
        return False, f"{section}.enabled must be true or false"
    return flag, ""


def load_workflow_config(path: str | None = None) -> WorkflowConfig:
    """Read the switch; any doubt about the file means off, with a reason."""
    path = path or config_path()
    try:
        with open(path, encoding="utf-8") as fh:
            if os.fstat(fh.fileno()).st_size > _MAX_BYTES:
                return WorkflowConfig(error="workflow.json exceeds 64 KiB")
            payload = json.load(fh)
    except FileNotFoundError:
        return WorkflowConfig()
    except (OSError, ValueError) as exc:
        return WorkflowConfig(error=f"cannot read workflow.json: {exc}")
    if not isinstance(payload, dict):
        return WorkflowConfig(error="workflow.json must contain an object")
    version = payload.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        return WorkflowConfig(error='workflow.json needs "version": 1')
    if version > CONFIG_VERSION:
        return WorkflowConfig(error="workflow.json uses a newer format — upgrade OpenTab")
    work_items, items_error = _enabled(payload, "work_items")
    states = ()
    if work_items:
        try:
            states = _states(payload["work_items"])
        except ValueError as exc:
            work_items, items_error = False, str(exc)
    return WorkflowConfig(
        work_items=work_items,
        error=items_error,
        states=states,
    )
