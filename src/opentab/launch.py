"""Shared local resume arguments; presentation layers choose how to run them."""
from __future__ import annotations

import shlex

from opentab.sources import RESUME_COMMANDS


def resume_argv(workflow) -> tuple[str, list[str]] | None:
    command = RESUME_COMMANDS.get(workflow.source)
    directory = workflow.directory
    if (
        not command
        or not directory
        or directory == "(unknown)"
        or not workflow.id
        or "\0" in directory
        or "\0" in workflow.id
    ):
        return None
    return directory, [*shlex.split(command), workflow.id]
