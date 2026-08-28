"""Startup configuration, read once from the environment.

Two properties matter more than convenience here.

**It fails with the whole list.** A service configured one missing variable per restart
takes nine restarts to start; every problem found in a single pass is reported in a single
message, with the variable's real name and what it is for.

**Secrets do not leak into anything a human or a log will read.** They are redacted in
``repr``, excluded from :meth:`safe_dict`, and :meth:`scrub` will remove a live secret value
from any string about to be logged or emailed. The address-shaped settings (the Graph user
principal name, the SMTP envelope) are redacted on the same footing, because the house rule
is that this service never prints an email address anywhere, for any reason.

**Routes are the unit of configuration.** A route is one watched folder and where its
results go, and the service runs N of them. ``ROUTES`` names them; each one's folders come
from its own variables. A ``.env`` written before routes existed has none of that, so its
``SOURCE_FOLDER_ID`` / ``OUTPUT_FOLDER_ID`` / ``ARCHIVE_FOLDER_ID`` become exactly one route
called ``default`` and it keeps working untouched. ``config.routes`` is therefore never
empty, and the three single-folder attributes remain readable as the first route's folders
so a module that has not been migrated yet still sees what it always saw.

Environment variables, all read by :meth:`Config.from_env`::

    GRAPH_TENANT_ID GRAPH_CLIENT_ID GRAPH_CLIENT_SECRET GRAPH_USER_ID
    ROUTES + per route ROUTE_<NAME>_LABEL _SOURCE _OUTPUT _ARCHIVE _ENGINE _ENABLED
    SOURCE_FOLDER_ID OUTPUT_FOLDER_ID ARCHIVE_FOLDER_ID   (the single-folder form, still read)
    TRANSCRIBE_ENGINE  + one of OPENAI_API_KEY | ELEVENLABS_API_KEY | AZURE_SPEECH_KEY
                       (+ AZURE_SPEECH_REGION when the engine is azure)
    ANALYSIS_API_KEY ANALYSIS_BASE_URL ANALYSIS_MODEL_CHEAP ANALYSIS_MODEL_STRONG
    GATE_MODE GATE_HELD_STORE GATE_REVIEW_BASE_URL + per route ROUTE_<NAME>_REVIEWER
    SMTP_HOST SMTP_PORT SMTP_USER SMTP_PASSWORD SMTP_FROM SMTP_TO SMTP_STARTTLS
    HEARTBEAT_URL LEDGER_PATH WORK_DIR WORK_DIR_MAX_BYTES WORK_DIR_KEEP_FINISHED_HOURS
    ORPHAN_FOLDER_ID
    GRAPH_SECRET_EXPIRES_ON ENGINE_KEY_EXPIRES_ON ANALYSIS_KEY_EXPIRES_ON
    ENGINE_MAX_CONCURRENT ENGINE_MAX_PER_MINUTE
    POLL_INTERVAL_S SETTLE_INTERVAL_S LEASE_SECONDS CONCURRENCY MAX_ATTEMPTS
    ARCHIVE_AGE_DAYS DIGEST_HOUR SWEEP_HOUR ARCHIVE_DAY_OF_MONTH TIMEZONE
    LANGUAGES VOCABULARY VOCABULARY_FILE HTTP_TIMEOUT_S MAX_RETRIES LOG_LEVEL
"""

from __future__ import annotations

import logging
import os
import stat
import tempfile
from datetime import date as _date
from dataclasses import dataclass, field, fields as dataclass_fields, replace
from typing import Any, Mapping, Sequence

from .diskbudget import (
    DEFAULT_KEEP_FINISHED_S,
    DEFAULT_WORK_DIR_MAX_BYTES,
    MINIMUM_WORK_DIR_MAX_BYTES,
    format_bytes,
    parse_bytes,
)
from .models import DEFAULT_ROUTE, Route, is_route_name, route_env_var

log = logging.getLogger("transcriber.config")

__all__ = [
    "Config",
    "ConfigError",
    "GATE_MODES",
    "nested_folder_problems",
    "ENGINES",
    "ENGINE_KEY_VARS",
    "ROUTE_SUFFIXES",
    "make_private_dir",
]

ENGINES = ("openai", "elevenlabs", "azure")
ENGINE_KEY_VARS = {
    "openai": "OPENAI_API_KEY",
    "elevenlabs": "ELEVENLABS_API_KEY",
    "azure": "AZURE_SPEECH_KEY",
}

#: Fields whose value must never reach a log line, a digest, the ledger or a repr.
SECRET_FIELDS = frozenset(
    {
        "graph_client_secret",
        "graph_user_id",
        "engine_key",
        "engine_keys",
        "analysis_api_key",
        "smtp_user",
        "smtp_password",
        "smtp_from",
        "smtp_to",
        "heartbeat_url",
        # Addresses, on the same footing as the SMTP ones: the house rule is that this
        # service never prints an email address anywhere, for any reason.
        "route_reviewers",
    }
)

#: How much of the sensitivity gate is switched on. It ships **dark** — ``shadow`` classifies
#: every recording and records what it would have held while withholding nothing — because
#: the estimates of how much this touches differ by a factor of twenty-five, and arming it
#: before that number is real is how the review queue becomes a wall.
GATE_MODES: tuple[str, str, str] = ("off", "shadow", "on")

#: The per-route settings, in the order the wizard asks for them and the ``.env`` writes
#: them. One list, so config, the wizard and the ``routes`` command cannot disagree about
#: what a route is made of.
ROUTE_SUFFIXES = ("LABEL", "SOURCE", "OUTPUT", "ARCHIVE", "ENGINE", "ENABLED", "REVIEWER")

#: The single-folder variables a pre-routes ``.env`` uses, and the route field each becomes.
LEGACY_FOLDER_VARS = {
    "SOURCE_FOLDER_ID": "source_folder_id",
    "OUTPUT_FOLDER_ID": "output_folder_id",
    "ARCHIVE_FOLDER_ID": "archive_folder_id",
}

_REQUIRED = object()  # sentinel: no default, must be supplied


class ConfigError(RuntimeError):
    """Raised at startup with every problem at once, never one at a time."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        body = "\n".join(f"  - {p}" for p in self.problems)
        super().__init__(
            f"transcriber configuration is not usable — {len(self.problems)} problem(s):\n"
            f"{body}\n"
            "Set these in the service environment and start again. "
            "Nothing was started, and no state was written."
        )


@dataclass(frozen=True)
class _Var:
    name: str          # attribute on Config
    env: str           # environment variable
    kind: str          # str | int | float | bool | csv
    default: Any
    description: str


_SPEC: tuple[_Var, ...] = (
    # --- Microsoft Graph -------------------------------------------------------------
    _Var("graph_tenant_id", "GRAPH_TENANT_ID", "str", _REQUIRED, "Entra tenant id for the client-credentials token"),
    _Var("graph_client_id", "GRAPH_CLIENT_ID", "str", _REQUIRED, "app registration (client) id"),
    _Var("graph_client_secret", "GRAPH_CLIENT_SECRET", "str", _REQUIRED, "app registration client secret"),
    _Var("graph_user_id", "GRAPH_USER_ID", "str", _REQUIRED, "id or principal name of the OneDrive owner"),
    # The single-folder form, from before routes existed. No longer required — a .env that
    # sets ROUTES does not need them — but still read, and still the whole configuration of
    # a service that has never been migrated. Whether one is missing is decided by the route
    # validation below, which knows whether it is looking at a route or at this.
    _Var("source_folder_id", "SOURCE_FOLDER_ID", "str", "", "driveItem id of the recordings folder (/CALLS), when there is only one; with ROUTES set, each route names its own"),
    _Var("output_folder_id", "OUTPUT_FOLDER_ID", "str", "", "driveItem id of the folder the .md outputs are written to, when there is only one"),
    _Var("archive_folder_id", "ARCHIVE_FOLDER_ID", "str", "", "driveItem id of the folder aged recordings are moved to, when there is only one; empty means never archive"),
    _Var("orphan_folder_id", "ORPHAN_FOLDER_ID", "str", "", "optional folder a half-written output set is moved aside to; left unset, strays are named in the error and replaced on the next attempt"),
    _Var("graph_secret_expires_on", "GRAPH_SECRET_EXPIRES_ON", "str", "", "ISO date the Entra client secret expires; the digest counts down to it (optional but strongly recommended)"),
    # --- transcription engine --------------------------------------------------------
    _Var("engine", "TRANSCRIBE_ENGINE", "str", _REQUIRED, "one of: " + ", ".join(ENGINES)),
    _Var("engine_base_url", "ENGINE_BASE_URL", "str", "", "override the engine's default endpoint (optional)"),
    _Var("azure_region", "AZURE_SPEECH_REGION", "str", "", "Azure Speech region, required when TRANSCRIBE_ENGINE=azure"),
    # --- the analysis pass -----------------------------------------------------------
    _Var("analysis_api_key", "ANALYSIS_API_KEY", "str", _REQUIRED, "API key for the analysis models (defaults to OPENAI_API_KEY when that is set)"),
    _Var("analysis_base_url", "ANALYSIS_BASE_URL", "str", "https://api.anthropic.com", "analysis API base url"),
    _Var("analysis_model_cheap", "ANALYSIS_MODEL_CHEAP", "str", "", "the router model — classifies every recording, so nothing is skipped on a guess"),
    _Var("analysis_model_strong", "ANALYSIS_MODEL_STRONG", "str", "", "the model that runs on substantive recordings"),
    _Var("engine_key_expires_on", "ENGINE_KEY_EXPIRES_ON", "str", "", "ISO date the transcription engine key expires, if it has one (optional)"),
    # --- the sensitivity gate --------------------------------------------------------
    _Var("gate_mode", "GATE_MODE", "str", "shadow", "off | shadow | on — 'shadow' reads every recording and records what it would have held while withholding nothing; 'on' actually holds a passage back until a person approves it; 'off' does not read for it at all"),
    _Var("gate_held_store", "GATE_HELD_STORE", "str", "", "path to the store of held passages — the only copy of that text outside the audio, so it is never put in the work directory; empty means held.sqlite3 beside the ledger"),
    _Var("gate_review_base_url", "GATE_REVIEW_BASE_URL", "str", "", "https address of the page where held passages are approved, linked from the morning email; required before the gate can be switched on"),
    _Var("analysis_key_expires_on", "ANALYSIS_KEY_EXPIRES_ON", "str", "", "ISO date the analysis API key expires, if it has one (optional)"),
    # --- naming a recording that arrived without one ----------------------------------
    _Var("naming", "NAMING", "bool", True, "work out what to call a recording that arrived under the voice recorder's own default name, from the site spoken in it, and say so in the morning email"),
    _Var("naming_apply", "NAMING_APPLY", "bool", False, "write that name into the transcript's subject line and heading; off means it is only reported and nothing in the record changes"),
    _Var("naming_sites_file", "NAMING_SITES_FILE", "str", "", "path to the site list written by ops/build-site-book.py from the record's nightly build; without it no recording is ever named"),
    _Var("naming_min_seconds", "NAMING_MIN_SECONDS", "int", 120, "shortest recording that may be named — below this an engine's own repetitions are indistinguishable from a site being named twice"),
    _Var("naming_opening_seconds", "NAMING_OPENING_SECONDS", "int", 60, "how much of the start of a recording counts as him announcing what it is — 'this is a site walk of Beach Court'"),
    # --- digest email ----------------------------------------------------------------
    _Var("smtp_host", "SMTP_HOST", "str", _REQUIRED, "SMTP host for the morning digest"),
    _Var("smtp_port", "SMTP_PORT", "int", 587, "SMTP port"),
    _Var("smtp_user", "SMTP_USER", "str", _REQUIRED, "SMTP username"),
    _Var("smtp_password", "SMTP_PASSWORD", "str", _REQUIRED, "SMTP password"),
    _Var("smtp_from", "SMTP_FROM", "str", _REQUIRED, "digest envelope sender"),
    _Var("smtp_to", "SMTP_TO", "csv", _REQUIRED, "digest recipients, comma separated"),
    _Var("smtp_starttls", "SMTP_STARTTLS", "bool", True, "issue STARTTLS before authenticating"),
    _Var("heartbeat_url", "HEARTBEAT_URL", "str", _REQUIRED, "external URL pinged after a successful digest, so something outside notices silence"),
    # --- durable state ---------------------------------------------------------------
    _Var("ledger_path", "LEDGER_PATH", "str", _REQUIRED, "path to the SQLite ledger — no default, because two ledgers is the same as none"),
    _Var("work_dir", "WORK_DIR", "str", os.path.join(tempfile.gettempdir(), "transcriber"), "scratch directory for downloads"),
    _Var("work_dir_max_bytes", "WORK_DIR_MAX_BYTES", "bytes", DEFAULT_WORK_DIR_MAX_BYTES, "how much scratch the work directory may hold before the worker stops claiming new recordings (4GiB, 500MB or a plain number of bytes; 0 means no limit)"),
    _Var("work_dir_keep_finished_hours", "WORK_DIR_KEEP_FINISHED_HOURS", "int", int(DEFAULT_KEEP_FINISHED_S // 3600), "hours the downloaded audio of a finished recording — done, quarantined, or written off as silence — is kept in the work directory before it is cleared away"),
    # --- loop and timing -------------------------------------------------------------
    _Var("poll_interval_s", "POLL_INTERVAL_S", "int", 120, "seconds between delta polls"),
    _Var("settle_interval_s", "SETTLE_INTERVAL_S", "int", 60, "seconds between the two size reads of the completeness check"),
    _Var("lease_seconds", "LEASE_SECONDS", "int", 900, "how long a worker's claim survives without renewal"),
    _Var("concurrency", "CONCURRENCY", "int", 2, "recordings processed at once"),
    _Var("engine_max_concurrent", "ENGINE_MAX_CONCURRENT", "int", 3, "transcription requests in flight at once, across every route and every thread — the API's limit, not the machine's"),
    _Var("engine_max_per_minute", "ENGINE_MAX_PER_MINUTE", "int", 0, "transcription requests started per minute, across every route and every thread; 0 means no per-minute limit"),
    _Var("queue_stale_hours", "QUEUE_STALE_HOURS", "int", 24, "how long the queue may sit before the digest calls it stale rather than busy"),
    _Var("stuck_after_hours", "STUCK_AFTER_HOURS", "int", 6, "how long an unfinished recording may sit before the subject line calls it FAILED rather than queued — the difference between a backlog and a breakage"),
    _Var("max_attempts", "MAX_ATTEMPTS", "int", 3, "failures before an item is quarantined for a person"),
    _Var("archive_age_days", "ARCHIVE_AGE_DAYS", "int", 60, "age at which a done recording is moved to the archive folder"),
    _Var("digest_hour", "DIGEST_HOUR", "int", 6, "local hour the digest is sent, every day"),
    _Var("sweep_hour", "SWEEP_HOUR", "int", 1, "local hour of the nightly re-enumeration"),
    _Var("archive_day_of_month", "ARCHIVE_DAY_OF_MONTH", "int", 1, "day of month the archive pass runs"),
    _Var("timezone", "TIMEZONE", "str", "Africa/Johannesburg", "IANA zone for the scheduled jobs"),
    # --- content hints ---------------------------------------------------------------
    _Var("languages", "LANGUAGES", "csv", "en-ZA,af-ZA", "expected languages, best first"),
    _Var("vocabulary", "VOCABULARY", "csv", "", "construction and site vocabulary passed to the engine as hints"),
    _Var("vocabulary_file", "VOCABULARY_FILE", "str", "", "file of vocabulary terms, one per line, merged with VOCABULARY"),
    # --- http ------------------------------------------------------------------------
    _Var("http_timeout_s", "HTTP_TIMEOUT_S", "int", 60, "socket timeout for one HTTP call"),
    _Var("max_retries", "MAX_RETRIES", "int", 5, "retries after 429/5xx before giving up loudly"),
    _Var("log_level", "LOG_LEVEL", "str", "INFO", "DEBUG | INFO | WARNING | ERROR"),
)


@dataclass
class Config:
    """Everything the service needs to run. Build it with :meth:`from_env`."""

    # Graph
    graph_tenant_id: str = ""
    graph_client_id: str = ""
    graph_client_secret: str = ""
    graph_user_id: str = ""
    #: Every route the service runs, in the order they were configured. **Never empty**: a
    #: configuration with no ``ROUTES`` is exactly one route called ``default``, built from
    #: the three single-folder variables below, so every caller can loop over this without
    #: a special case for the old shape.
    routes: tuple[Route, ...] = ()
    #: The first route's folders, kept readable under their old names so a module that has
    #: not been migrated to routes yet still sees what it always saw. They are derived, not
    #: separate state — assigning one writes through to ``routes[0]``, so the two can never
    #: drift apart and quietly send a transcript to a folder nobody chose.
    source_folder_id: str = ""
    output_folder_id: str = ""
    archive_folder_id: str = ""
    #: Optional. Where a half-written output set is moved aside to; see ``outputs._rollback``.
    orphan_folder_id: str = ""
    #: Optional ISO dates. The one credential with a hard lifetime is the Entra client
    #: secret, and the digest counts down to it so a silent cliff becomes a warning.
    graph_secret_expires_on: str = ""
    # engine
    engine: str = "openai"
    engine_key: str = ""
    #: Engine name -> API key, for every engine any route actually uses. A route may
    #: override the service engine, and an override with no key is a route that cannot
    #: transcribe anything — caught at startup rather than at the first recording.
    engine_keys: dict[str, str] = field(default_factory=dict)
    engine_base_url: str = ""
    azure_region: str = ""
    # analysis
    analysis_api_key: str = ""
    analysis_base_url: str = "https://api.anthropic.com"
    analysis_model_cheap: str = ""
    analysis_model_strong: str = ""
    engine_key_expires_on: str = ""
    analysis_key_expires_on: str = ""
    # the sensitivity gate
    #: off | shadow | on. Defaults to ``shadow``: it classifies and measures, and withholds
    #: nothing. Switching it to ``on`` is the deliberate act of arming it.
    gate_mode: str = "shadow"
    #: Where held passages live. Empty means the default beside the ledger — see
    #: :attr:`held_store_path`, which is what everything should read.
    gate_held_store: str = ""
    gate_review_base_url: str = ""
    # naming a recording that arrived without one
    #: Whether to work out a name at all. On by default because it only ever *reports*
    #: until :attr:`naming_apply` is set as well.
    naming: bool = True
    #: Whether the worked-out name reaches the transcript's subject line and heading.
    #: **Off by default, and deliberately two settings rather than one word.** The gate
    #: learned this the hard way: three call sites override an unrecognised mode word to
    #: "on", so a typo in a mode word arms the thing it was meant to disarm. A boolean
    #: cannot be misread that way.
    naming_apply: bool = False
    naming_sites_file: str = ""
    naming_min_seconds: int = 120
    #: How long "the beginning of the recording" is. He announces the site and what the
    #: visit is for in the first sentence; a call taken a few minutes in must fall outside.
    naming_opening_seconds: int = 60
    #: route name -> the address that reviews that route's held passages; a route absent
    #: from here, or present with an empty value, is reviewed by the service owner. A staff
    #: member reviews their own held passages: he sees the count and the site, never the
    #: words, because staff record voluntarily and can simply stop.
    route_reviewers: dict[str, str] = field(default_factory=dict)
    # digest
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_to: tuple[str, ...] = ()
    smtp_starttls: bool = True
    heartbeat_url: str = ""
    # state
    ledger_path: str = ""
    work_dir: str = ""
    #: How much the work directory may hold before the worker stops claiming. Nothing is
    #: dropped when it is reached: discovery carries on, the ledger rows stay claimable, and
    #: the drain starts again as recordings finish and their scratch is removed. 0 means no
    #: limit, which is the old behaviour and a full disk waiting for a busy week.
    work_dir_max_bytes: int = DEFAULT_WORK_DIR_MAX_BYTES
    #: How long the audio of a recording that is finished with is kept. A failure keeps its
    #: download so a retry is cheap and so a person can hear what went wrong; without an end
    #: to that, the audio of quarantined recordings piles up until the work directory is
    #: permanently over budget and the drain claims nothing at all, forever.
    work_dir_keep_finished_hours: int = int(DEFAULT_KEEP_FINISHED_S // 3600)
    # loop and timing
    poll_interval_s: int = 120
    settle_interval_s: int = 60
    lease_seconds: int = 900
    concurrency: int = 2
    #: The transcription API's limits, which are not the machine's. ``concurrency`` decides
    #: how many recordings this VM works on; these decide how hard the engine is pushed, and
    #: they are shared across every route and every thread in the process.
    engine_max_concurrent: int = 3
    engine_max_per_minute: int = 0
    queue_stale_hours: int = 24
    stuck_after_hours: int = 6
    max_attempts: int = 3
    archive_age_days: int = 60
    digest_hour: int = 6
    sweep_hour: int = 1
    archive_day_of_month: int = 1
    timezone: str = "Africa/Johannesburg"
    # hints
    languages: tuple[str, ...] = ("en-ZA", "af-ZA")
    vocabulary: tuple[str, ...] = ()
    vocabulary_file: str = ""
    # http
    http_timeout_s: int = 60
    max_retries: int = 5
    log_level: str = "INFO"
    #: Things that are not wrong enough to refuse to start but that a person should know
    #: about the configuration they wrote — in plain English, one per line. Logged at
    #: WARNING on startup and available to ``status`` and the digest, because a notice that
    #: only ever went to a log file is a notice nobody read.
    notices: tuple[str, ...] = ()

    # -- routes -------------------------------------------------------------------

    def __post_init__(self) -> None:
        """Make ``routes`` authoritative, whichever way this Config was built.

        Constructed from ``from_env`` the routes are already parsed and validated.
        Constructed directly — a test, ``offline()``, the wizard — there may be only the
        three single-folder values, and those are one route called ``default``. Either way
        the object leaves here with at least one route and the legacy attributes reading as
        that route's folders.
        """
        routes = tuple(self.routes or ())
        if not routes:
            routes = (
                Route(
                    name=DEFAULT_ROUTE,
                    label="Recordings",
                    source_folder_id=self.source_folder_id,
                    output_folder_id=self.output_folder_id,
                    archive_folder_id=self.archive_folder_id,
                ),
            )
        object.__setattr__(self, "routes", routes)
        self._mirror_first_route()
        object.__setattr__(self, "_routes_ready", True)

    def _mirror_first_route(self) -> None:
        first = self.routes[0]
        for name in LEGACY_FOLDER_VARS.values():
            object.__setattr__(self, name, getattr(first, name))

    def __setattr__(self, name: str, value: Any) -> None:
        """Keep the derived folder attributes and ``routes[0]`` as one fact, not two.

        The three single-folder attributes are what the unmigrated modules read. Making them
        plain fields would let a config be written on one side and read on the other, which
        is how a transcript ends up in a folder nobody configured; making them read-only
        would break every caller that still assigns one. So a write goes *through* to the
        first route, and every read comes back from it.
        """
        if name in LEGACY_FOLDER_VARS.values() and getattr(self, "_routes_ready", False):
            object.__setattr__(self, name, value)
            first = replace(self.routes[0], **{name: value})
            object.__setattr__(self, "routes", (first, *self.routes[1:]))
            return
        object.__setattr__(self, name, value)
        if name == "routes" and getattr(self, "_routes_ready", False) and self.routes:
            # Replacing the routes wholesale — the wizard, a test — has to take the derived
            # attributes with it, or the two halves of one fact go out of step.
            self._mirror_first_route()

    @property
    def enabled_routes(self) -> tuple[Route, ...]:
        """The routes actually watched. A paused route keeps its ledger history and its cursor."""
        return tuple(r for r in self.routes if r.enabled)

    @property
    def route_names(self) -> tuple[str, ...]:
        return tuple(r.name for r in self.routes)

    def route(self, name: str) -> Route | None:
        """One route by name, or None. Callers say what they will do about a missing one."""
        wanted = (name or "").strip()
        for candidate in self.routes:
            if candidate.name == wanted:
                return candidate
        return None

    def engine_for(self, route: Route | str | None = None) -> str:
        """Which engine transcribes this route's recordings — its override, or the default."""
        found = self.route(route) if isinstance(route, str) else route
        return (getattr(found, "engine", "") or "").strip() or self.engine

    def reviewer_for(self, route: Route | str | None = None) -> str:
        """Who reviews this route's held passages. Empty means the service owner.

        Never logged and never printed: ``route_reviewers`` is a secret field for the same
        reason ``SMTP_TO`` is.
        """
        name = route if isinstance(route, str) else getattr(route, "name", "") or ""
        return str(self.route_reviewers.get(name.strip(), "") or "").strip()

    @property
    def held_store_path(self) -> str:
        """Where held passages are kept — the configured path, or the default beside the ledger.

        A held passage is the only copy of that information outside the audio, so it
        inherits the ledger's discipline rather than the work directory's: the work
        directory is swept on a disk budget, and a queue that empties itself when a disk
        fills is the silent-emptying failure this gate exists to refuse.
        """
        return _held_store_path(self.gate_held_store, self.ledger_path, self.work_dir)

    def engine_key_for(self, route: Route | str | None = None) -> str:
        """The API key for whichever engine this route uses. Empty is a configuration fault."""
        engine = self.engine_for(route)
        if engine in self.engine_keys:
            return self.engine_keys[engine]
        return self.engine_key if engine == self.engine else ""

    # -- construction -------------------------------------------------------------

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        """Read the environment, or raise :class:`ConfigError` listing every problem."""
        source = os.environ if env is None else env
        problems: list[str] = []
        values: dict[str, Any] = {}

        for var in _SPEC:
            raw = source.get(var.env)
            if raw is None or str(raw).strip() == "":
                if var.default is _REQUIRED:
                    problems.append(f"{var.env} is not set — {var.description}")
                    continue
                raw = var.default
                if not isinstance(raw, str):
                    values[var.name] = _coerce_default(var, raw)
                    continue
            try:
                values[var.name] = _coerce(var, raw)
            except ValueError as exc:
                problems.append(f"{var.env}={raw!r} is not usable — {exc}")

        engine = str(values.get("engine", "")).strip().lower()
        if "engine" in values:
            values["engine"] = engine
            if engine not in ENGINES:
                problems.append(
                    f"TRANSCRIBE_ENGINE={engine!r} is not a known engine — one of: " + ", ".join(ENGINES)
                )

        # The engine's key lives under the engine's own variable name, so an operator
        # switching engines cannot leave the previous engine's key in place and have it
        # silently used.
        engine_keys: dict[str, str] = {}
        key_var = ENGINE_KEY_VARS.get(engine)
        if key_var:
            engine_key = (source.get(key_var) or "").strip()
            if not engine_key:
                problems.append(f"{key_var} is not set — the API key for the {engine} transcription engine")
            values["engine_key"] = engine_key
            if engine_key:
                engine_keys[engine] = engine_key
        if engine == "azure" and not values.get("azure_region"):
            problems.append("AZURE_SPEECH_REGION is not set — required when TRANSCRIBE_ENGINE=azure")

        # One key covers both when they are the same provider; still explicit, never guessed
        # from an unrelated variable.
        if "analysis_api_key" not in values:
            fallback = (source.get("OPENAI_API_KEY") or "").strip()
            if fallback:
                values["analysis_api_key"] = fallback
                problems = [p for p in problems if not p.startswith("ANALYSIS_API_KEY")]

        if "vocabulary_file" in values and values["vocabulary_file"]:
            path = values["vocabulary_file"]
            try:
                with open(path, encoding="utf-8") as handle:
                    terms = [line.strip() for line in handle if line.strip() and not line.startswith("#")]
            except OSError as exc:
                problems.append(f"VOCABULARY_FILE={path!r} cannot be read — {exc.strerror or exc}")
            else:
                merged = list(values.get("vocabulary", ())) + terms
                values["vocabulary"] = tuple(dict.fromkeys(merged))

        for name in ("poll_interval_s", "settle_interval_s", "lease_seconds", "concurrency",
                     "engine_max_concurrent", "queue_stale_hours", "stuck_after_hours",
                     "max_attempts", "archive_age_days",
                     "http_timeout_s", "work_dir_keep_finished_hours"):
            if name in values and values[name] < 1:
                problems.append(f"{_env_of(name)}={values[name]} must be at least 1")
        if values.get("engine_max_per_minute", 0) < 0:
            problems.append(
                "ENGINE_MAX_PER_MINUTE cannot be negative — set the number of transcription "
                "requests a minute the API allows, or 0 for no per-minute limit"
            )
        # 0 is off, which is what every installation ran before this existed. Anything above
        # 0 has to be big enough for one ordinary recording, or the budget refuses the very
        # work it is meant to pace and the queue never moves.
        budget = values.get("work_dir_max_bytes", DEFAULT_WORK_DIR_MAX_BYTES)
        if budget < 0:
            problems.append("WORK_DIR_MAX_BYTES cannot be negative")
        elif 0 < budget < MINIMUM_WORK_DIR_MAX_BYTES:
            problems.append(
                f"WORK_DIR_MAX_BYTES={format_bytes(budget)} is too small to transcribe "
                f"anything with — an hour-long recording is around 58 MB and is downloaded "
                f"and then split into pieces beside itself, so the smallest workable limit "
                f"is {format_bytes(MINIMUM_WORK_DIR_MAX_BYTES)}. Raise it, or set it to 0 "
                f"for no limit at all."
            )
        if values.get("lease_seconds", 900) <= values.get("settle_interval_s", 60):
            problems.append(
                "LEASE_SECONDS must exceed SETTLE_INTERVAL_S, or a claim expires while the "
                "completeness check is still waiting for the upload to settle"
            )
        if not 0 <= values.get("digest_hour", 6) <= 23:
            problems.append("DIGEST_HOUR must be 0-23")
        if not 0 <= values.get("sweep_hour", 1) <= 23:
            problems.append("SWEEP_HOUR must be 0-23")
        if not 1 <= values.get("archive_day_of_month", 1) <= 28:
            problems.append("ARCHIVE_DAY_OF_MONTH must be 1-28, so it exists in February too")

        # The routes, and every way a set of them can be wrong. Folder identity is checked
        # here rather than discovered later, because the failures it prevents are silent:
        # point a route's output at some route's source and the service reads its own
        # transcripts back in as recordings; watch one folder from two routes and whichever
        # cursor moves first carries the other past a recording it never saw.
        routes, declared = _routes_from_env(source, problems)
        _validate_routes(
            routes,
            declared=declared,
            orphan_folder_id=str(values.get("orphan_folder_id") or "").strip(),
            problems=problems,
        )
        values["routes"] = routes

        # A route may transcribe with a different engine from the service default, and an
        # override whose key was never set is a route that fails on its first recording.
        for route in routes:
            override = (route.engine or "").strip().lower()
            if not route.enabled or not override or override not in ENGINE_KEY_VARS:
                continue
            route_key_var = ENGINE_KEY_VARS[override]
            route_key = (source.get(route_key_var) or "").strip()
            if route_key:
                engine_keys[override] = route_key
            else:
                problems.append(
                    f"{route_key_var} is not set — {_route_phrase(route)} is set to "
                    f"transcribe with {override}, which needs its own API key"
                )
            if override == "azure" and not str(source.get("AZURE_SPEECH_REGION") or "").strip():
                problems.append(
                    f"AZURE_SPEECH_REGION is not set — {_route_phrase(route)} is set "
                    "to transcribe with azure, which needs the region as well as the key"
                )
        values["engine_keys"] = engine_keys

        # Both forms present. ROUTES wins, as documented — but silently preferring one over
        # the other leaves an operator editing SOURCE_FOLDER_ID and wondering why nothing
        # changes, so it is said out loud instead.
        notices: list[str] = []
        if declared:
            stale = sorted(
                var for var in LEGACY_FOLDER_VARS if str(source.get(var) or "").strip()
            )
            if stale:
                notices.append(
                    "This .env lists routes in ROUTES and also still sets "
                    + ", ".join(stale)
                    + ". The routes are what the service uses; those older single-folder "
                    "settings are ignored completely. Delete them so the file says what the "
                    "service actually does."
                )
        # The sensitivity gate. Everything here is refused at startup rather than at 06:00
        # on the morning somebody first tries to approve something.
        gate_mode = str(values.get("gate_mode", "shadow") or "shadow").strip().lower()
        values["gate_mode"] = gate_mode
        if gate_mode not in GATE_MODES:
            problems.append(
                f"GATE_MODE={gate_mode!r} is not one of: " + ", ".join(GATE_MODES)
                + ". 'shadow' is the default: it reads every recording and records what it "
                "would have held, and withholds nothing."
            )
        review_url = str(values.get("gate_review_base_url") or "").strip()
        if review_url and not review_url.startswith("https://"):
            problems.append(
                f"GATE_REVIEW_BASE_URL={review_url!r} must start with https:// — approvals, "
                "and the passages behind them, travel over it"
            )
        if gate_mode == "on" and not review_url:
            problems.append(
                "GATE_REVIEW_BASE_URL is not set, and GATE_MODE=on means passages are held "
                "back until somebody approves them. Without the review page there is "
                "nowhere to approve anything and nothing would ever be released. Set the "
                "address, or run with GATE_MODE=shadow until the page exists."
            )
        held_store = _held_store_path(
            str(values.get("gate_held_store") or ""),
            str(values.get("ledger_path") or ""),
            str(values.get("work_dir") or ""),
        )
        work_dir_value = str(values.get("work_dir") or "").strip()
        if gate_mode != "off" and work_dir_value and _is_inside(held_store, work_dir_value):
            why = (
                "The work directory is cleared on a disk budget, and a held passage is the "
                "only copy of that text outside the audio — it would be deleted without "
                "anybody deciding to."
            )
            if str(values.get("gate_held_store") or "").strip() or gate_mode == "on":
                # Refused when it was pointed there deliberately, and whenever the gate
                # is armed: a store that is swept on a disk budget is a queue that empties
                # itself, which is the one thing this gate may never do. Nothing about it
                # looks wrong at 06:00, which is why it is refused at the keyboard.
                problems.append(
                    f"held passages would be kept at {held_store!r}, inside "
                    f"WORK_DIR={work_dir_value!r}. {why} Set GATE_HELD_STORE to a path "
                    "outside the work directory."
                )
            else:
                # Only inherited, from a ledger that already lives in the work directory.
                # That is a configuration people have in the field and it starts today, so
                # it keeps starting — said out loud rather than refused.
                notices.append(
                    f"Held passages would be kept at {held_store}, which is inside the work "
                    f"directory {work_dir_value}, because that is where LEDGER_PATH points. "
                    f"{why} Set GATE_HELD_STORE to a path outside the work directory before "
                    "switching the gate on."
                )

        # Who reviews each route's held passages. An address, so it is validated like one
        # and never printed anywhere afterwards.
        reviewers: dict[str, str] = {}
        for route in routes:
            reviewer_var = route_env_var(route.name, "REVIEWER")
            address = str(source.get(reviewer_var) or "").strip()
            if not address:
                continue
            if not _looks_like_address(address):
                problems.append(
                    f"{reviewer_var} is not an email address. It names whoever reviews the "
                    f"held passages from {_route_phrase(route)}; leave it empty for the "
                    "service owner to review them."
                )
                continue
            reviewers[route.name] = address
        values["route_reviewers"] = reviewers

        # The case that actually matters, and the one nothing checked. An empty
        # ROUTE_<NAME>_REVIEWER is treated as "the service owner reviews them", and
        # ``withheld.reviewer_for`` then returns the principal for EVERY category — not
        # just the staff matters that are genuinely his. So the first deployment that arms
        # the gate on a staff route puts that person's own health, their family
        # circumstances and everything they asked not be written down onto James's review
        # page, words, stored context and all. Nobody has to set anything for that to
        # happen; it is what the default does.
        #
        # Decision 6 says why that is not a privacy nicety: staff record voluntarily and
        # choose whether to keep a folder at all. One of them works out that he reads the
        # held text from their calls, the rational answer is to stop recording, and then the
        # recordings are gone. That is the original loss arriving as a social effect, and it
        # is not fixable in code afterwards — so it is refused at the keyboard, like the
        # held store inside WORK_DIR, and not discovered at 06:00.
        unassigned = [route for route in routes if route.enabled and route.name not in reviewers]
        if unassigned:
            named = ", ".join(route_env_var(route.name, "REVIEWER") for route in unassigned)
            plain = (
                "Without it, every passage held from "
                + _join_phrases([_route_phrase(route) for route in unassigned])
                + " goes to the service owner to review — including a staff member's own "
                "health, their family circumstances, and anything they asked not be written "
                "down. Only staff disciplinary matters are meant to reach him; the rest a "
                "person reviews for themselves, and he sees the count and the site."
            )
            if gate_mode == "on":
                problems.append(
                    f"GATE_MODE=on, and no reviewer is named for: {named}. {plain} Set each "
                    "one to the address of whoever records on that route, or to the service "
                    "owner's own address if that route is his."
                )
            elif gate_mode == "shadow":
                notices.append(
                    f"No reviewer is named for: {named}. Nothing is being withheld in "
                    f"shadow, so nothing is going anywhere yet. {plain} Set them before "
                    "switching the gate on."
                )

        # Naming. Never a problem — a missing site list means no recording is ever named,
        # which is exactly the behaviour before this existed. A notice, so that a list that
        # quietly stopped being written does not read as a quiet fortnight.
        if bool(values.get("naming", True)):
            book_path = str(values.get("naming_sites_file") or "").strip()
            if not book_path:
                notices.append(
                    "No site list is configured (NAMING_SITES_FILE), so no recording will "
                    "be given a name. Everything else is unaffected. Point it at the file "
                    "ops/build-site-book.py writes from the record's nightly build."
                )
            elif not os.path.exists(book_path):
                notices.append(
                    f"The site list {book_path} is not there, so no recording will be "
                    f"given a name. Everything else is unaffected."
                )
            if bool(values.get("naming_apply")) and not book_path:
                notices.append(
                    "NAMING_APPLY is on but there is no site list, so there is nothing to "
                    "apply."
                )

        configured_reviewer_vars = {route_env_var(r.name, "REVIEWER") for r in routes}
        stray_reviewers = sorted(
            key for key in source
            if key.startswith("ROUTE_") and key.endswith("_REVIEWER")
            and key not in configured_reviewer_vars and str(source.get(key) or "").strip()
        )
        if stray_reviewers:
            notices.append(
                "These name a reviewer for a route that is not configured, so they assign "
                "nobody: " + ", ".join(stray_reviewers) + ". Held passages from a route with "
                "no reviewer go to the service owner."
            )

        values["notices"] = tuple(notices)

        for name in ("graph_secret_expires_on", "engine_key_expires_on", "analysis_key_expires_on"):
            raw = str(values.get(name) or "").strip()
            if not raw:
                continue
            try:
                when = _date.fromisoformat(raw[:10])
            except ValueError:
                problems.append(
                    f"{_env_of(name)}={raw!r} is not an ISO date (YYYY-MM-DD)"
                )
                continue
            if when < _date.today():
                log.warning(
                    "%s is %s, which is in the past: that credential has expired and nothing "
                    "will process until it is renewed",
                    _env_of(name), when.isoformat(),
                )

        if problems:
            raise ConfigError(problems)

        config = cls(**values)
        for notice in config.notices:
            log.warning("%s", notice)
        _make_private(config.work_dir)
        return config

    @classmethod
    def offline(cls, ledger_path: str = ":memory:") -> "Config":
        """A credential-free config for ``selftest`` and the test suite.

        Every value here is obviously fake so that a config which reached production by
        accident fails at the first call rather than doing something plausible.
        """
        return cls(
            graph_tenant_id="offline-tenant",
            graph_client_id="offline-client",
            graph_client_secret="offline-not-a-secret",
            graph_user_id="offline-user",
            source_folder_id="offline-source",
            output_folder_id="offline-output",
            archive_folder_id="offline-archive",
            engine="openai",
            engine_key="offline-not-a-key",
            analysis_api_key="offline-not-a-key",
            smtp_host="localhost",
            smtp_user="offline",
            smtp_password="offline-not-a-secret",
            smtp_from="offline@invalid",
            smtp_to=("offline@invalid",),
            heartbeat_url="https://example.invalid/heartbeat",
            ledger_path=ledger_path,
            work_dir=os.path.join(tempfile.gettempdir(), "transcriber-offline"),
        )

    # -- redaction ----------------------------------------------------------------

    def safe_dict(self) -> dict[str, Any]:
        """Everything except the secrets — what may be logged or put in a digest."""
        out: dict[str, Any] = {}
        for f in dataclass_fields(self):
            out[f.name] = "***REDACTED***" if f.name in SECRET_FIELDS else getattr(self, f.name)
        return out

    def secret_values(self) -> tuple[str, ...]:
        values: list[str] = []
        for name in SECRET_FIELDS:
            value = getattr(self, name, None)
            if isinstance(value, str) and len(value) >= 4:
                values.append(value)
            elif isinstance(value, Mapping):
                values.extend(v for v in value.values() if isinstance(v, str) and len(v) >= 4)
            elif isinstance(value, (tuple, list)):
                values.extend(v for v in value if isinstance(v, str) and len(v) >= 4)
        return tuple(values)

    def scrub(self, text: str) -> str:
        """Remove any live secret from a string on its way to a log, a file or an email."""
        if not text:
            return text
        for value in self.secret_values():
            text = text.replace(value, "***REDACTED***")
        return text

    def __repr__(self) -> str:
        parts = []
        for f in dataclass_fields(self):
            value = "***REDACTED***" if f.name in SECRET_FIELDS else repr(getattr(self, f.name))
            parts.append(f"{f.name}={value}")
        return "Config(" + ", ".join(parts) + ")"

    __str__ = __repr__


def _routes_from_env(
    source: Mapping[str, str], problems: list[str]
) -> tuple[tuple[Route, ...], bool]:
    """Every configured route, and whether ``ROUTES`` was the thing that configured them.

    Two shapes are supported and only two. ``ROUTES=calls,site-meetings`` names the routes
    and each one's folders come from its own variables; nothing at all means the
    single-folder ``.env`` written before routes existed, which is one route called
    ``default``. The second is not a deprecated path to be tolerated — it is the shape of
    every installation in the field, and it must keep working untouched.
    """
    raw = str(source.get("ROUTES") or "").strip()
    if not raw:
        return (
            (
                Route(
                    name=DEFAULT_ROUTE,
                    label="Recordings",
                    source_folder_id=str(source.get("SOURCE_FOLDER_ID") or "").strip(),
                    output_folder_id=str(source.get("OUTPUT_FOLDER_ID") or "").strip(),
                    archive_folder_id=str(source.get("ARCHIVE_FOLDER_ID") or "").strip(),
                    engine="",
                    enabled=True,
                ),
            ),
            False,
        )

    routes: list[Route] = []
    seen: set[str] = set()
    for name in [part.strip() for part in raw.replace("\n", ",").split(",")]:
        if not name:
            continue
        if not is_route_name(name):
            problems.append(
                f"ROUTES lists {name!r}, which is not a usable route name — a route name is "
                "lowercase letters, digits and hyphens, starting with a letter or a digit "
                f"(so {_suggest_route_name(name)!r} rather than {name!r}). The name is used "
                "as a key in the ledger and in the environment variable names for its "
                "folders, which is why it cannot contain anything else."
            )
            continue
        if name in seen:
            problems.append(
                f"ROUTES lists {name!r} twice — each route is named once, because its name "
                "is what its folders, its cursor and its ledger rows are keyed on"
            )
            continue
        seen.add(name)

        def var(suffix: str) -> str:
            return str(source.get(route_env_var(name, suffix)) or "").strip()

        engine = var("ENGINE").lower()
        if engine and engine not in ENGINES:
            problems.append(
                f"{route_env_var(name, 'ENGINE')}={engine!r} is not a known engine — "
                "one of: " + ", ".join(ENGINES) + ", or leave it empty to use the service default"
            )
            engine = ""

        enabled_raw = var("ENABLED")
        enabled = True
        if enabled_raw:
            lowered = enabled_raw.lower()
            if lowered in ("1", "true", "yes", "on"):
                enabled = True
            elif lowered in ("0", "false", "no", "off"):
                enabled = False
            else:
                problems.append(
                    f"{route_env_var(name, 'ENABLED')}={enabled_raw!r} is not usable — "
                    "expected true or false"
                )

        routes.append(
            Route(
                name=name,
                label=var("LABEL"),
                source_folder_id=var("SOURCE"),
                output_folder_id=var("OUTPUT"),
                archive_folder_id=var("ARCHIVE"),
                engine=engine,
                enabled=enabled,
            )
        )
    return tuple(routes), True


def _suggest_route_name(name: str) -> str:
    """What the operator probably meant, so the error can show it rather than describe it."""
    cleaned = "".join(
        c if c.isalnum() else "-" for c in (name or "").strip().lower()
    ).strip("-")
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned or "calls"


def _join_phrases(phrases: list[str]) -> str:
    """``a, b and c`` — for a sentence a person reads, not a list a machine parses."""
    items = [p for p in phrases if p]
    if not items:
        return "any route"
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def _route_phrase(route: Route) -> str:
    """How a route is named in a sentence somebody reads.

    ``Phone calls (calls)`` when it has a label, ``route 'calls'`` when it does not — never
    ``calls (calls)``, which is what naming a thing twice looks like.
    """
    label = (route.label or "").strip()
    return f"{label} ({route.name})" if label else f"route {route.name!r}"


def _folder_var(route: Route, suffix: str, *, declared: bool) -> str:
    """The variable an operator has to edit to fix this route's folder.

    Which one it is depends on how the service was configured, and telling somebody to set
    ``ROUTE_DEFAULT_SOURCE`` when their file says ``SOURCE_FOLDER_ID`` sends them looking
    for a setting that is not there.
    """
    if declared:
        return route_env_var(route.name, suffix)
    return {"SOURCE": "SOURCE_FOLDER_ID", "OUTPUT": "OUTPUT_FOLDER_ID",
            "ARCHIVE": "ARCHIVE_FOLDER_ID"}[suffix]


def _validate_routes(
    routes: Sequence[Route],
    *,
    declared: bool,
    orphan_folder_id: str,
    problems: list[str],
) -> None:
    """Every way a set of routes can be wrong, reported together and in plain words.

    The feedback-loop rule is the one that matters. Everything else here costs an operator
    a restart; that one, left in, has the service transcribing its own transcripts as
    though they were recordings, for as long as nobody notices.

    Two routes **sharing an output folder is deliberately allowed**. Pooling several kinds
    of recording into one folder is a thing he asked for, so it is not quietly forbidden
    here on the grounds of tidiness.
    """
    enabled = [r for r in routes if r.enabled]
    if not routes:
        problems.append(
            "there are no routes to run — set ROUTES to at least one route name, or set "
            "SOURCE_FOLDER_ID and OUTPUT_FOLDER_ID for a single watched folder"
        )
        return
    if not enabled:
        problems.append(
            "every route is switched off ("
            + ", ".join(f"{_route_phrase(r)}" for r in routes)
            + ") — nothing would be watched and nothing would be transcribed. Switch at "
            "least one back on."
        )

    for route in enabled:
        if not route.source_folder_id:
            problems.append(
                f"{_folder_var(route, 'SOURCE', declared=declared)} is not set — "
                + (
                    f"{_route_phrase(route)} has no folder to watch for recordings"
                    if declared
                    else "the folder recordings arrive in. Set it, or list your routes in "
                         "ROUTES and give each one its own folders."
                )
            )
        if not route.output_folder_id:
            problems.append(
                f"{_folder_var(route, 'OUTPUT', declared=declared)} is not set — "
                + (
                    f"{_route_phrase(route)} has nowhere to write its transcripts"
                    if declared
                    else "the folder the transcripts, summaries and actions are written to"
                )
            )

    # 4. The feedback loop. Checked across every route, switched on or off, because a paused
    #    route is a folder somebody will switch back on without re-reading the whole file.
    for writer in routes:
        if not writer.output_folder_id:
            continue
        for watcher in routes:
            if not watcher.source_folder_id or writer.output_folder_id != watcher.source_folder_id:
                continue
            if writer.name == watcher.name:
                problems.append(
                    f"{_route_phrase(writer)} writes its transcripts into the very "
                    "folder it watches for recordings — the service would read its own "
                    "transcripts back in as new recordings and transcribe them again, over "
                    "and over. Send its transcripts somewhere else."
                )
            else:
                problems.append(
                    f"{_route_phrase(writer)} writes its transcripts into the folder "
                    f"{_route_phrase(watcher)} watches for recordings — the service "
                    "would read its own transcripts back in as new recordings and transcribe "
                    "them again. One of those two folders has to change."
                )

    # 5. One folder, one route. Two cursors over one folder is two claims on one recording.
    for index, first in enumerate(enabled):
        if not first.source_folder_id:
            continue
        for second in enabled[index + 1:]:
            if first.source_folder_id != second.source_folder_id:
                continue
            problems.append(
                f"{_route_phrase(first)} and {_route_phrase(second)} watch "
                "the same folder — a recording can only belong to one route, and whichever "
                "of the two saw it first would own it while the other moved its cursor past "
                "it as though it had been handled"
            )

    # 7. The archive holds untouched originals, so it can be neither a folder we read from
    #    nor a folder we write to.
    for archiver in routes:
        if not archiver.archives:
            continue
        for other in routes:
            if other.source_folder_id and archiver.archive_folder_id == other.source_folder_id:
                where = (
                    "the very folder it watches"
                    if archiver.name == other.name
                    else f"the folder {_route_phrase(other)} watches"
                )
                problems.append(
                    f"{_route_phrase(archiver)} archives its old recordings into "
                    f"{where} for recordings — every recording it filed away would be "
                    "discovered all over again the moment it was moved"
                )
            if other.output_folder_id and archiver.archive_folder_id == other.output_folder_id:
                whose = (
                    "its own transcripts"
                    if archiver.name == other.name
                    else f"the transcripts of {_route_phrase(other)}"
                )
                problems.append(
                    f"{_route_phrase(archiver)} archives its old recordings into "
                    f"the folder that holds {whose} — the archive is meant to be the "
                    "untouched original recordings, and mixing the two makes it neither"
                )

    if orphan_folder_id:
        for route in routes:
            if orphan_folder_id == route.source_folder_id:
                problems.append(
                    "ORPHAN_FOLDER_ID is the folder "
                    f"{_route_phrase(route)} watches — a half-written output set "
                    "moved aside into it would be picked up as though it were a new recording"
                )
            if orphan_folder_id == route.output_folder_id:
                problems.append(
                    "ORPHAN_FOLDER_ID is the folder "
                    f"{_route_phrase(route)} writes its transcripts to — a half-written "
                    "set moved aside into it would sit alongside the good ones with nothing "
                    "to tell them apart"
                )
            if route.archives and orphan_folder_id == route.archive_folder_id:
                problems.append(
                    "ORPHAN_FOLDER_ID is the folder "
                    f"{_route_phrase(route)} archives recordings into — the archive is "
                    "for finished originals, not for the wreckage of a failed write"
                )


def nested_folder_problems(
    routes: Sequence[Route], ancestors_of: Any
) -> list[str]:
    """The overlaps folder **ids** cannot show: one route's folder inside another's.

    Graph's ``/items/{id}/delta`` is a subtree feed, so a route watching ``/Recordings``
    is also watching ``/Recordings/SiteMeetings`` inside it — and a site meeting recorded
    into the second folder is claimed by whichever route polls first, transcribed into that
    route's output folder, and sixty days later moved into that route's archive. Both
    folders are pickable, both ids are different, and nothing in an id says one contains the
    other, which is why :func:`_validate_routes` cannot catch this and the wizard can: the
    wizard is standing at the live tree when the ids are chosen.

    ``ancestors_of(folder_id)`` returns that folder's ancestor ids, nearest first, and an
    empty sequence when the answer is not known. Not knowing is never reported as a
    problem — a refusal invented from a Graph call that failed would be worse than the bug.
    """
    enabled = [r for r in routes if r.enabled]
    chain: dict[str, tuple[str, ...]] = {}

    def above(folder_id: str) -> tuple[str, ...]:
        wanted = (folder_id or "").strip()
        if not wanted:
            return ()
        if wanted not in chain:
            try:
                chain[wanted] = tuple(str(f) for f in (ancestors_of(wanted) or ()) if f)
            except Exception:  # noqa: BLE001 - an unanswerable question is not a problem
                chain[wanted] = ()
        return chain[wanted]

    problems: list[str] = []
    for index, first in enumerate(enabled):
        for second in enabled[index + 1:]:
            outer, inner = None, None
            if first.source_folder_id and first.source_folder_id in above(second.source_folder_id):
                outer, inner = first, second
            elif second.source_folder_id and second.source_folder_id in above(first.source_folder_id):
                outer, inner = second, first
            if outer is None or inner is None:
                continue
            problems.append(
                f"the folder {_route_phrase(inner)} watches is inside the folder "
                f"{_route_phrase(outer)} watches. OneDrive reports changes for a folder and "
                f"everything under it, so {_route_phrase(outer)} would see "
                f"{_route_phrase(inner)}'s recordings too and could claim them first: their "
                f"transcripts would be written to the wrong folder and the recordings "
                f"themselves would eventually be filed in the wrong archive. Put the two "
                f"folders side by side rather than one inside the other"
            )

    for archiver in routes:
        if not archiver.archives:
            continue
        for watcher in enabled:
            if not watcher.source_folder_id:
                continue
            if watcher.source_folder_id not in above(archiver.archive_folder_id):
                continue
            whose = (
                "the very folder it watches"
                if archiver.name == watcher.name
                else f"the folder {_route_phrase(watcher)} watches"
            )
            problems.append(
                f"{_route_phrase(archiver)} archives its old recordings into a folder inside "
                f"{whose}. OneDrive reports a folder and everything under it, so filing a "
                "recording away would not take it out of the watched folder at all — it "
                "would still be enumerated every night. Put the archive folder outside the "
                "folder being watched"
            )
    return problems


def _held_store_path(configured: str, ledger_path: str, work_dir: str) -> str:
    """Where held passages go: what was configured, or the default beside the ledger.

    One function, used by both the startup validation and :attr:`Config.held_store_path`, so
    the path that is checked is the path that is used.
    """
    explicit = (configured or "").strip()
    if explicit:
        return explicit
    ledger = (ledger_path or "").strip()
    if ledger and ledger != ":memory:":
        return os.path.join(os.path.dirname(os.path.abspath(ledger)), "held.sqlite3")
    # An in-memory ledger is a test or a selftest, never a running service.
    return os.path.join((work_dir or tempfile.gettempdir()).strip(), "held.sqlite3")


def _is_inside(path: str, directory: str) -> bool:
    """True when ``path`` is ``directory`` or sits under it. Best effort, never raises."""
    try:
        target = os.path.abspath(os.path.expanduser(str(path or "")))
        parent = os.path.abspath(os.path.expanduser(str(directory or "")))
    except (OSError, ValueError):  # pragma: no cover - abspath on a hostile string
        return False
    if not target or not parent:
        return False
    return target == parent or target.startswith(parent.rstrip(os.sep) + os.sep)


def _looks_like_address(value: str) -> bool:
    """Enough of an email address to be worth writing down. Checked, then never printed."""
    text = (value or "").strip()
    if not text or any(c.isspace() for c in text) or text.count("@") != 1:
        return False
    local, _, domain = text.partition("@")
    return bool(local and "." in domain and not domain.startswith(".") and not domain.endswith("."))


def _env_of(name: str) -> str:
    for var in _SPEC:
        if var.name == name:
            return var.env
    return name.upper()


def _coerce_default(var: _Var, raw: Any) -> Any:
    if var.kind == "csv" and isinstance(raw, (tuple, list)):
        return tuple(raw)
    return raw


def _coerce(var: _Var, raw: Any) -> Any:
    if var.kind == "str":
        return str(raw).strip()
    if var.kind == "int":
        try:
            return int(str(raw).strip())
        except ValueError:
            raise ValueError("expected a whole number") from None
    if var.kind == "bytes":
        # ``parse_bytes`` raises ValueError with the sentence to print, like every other
        # kind here, so a size is reported in the same one-pass list as everything else.
        return parse_bytes(raw)
    if var.kind == "float":
        try:
            return float(str(raw).strip())
        except ValueError:
            raise ValueError("expected a number") from None
    if var.kind == "bool":
        text = str(raw).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
        raise ValueError("expected true or false")
    if var.kind == "csv":
        parts = [p.strip() for p in str(raw).replace("\n", ",").split(",")]
        return tuple(p for p in parts if p)
    raise ValueError(f"unknown kind {var.kind}")


# A spec entry with no field (or a field with no spec entry) means an environment variable
# that is read and dropped, or a setting no operator can set. Both are silent failures, so
# they are caught at import time instead.
#: Fields with no environment variable of their own: the engine keys, which are read from
#: whichever variable belongs to the engine in use; ``routes``, assembled from ROUTES and the
#: per-route variables; and ``notices``, which the parse produces rather than reads.
_DERIVED_FIELDS = frozenset(
    {"engine_key", "engine_keys", "routes", "notices", "route_reviewers"}
)
_SPEC_NAMES = {v.name for v in _SPEC}
_FIELD_NAMES = {f.name for f in dataclass_fields(Config)} - _DERIVED_FIELDS
if _SPEC_NAMES != _FIELD_NAMES:
    raise RuntimeError(
        "config spec and Config fields disagree: "
        f"spec-only={sorted(_SPEC_NAMES - _FIELD_NAMES)} field-only={sorted(_FIELD_NAMES - _SPEC_NAMES)}"
    )


def make_private_dir(path: str) -> str:
    """Create the scratch directory readable only by the service account.

    The work directory holds the raw audio of confidential site and commercial conversations
    and, for any recording that failed, its transcript — kept on purpose so the next attempt
    is cheap. Created with the process umask it is world-readable, and a fixed name inside a
    world-writable sticky directory is the classic pre-creation hazard as well: whoever
    creates it first owns it. ``makedirs``' ``mode`` is ignored when the directory already
    exists, so the ``chmod`` is not redundant.
    """
    target = str(path or "").strip()
    if not target:
        return target
    os.makedirs(target, mode=0o700, exist_ok=True)
    try:
        os.chmod(target, stat.S_IRWXU)
    except OSError as exc:  # a shared volume that will not take a chmod is worth saying
        log.warning(
            "could not restrict %s to this account only (%s); the recordings and transcripts "
            "in it may be readable by other users on this machine",
            target, exc,
        )
    return target


_make_private = make_private_dir
