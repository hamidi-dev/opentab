"""Lazy public surface for browser reports and the self-contained page."""

from __future__ import annotations

_LAZY_ATTRS = {
    "DEFAULT_BIND": "opentab.web.report",
    "DEFAULT_PORT": "opentab.web.report",
    "DEFAULT_REPORT": "opentab.web.report",
    "ReportServer": "opentab.web.report",
    "build_payload": "opentab.web.report",
    "html_command": "opentab.web.report",
    "open_report": "opentab.web.report",
    "render_html": "opentab.web.page",
    "serve_command": "opentab.web.report",
    "session_extras": "opentab.web.report",
}
_LAZY_MODULES = {
    "page": "opentab.web.page",
    "report": "opentab.web.report",
}


def __getattr__(name: str):
    import importlib

    module = _LAZY_MODULES.get(name) or _LAZY_ATTRS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    imported = importlib.import_module(module)
    value = imported if name in _LAZY_MODULES else getattr(imported, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY_ATTRS, *_LAZY_MODULES})


__all__ = sorted({*_LAZY_ATTRS, *_LAZY_MODULES})
