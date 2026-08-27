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

Environment variables, all read by :meth:`Config.from_env`::

    GRAPH_TENANT_ID GRAPH_CLIENT_ID GRAPH_CLIENT_SECRET GRAPH_USER_ID
    SOURCE_FOLDER_ID OUTPUT_FOLDER_ID ARCHIVE_FOLDER_ID
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
from dataclasses import dataclass, field, fields as dataclass_fields
from typing import Any, Mapping, Sequence

log = logging.getLogger("transcriber.config")

__all__ = ["Config", "ConfigError", "ENGINES", "ENGINE_KEY_VARS", "make_private_dir"]

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
        "analysis_api_key",
        "smtp_user",
        "smtp_password",
        "smtp_from",
        "smtp_to",
        "heartbeat_url",
    }
)

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
    _Var("source_folder_id", "SOURCE_FOLDER_ID", "str", _REQUIRED, "driveItem id of the recordings folder (/CALLS)"),
    _Var("output_folder_id", "OUTPUT_FOLDER_ID", "str", _REQUIRED, "driveItem id of the folder the .md outputs are written to"),
    _Var("archive_folder_id", "ARCHIVE_FOLDER_ID", "str", _REQUIRED, "driveItem id of the folder aged recordings are moved to"),
    _Var("orphan_folder_id", "ORPHAN_FOLDER_ID", "str", "", "optional folder a half-written output set is moved aside to; left unset, strays are named in the error and replaced on the next attempt"),
    _Var("graph_secret_expires_on", "GRAPH_SECRET_EXPIRES_ON", "str", "", "ISO date the Entra client secret expires; the digest counts down to it (optional but strongly recommended)"),
    # --- transcription engine --------------------------------------------------------
    _Var("engine", "TRANSCRIBE_ENGINE", "str", _REQUIRED, "one of: " + ", ".join(ENGINES)),
    _Var("engine_base_url", "ENGINE_BASE_URL", "str", "", "override the engine's default endpoint (optional)"),
    _Var("azure_region", "AZURE_SPEECH_REGION", "str", "", "Azure Speech region, required when TRANSCRIBE_ENGINE=azure"),
    # --- the analysis pass -----------------------------------------------------------
    _Var("analysis_api_key", "ANALYSIS_API_KEY", "str", _REQUIRED, "API key for the analysis models (defaults to OPENAI_API_KEY when that is set)"),
    _Var("analysis_base_url", "ANALYSIS_BASE_URL", "str", "https://api.openai.com/v1", "analysis API base url"),
    _Var("analysis_model_cheap", "ANALYSIS_MODEL_CHEAP", "str", "gpt-4o-mini", "the router model — classifies every recording, so nothing is skipped on a guess"),
    _Var("analysis_model_strong", "ANALYSIS_MODEL_STRONG", "str", "gpt-4o", "the model that runs on substantive recordings"),
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
    engine_base_url: str = ""
    azure_region: str = ""
    # analysis
    analysis_api_key: str = ""
    analysis_base_url: str = "https://api.openai.com/v1"
    analysis_model_cheap: str = "gpt-4o-mini"
    analysis_model_strong: str = "gpt-4o"
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
        key_var = ENGINE_KEY_VARS.get(engine)
        if key_var:
            engine_key = (source.get(key_var) or "").strip()
            if not engine_key:
                problems.append(f"{key_var} is not set — the API key for the {engine} transcription engine")
            values["engine_key"] = engine_key
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

        # Three distinct folders, checked here rather than discovered later. Point
        # OUTPUT_FOLDER_ID at the source folder — a natural reading of the record's own
        # "folder = wherever the transcriber writes" — and the live poll classifies every
        # non-audio item in it as our own output and drops it with no ledger row at all. The
        # nightly sweep shares that classify call, so the backstop inherits the primary's
        # exact blind spot, and the only symptom is a zero-arrival alert naming the wrong cause.
        named = {
            "SOURCE_FOLDER_ID": values.get("source_folder_id"),
            "OUTPUT_FOLDER_ID": values.get("output_folder_id"),
            "ARCHIVE_FOLDER_ID": values.get("archive_folder_id"),
            "ORPHAN_FOLDER_ID": values.get("orphan_folder_id"),
        }
        given = [(name, str(value).strip()) for name, value in named.items() if str(value or "").strip()]
        for index, (first_name, first_value) in enumerate(given):
            for second_name, second_value in given[index + 1:]:
                if first_value == second_value:
                    problems.append(
                        f"{first_name} and {second_name} are the same folder "
                        f"({first_value!r}); they must be three different folders, or files "
                        f"this service writes are indistinguishable from files it must read"
                    )

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
_SPEC_NAMES = {v.name for v in _SPEC}
_FIELD_NAMES = {f.name for f in dataclass_fields(Config)} - {"engine_key"}
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
