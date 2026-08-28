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
    SMTP_HOST SMTP_PORT SMTP_USER SMTP_PASSWORD SMTP_FROM SMTP_TO SMTP_STARTTLS
    HEARTBEAT_URL LEDGER_PATH WORK_DIR ORPHAN_FOLDER_ID
    GRAPH_SECRET_EXPIRES_ON ENGINE_KEY_EXPIRES_ON ANALYSIS_KEY_EXPIRES_ON
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

from .models import DEFAULT_ROUTE, Route, is_route_name, route_env_var

log = logging.getLogger("transcriber.config")

__all__ = [
    "Config",
    "ConfigError",
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
    }
)

#: The per-route settings, in the order the wizard asks for them and the ``.env`` writes
#: them. One list, so config, the wizard and the ``routes`` command cannot disagree about
#: what a route is made of.
ROUTE_SUFFIXES = ("LABEL", "SOURCE", "OUTPUT", "ARCHIVE", "ENGINE", "ENABLED")

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
    _Var("analysis_key_expires_on", "ANALYSIS_KEY_EXPIRES_ON", "str", "", "ISO date the analysis API key expires, if it has one (optional)"),
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
    # --- loop and timing -------------------------------------------------------------
    _Var("poll_interval_s", "POLL_INTERVAL_S", "int", 120, "seconds between delta polls"),
    _Var("settle_interval_s", "SETTLE_INTERVAL_S", "int", 60, "seconds between the two size reads of the completeness check"),
    _Var("lease_seconds", "LEASE_SECONDS", "int", 900, "how long a worker's claim survives without renewal"),
    _Var("concurrency", "CONCURRENCY", "int", 2, "recordings processed at once"),
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
    # loop and timing
    poll_interval_s: int = 120
    settle_interval_s: int = 60
    lease_seconds: int = 900
    concurrency: int = 2
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
                     "max_attempts", "archive_age_days", "http_timeout_s"):
            if name in values and values[name] < 1:
                problems.append(f"{_env_of(name)}={values[name]} must be at least 1")
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
_DERIVED_FIELDS = frozenset({"engine_key", "engine_keys", "routes", "notices"})
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
