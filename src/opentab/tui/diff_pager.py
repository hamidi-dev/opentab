"""Safe preparation and terminal handoff for an explicitly configured diff pager."""

from __future__ import annotations

import contextlib
import os
import shlex
import subprocess
import unicodedata
from collections.abc import Mapping

try:
    import curses
except ImportError:  # native Windows has no stdlib curses
    curses = None


ENV_VAR = "OPENTAB_DIFF_PAGER"


class PagerConfigError(ValueError):
    pass


def configured_argv(environ: Mapping[str, str] | None = None) -> list[str] | None:
    """Parse the explicit pager command as argv, never as shell syntax."""
    env = os.environ if environ is None else environ
    if ENV_VAR not in env:
        return None
    raw = env.get(ENV_VAR, "")
    try:
        argv = shlex.split(raw)
    except ValueError as exc:
        raise PagerConfigError("malformed diff pager command") from exc
    if not argv or any("\0" in arg for arg in argv):
        raise PagerConfigError("empty diff pager command")
    return argv


def _terminal_safe(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(
        char if char in "\n\t" or not unicodedata.category(char).startswith("C") else " "
        for char in text
    )


def _path(value: object) -> str:
    return _terminal_safe(str(value or "unknown")).replace("\n", " ").replace("\t", " ")


def prepare_patch(file: Mapping, edit: Mapping, diff: Mapping) -> str:
    """Return inert patch text, adding unified headers to hunk-only records."""
    raw = diff.get("patch")
    if not isinstance(raw, str) or not raw:
        return ""
    patch = _terminal_safe(raw)
    lines = patch.splitlines()
    hunk_only = next((line for line in lines if line.strip()), "").startswith("@@")
    if not hunk_only:
        return patch

    target = _path(file.get("file") or edit.get("file"))
    source = _path(edit.get("from_file") or target)
    status = str(edit.get("status") or file.get("status") or "").lower()
    before = "/dev/null" if status == "added" else f"a/{source}"
    after = "/dev/null" if status == "deleted" else f"b/{target}"
    return f"--- {before}\n+++ {after}\n{patch}"


def run(argv: list[str], patch: str, stdscr, mouse_mask: int = 0) -> tuple[str, int | None]:
    """Run the pager with inherited output and restore curses on every exit path."""
    if curses is None or stdscr is None or not hasattr(stdscr, "refresh"):
        return "unavailable", None

    saved = False
    try:
        curses.def_prog_mode()
        saved = True
        curses.endwin()
        try:
            completed = subprocess.run(
                argv,
                input=patch,
                text=True,
                shell=False,
                check=False,
            )
        except KeyboardInterrupt:
            return "interrupted", None
        except (OSError, ValueError, subprocess.SubprocessError):
            return "spawn-failed", None
        return ("ok" if completed.returncode == 0 else "nonzero", completed.returncode)
    except curses.error:
        return "unavailable", None
    finally:
        if saved:
            with contextlib.suppress(Exception):
                curses.reset_prog_mode()
        with contextlib.suppress(Exception):
            curses.curs_set(0)
        with contextlib.suppress(Exception):
            curses.mousemask(mouse_mask)
        with contextlib.suppress(Exception):
            stdscr.clearok(True)
        with contextlib.suppress(Exception):
            stdscr.refresh()
