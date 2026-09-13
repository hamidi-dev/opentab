"""Validated, offline release highlights shared by the TUI and web report."""

from __future__ import annotations

import json
import re

RELEASES_URL = "https://github.com/hamidi-dev/opentab/releases"
_STABLE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def stable_version(value) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    match = _STABLE.fullmatch(value)
    if not match:
        return None
    if any(len(part) > 32 for part in match.groups()):
        return None
    try:
        return int(match.group(1)), int(match.group(2)), int(match.group(3))
    except ValueError:
        # Python 3.11+ rejects excessively long decimal strings. State is authored
        # input, so an absurd version must degrade like any other malformed value.
        return None


def _resource_text() -> str:
    from importlib.resources import files

    return files("opentab").joinpath("data").joinpath("whats-new.json").read_text("utf-8")


def _valid_release(release) -> bool:
    if not isinstance(release, dict) or stable_version(release.get("version")) is None:
        return False
    if release.get("release_url") != RELEASES_URL + "/tag/v" + release["version"]:
        return False
    sections = release.get("sections")
    if not isinstance(sections, list) or not sections:
        return False
    seen = set()
    for section in sections:
        if not isinstance(section, dict):
            return False
        title = section.get("title")
        if title not in ("New", "Improved", "Fixed") or title in seen:
            return False
        seen.add(title)
        items = section.get("items")
        if not isinstance(items, list) or not items:
            return False
        for item in items:
            if not isinstance(item, dict):
                return False
            if not isinstance(item.get("text"), str) or not item["text"].strip():
                return False
            if item.get("availability", "both") not in ("both", "tui", "web"):
                return False
            hint = item.get("hint")
            if hint is None:
                continue
            if not isinstance(hint, dict) or not isinstance(hint.get("text"), str):
                return False
            binding = hint.get("binding")
            if binding is not None and (
                not isinstance(binding, dict)
                or not isinstance(binding.get("context"), str)
                or not isinstance(binding.get("action"), str)
            ):
                return False
    return True


def load_release_history(installed_version: str | None = None) -> list[dict] | None:
    """Load validated releases through the installed stable version, newest first."""
    if installed_version is None:
        from opentab import __version__

        installed_version = __version__
    try:
        data = json.loads(_resource_text())
    except Exception:  # noqa: BLE001 -- a missing/broken package resource is non-fatal
        return None
    installed = stable_version(installed_version)
    if not isinstance(data, dict) or installed is None:
        return None
    releases = data.get("releases")
    if not isinstance(releases, list) or not releases:
        return None
    versions = []
    seen_versions = set()
    for release in releases:
        if not _valid_release(release):
            return None
        version = stable_version(release["version"])
        if version is None:
            return None
        if version in seen_versions:
            return None
        versions.append(version)
        seen_versions.add(version)
    if installed not in versions:
        return None
    return [
        release
        for version, release in sorted(zip(versions, releases), reverse=True)
        if version <= installed
    ]


def load_release_notes(installed_version: str | None = None) -> dict | None:
    """Load the bundled note matching the requested installed stable release."""
    history = load_release_history(installed_version)
    return history[0] if history else None


def should_announce(stored_version, installed_version: str, notes: dict | None) -> bool:
    stored = stable_version(stored_version)
    installed = stable_version(installed_version)
    return bool(
        stored
        and installed
        and notes
        and notes.get("version") == installed_version
        and installed > stored
    )


def marker_to_save(disk_value, installed_version: str) -> str:
    """Keep a newer valid marker written by another or downgraded process."""
    disk = stable_version(disk_value)
    installed = stable_version(installed_version)
    if disk and (not installed or disk > installed):
        return str(disk_value)
    return installed_version if installed else (str(disk_value) if disk else "")


def public_payload(installed_version: str) -> dict:
    releases = load_release_history(installed_version)
    if releases is not None:
        return {"version": installed_version, "releases": releases}
    return {
        "version": installed_version,
        "unavailable": True,
        "release_url": RELEASES_URL,
    }
