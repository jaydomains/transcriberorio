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

**A name nobody reads is reported, not ignored.** ``GATE_MODEE=on`` used to load clean and
leave the gate in shadow: the operator believed passages were being withheld and nothing
was. So every variable in the environment that no part of this service reads is looked at
after the parse, and one close to a real name — a letter dropped, a letter doubled — is a
problem with the real name printed beside it, because it can only be a typo. Only
near-misses: the process environment carries systemd's own variables and the shell's, and a
service that refuses to start because ``JOURNAL_STREAM`` is not one of its settings would be
worse than the bug this catches.

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
    HEARTBEAT_URL ALLOW_PLAINTEXT_ENDPOINTS
    LEDGER_PATH WORK_DIR WORK_DIR_MAX_BYTES WORK_DIR_KEEP_FINISHED_HOURS
    ORPHAN_FOLDER_ID
    GRAPH_SECRET_EXPIRES_ON ENGINE_KEY_EXPIRES_ON ANALYSIS_KEY_EXPIRES_ON
    ENGINE_MAX_CONCURRENT ENGINE_MAX_PER_MINUTE
    POLL_INTERVAL_S SETTLE_INTERVAL_S LEASE_SECONDS CONCURRENCY MAX_ATTEMPTS
    ARCHIVE_AGE_DAYS DIGEST_HOUR SWEEP_HOUR ARCHIVE_DAY_OF_MONTH TIMEZONE
    LANGUAGES VOCABULARY VOCABULARY_FILE HTTP_TIMEOUT_S MAX_RETRIES LOG_LEVEL
"""

from __future__ import annotations

import difflib
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
from .models import DEFAULT_ROUTE, Route, is_route_name, route_env_stem, route_env_var

log = logging.getLogger("transcriber.config")

__all__ = [
    "environment_the_service_reads",
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
    _Var("site_evidence", "SITE_EVIDENCE", "bool", True, "write the job-list candidates and their scores into the summary and actions files, so whatever reads them next is not left hunting through raw text for which job a site walk was about. Evidence only — it files nothing, and never appears in the transcript"),
    _Var("engine_site_names", "ENGINE_SITE_NAMES", "bool", True, "tell the transcription engine the job names from the site list, so it writes 'Lonehill' instead of 'on loan' — a name it never transcribed cannot be matched afterwards by anything"),
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
    # --- the group view --------------------------------------------------------------
    # Each person runs their own copy against their own drive, so each copy sees one
    # person's recordings and nothing else. That is the right shape and it leaves one hole:
    # when somebody's copy stops working, only that person is told, and it is not their
    # record that suffers. These four settings close it, and all four are optional — one
    # person running this alone gets no group section at all, rather than a group of one.
    _Var("instance_name", "INSTANCE_NAME", "str", "", "whose copy this is, in the group view — a person's name, not a hostname (optional; without it this copy stays out of the group view)"),
    _Var("group_folder_id", "GROUP_FOLDER_ID", "str", "", "OneDrive folder every copy drops its daily status into, so one of them can report on all of them (optional)"),
    _Var("group_drive_user_id", "GROUP_DRIVE_USER_ID", "str", "", "id or principal name of whoever owns GROUP_FOLDER_ID; empty means it is in this copy's own drive (optional)"),
    _Var("group_admin_to", "GROUP_ADMIN_TO", "csv", "", "who receives the one consolidated email about the whole group. Set on the ADMIN's copy only: whichever copy has this reads every status file and sends the group email, and the copies that do not have it simply write their status and stay quiet (optional)"),
    _Var("group_silent_after_hours", "GROUP_SILENT_AFTER_HOURS", "int", 36, "how long a copy may go without dropping a status file before the group email calls it silent rather than quiet"),
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

#: Variables this service reads that are not in ``_SPEC`` — they belong to another module,
#: or they are read once here and never kept on the Config. Listed so that the unknown-name
#: check below does not report a setting somebody was told to set in the documentation. A
#: name added to one of those modules belongs here too, or the next ``.env`` that uses it
#: gets a notice saying it does nothing.
_ALSO_READ = frozenset(
    {
        "ROUTES",                   # parsed here into the routes themselves
        "ANALYSIS_PROVIDER",        # extract.AnalysisSettings.from_config
        "ALLOW_PLAINTEXT_ENDPOINTS",  # the opt-in beside the https:// check in from_env
        "LOG_FORMAT",               # logging_setup
        "REVIEW_BIND",              # review_server, and the `review` subcommand
        "REVIEW_PORT",
        "REVIEW_CERTFILE",
        "REVIEW_KEYFILE",
        "REVIEW_UNDO_SECONDS",
    }
)

#: Every environment variable name the service actually reads, apart from the per-route
#: ones, which depend on the route names and are worked out when they are needed.
_KNOWN_ENV_NAMES = (
    frozenset(var.env for var in _SPEC) | frozenset(ENGINE_KEY_VARS.values()) | _ALSO_READ
)

#: The first word of each of those names. A leftover variable is only ever compared against
#: the real names when it starts with one of these, because that is what tells this
#: service's settings apart from the machine's: ``SMTP_STARTLS`` is ours and misspelt,
#: ``LANGUAGE`` is the operating system's locale and is none of our business — and it is
#: one letter away from ``LANGUAGES``, which is exactly how a well-meant check ends up
#: stopping a service from starting on an ordinary host.
_KNOWN_ENV_FAMILIES = frozenset(name.split("_", 1)[0] for name in _KNOWN_ENV_NAMES)


def environment_the_service_reads(source: "Mapping[str, str] | None" = None) -> dict[str, str]:
    """The settings out of a whole process environment, and nothing else.

    A deployed host hands its settings to the service through systemd's
    ``EnvironmentFile=``, so there is no ``.env`` in the working directory for
    ``config list`` or ``routes list`` to read — the two commands an operator
    reaches for to ask what this thing is set up to do. They can answer from the
    process environment instead, but only after the host's own variables (PATH,
    HOME, systemd's INVOCATION_ID, whatever the shell exported) are dropped:
    printing those back would bury the answer and would put unrelated values on
    a screen somebody is about to photograph for a support thread.
    """
    import os as _os

    everything = _os.environ if source is None else source
    kept: dict[str, str] = {}
    for key, value in everything.items():
        if key in _KNOWN_ENV_NAMES:
            kept[key] = value
        elif key.startswith("ROUTE_") and key.rsplit("_", 1)[-1] in ROUTE_SUFFIXES:
            kept[key] = value
    return kept

#: How alike two names have to be before one is called a misspelling of the other. High on
#: purpose: every misspelling actually seen in the field — a letter dropped, a letter
#: doubled, two letters swapped — scores above 0.94, and nothing else should reach this.
_NEAR_MISS = 0.86


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
    site_evidence: bool = True
    engine_site_names: bool = True
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
    instance_name: str = ""
    group_folder_id: str = ""
    group_drive_user_id: str = ""
    group_admin_to: tuple[str, ...] = ()
    group_silent_after_hours: int = 36
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
            raw = _unquoted(source.get(var.env))
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
            engine_key = _text(source, key_var)
            if not engine_key:
                problems.append(f"{key_var} is not set — the API key for the {engine} transcription engine")
            values["engine_key"] = engine_key
            if engine_key:
                engine_keys[engine] = engine_key
        if engine == "azure" and not values.get("azure_region"):
            problems.append("AZURE_SPEECH_REGION is not set — required when TRANSCRIBE_ENGINE=azure")

        # One key covers both when they are the same provider; still explicit, never guessed
        # from an unrelated variable.
        #
        # Which is what it was doing. The fallback fired on the mere presence of
        # OPENAI_API_KEY, and the shipped default analysis provider is Anthropic — so an
        # OpenAI key was accepted as the Anthropic credential and the "ANALYSIS_API_KEY is
        # not set" problem was struck off the list. The service then starts clean and every
        # analysis call comes back 401, which reads like the provider having a bad morning
        # rather than like a setting nobody set. Now the substitution only happens when the
        # analysis pass is genuinely going to call OpenAI, worked out the way extract.py
        # works it out: the explicit setting first, then the host in the base URL.
        if "analysis_api_key" not in values:
            declared = _text(source, "ANALYSIS_PROVIDER").lower()
            url = str(
                values.get("analysis_base_url")
                or _text(source, "ANALYSIS_BASE_URL")
                or "https://api.anthropic.com"
            ).lower()
            if declared:
                provider = declared
            elif "anthropic" in url:
                provider = "anthropic"
            elif "openai" in url or "azure.com" in url:
                provider = "openai"
            else:
                provider = ""
            fallback = _text(source, "OPENAI_API_KEY")
            if fallback and provider == "openai":
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
            route_key = _text(source, route_key_var)
            if route_key:
                engine_keys[override] = route_key
            else:
                problems.append(
                    f"{route_key_var} is not set — {_route_phrase(route)} is set to "
                    f"transcribe with {override}, which needs its own API key"
                )
            if override == "azure" and not _text(source, "AZURE_SPEECH_REGION"):
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
                var for var in LEGACY_FOLDER_VARS if _text(source, var)
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
        # The same rule as GATE_REVIEW_BASE_URL just above, in the same words, for the other
        # three addresses this service sends something of its own to. The reason is the same
        # as well: an http:// endpoint puts whatever travels over it in clear at every hop
        # between this machine and that host, and a key read off the wire is a key somebody
        # else is now using. It is also where an on-path attacker puts the redirect that
        # sends the next request — key and all — to a host nobody chose.
        #
        # A developer pointing at a mock on their own machine is a real case and refusing it
        # outright would be the code being clever about somebody's laptop. So there is one
        # way to say so deliberately, and no way to end up in plaintext by accident: with
        # ALLOW_PLAINTEXT_ENDPOINTS set it starts, and the notice says out loud what is
        # travelling in the open.
        plaintext_allowed = _flag(source, "ALLOW_PLAINTEXT_ENDPOINTS")
        for name, carries in (
            ("engine_base_url",
             "the transcription engine's API key travels over it, and the audio of every "
             "recording with it"),
            ("analysis_base_url",
             "the analysis API key travels over it, and the whole transcript with it — "
             "unredacted, before anything has been held back"),
            ("heartbeat_url",
             "it names this installation to the monitor that watches it, and anyone who can "
             "read the address can send the same ping and keep the alarm quiet"),
        ):
            address = str(values.get(name) or "").strip()
            if not address or address.startswith("https://"):
                continue
            # The heartbeat URL is a secret field — it carries an account identifier in its
            # path — so it is named and never printed, the way it is everywhere else.
            shown = _env_of(name) if name == "heartbeat_url" else f"{_env_of(name)}={address!r}"
            if plaintext_allowed and address.startswith("http://"):
                notices.append(
                    f"{_env_of(name)} is a plain http:// address and "
                    "ALLOW_PLAINTEXT_ENDPOINTS is set, so it is allowed. What travels over "
                    f"it travels in the open: {carries}. That is only ever right for "
                    "something running on this machine."
                )
                continue
            problems.append(
                f"{shown} must start with https:// — {carries}. If it is a mock or a proxy "
                "on this machine and plain http really is what you want, set "
                "ALLOW_PLAINTEXT_ENDPOINTS=true: it will then start, and say what is "
                "travelling in the open rather than saying nothing."
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
            address = _text(source, reviewer_var)
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

        # Two routes pooling into one output folder is deliberately allowed — he asked for
        # it, and _validate_routes says so in as many words. But that rule was written when
        # a route was a KIND of recording: pooling calls and site meetings into one folder
        # is tidiness, and forbidding it would be the code being clever about his filing.
        #
        # A route is now also how a PERSON is carried — that is what ROUTE_<NAME>_REVIEWER
        # is for. Two routes with different reviewers sharing one output folder is not
        # tidiness: it is one person's transcripts, summaries and proposals landing where
        # another person reads them, and it starts perfectly clean with nothing said.
        #
        # Still not forbidden, because a shared team folder is a real thing somebody may
        # want on purpose. But it is no longer SILENT: refusing would break a rule he set,
        # and saying nothing would leave a disclosure looking like a working configuration.
        # A route with no reviewer named is the service owner, who is a person too — so
        # "one named, one not" is two people, not one.
        pooled: dict[str, list[Route]] = {}
        for route in routes:
            if not route.enabled or not route.output_folder_id:
                continue
            pooled.setdefault(route.output_folder_id, []).append(route)
        for sharing in pooled.values():
            if len(sharing) < 2:
                continue
            whose = {reviewers.get(route.name, "") for route in sharing}
            if len(whose) < 2:
                continue
            notices.append(
                _join_phrases([_route_phrase(route) for route in sharing])
                + " all write into the SAME output folder, and they do not have the same "
                "reviewer — so they are carrying different people. Every transcript, "
                "summary and list of proposals from each of them lands where the others "
                "can read it. That is allowed, and it is right for a shared team folder; "
                "it is the wrong thing entirely if these are meant to be one person each. "
                "Give them separate output folders if so."
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

        # Every name in the environment that nothing above read. A misspelt setting used to
        # load perfectly and do the opposite of what the file said — GATE_MODEE=on left the
        # gate in shadow, withholding nothing while the operator believed passages were
        # being held — and `config set` refuses an unknown name, but SETUP.md and
        # ops/DEPLOY.md both say to copy .env.example and edit it by hand, which never goes
        # near `config set`. So the same check is done here, over the whole environment.
        route_problems, route_notices = _route_variable_reports(source, routes, declared)
        problems.extend(route_problems)
        notices.extend(route_notices)
        problems.extend(_misspelt_variable_problems(source, routes))

        # The expiry dates. A date that is set is checked for shape here and counted down in
        # the morning email; a date that is NOT set was indistinguishable from a countdown
        # that has not started yet, and silence is exactly the wrong answer — an expired
        # Entra client secret is the single most likely way this service dies, after a year
        # of working perfectly, on a morning nobody was warned about. So an in-use
        # credential with no date says so, every day, the way a missing site list does.
        # Only the credentials actually in use: a key that was never configured has nothing
        # to expire.
        undated: list[str] = []
        for name, credential in (
            ("graph_secret_expires_on", "graph_client_secret"),
            ("engine_key_expires_on", "engine_key"),
            ("analysis_key_expires_on", "analysis_api_key"),
        ):
            raw = str(values.get(name) or "").strip()
            if not raw:
                if str(values.get(credential) or "").strip():
                    undated.append(_env_of(name))
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

        if undated:
            notices.append(
                "No expiry date is set for: " + ", ".join(undated) + ". Nothing is wrong "
                "today, and nothing counts down either: the morning email can only warn "
                "before a credential stops working if it knows the date. The one that "
                "matters most is the OneDrive app secret, which expires on a date chosen "
                "when it was created and takes the whole service down that morning with no "
                "notice of any kind. Put each date in, as YYYY-MM-DD."
            )

        values["notices"] = tuple(notices)

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
    raw = _text(source, "ROUTES")
    if not raw:
        return (
            (
                Route(
                    name=DEFAULT_ROUTE,
                    label="Recordings",
                    source_folder_id=_text(source, "SOURCE_FOLDER_ID"),
                    output_folder_id=_text(source, "OUTPUT_FOLDER_ID"),
                    archive_folder_id=_text(source, "ARCHIVE_FOLDER_ID"),
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
            return _text(source, route_env_var(name, suffix))

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


def _longest_stem(key: str, stems: Mapping[str, str]) -> str | None:
    """Which route's variables this key belongs to, or None — longest name first.

    Longest first because ``site`` and ``site-meetings`` can both be configured, and
    ``ROUTE_SITE_MEETINGS_SOURCE`` belongs to the second even though it starts with the
    first one's stem.
    """
    found: str | None = None
    for stem in stems:
        if key.startswith(f"ROUTE_{stem}_") and (found is None or len(stem) > len(found)):
            found = stem
    return found


def _route_variable_reports(
    source: Mapping[str, str], routes: Sequence[Route], declared: bool
) -> tuple[list[str], list[str]]:
    """Every ``ROUTE_*`` variable in the environment that no route will ever read.

    A route's settings are *pulled*: each route asks for the seven names it expects, and
    nothing ever looked at what else was in the file. So ``ROUTE_CALLS_ENABLD=false`` left
    the route watching, ``ROUTE_CALLS_ARCHIV`` gave it no archive folder at all, and
    ``ROUTE_SITE_ENABLE=false`` left the route enabled — each of them a line saying the
    opposite of what the service does, with nothing said anywhere.

    A key naming a route that IS configured can only be a typo in the suffix: there are
    seven suffixes and there will never be others, so that one is refused rather than
    mentioned. A key naming a route that is NOT configured is usually a leftover from a
    route that was renamed or removed, which does nothing either way — a notice. Unless the
    name it carries is a near-miss of a route that IS configured, which is a typo again.
    """
    stems: dict[str, str] = {route_env_stem(route.name): route.name for route in routes}
    # A name listed in ROUTES that was refused above — not a usable route name, or listed
    # twice — has already been reported by name. Its variables are not reported again on
    # top of that, which would be two complaints about one mistake.
    for part in _text(source, "ROUTES").replace("\n", ",").split(","):
        listed = part.strip()
        if listed:
            stems.setdefault(route_env_stem(listed), listed)

    configured = [route.name for route in routes]
    problems: list[str] = []
    notices: list[str] = []
    stray_reviewers: list[str] = []
    unread: list[str] = []
    ignored_without_routes: list[str] = []

    for key in sorted(k for k in source if k.startswith("ROUTE_")):
        if not _text(source, key):
            continue                      # an empty variable configures nothing either way
        stem = _longest_stem(key, stems)
        if stem is not None:
            suffix = key[len("ROUTE_") + len(stem) + 1:]
            if suffix in ROUTE_SUFFIXES:
                if declared or suffix == "REVIEWER":
                    continue              # read, exactly as the file expects
                ignored_without_routes.append(key)
                continue
            meant = difflib.get_close_matches(suffix, ROUTE_SUFFIXES, n=1, cutoff=0.5)
            problems.append(
                f"{key} is not one of a route's settings, so nothing reads it and what it "
                f"says is not what {stems[stem]!r} does. A route has seven settings: "
                + ", ".join(ROUTE_SUFFIXES) + "."
                + (f" Did you mean ROUTE_{stem}_{meant[0]}?" if meant else "")
            )
            continue

        # The key names some other route. Work out which name it is carrying, so the
        # sentence can show it rather than describe it.
        suffix = next((s for s in ROUTE_SUFFIXES if key.endswith("_" + s)), "")
        middle = key[len("ROUTE_"):len(key) - len(suffix) - 1] if suffix else ""
        # ``ROUTE__SOURCE`` names no route at all, and _suggest_route_name answers an empty
        # string with an example name — which would have this sentence inventing a route.
        named = _suggest_route_name(middle) if any(c.isalnum() for c in middle) else ""
        near = (
            difflib.get_close_matches(named, configured, n=1, cutoff=_NEAR_MISS)
            if named else []
        )
        if near:
            problems.append(
                f"{key} names a route called {named!r}, and there is no such route — the "
                f"one this service runs is called {near[0]!r}. Nothing reads {key}, so "
                f"what it says is not what {near[0]!r} does. Did you mean "
                f"{route_env_var(near[0], suffix)}?"
            )
        elif suffix == "REVIEWER":
            stray_reviewers.append(key)
        else:
            unread.append(key)

    if stray_reviewers:
        notices.append(
            "These name a reviewer for a route that is not configured, so they assign "
            "nobody: " + ", ".join(stray_reviewers) + ". Held passages from a route with "
            "no reviewer go to the service owner."
        )
    if ignored_without_routes:
        notices.append(
            "This file has no ROUTES line, so the service watches the single folder in "
            "SOURCE_FOLDER_ID and writes to OUTPUT_FOLDER_ID — and these are not read at "
            "all: " + ", ".join(ignored_without_routes) + ". List the route in ROUTES to "
            "use them, or delete them so the file says what the service does."
        )
    if unread:
        notices.append(
            "No route reads these, so they do nothing: " + ", ".join(unread) + ". They "
            "name a route that is not configured, or a setting a route does not have — "
            "which is what a renamed or deleted route leaves behind. Delete them so the "
            "file says what the service does."
        )
    return problems, notices


def _misspelt_variable_problems(
    source: Mapping[str, str], routes: Sequence[Route]
) -> list[str]:
    """Names nothing here reads, when they look like names it does.

    The whole environment, not the ``.env``: under systemd the file is loaded with
    ``EnvironmentFile=``, so what arrives is the file's names *and* systemd's own —
    ``INVOCATION_ID``, ``JOURNAL_STREAM``, ``STATE_DIRECTORY`` — and the shell's on top of
    that. Refusing to start on anything unrecognised would therefore refuse to start on an
    ordinary host, which would be a worse fault than the one being fixed. So a leftover name
    is only ever compared against the real ones when it begins with a word one of them
    begins with, and only a near-miss is reported at all: ``SMTP_STARTLS`` can only be
    ``SMTP_STARTTLS`` misspelt, while ``LANGUAGE`` is the machine's locale and is none of
    our business even though it is one letter from ``LANGUAGES``. Anything else unknown is
    passed over in silence — ``HTTP_PROXY`` and ``LOG_DIR`` are somebody else's settings on
    plenty of ordinary hosts, and a warning about them every morning teaches a person to
    stop reading the warnings.

    A near-miss is refused rather than mentioned, whether or not the real setting is also
    set. Both ways round it is a line that lies: with the real one unset the service is
    quietly running the default, and with the real one set it is doing what that one says
    while the file offers two answers and the reader believes the wrong one.
    """
    known = set(_KNOWN_ENV_NAMES)
    for route in routes:
        for suffix in ROUTE_SUFFIXES:
            known.add(route_env_var(route.name, suffix))
    candidates = sorted(known)

    problems: list[str] = []
    for key in sorted(source):
        if key in known or key.startswith("ROUTE_") or not _text(source, key):
            continue
        if key.split("_", 1)[0] not in _KNOWN_ENV_FAMILIES:
            continue
        close = difflib.get_close_matches(key, candidates, n=1, cutoff=_NEAR_MISS)
        if not close:
            continue
        meant = close[0]
        problems.append(
            f"{key} is not a setting this service reads, so nothing uses it — and {meant}, "
            f"which is what it looks like a misspelling of, "
            + (
                f"is set to something else, so that is what the service is doing"
                if _text(source, meant)
                else "is not set, so that setting is running at its default"
            )
            + f". Correct the spelling to {meant}, or delete the line."
        )
    return problems


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


def _unquoted(raw: Any) -> Any:
    """One raw value with a matched pair of surrounding quotes taken off.

    Three things deliver these settings and they did not agree about quotes. systemd's
    ``EnvironmentFile=`` strips a surrounding pair, the wizard's own reader strips a pair,
    and ``docker --env-file`` does not — so ``ROUTE_DEFAULT_ARCHIVE=""``, which is how the
    wizard writes "this route is never archived", arrived by the third route as the
    two-character string ``""`` and was taken for a driveItem id. Everything downstream then
    believes there is an archive folder, and every monthly move fails against a folder that
    was never there. Stripping the pair here is what makes the three paths agree, and
    agreeing is the whole point: which of them was used is not something a person should
    have to know.

    A value that genuinely begins and ends with a quote character loses them. That is the
    price of the three agreeing, and no setting this service reads is meant to have any.
    """
    if not isinstance(raw, str):
        return raw
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1].strip()
    return text


def _text(source: Mapping[str, str], name: str) -> str:
    """One environment variable, unquoted and stripped, never None."""
    return str(_unquoted(source.get(name)) or "").strip()


def _flag(source: Mapping[str, str], name: str) -> bool:
    """An opt-in variable, read the way ``_coerce`` reads any other boolean setting."""
    return _text(source, name).lower() in ("1", "true", "yes", "on")


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
