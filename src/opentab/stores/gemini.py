"""Gemini CLI chat-recording backend."""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import math
import os
import re
import sys
from typing import NamedTuple

from opentab.demo import demo_config, scramble_node, scramble_workflow
from opentab.formatting import _clean_prompt, iso_to_epoch, iso_to_local, worked_seconds
from opentab.models import Workflow
from opentab.util import (
    LazyStatusRoot,
    git_root,
    read_files_parallel,
    safe_int,
    tool_rows_from_turns,
)

# Gemini CLI resolves its home through $GEMINI_CLI_HOME (which replaces the HOME
# directory, not the .gemini directory inside it) and keeps everything under
# <home>/.gemini.
PROJECT_ROOT_FILE = ".project_root"
REGISTRY_FILE = "projects.json"
SETTINGS_FILE = "settings.json"

# general.sessionRetention's schema defaults. Gemini's getDefaultsFromSchema() recurses
# into `properties`, so these apply even though the object itself defaults to undefined.
GEMINI_RETENTION_DEFAULT_MAX_AGE = "30d"
GEMINI_RETENTION_DEFAULT_DAYS = 30
GEMINI_RETENTION_MIN_RETENTION = "1d"
GEMINI_RETENTION_MIN_MAX_COUNT = 1
# No "keep forever" value exists, so the fix is enabled:false; this only decides whether
# an explicit long maxAge is long enough to stop warning about.
GEMINI_RETENTION_RECOMMENDED_DAYS = 3650
GEMINI_RETENTION_WARNING_ID = "gemini-retention-v1"
# Gemini's own parseRetentionPeriod units: <number><h|d|w|m>, "m" being 30 days.
_RETENTION_UNIT_DAYS = {"h": 1.0 / 24.0, "d": 1.0, "w": 7.0, "m": 30.0}
# resolveEnvVarsInString's regex: only a value matching THIS is rewritten before parsing,
# so only this makes a literal unevaluable here. A bare "$" is not a substitution.
_ENV_VAR_RE = re.compile(r"\$(?:(\w+)|\{([^}]+?)(?::-([^}]*))?\})")
# String.prototype.trim's whitespace set, which is not str.strip()'s: JS leaves the
# C0 separators (U+001C..U+001F) in place, and a count Python trims to a number is one
# Gemini rejects -- taking the whole layer's coercion down with it.
_JS_WHITESPACE = "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_MISSING = object()


def default_gemini_dir() -> str:
    home = (os.environ.get("GEMINI_CLI_HOME") or "").strip() or os.path.expanduser("~")
    return os.path.join(home, ".gemini")


def gemini_settings_path() -> str:
    """Gemini CLI's user-level settings.json (its ``Storage.getGlobalSettingsPath``)."""
    return os.path.join(default_gemini_dir(), SETTINGS_FILE)


def gemini_system_settings_path() -> str:
    """Gemini's ``getSystemSettingsPath`` -- the layer that OVERRIDES the user's."""
    override = (os.environ.get("GEMINI_CLI_SYSTEM_SETTINGS_PATH") or "").strip()
    if override:
        return override
    if sys.platform == "darwin":
        return "/Library/Application Support/GeminiCli/settings.json"
    if os.name == "nt":
        return "C:\\ProgramData\\gemini-cli\\settings.json"
    return "/etc/gemini-cli/settings.json"


def gemini_system_defaults_path() -> str:
    """Gemini's ``getSystemDefaultsPath`` -- a layer BELOW the user's."""
    override = (os.environ.get("GEMINI_CLI_SYSTEM_DEFAULTS_PATH") or "").strip()
    if override:
        return override
    return os.path.join(os.path.dirname(gemini_system_settings_path()), "system-defaults.json")


def _strip_json_comments(text: str) -> str:
    """Drop // and /* */ comments outside strings, as Gemini's loader does.

    Gemini runs `strip-json-comments` before `JSON.parse`, so a commented settings file
    is valid to it. Only tried after a strict parse fails, so a bug here can only affect
    a file `json.loads` already rejected.
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            out.append(" ")
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            # A SPACE, not nothing: strip-json-comments blanks a comment out, so
            # `1/*x*/0` stays two tokens there and must not become 10 here.
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            out.append(" ")
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _js_truthy(value: object) -> bool:
    """JavaScript truthiness for a JSON value -- `[]` and `{}` are TRUE in JS."""
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return value != ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    return True


def _zod_boolean(value: object) -> bool | None:
    """Gemini's boolean preprocess, or None when the value survives it unvalidated.

    A string spelling "true"/"false" is coerced; anything else fails validation, which
    is only a WARNING -- the raw value is kept and the gate then reads it for JS
    truthiness.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lower = value.lower()
        if lower == "true":
            return True
        if lower == "false":
            return False
    return None


# [0-9], never \d: Python's \d also matches e.g. Arabic-Indic digits, which are NaN
# to JavaScript -- and a count that reads as 0 here but NaN there flips a live 30-day
# policy into a rejected one.
_JS_DECIMAL_RE = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_JS_RADIX_RE = re.compile(r"0[xX][0-9a-fA-F]+|0[oO][0-7]+|0[bB][01]+")


def _js_number(text: str) -> float | None:
    """JavaScript ``Number(str)``, which is not ``float(str)``.

    It takes 0x/0o/0b literals and rejects Python's "nan", "infinity" spelling and
    digit underscores -- so `maxCount: "0x10"` is a live cap of 16 in Gemini while
    ``float`` raises, and `"1_0"` is NaN there while ``float`` reads ten.
    """
    body = text.strip(_JS_WHITESPACE)
    if not body:
        return 0.0
    if body in ("Infinity", "+Infinity"):
        return math.inf
    if body == "-Infinity":
        return -math.inf
    try:
        if _JS_RADIX_RE.fullmatch(body):
            return float(int(body, 0))
        if _JS_DECIMAL_RE.fullmatch(body):
            return float(body)
    except (ValueError, OverflowError):
        # A literal too large for a float is Infinity in JS, never an exception.
        return math.inf
    return None


def _zod_number(value: object) -> float | None:
    """Gemini's number preprocess: a numeric string becomes a number, so "50" is 50."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip(_JS_WHITESPACE):
        return _js_number(value)
    return None


def _parse_retention_period(period: object) -> float | None:
    """Days in a Gemini retention string, or None for one Gemini itself rejects."""
    if not isinstance(period, str):
        return None
    # No .strip(): Gemini's regex is /^(\d+)([dhwm])$/, so " 7d " throws there, and
    # reading it as 7 days would make a 1-day maxAge look like a floor violation.
    match = re.fullmatch(r"([0-9]+)([dhwm])", period)
    if not match:
        return None
    digits = match.group(1).lstrip("0")
    if not digits:  # all zeros: parseRetentionPeriod rejects zero outright
        return None
    try:
        return int(digits) * _RETENTION_UNIT_DAYS[match.group(2)]
    except (ValueError, OverflowError):
        # Past CPython's int-from-string limit or past float range: both are ordinary
        # large numbers to JS, so clamp rather than raise. The zeros are stripped FIRST,
        # because a 5,000-zero period is zero there, not infinity -- and clamping it
        # would turn a rejected floor (which leaves the 30-day default deleting) into a
        # verdict that nothing is deleted at all.
        return math.inf


class GeminiRetention(NamedTuple):
    settings_path: str  # the layer that decided, i.e. the file to edit
    max_age: str | None  # as written, e.g. "30d"
    max_age_days: float | None
    max_count: float | None
    # default / configured / unverifiable / unknown / workspace delete (or may);
    # off / inert do not.
    source: str

    @property
    def deletes(self) -> bool:
        return self.source not in ("off", "inert")

    @property
    def needs_warning(self) -> bool:
        if not self.deletes:
            return False
        # Any count cap deletes the oldest sessions; no value of it is safe.
        if self.source in ("unverifiable", "unknown") or self.max_count is not None:
            return True
        return self.max_age_days is None or self.max_age_days < GEMINI_RETENTION_RECOMMENDED_DAYS


def _read_settings(path: str) -> tuple[dict, bool]:
    """One settings layer as a dict, plus whether Gemini would refuse to start on it."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        return {}, False
    except (OSError, ValueError):
        # ValueError too: a settings file that is not UTF-8 raises UnicodeDecodeError,
        # which is NOT an OSError, and an embedded NUL in a path raises from open().
        return {}, True
    try:
        data = json.loads(text)
    except ValueError:
        try:
            data = json.loads(_strip_json_comments(text))
        except ValueError:
            return {}, True
    if not isinstance(data, dict):
        return {}, True
    return data, False


def gemini_max_count_label(value: float) -> str:
    """A maxCount as Gemini would have read it -- 50, not 50.0 (its schema is a number)."""
    return str(int(value)) if float(value).is_integer() else str(value)


def _retention_layer(data: dict) -> object:
    general = data.get("general")
    if not isinstance(general, dict) or "sessionRetention" not in general:
        return _MISSING
    return general["sessionRetention"]


def _layer_is_invalid(raw: dict) -> bool:
    """True when a key here fails Gemini's schema, which un-coerces the WHOLE layer.

    Zod validates the entire settings object at once, and a failure is only a warning:
    Gemini then keeps the raw, *uncoerced* values. That flips `enabled: "false"` from
    False to a JS-truthy string, i.e. from "keeps history" to "deletes". This can only
    see the sessionRetention keys, so a bad key elsewhere in the file is invisible --
    the documented residual, and the reason a value is otherwise read fail-open.
    """
    if "enabled" in raw and _zod_boolean(raw["enabled"]) is None:
        return True
    if "maxCount" in raw and _zod_number(raw["maxCount"]) is None:
        return True
    return any(key in raw and not isinstance(raw[key], str) for key in ("maxAge", "minRetention"))


def _evaluate_retention(
    merged: dict, deciding: str, source: str, coerce: bool = True
) -> GeminiRetention:
    """Gemini's own gate and validateRetentionConfig over one merged policy.

    ``coerce`` is False when the layer that set ``enabled`` failed validation, which
    Gemini decides per FILE and before merging -- so a later layer fixing an unrelated
    key cannot restore the coercion this one lost.
    """
    # A missing key falls through to the schema default, which is what the merge does.
    enabled_raw = merged.get("enabled", True)
    max_age = merged.get("maxAge", GEMINI_RETENTION_DEFAULT_MAX_AGE)
    max_count_raw = merged.get("maxCount", _MISSING)

    max_age_label = max_age if isinstance(max_age, str) and max_age else None
    max_age_days = _parse_retention_period(max_age)
    max_count = None if max_count_raw is _MISSING else _zod_number(max_count_raw)
    kept = GeminiRetention(deciding, max_age_label, max_age_days, max_count, "inert")

    enabled = _zod_boolean(enabled_raw) if coerce else None
    if enabled is None:  # uncoerced -> the raw value reaches the JS gate
        enabled = _js_truthy(enabled_raw)
    if not enabled:
        return kept._replace(source="off")

    # validateRetentionConfig: every rejection here disables cleanup entirely, so the
    # history survives. Borrow that verdict -- and check the DEFINITIVE rejections
    # first, since one of them settles the config whatever the unevaluable parts hold.
    min_retention = merged.get("minRetention", GEMINI_RETENTION_MIN_RETENTION)
    if max_count_raw is None:
        return kept  # explicit null: `null < 1` is true in JS, so Gemini rejects it
    if max_count is not None and max_count < GEMINI_RETENTION_MIN_MAX_COUNT:
        return kept
    age_is_string = isinstance(max_age, str) and max_age != ""
    if age_is_string and not _ENV_VAR_RE.search(max_age) and max_age_days is None:
        return kept  # Gemini's parseRetentionPeriod rejects it too -> cleanup off
    if not age_is_string and max_age:
        # A truthy non-string maxAge reaches parseRetentionPeriod, whose .match() throws
        # on it -- caught, and the error disables cleanup.
        return kept
    if max_age_label is None and max_count_raw is _MISSING:
        return kept  # "Either maxAge or maxCount must be specified"

    # What is left is a live policy unless a value only Gemini can resolve says
    # otherwise -- it expands $VAR before parsing, so those fail OPEN.
    unknown = any(
        _ENV_VAR_RE.search(value) is not None
        for value in (max_age, min_retention)
        if isinstance(value, str)
    )
    if max_count_raw is not _MISSING and max_count is None:
        unknown = True  # a raw non-number still leaves the maxAge rule running
    if unknown:
        return kept._replace(source="unknown")
    if max_age_days is not None:
        floor = _parse_retention_period(min_retention)
        if floor is None:  # Gemini falls back to its own default on an unparseable floor
            floor = _parse_retention_period(GEMINI_RETENTION_MIN_RETENTION)
        if floor is not None and max_age_days < floor:
            return kept
    if max_count is not None and not math.isfinite(max_count):
        # Coerced and accepted, but `i >= Infinity` never fires: no cap at all.
        max_count = None
        kept = kept._replace(max_count=None)
    return kept._replace(source=source)


def gemini_workspace_layers() -> list[tuple[str, object]]:
    """Each recorded project's own sessionRetention -- Gemini ranks these ABOVE the user's.

    Only the projects Gemini itself listed in ``projects.json`` are consulted, and only
    to answer "can the machine-wide verdict be trusted"; folder trust is not read, so an
    untrusted project can produce a warning Gemini would ignore. That is the safe
    direction, and far cheaper than mirroring ``trustedFolders.json``. The layer travels
    with its path so evaluating it needs no second read.
    """
    registry, _broken = _read_settings(os.path.join(default_gemini_dir(), REGISTRY_FILE))
    projects = registry.get("projects")
    found: list[tuple[str, object]] = []
    for project in projects if isinstance(projects, dict) else {}:
        if not isinstance(project, str) or not project:
            continue
        path = os.path.join(project, ".gemini", SETTINGS_FILE)
        data, broken = _read_settings(path)
        raw = _MISSING if broken else _retention_layer(data)
        if raw is not _MISSING:
            found.append((path, raw))
    return found


def _merge_layers(layers: list[tuple[str, object]]) -> tuple[dict | None, str, bool, bool]:
    """Merge sessionRetention layers low-to-high.

    Returns (policy, deciding path, was any set?, may ``enabled`` be coerced?).
    A non-object layer is not merged INTO -- it replaces the object outright, so the
    accumulator is poisoned rather than updated, and a higher layer that is an object
    starts over from it. ``None`` back means the effective value is not an object,
    which makes Gemini's ``!settings...enabled`` gate false.

    Coercion is tracked per LAYER because that is where Gemini validates: the file that
    supplied ``enabled`` is the one whose validity decides whether the value was
    coerced, and a higher layer fixing an unrelated key does not undo that.
    """
    merged: dict | None = {}
    deciding = ""
    configured = False
    coerce = True
    for path, raw in layers:
        if raw is _MISSING:
            continue
        configured = True
        deciding = path
        if not isinstance(raw, dict):
            merged = None
            continue
        if "enabled" in raw:
            coerce = not _layer_is_invalid(raw)
        merged = dict(raw) if merged is None else {**merged, **raw}
    return merged, deciding, configured, coerce


def gemini_retention() -> GeminiRetention:
    """Read Gemini CLI's session retention policy without changing it.

    Cleanup runs on every launch (``cleanupExpiredSessions``) and deletes each expired
    session plus its subagent directory and artifacts -- the same files this backend
    reads. It is ON by default: Gemini merges ``getDefaultsFromSchema()`` under the
    settings files and that walk recurses into ``properties``, so a settings.json that
    never mentions sessionRetention still resolves to
    ``{enabled: true, maxAge: "30d", minRetention: "1d"}``.

    Layers merge in Gemini's own precedence -- schema defaults, system defaults, user,
    *workspace*, **system** -- and the reported path is the highest layer that actually
    set a key, because naming a file a later layer overrides is useless advice. The
    workspace layer outranks the user's, so when the machine-wide policy would keep
    history the recorded projects are re-evaluated with their own settings slotted into
    that position, **below** the system layer that still outranks them. It costs a
    handful of small reads, and only on the path that would otherwise stay silent.

    Everything else fails OPEN. Gemini coerces numeric and boolean *strings* but throws
    the whole layer's coercion away if any key fails validation (which is only a
    warning) and then reads the raw value for **JS** truthiness, and it expands ``$VAR``
    in settings strings before any of that -- so a value this cannot pin down exactly is
    reported as deleting rather than as safe. A warning nobody needed costs a keystroke;
    a missing one costs the history.
    """
    user_path = gemini_settings_path()
    below: list[tuple[str, object]] = []  # everything the workspace layer outranks
    system: list[tuple[str, object]] = []  # ...and the one layer it does not
    for path in (gemini_system_defaults_path(), user_path, gemini_system_settings_path()):
        data, broken = _read_settings(path)
        if broken:
            # A file Gemini cannot parse is FATAL to it (FatalConfigError), so the
            # policy is not "the default" -- it is unknown until the file is fixed.
            return GeminiRetention(path, None, None, None, "unverifiable")
        layer = (path, _retention_layer(data))
        (system if path == gemini_system_settings_path() else below).append(layer)

    def verdict_for(layers: list[tuple[str, object]], source: str) -> GeminiRetention:
        merged, deciding, configured, coerce = _merge_layers(layers)
        if merged is None:
            return GeminiRetention(deciding, None, None, None, "off")
        if source == "machine":
            source = "configured" if configured else "default"
        return _evaluate_retention(merged, deciding or user_path, source, coerce)

    verdict = verdict_for([*below, *system], "machine")
    if verdict.needs_warning:
        return verdict
    # A project sits between the two groups: above the user's file, below the system's.
    for path, raw in gemini_workspace_layers():
        project = verdict_for([*below, (path, raw), *system], "workspace")
        if project.needs_warning:
            return project._replace(settings_path=path)
    return verdict


class GeminiStore:
    """Read Gemini CLI's chat recordings.

    ``promptTokenCount`` includes the cache read, while ``thoughtsTokenCount`` and
    ``toolUsePromptTokenCount`` are additive on top of it, so uncached input is derived by
    reconciling against the recorded total. No cost is recorded, so every token is
    estimated under ``$``. Subagents are nested transcripts folded into their parent.
    """

    combined = False
    source_name = "Gemini"
    # Gemini CLI records tokens but never a price -- a subscription-style backend, like
    # Claude Code and Codex: $0 in normal mode, a list-price estimate under "$".
    records_cost = False

    def __init__(self, root_dir: str, args: argparse.Namespace):
        self.root_dir = root_dir
        self.args = args
        self.demo, self.demo_scale, self.demo_cats = demo_config(args)
        self._sessions: dict[str, dict] | None = None
        self._git_root_cache: dict[str, str] = {}
        self._project_cache: dict[str, str] = {}
        self._registry: dict[str, str] | None = None  # slug -> project path
        self._by_hash: dict[str, str] | None = None  # sha256(path) -> project path

    def _git_root(self, cwd: str) -> str:
        if cwd not in self._git_root_cache:
            self._git_root_cache[cwd] = git_root(cwd)
        return self._git_root_cache[cwd]

    # ---- project attribution -------------------------------------------------
    # A chat lives under tmp/<slug>/chats/, where <slug> is a short id (or, before the
    # registry migration, sha256 of the project path) that names the project but does not
    # spell it. Three sources recover the real directory, in falling order of authority.

    def _registry_path(self) -> str:
        return os.path.join(self.root_dir, REGISTRY_FILE)

    def _load_registry(self) -> dict[str, str]:
        # ~/.gemini/projects.json maps {project path -> slug}; invert it. Also index the
        # paths by sha256 for the pre-registry layout, whose directory name IS that hash
        # (one-way, but trivially invertible against the set of paths we already know).
        if self._registry is not None:
            return self._registry
        reg: dict[str, str] = {}
        by_hash: dict[str, str] = {}
        try:
            with open(self._registry_path(), encoding="utf-8") as fh:
                data = json.load(fh)
            projects = data.get("projects") if isinstance(data, dict) else None
            if isinstance(projects, dict):
                for path, slug in projects.items():
                    if not isinstance(path, str) or not isinstance(slug, str):
                        continue
                    reg.setdefault(slug, path)
                    by_hash.setdefault(hashlib.sha256(path.encode("utf-8")).hexdigest(), path)
        except (OSError, ValueError):
            pass
        self._registry, self._by_hash = reg, by_hash
        return reg

    def _project_dir(self, slug_dir: str, project_hash: str) -> str:
        # slug_dir is the tmp/<slug> directory; project_hash the metadata's sha256 of the
        # project root. The marker file wins: Gemini writes it beside the chats and
        # rebuilds it when the registry is lost, so it is the one source that survives a
        # reset projects.json.
        cached = self._project_cache.get(slug_dir)
        if cached is not None and not project_hash:
            return cached
        if cached is None:
            marker = os.path.join(slug_dir, PROJECT_ROOT_FILE)
            path = ""
            try:
                with open(marker, encoding="utf-8", errors="replace") as fh:
                    path = fh.read(4096).strip()
            except OSError:
                pass
            if not path:
                path = self._load_registry().get(os.path.basename(slug_dir), "")
            self._project_cache[slug_dir] = cached = path
        if cached:
            return cached
        # Last resort: the session's own projectHash, matched against the registry's
        # paths. This is what answers a legacy hash-named directory with no marker.
        if project_hash:
            self._load_registry()
            return (self._by_hash or {}).get(project_hash, "")
        return ""

    # ---- token accounting ----------------------------------------------------

    @staticmethod
    def _int(value) -> int:
        return safe_int(value)

    @classmethod
    def _split_tokens(cls, tok: dict) -> tuple[int, int, int, int]:
        """Return (uncached input, output, reasoning, cache read) for one call.

        Gemini's ``usageMetadata`` reports ``promptTokenCount`` INCLUSIVE of
        ``cachedContentTokenCount``, with ``thoughtsTokenCount`` and
        ``toolUsePromptTokenCount`` additive on top -- so ``totalTokenCount`` equals
        prompt + candidates + thoughts + toolUse. Verified on a real transcript: both
        recorded turns close on that identity exactly, one of them with 8,125 cached
        tokens. Subtracting the cache read is therefore the DEFAULT, not the special case.

        The recorded total is still consulted, for one job: a total that only closes when
        the cache read is added on top is describing an *exclusive* prompt count, and that
        one is kept verbatim. Defaulting the other way would double-count the cache read
        on any response that omits its total -- billing those tokens once as input and
        again as a cache read, and inflating both the token column and the ``$`` estimate.
        """
        prompt = cls._int(tok.get("input"))
        output = cls._int(tok.get("output"))
        cached = cls._int(tok.get("cached"))
        thoughts = cls._int(tok.get("thoughts"))
        tool = cls._int(tok.get("tool"))
        total = tok.get("total")
        inp = prompt
        if cached > 0:
            inclusive = prompt + output + thoughts + tool
            recorded = cls._int(total) if total is not None else None
            exclusive_shape = recorded is not None and recorded == inclusive + cached
            if not exclusive_shape:
                inp = max(0, prompt - cached)
        # Tool-use prompt tokens are prompt tokens billed at the input rate; they have no
        # column of their own, so they ride with input rather than being dropped.
        return inp + tool, output, thoughts, cached

    @staticmethod
    def _new_acc() -> dict:
        return {
            "runs": 0,
            "input": 0,  # uncached (the cache read is split out)
            "output": 0,  # candidates only; thinking is additive and priced separately
            "reasoning": 0,  # thoughtsTokenCount: NOT inside output, so it is counted
            "cache_read": 0,
            "cache_write": 0,  # Gemini's implicit cache records no write
            "tokens_total": 0,
            "cost": 0.0,  # always 0: the CLI records no price
        }

    @staticmethod
    def _new_session(sid: str) -> dict:
        return {
            "sid": sid,
            "cwd": "",
            "kind": "main",
            "agent": "",
            "parent_id": None,
            "ts_min": None,
            "ts_max": None,
            "summary": "",
            "models": {},
            "seen_msgs": {},  # message id -> index into turns (a re-append updates it)
            "turns": [],
            "prompts": [],
        }

    # ---- file discovery ------------------------------------------------------

    def _chats_root(self) -> str:
        return os.path.join(self.root_dir, "tmp")

    def _files(self) -> list[str]:
        # Main sessions sit directly in chats/; a subagent is nested one level deeper, in
        # a directory named after its PARENT's session id. A parser that accepts only the
        # flat layout drops every subagent transcript on the floor.
        root = self._chats_root()
        out: list[str] = []
        for pattern in (
            os.path.join(root, "*", "chats", "*.jsonl"),
            os.path.join(root, "*", "chats", "*.json"),
            os.path.join(root, "*", "chats", "*", "*.jsonl"),
            os.path.join(root, "*", "chats", "*", "*.json"),
        ):
            out.extend(glob.glob(pattern))
        # A rewrite leaves the unreadable original beside the new file; it is a backup of
        # bytes we already read, so counting it would double every token in that session.
        return sorted(p for p in out if self._is_transcript(p))

    def cache_inputs(self) -> list[str]:
        # The transcripts plus the project registry: the registry only ever renames a
        # project (never a token), but a session whose directory it resolves would keep
        # reporting "(unknown)" from a warm cache until something else changed.
        return self._files() + [self._registry_path()]

    def _session_files(self, session_id: str) -> list[str]:
        # A session id names a subagent file exactly, and appears inside a main
        # session's file whose name carries only the first 8 characters.
        if not session_id or "/" in session_id or "\\" in session_id:
            return []
        root = self._chats_root()
        out: list[str] = []
        for pattern in (
            os.path.join(root, "*", "chats", "*", glob.escape(session_id) + ".jsonl"),
            os.path.join(root, "*", "chats", glob.escape(session_id) + ".json*"),
            os.path.join(root, "*", "chats", "session-*-" + glob.escape(session_id[:8]) + ".jsonl"),
        ):
            out.extend(glob.glob(pattern))
        return [p for p in out if self._is_transcript(p)]

    @staticmethod
    def _is_transcript(path: str) -> bool:
        # Exactly the suffixes the parser reads, minus the `.unreadable-<ms>` copy a
        # rewrite leaves behind. Detection borrows this too: a tree holding only backups
        # would otherwise advertise the source and then produce no sessions at all.
        name = os.path.basename(path)
        return name.endswith((".json", ".jsonl")) and ".unreadable-" not in name

    @staticmethod
    def _parent_id_from_path(path: str) -> str | None:
        # Parentage is structural: chats/<parent session id>/<child>.jsonl. Reading it off
        # the path (rather than from a field inside the file) is what makes it available
        # before any parse, and makes a cycle impossible.
        parent_dir = os.path.basename(os.path.dirname(path))
        return None if parent_dir == "chats" else parent_dir

    def _path_index(self) -> tuple[dict[str, str], set[str]]:
        """`({child id: parent id}, {ids that have a transcript})`, from PATHS alone.

        Gemini nests a spawned transcript at `chats/<parent id>/<own id>.jsonl`, and a
        subagent may spawn its own, so the chain can be deeper than one level. Every link
        is visible in the filenames, which is what lets the status trio walk a whole tree
        without parsing anything: a main session is never a key in the first map (its
        filename carries only eight characters of its id), only ever a value.

        The second set is what stops the walk at a parent that no longer exists. It holds
        each nested transcript's own id plus the eight-character prefix a main session's
        filename carries, so `_has_transcript` can answer for either shape.
        """
        parents: dict[str, str] = {}
        known: set[str] = set()
        for path in self._files():
            stem = os.path.splitext(os.path.basename(path))[0]
            parent = self._parent_id_from_path(path)
            if parent:
                parents[stem] = parent
                known.add(stem)
            elif stem.startswith("session-"):
                known.add(stem[-8:])  # the id prefix the writer puts in the filename
            else:
                known.add(stem)  # a legacy chat named by its full id
        return parents, known

    @staticmethod
    def _has_transcript(sid: str, known: set[str]) -> bool:
        return sid in known or sid[:8] in known

    @classmethod
    def _walk_to_root(cls, sid: str, parents: dict[str, str], known: set[str]) -> str:
        """Follow the chain to the top, stopping at the last id that has a transcript.

        A parent whose file was deleted or rotated away is NOT a root: `_parse` promotes
        the orphan to a root of its own, and answering with the vanished parent instead
        would have `opentab cost` price a session id nothing can resolve -- $0 for a
        session the browser lists with real usage. Stopping short keeps the two agreeing.

        `seen` bounds the walk. The `dirname` rule cannot spell a cycle in omp's layout,
        but it can here (`chats/A/B.jsonl` beside `chats/B/A.jsonl`), and a status poll
        must not hang on a hand-made one.
        """
        seen = {sid}
        while sid in parents:
            nxt = parents[sid]
            if nxt in seen or not cls._has_transcript(nxt, known):
                break
            sid = nxt
            seen.add(sid)
        return sid

    def _head_meta(self, path: str) -> dict:
        # The metadata record is the file's first line, so a status scan that stops at the
        # row it wanted pays one short read per candidate rather than a parse.
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                first = fh.readline(65536)
        except OSError:
            return {}
        try:
            o = json.loads(first)
        except ValueError:
            return {}
        return o if isinstance(o, dict) else {}

    @staticmethod
    def _slug_dir(path: str) -> str:
        # tmp/<slug> for a transcript at either depth: chats/<file> for a main session,
        # chats/<parent id>/<file> for a subagent. Walk up to the chats/ directory rather
        # than counting levels, so both layouts take the same route.
        cur = os.path.dirname(path)
        while os.path.basename(cur) != "chats" and os.path.dirname(cur) != cur:
            cur = os.path.dirname(cur)
        return os.path.dirname(cur)

    def _head_cwd(self, path: str) -> str:
        cwd = self._project_dir(
            self._slug_dir(path), str(self._head_meta(path).get("projectHash") or "")
        )
        return self._git_root(cwd) if cwd else "(unknown)"

    # ---- status one-shot -----------------------------------------------------

    def recent_roots(self) -> list[dict]:
        # Newest subtree activity first, with no parse: a resume appends to the same file,
        # so its mtime is the session's last activity. Every file is keyed to the id at the
        # TOP of its spawn chain, so a root still shows as busy while only a descendant --
        # at any depth -- is writing.
        parents, known = self._path_index()
        newest: dict[str, tuple[int, str]] = {}
        for path in self._files():
            stem = os.path.splitext(os.path.basename(path))[0]
            if self._parent_id_from_path(path):
                sid = self._walk_to_root(stem, parents, known)
            else:
                sid = str(self._head_meta(path).get("sessionId") or "")
            if not sid:
                continue
            try:
                last_active = int(os.stat(path).st_mtime * 1000)  # ms, like Store's
            except OSError:
                continue  # deleted mid-scan
            prev = newest.get(sid)
            if prev is None or last_active > prev[0]:
                newest[sid] = (last_active, path)
        rows = [
            LazyStatusRoot(
                {"id": sid, "last_active": last_active},
                {"directory": lambda p=path: self._head_cwd(p)},
            )
            for sid, (last_active, path) in newest.items()
        ]
        rows.sort(key=lambda r: r["last_active"], reverse=True)
        return rows

    def root_of(self, session_id: str) -> str | None:
        # A main session's id is its own root. A spawned id walks the chain to the TOP of
        # its tree, at any depth -- stopping at the immediate parent would price only the
        # branch below it, not the session `opentab cost` was asked about.
        parents, known = self._path_index()
        if session_id in parents:
            return self._walk_to_root(session_id, parents, known)
        for path in self._session_files(session_id):
            if str(self._head_meta(path).get("sessionId") or "") == session_id:
                return session_id
        return None

    def _subtree_files(self, workflow_id: str) -> list[str]:
        # Every transcript in this session's tree, found from paths alone. The recursive
        # walk matters: a grandchild lives under its own parent's directory, so globbing
        # only `chats/<workflow_id>/*` would price a subtree missing its deepest branch.
        parents, known = self._path_index()
        paths = list(self._session_files(workflow_id))
        for path in self._files():
            if not self._parent_id_from_path(path):
                continue
            stem = os.path.splitext(os.path.basename(path))[0]
            if self._walk_to_root(stem, parents, known) == workflow_id and stem != workflow_id:
                paths.append(path)
        return sorted(set(paths))

    def status_nodes(self, workflow_id: str) -> list[dict]:
        # workflow_nodes for --status, off a parse of just this subtree's files.
        if self._sessions is not None and workflow_id in self._sessions:
            return self.workflow_nodes(workflow_id)
        sessions: dict[str, dict] = {}
        for path, text in read_files_parallel(self._subtree_files(workflow_id)):
            self._parse_file(path, text, sessions)
        self._finalize_all(sessions)
        return self._tree_nodes(sessions, workflow_id)

    # ---- parsing -------------------------------------------------------------

    def _parse(self) -> dict[str, dict]:
        if self._sessions is not None:
            return self._sessions
        sessions: dict[str, dict] = {}
        for path, text in read_files_parallel(self._files()):
            self._parse_file(path, text, sessions)
        self._finalize_all(sessions)
        # Drop sessions with no recorded usage (launching gemini and asking nothing writes
        # a metadata-only file) -- but KEEP a usage-less ancestor of one that did spend.
        # A session that only delegated has no tokens of its own, and dropping it would
        # promote its child to root in the browser while `recent_roots`/`root_of` (which
        # read the path, not the usage) keep naming the parent: `opentab cost` would then
        # price $0 for a session the browser lists with its subtree's whole spend.
        # A cycle is not spellable by Gemini itself but IS spellable on disk
        # (`chats/A/B.jsonl` beside `chats/B/A.jsonl`), and every member of one would
        # otherwise be `is_child` -- so `workflows()` would emit none of them and the whole
        # cycle's tokens would vanish from the browser while `recent_roots` still offered
        # its ids. Breaking it at the first session that reaches itself leaves exactly one
        # root, which is the invariant the rest of the tree code relies on.
        for sid, s in sessions.items():
            parent, seen = s["parent_id"], {sid}
            while parent is not None and parent in sessions and parent not in seen:
                seen.add(parent)
                parent = sessions[parent]["parent_id"]
            if parent == sid or (parent is not None and parent in seen):
                s["parent_id"] = None
        keep: set[str] = set()
        for sid, s in sessions.items():
            if not s["models"]:
                continue
            keep.add(sid)
            parent, seen = s["parent_id"], {sid}
            while parent is not None and parent in sessions and parent not in seen:
                seen.add(parent)
                keep.add(parent)
                parent = sessions[parent]["parent_id"]
        kept = {sid: s for sid, s in sessions.items() if sid in keep}
        # Safety net: an ancestor outside this batch (its transcript deleted or rotated)
        # still has to leave its child a root of its own rather than a dangling pointer.
        for s in kept.values():
            parent, seen = s["parent_id"], set()
            while parent is not None and parent not in kept and parent not in seen:
                seen.add(parent)
                parent = sessions[parent]["parent_id"] if parent in sessions else None
            s["parent_id"] = parent if parent in kept else None
        self._link_subagents(kept)
        self._sessions = kept
        return self._sessions

    def _parse_file(self, path: str, text: str, sessions: dict[str, dict]) -> None:
        records = self._records(path, text)
        if not records:
            return
        meta = records[0] if isinstance(records[0], dict) else {}
        parent = self._parent_id_from_path(path)
        stem = os.path.splitext(os.path.basename(path))[0]
        # A nested transcript is named by its OWN full session id, so the filename is the
        # id -- and it has to win over the metadata, which is what `_session_files` globs
        # and `root_of` answers from. A main session's filename carries only the first
        # eight characters of the id, so there the metadata is the only full source.
        sid = stem if parent else (str(meta.get("sessionId") or "") or stem)
        s = sessions.get(sid)
        if s is None:
            s = sessions[sid] = self._new_session(sid)
        if parent and parent != sid:
            s["parent_id"] = parent
            s["kind"] = "subagent"
        if not s["cwd"]:
            s["cwd"] = self._project_dir(self._slug_dir(path), str(meta.get("projectHash") or ""))
        for rec in records:
            if not isinstance(rec, dict):
                continue
            self._ingest(s, rec)

    def _records(self, path: str, text: str) -> list:
        """Every record in a transcript, in file order.

        Two on-disk shapes are read. The current one is append-only JSONL whose first line
        is the session metadata; the legacy one is a single JSON document with a
        ``messages`` array. Both are flattened to ``[metadata, *messages]``.
        """
        if path.endswith(".json"):
            try:
                doc = json.loads(text)
            except ValueError:
                return []
            if not isinstance(doc, dict):
                return []
            msgs = doc.get("messages")
            meta = {k: v for k, v in doc.items() if k != "messages"}
            return [meta] + (msgs if isinstance(msgs, list) else [])
        out: list = []
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue  # a torn final line while the agent is mid-write
            out.append(o)
        return out

    def _ingest(self, s: dict, rec: dict) -> None:
        # A metadata patch ($set) carries the session summary, which is the title -- and
        # may carry a whole `messages` array. That is a CHECKPOINT: Gemini's own loader
        # clears its message map and rebuilds it from the array. opentab merges instead of
        # clearing, for the `$rewindTo` reason -- a checkpoint written after a compaction
        # holds fewer messages than were actually billed, and clearing would drop the
        # difference. Merging is safe because the records are re-ingested by id, so a
        # message already seen updates in place rather than being counted twice. Verified
        # on a real transcript: the FIRST content record of every session is one of these.
        patch = rec.get("$set")
        if isinstance(patch, dict):
            summary = patch.get("summary")
            if isinstance(summary, str) and summary.strip():
                s["summary"] = summary.strip()
            for msg in patch.get("messages") or []:
                if isinstance(msg, dict):
                    self._ingest(s, msg)
            return
        # $rewindTo drops the rewound turns from Gemini's own resumed history. It is
        # deliberately NOT applied here: those calls were made and billed, and a spend
        # browser that hid them would under-report exactly the sessions someone edited
        # their way through. The same reasoning keeps Zaly's abandoned branches.
        if "$rewindTo" in rec:
            return
        if "sessionId" in rec and "type" not in rec:
            summary = rec.get("summary")
            if isinstance(summary, str) and summary.strip():
                s["summary"] = summary.strip()
            self._stamp(s, rec.get("startTime"))
            self._stamp(s, rec.get("lastUpdated"))
            return
        typ = rec.get("type")
        ts = rec.get("timestamp") if isinstance(rec.get("timestamp"), str) else ""
        self._stamp(s, ts)
        if typ == "user":
            text = self._text_of(rec.get("content")).strip()
            if self._ignored_user_text(text):
                return
            pid = str(rec.get("id") or ts)
            prompt = {"ts": ts, "id": pid, "title": text}
            # A repeated id is the same prompt re-recorded (a checkpoint replays it), not a
            # second one -- appending would double the ▸ header it opens in the Turns tab.
            for i, p in enumerate(s["prompts"]):
                if p["id"] == pid:
                    s["prompts"][i] = prompt
                    return
            s["prompts"].append(prompt)
            return
        if typ != "gemini":
            return
        calls = rec.get("toolCalls")
        tools = [
            c.get("name")
            for c in (calls if isinstance(calls, list) else [])
            if isinstance(c, dict) and c.get("name")
        ]
        self._apply_usage(s, rec, rec.get("tokens"), tools, ts)

    @staticmethod
    def _ignored_user_text(text: str) -> bool:
        # Gemini's own rule (sessionUtils.isIgnoredUserContent), borrowed rather than
        # re-derived: empty, a slash command, a `?` help query, or one of the wrappers the
        # CLI injects as a pseudo user turn. `<session_context>` opens EVERY session, so
        # without this it becomes the session title and the first ▸ header in the Turns
        # tab -- the injected-wrapper trap ClaudeStore's `_prompt_text` already dodges.
        # A tool result also arrives as a `user` record, but its content is a
        # functionResponse part with no text, so `_text_of` already returns "" for it.
        return not text or text.startswith(("/", "?", "<session_context>", "<hook_context>"))

    def _apply_usage(self, s: dict, rec: dict, tok, tools: list, ts: str) -> None:
        inp, out, reasoning, cache_read = (
            self._split_tokens(tok) if isinstance(tok, dict) else (0, 0, 0, 0)
        )
        mid = rec.get("id")
        idx = s["seen_msgs"].get(mid) if isinstance(mid, str) else None
        if inp + out + reasoning + cache_read == 0:
            # A re-append with no usage still SUPERSEDES the row it replaces -- Gemini's
            # map is keyed by id and the last record wins. Returning early here would leave
            # the earlier turn's tokens attributed to a message that no longer claims them.
            if idx is not None:
                self._unapply(s, s["turns"].pop(idx))
                del s["seen_msgs"][mid]
                for key, i in s["seen_msgs"].items():
                    if i > idx:
                        s["seen_msgs"][key] = i - 1
            return
        model = rec.get("model")
        model = self._model_label(model if isinstance(model, str) else "")
        turn = {
            "ts": ts,
            "depth": 0,
            "agent": "-",
            "effort": "",
            "model_name": model,
            "cost": 0.0,  # the CLI records no price; "$" estimates from the tokens
            "input": inp,
            "output": out,
            "reasoning": reasoning,
            "cache_read": cache_read,
            "cache_write": 0,
            "tokens_total": inp + out + reasoning + cache_read,
            "tools": tools,
        }
        # A streamed message is appended repeatedly under one id as it fills in (the
        # tokens arrive last), so the id is an UPDATE key, not a duplicate marker:
        # replacing the earlier row is what keeps a turn's tools without counting its
        # tokens twice. Verified on a real transcript, where one turn is written twice
        # under one id with identical tokens, the second append adding its toolCalls.
        if idx is not None:
            prev = s["turns"][idx]
            self._unapply(s, prev)
            s["turns"][idx] = turn
        else:
            if isinstance(mid, str):
                s["seen_msgs"][mid] = len(s["turns"])
            s["turns"].append(turn)
        acc = s["models"].get(model)
        if acc is None:
            acc = s["models"][model] = self._new_acc()
        acc["runs"] += 1
        acc["input"] += inp
        acc["output"] += out
        acc["reasoning"] += reasoning
        acc["cache_read"] += cache_read
        acc["tokens_total"] += turn["tokens_total"]

    @staticmethod
    def _unapply(s: dict, turn: dict) -> None:
        # Back out a superseded streaming row so the model totals stay the sum of the
        # turns on record. The model can differ between the two appends, so unwind
        # against the row's own model rather than the replacement's.
        acc = s["models"].get(turn["model_name"])
        if acc is None:
            return
        acc["runs"] -= 1
        acc["input"] -= turn["input"]
        acc["output"] -= turn["output"]
        acc["reasoning"] -= turn["reasoning"]
        acc["cache_read"] -= turn["cache_read"]
        acc["tokens_total"] -= turn["tokens_total"]
        if acc["runs"] <= 0 and acc["tokens_total"] <= 0:
            del s["models"][turn["model_name"]]

    @staticmethod
    def _model_label(model: str) -> str:
        # Gemini records a bare id ("gemini-2.5-pro"); provider-prefix it so the Providers
        # rollup, which groups on the "/" prefix, sees a route (the CsvStore pattern).
        model = (model or "").strip()
        if not model:
            return "unknown (not recorded)"
        return model if "/" in model else "google/" + model

    @staticmethod
    def _stamp(s: dict, ts) -> None:
        if not isinstance(ts, str) or not ts:
            return
        if s["ts_min"] is None or ts < s["ts_min"]:
            s["ts_min"] = ts
        if s["ts_max"] is None or ts > s["ts_max"]:
            s["ts_max"] = ts

    @staticmethod
    def _text_of(content) -> str:
        # A message's content is @google/genai's PartListUnion: a bare string, one part,
        # or a list of parts. Only text parts carry the user's words.
        if isinstance(content, str):
            return content
        if isinstance(content, dict):
            t = content.get("text")
            return t if isinstance(t, str) else ""
        if isinstance(content, list):
            parts = []
            for p in content:
                if isinstance(p, str):
                    parts.append(p)
                elif isinstance(p, dict) and isinstance(p.get("text"), str):
                    parts.append(p["text"])
            return " ".join(p for p in parts if p.strip())
        return ""

    # ---- tree ----------------------------------------------------------------

    def _link_subagents(self, sessions: dict[str, dict]) -> None:
        for s in sessions.values():
            s["children"] = []
            s["is_child"] = False
        for sid, s in sessions.items():
            pid = s["parent_id"]
            if pid and pid != sid and pid in sessions:
                sessions[pid]["children"].append(sid)
                s["is_child"] = True
        for sid, s in sessions.items():
            if s["is_child"] or not s["children"]:
                continue  # a child, or a flat root whose own rows already fit
            self._fold_tree_rows(sid, s, sessions)

    @staticmethod
    def _descendants(sessions: dict[str, dict], sid: str) -> list[tuple[str, int]]:
        out: list[tuple[str, int]] = []
        queue, seen = [(sid, 0)], {sid}
        while queue:
            cur, depth = queue.pop(0)
            for child in sessions[cur]["children"]:
                if child in seen:
                    continue
                seen.add(child)
                out.append((child, depth + 1))
                queue.append((child, depth + 1))
        return out

    def _fold_tree_rows(self, sid: str, s: dict, sessions: dict[str, dict]) -> None:
        # Rebuild the root's rows so cost/tokens cover the whole subtree while root_* keeps
        # the root's own share -- CodexStore's root-vs-total shape. Every token is unpriced
        # here, so the unpriced split simply mirrors the token columns.
        total: dict[str, dict] = {}
        own: dict[str, dict] = {}

        def add(bucket: dict[str, dict], model: str, acc: dict) -> None:
            t = bucket.setdefault(model, self._new_acc())
            for k in t:
                t[k] += acc[k]

        for model, acc in s["models"].items():
            add(total, model, acc)
            add(own, model, acc)
        for child, _depth in self._descendants(sessions, sid):
            for model, acc in sessions[child]["models"].items():
                add(total, model, acc)
        s["model_rows"] = [
            self._model_row(sid, model, acc, own.get(model, self._new_acc()))
            for model, acc in total.items()
        ]
        self._roll_totals(s)

    def _finalize_all(self, sessions: dict[str, dict]) -> None:
        for s in sessions.values():
            self._finalize(s)
        self._link_subagents(sessions)

    def _finalize(self, s: dict) -> None:
        sid = s["sid"]
        # Gemini's `summary` is an AI-written title -- but ONLY for a main session: its own
        # summariser skips `kind == "subagent"` outright, so anything sitting in that field
        # on a child came from somewhere else. Measured on a real subagent: it holds the
        # `complete_task` RESULT, so trusting it titles the row with a JSON blob
        # (`{ "response": "Why do programmers wear glasses?...` ). The task instruction the
        # agent was spawned with is the honest title there.
        prompt = s["prompts"][0]["title"] if s["prompts"] else ""
        # Derived here rather than latched at first sight, because a checkpoint can replay
        # the opening prompt with edited text under the same id.
        title_prompt = " ".join(prompt.split())[:80]
        summary = "" if s["kind"] == "subagent" else s["summary"]
        s["title"] = summary or title_prompt or "(untitled)"
        s["directory"] = self._git_root(s["cwd"]) if s["cwd"] else "(unknown)"
        s["created_at"] = iso_to_local(s["ts_min"] or "")
        s["ended_at"] = iso_to_local(s["ts_max"] or "") if s["ts_max"] else ""
        prompt_epochs = [iso_to_epoch(p["ts"]) for p in s["prompts"]]
        s["worked_seconds"] = worked_seconds(
            [iso_to_epoch(t["ts"]) for t in s["turns"]] + prompt_epochs, prompt_epochs
        )
        s["model_rows"] = [
            self._model_row(sid, model, acc, acc) for model, acc in s["models"].items()
        ]
        self._roll_totals(s)

    @staticmethod
    def _model_row(sid: str, model: str, acc: dict, own: dict) -> dict:
        # Nothing is priced, so cost is 0 and every token lands in the unpriced split the
        # "$" view estimates from -- the ClaudeStore/CodexStore subscription shape.
        return {
            "root_id": sid,
            "model_name": model,
            "runs": acc["runs"],
            "cost": 0.0,
            "root_cost": 0.0,
            "tokens_total": acc["tokens_total"],
            "input": acc["input"],
            "reasoning": acc["reasoning"],
            "cache_read": acc["cache_read"],
            "cache_write": acc["cache_write"],
            "output": acc["output"],
            "unpriced_input": acc["input"],
            "unpriced_reasoning": acc["reasoning"],
            "unpriced_cache_read": acc["cache_read"],
            "unpriced_cache_write": acc["cache_write"],
            "unpriced_output": acc["output"],
            "root_unpriced_input": own["input"],
            "root_unpriced_reasoning": own["reasoning"],
            "root_unpriced_cache_read": own["cache_read"],
            "root_unpriced_cache_write": own["cache_write"],
            "root_unpriced_output": own["output"],
        }

    @staticmethod
    def _roll_totals(s: dict) -> None:
        rows = s["model_rows"]
        s["total_cost"] = 0.0
        s["root_cost"] = 0.0
        s["total_tokens"] = sum(r["tokens_total"] for r in rows)
        s["unpriced_tokens"] = sum(
            r["unpriced_input"]
            + r["unpriced_output"]
            + r["unpriced_reasoning"]
            + r["unpriced_cache_read"]
            + r["unpriced_cache_write"]
            for r in rows
        )

    # ---- public contract -----------------------------------------------------

    def workflows(self) -> list[Workflow]:
        self._sessions = None  # reload (r) re-reads fresh; model methods reuse cache
        self._project_cache.clear()
        self._registry = self._by_hash = None
        sessions = self._parse()
        rows = []
        for sid, s in sessions.items():
            if s["is_child"]:
                continue  # a subagent rolls up into its parent's row
            kids = self._descendants(sessions, sid)
            ends = [s["ts_max"]] + [sessions[k]["ts_max"] for k, _d in kids]
            ended = max((t for t in ends if t), default=None)
            # Worked time over the folded tree: a subagent still writing is the agent
            # working, and can outlive the root's last message. Only the root's own
            # prompts mark idle -- a child's "user" record is the task it was spawned
            # with, not something a human typed and then walked away from.
            kid_ts = [t["ts"] for k, _d in kids for t in sessions[k]["turns"]]
            prompt_epochs = [iso_to_epoch(p["ts"]) for p in s["prompts"]]
            worked = worked_seconds(
                [iso_to_epoch(t["ts"]) for t in s["turns"]]
                + [iso_to_epoch(ts) for ts in kid_ts]
                + prompt_epochs,
                prompt_epochs,
            )
            rows.append(
                Workflow(
                    id=sid,
                    title=s["title"],
                    directory=s["directory"],
                    created_at=s["created_at"],
                    root_cost=s["root_cost"],
                    total_cost=s["total_cost"],
                    subagents=len(kids),
                    model_count=0,  # filled by App._load_model_cache
                    total_tokens=s["total_tokens"],
                    unpriced_tokens=s["unpriced_tokens"],
                    source=self.source_name,
                    ended_at=iso_to_local(ended) if ended else s["ended_at"],
                    worked_seconds=worked,
                )
            )
        if self.demo:
            rows = [self._demo_workflow(w) for w in rows]
        # Every row costs $0, so the order rides entirely on tokens; break ties by id so
        # it cannot reshuffle between launches (the ClaudeStore.sort_workflows rule).
        rows.sort(key=lambda w: (w.total_cost, w.total_tokens, w.id), reverse=True)
        return rows

    def _demo_workflow(self, w: Workflow) -> Workflow:
        return scramble_workflow(w, self.demo_scale, self.demo_cats)

    def summary(self, workflows: list[Workflow]) -> dict[str, int | float]:
        return {
            "workflows": len(workflows),
            "cost": sum(w.total_cost for w in workflows),
            "tokens": sum(w.total_tokens for w in workflows),
            "subagents": sum(w.subagents for w in workflows),
            "unpriced_tokens": sum(w.unpriced_tokens for w in workflows),
            "paid_workflows": sum(1 for w in workflows if w.total_cost > 0),
        }

    def model_breakdown(self) -> list[dict]:
        out: list[dict] = []
        for s in self._parse().values():
            if s["is_child"]:
                continue  # its usage is already inside the root's folded rows
            out.extend(s["model_rows"])
        return out

    def workflow_nodes(self, workflow_id: str) -> list[dict]:
        return self._tree_nodes(self._parse(), workflow_id)

    def _session_acc(self, s: dict) -> tuple[dict, str]:
        acc = self._new_acc()
        best, best_runs = "unknown (not recorded)", -1
        for model_name, m in s["models"].items():
            for k in acc:
                acc[k] += m[k]
            if m["runs"] > best_runs:
                best_runs, best = m["runs"], model_name
        return acc, best

    def _tree_nodes(self, sessions: dict[str, dict], workflow_id: str) -> list[dict]:
        s = sessions.get(workflow_id)
        if not s:
            return []
        acc, best = self._session_acc(s)
        nodes = [
            self._node(workflow_id, 0, "-", s["title"], s["created_at"], best, s["root_cost"], acc)
        ]
        for child, depth in self._descendants(sessions, workflow_id):
            cs = sessions[child]
            cacc, cbest = self._session_acc(cs)
            nodes.append(
                self._node(
                    child,
                    depth,
                    cs["agent"] or "subagent",
                    cs["title"],
                    cs["created_at"],
                    cbest,
                    cs["total_cost"],
                    cacc,
                )
            )
        if self.demo:
            nodes = [self._demo_node(n) for n in nodes]
        return nodes

    @staticmethod
    def _node(
        node_id: str,
        depth: int,
        agent: str,
        title: str,
        created_at: str,
        model_name: str,
        cost: float,
        acc: dict,
    ) -> dict:
        return {
            "id": node_id,
            "depth": depth,
            "agent": agent,
            "title": title,
            "created_at": created_at,
            "cost": round(cost, 6),
            "model_name": model_name,
            "tokens_input": acc["input"],
            "tokens_output": acc["output"],
            "tokens_reasoning": acc["reasoning"],
            "tokens_cache_read": acc["cache_read"],
            "tokens_cache_write": acc["cache_write"],
            "tokens_total": acc["tokens_total"],
        }

    def _demo_node(self, n: dict) -> dict:
        return scramble_node(n, self.demo_scale, self.demo_cats)

    def _subtree_turns(self, workflow_id: str) -> list[dict]:
        sessions = self._parse()
        s = sessions.get(workflow_id)
        if not s:
            return []
        turns = list(s["turns"])
        for child, depth in self._descendants(sessions, workflow_id):
            cs = sessions[child]
            agent = cs["agent"] or "subagent"
            for t in cs["turns"]:
                turns.append({**t, "depth": depth, "agent": agent})
        return turns

    def message_timeline(self, workflow_id: str) -> list[dict]:
        # Chronological per-turn rows for the Turns tab: walking the two time-sorted
        # streams in lockstep tags each turn with the latest prompt at ts <= the turn's ts.
        s = self._parse().get(workflow_id)
        if not s:
            return []
        prompts = sorted(s["prompts"], key=lambda p: p["ts"])
        out = []
        pi, cur_id, cur_title, cur_full = 0, "", "", ""
        for t in sorted(self._subtree_turns(workflow_id), key=lambda r: r["ts"]):
            while pi < len(prompts) and prompts[pi]["ts"] <= t["ts"]:
                cur_id, cur_full = prompts[pi]["id"], prompts[pi]["title"]
                cur_title = _clean_prompt(cur_full)
                pi += 1
            r = dict(t)
            r["time"] = iso_to_local(r.pop("ts"))
            r["prompt_id"] = cur_id
            r["prompt_title"] = cur_title
            r["prompt_full"] = cur_full
            out.append(r)
        return out

    def supports_turns(self, workflow_id: str) -> bool:
        return True

    def tool_breakdown(self, workflow_id: str) -> list[dict]:
        return tool_rows_from_turns(self._subtree_turns(workflow_id))

    def supports_tools(self, workflow_id: str) -> bool:
        # Every gemini message records the tool calls it made; one that called nothing
        # shows the honest empty message.
        return True
