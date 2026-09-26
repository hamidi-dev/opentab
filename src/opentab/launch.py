"""Shared local resume arguments; presentation layers choose how to run them."""
from __future__ import annotations

import shlex

from opentab.sources import RESUME_COMMANDS


def resume_argv(workflow) -> tuple[str, list[str]] | None:
    """Return directory and argv; an empty directory means the launch host's home."""
    command = RESUME_COMMANDS.get(workflow.source)
    directory = workflow.directory
    # Gateway/older Hermes sessions often have no cwd. Hermes resolves --resume
    # from its own session database, independent of the original project path.
    # Leave home unresolved here: a remote session must use the REMOTE home.
    if workflow.source == "Hermes" and directory in ("", "(unknown)"):
        directory = ""
    elif not directory or directory == "(unknown)":
        return None
    if not command or not workflow.id or "\0" in directory or "\0" in workflow.id:
        return None
    return directory, [*shlex.split(command), workflow.id]
