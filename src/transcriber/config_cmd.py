"""``transcriber config`` — read and change one setting without opening an editor.

    transcriber config list                                 every setting, secrets masked
    transcriber config get ANALYSIS_MODEL_STRONG
    transcriber config set ANALYSIS_MODEL_STRONG claude-opus-5
    transcriber config set --engine elevenlabs              the friendly alias

"Customisable" has to mean changeable by the person who owns the service, and asking a
building consultant to hand-edit a forty-line ``.env`` at 06:30 because the model id needs
changing is not that.

Three rules this module exists to keep:

* **It validates before it writes.** An unknown key, a number out of range, or a model id
  that is not one of the documented ones is refused here, with the valid options printed.
  The alternative is a ``.env`` that looks fine and fails on the first recording of the
  morning, which is precisely the class of failure this whole service was built to remove.
  The check is done twice: the value on its own, and then the **whole file re-read through
  the real** :class:`~transcriber.config.Config`, so a change that is fine by itself and
  wrong in combination (a lease shorter than the settle interval, say) is caught too.
* **It never prints a secret.** A key is shown as its last four characters and an address
  is not shown at all — the house rule is that this service never prints an email address
  anywhere, for any reason, and ``config list`` is an anywhere.
* **It writes through** :func:`~transcriber.setup_wizard.write_env_file`, so the grouping,
  the header and the 0600 mode the wizard established survive being edited by this command.
  Nothing here writes ``.env`` by any other route.

Routes are deliberately *not* settable here: ``ROUTES`` and the ``ROUTE_*`` variables are
six settings that have to agree with each other, and they have their own command with the
folder pickers and the cross-route validation. Naming one is answered with a pointer to it.
"""

from __future__ import annotations

import argparse
import datetime
import difflib
import os
import re
import sys
from dataclasses import dataclass
from typing import Any, Mapping

from . import config as config_mod
from .config import ENGINES, GATE_MODES, Config, ConfigError
from .diskbudget import MINIMUM_WORK_DIR_MAX_BYTES, format_bytes, parse_bytes
from .setup_wizard import load_env_file, mask, routes_from_values, write_env_file

__all__ = [
    "ANALYSIS_MODELS",
    "ANALYSIS_PROVIDERS",
    "LOG_FORMATS",
    "LOG_LEVELS",
    "SETTINGS",
    "Setting",
    "ALIASES",
    "add_arguments",
    "run",
    "check_value",
    "comments_would_be_lost",
]

EXIT_OK = 0
EXIT_FAILED = 1

#: The model ids this service is documented against, and the only ones ``set`` accepts for
#: the Anthropic provider. They come from the bundled ``claude-api`` reference by way of
#: ``extract.py``, not from memory — and that is the point of refusing anything else. A
#: hallucinated model id is accepted by a text editor, accepted by the ``.env``, accepted at
#: startup, and then refused by the API on the first recording of the day at 06:00. Better
#: to be refused at the keyboard, by name, with the two real ids printed.
ANALYSIS_MODELS = ("claude-haiku-4-5", "claude-opus-5")

ANALYSIS_PROVIDERS = ("anthropic", "openai")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
LOG_FORMATS = ("", "json", "text")

#: What a person calls a setting when they are not reading the ``.env``. ``config set
#: --engine elevenlabs`` is the same edit as ``config set TRANSCRIBE_ENGINE elevenlabs``,
#: spelled the way somebody actually asks for it.
ALIASES: dict[str, str] = {
    "engine": "TRANSCRIBE_ENGINE",
    "model": "ANALYSIS_MODEL_STRONG",
    "cheap-model": "ANALYSIS_MODEL_CHEAP",
    "digest-hour": "DIGEST_HOUR",
    "ledger": "LEDGER_PATH",
}

#: The ``.env`` variables that are a route's, not the service's. Listed and readable here,
#: never written here.
_ROUTE_VAR_RE = re.compile(
    r"^ROUTE_[A-Z0-9_]+_(LABEL|SOURCE|OUTPUT|ARCHIVE|ENGINE|ENABLED|REVIEWER)$"
)


@dataclass(frozen=True)
class Setting:
    """One thing a person can look at or change, and everything needed to check it.

    ``show`` decides what a reader is allowed to see:

    * ``plain`` — the value, as it is.
    * ``masked`` — the last four characters of an API key, enough to tell two apart and
      not enough to use one.
    * ``private`` — that it is set, and nothing more. For the address-shaped settings and
      the heartbeat URL, which carries an account identifier in its path.
    """

    name: str
    kind: str = "str"                 # str | int | bool | csv | choice
    description: str = ""
    group: str = "other"
    show: str = "plain"               # plain | masked | private
    choices: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    required: bool = False
    default: str = ""
    managed_by: str = ""              # set for the route variables: another command owns them

    @property
    def secret(self) -> bool:
        return self.show != "plain"


# ---------------------------------------------------------------------------------------
# the registry — derived from config.py's own spec, so the two cannot drift
# ---------------------------------------------------------------------------------------

try:
    _CONFIG_SPEC = config_mod._SPEC
    _CONFIG_REQUIRED = config_mod._REQUIRED
except AttributeError as exc:  # pragma: no cover - import-time guard
    raise RuntimeError(
        "config.py no longer exposes the variable spec this command reads. Rather than "
        "keep a second copy of every setting here — which would eventually disagree with "
        "the real one and refuse a value the service accepts — this is a hard failure."
    ) from exc


#: Which group each setting is printed under, in this order. Every setting has to appear in
#: exactly one of them; the check at the bottom of this module enforces it, so a setting
#: added to config.py cannot quietly become invisible to ``config list``.
GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Microsoft / OneDrive", (
        "GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET", "GRAPH_USER_ID",
        "GRAPH_SECRET_EXPIRES_ON", "ORPHAN_FOLDER_ID",
        "SOURCE_FOLDER_ID", "OUTPUT_FOLDER_ID", "ARCHIVE_FOLDER_ID",
    )),
    ("transcription", (
        "TRANSCRIBE_ENGINE", "OPENAI_API_KEY", "ELEVENLABS_API_KEY", "AZURE_SPEECH_KEY",
        "AZURE_SPEECH_REGION", "ENGINE_BASE_URL", "ENGINE_KEY_EXPIRES_ON",
    )),
    ("the AI pass", (
        "ANALYSIS_PROVIDER", "ANALYSIS_API_KEY", "ANALYSIS_BASE_URL",
        "ANALYSIS_MODEL_CHEAP", "ANALYSIS_MODEL_STRONG", "ANALYSIS_KEY_EXPIRES_ON",
    )),
    ("the morning email", (
        "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM", "SMTP_TO",
        "SMTP_STARTTLS", "DIGEST_HOUR", "HEARTBEAT_URL",
        # What the email calls a backlog. Both are about wording, and the wording is most
        # of the value: a queue read as failure is the confusion this service removes.
        "QUEUE_STALE_HOURS", "STUCK_AFTER_HOURS",
    )),
    ("the group view", (
        # Each person runs their own copy, so each copy knows about one person. These are
        # what let one of them report on all of them. Every one is optional: unset, a copy
        # writes nothing and says nothing about a group.
        "INSTANCE_NAME", "GROUP_FOLDER_ID", "GROUP_DRIVE_USER_ID", "GROUP_ADMIN_TO",
        "GROUP_SILENT_AFTER_HOURS",
    )),
    ("where it keeps its notes", (
        "LEDGER_PATH", "WORK_DIR", "WORK_DIR_MAX_BYTES", "WORK_DIR_KEEP_FINISHED_HOURS",
    )),
    ("timing", (
        "POLL_INTERVAL_S", "SETTLE_INTERVAL_S", "LEASE_SECONDS", "CONCURRENCY",
        "ENGINE_MAX_CONCURRENT", "ENGINE_MAX_PER_MINUTE",
        "MAX_ATTEMPTS", "ARCHIVE_AGE_DAYS", "SWEEP_HOUR", "ARCHIVE_DAY_OF_MONTH", "TIMEZONE",
    )),
    ("what it expects to hear", ("LANGUAGES", "VOCABULARY", "VOCABULARY_FILE")),
    ("the sensitivity gate", ("GATE_MODE", "GATE_HELD_STORE", "GATE_REVIEW_BASE_URL")),
    ("naming a recording that arrived without one", (
        "NAMING", "NAMING_APPLY", "NAMING_SITES_FILE", "NAMING_MIN_SECONDS",
        "ENGINE_SITE_NAMES", "SITE_EVIDENCE", "CLOSEA_DROP",
        "NAMING_OPENING_SECONDS")),
    ("http and logging", ("HTTP_TIMEOUT_S", "MAX_RETRIES", "LOG_LEVEL", "LOG_FORMAT")),
)

#: Extra rules laid over what config.py's spec already says: what may be shown, what values
#: are allowed, and the ranges config.py itself enforces at startup. Kept here rather than
#: inferred, because "0 is not a sensible poll interval" is a judgement, not a type.
_RULES: dict[str, dict[str, Any]] = {
    "GRAPH_CLIENT_SECRET": {"show": "masked"},
    "GRAPH_USER_ID": {"show": "private"},
    "OPENAI_API_KEY": {"show": "masked"},
    "ELEVENLABS_API_KEY": {"show": "masked"},
    "AZURE_SPEECH_KEY": {"show": "masked"},
    "ANALYSIS_API_KEY": {"show": "masked"},
    "SMTP_PASSWORD": {"show": "masked"},
    "SMTP_USER": {"show": "private"},
    "SMTP_FROM": {"show": "private"},
    "SMTP_TO": {"show": "private"},
    "HEARTBEAT_URL": {"show": "private"},
    "TRANSCRIBE_ENGINE": {"choices": tuple(ENGINES)},
    "GATE_MODE": {"choices": tuple(GATE_MODES)},
    # A floor of one minute. Zero or a negative number disables the guard that separates a
    # site genuinely named twice from an engine's own repetitions on forty seconds of wind
    # noise -- the one condition that catches a hallucinated site name, since "said twice"
    # and "said early" are the hallucination's signature rather than evidence against it.
    "NAMING_MIN_SECONDS": {"minimum": 60, "maximum": 3600},
    "NAMING_OPENING_SECONDS": {"minimum": 10, "maximum": 600},
    "LOG_LEVEL": {"choices": LOG_LEVELS},
    "SMTP_PORT": {"minimum": 1, "maximum": 65535},
    "DIGEST_HOUR": {"minimum": 0, "maximum": 23},
    "SWEEP_HOUR": {"minimum": 0, "maximum": 23},
    "ARCHIVE_DAY_OF_MONTH": {"minimum": 1, "maximum": 28},
    "POLL_INTERVAL_S": {"minimum": 1},
    "SETTLE_INTERVAL_S": {"minimum": 1},
    "LEASE_SECONDS": {"minimum": 1},
    "CONCURRENCY": {"minimum": 1, "maximum": 32},
    "WORK_DIR_KEEP_FINISHED_HOURS": {"minimum": 1, "maximum": 8760},
    "ENGINE_MAX_CONCURRENT": {"minimum": 1, "maximum": 32},
    "ENGINE_MAX_PER_MINUTE": {"minimum": 0, "maximum": 10000},
    "MAX_ATTEMPTS": {"minimum": 1, "maximum": 20},
    "ARCHIVE_AGE_DAYS": {"minimum": 1},
    "HTTP_TIMEOUT_S": {"minimum": 1},
    "MAX_RETRIES": {"minimum": 1, "maximum": 20},
}

#: Settings the service reads straight from the environment rather than through ``Config``,
#: so they are not in its spec and would otherwise be uneditable by this command.
_EXTRA: tuple[Setting, ...] = (
    Setting("OPENAI_API_KEY", "str", "API key for the OpenAI transcription engine; also the "
            "fallback for ANALYSIS_API_KEY", show="masked"),
    Setting("ELEVENLABS_API_KEY", "str", "API key for the ElevenLabs transcription engine",
            show="masked"),
    Setting("AZURE_SPEECH_KEY", "str", "key for the Azure Speech transcription engine",
            show="masked"),
    Setting("ANALYSIS_PROVIDER", "choice", "which API shape the analysis pass speaks",
            choices=ANALYSIS_PROVIDERS, default="anthropic"),
    Setting("LOG_FORMAT", "choice", "'json' for one JSON object per log line; empty for "
            "readable lines", choices=LOG_FORMATS),
)

#: The date-shaped settings. Wrong here means a countdown in the morning email that counts
#: down to nothing.
_DATE_SETTINGS = frozenset(
    {"GRAPH_SECRET_EXPIRES_ON", "ENGINE_KEY_EXPIRES_ON", "ANALYSIS_KEY_EXPIRES_ON"}
)

#: The single-folder settings from before routes existed. Still real, still editable — but
#: ignored completely once ``ROUTES`` is set, which is checked when one is written.
_LEGACY_FOLDER_SETTINGS = frozenset(config_mod.LEGACY_FOLDER_VARS)


def _build_settings() -> dict[str, Setting]:
    groups = {name: group for group, members in GROUPS for name in members}
    out: dict[str, Setting] = {}
    for var in _CONFIG_SPEC:
        rules = _RULES.get(var.env, {})
        required = var.default is _CONFIG_REQUIRED
        out[var.env] = Setting(
            name=var.env,
            kind="choice" if rules.get("choices") else var.kind,
            description=var.description,
            group=groups.get(var.env, "other"),
            show=str(rules.get("show", "plain")),
            choices=tuple(rules.get("choices", ())),
            minimum=rules.get("minimum"),
            maximum=rules.get("maximum"),
            required=required,
            default="" if required else _default_text(var.default, var.kind),
        )
    for extra in _EXTRA:
        rules = _RULES.get(extra.name, {})
        out[extra.name] = Setting(
            name=extra.name,
            kind=extra.kind,
            description=extra.description,
            group=groups.get(extra.name, "other"),
            show=str(rules.get("show", extra.show)),
            choices=extra.choices or tuple(rules.get("choices", ())),
            minimum=rules.get("minimum", extra.minimum),
            maximum=rules.get("maximum", extra.maximum),
            required=extra.required,
            default=extra.default,
        )
    return out


def _default_text(value: Any, kind: str = "str") -> str:
    if kind == "bytes":
        # 4294967296 is a true answer to "what does it use?" and a useless one to read.
        return format_bytes(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (tuple, list)):
        return ",".join(str(v) for v in value)
    return "" if value is None else str(value)


SETTINGS: dict[str, Setting] = _build_settings()

_UNGROUPED = sorted(n for n, s in SETTINGS.items() if s.group == "other")
if _UNGROUPED:  # pragma: no cover - import-time guard
    raise RuntimeError(
        "these settings would not be printed by `transcriber config list` because no group "
        f"claims them: {', '.join(_UNGROUPED)}. Add each to GROUPS in config_cmd.py — a "
        "setting nobody can see is a setting nobody can fix."
    )
_UNKNOWN_IN_GROUPS = sorted(
    {name for _group, members in GROUPS for name in members} - set(SETTINGS)
)
if _UNKNOWN_IN_GROUPS:  # pragma: no cover - import-time guard
    raise RuntimeError(
        "GROUPS names settings that do not exist: " + ", ".join(_UNKNOWN_IN_GROUPS)
    )


# ---------------------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------------------

def _analysis_provider(env: Mapping[str, str]) -> str:
    """Which API the analysis models have to be valid for.

    Explicit setting first, then the base url, then the default — the same order
    ``extract.py`` resolves it in, so this command and the pass agree about what a valid
    model id is.
    """
    explicit = str(env.get("ANALYSIS_PROVIDER") or "").strip().lower()
    if explicit:
        return explicit
    base = str(env.get("ANALYSIS_BASE_URL") or "").strip().lower()
    if "anthropic.com" in base:
        return "anthropic"
    if base and "openai" in base:
        return "openai"
    if base:
        return "unknown"
    return "anthropic"


def check_value(name: str, raw: str, env: Mapping[str, str]) -> str:
    """Everything wrong with writing ``raw`` into ``name``, or '' if it is fine.

    ``env`` is the rest of the ``.env`` as it will be after the change, because several of
    these rules are about the combination — which provider the model has to exist on,
    whether routes have taken over from the single-folder settings.
    """
    setting = SETTINGS.get(name)
    if setting is None:
        return _unknown_key_message(name)
    if setting.managed_by:
        return (
            f"{name} belongs to a route, and routes are changed with "
            f"`{setting.managed_by}` so that the folders can be picked from your drive and "
            "checked against each other."
        )

    value = raw.strip()

    if not value:
        if setting.required:
            return (
                f"{name} cannot be emptied — {setting.description}. The service will not "
                "start without it."
            )
        return ""  # clearing an optional setting is a legitimate edit

    if setting.choices:
        allowed = [c for c in setting.choices if c]
        if value.lower() not in [c.lower() for c in setting.choices]:
            empties = " (or leave it empty)" if "" in setting.choices else ""
            return f"{name}={value!r} is not one of: " + ", ".join(allowed) + empties

    if setting.kind == "int":
        try:
            number = int(value)
        except ValueError:
            return f"{name}={value!r} is not a whole number — {setting.description}"
        if setting.minimum is not None and number < setting.minimum:
            return (
                f"{name}={number} is below the smallest usable value, {setting.minimum} — "
                f"{setting.description}"
            )
        if setting.maximum is not None and number > setting.maximum:
            return (
                f"{name}={number} is above the largest usable value, {setting.maximum} — "
                f"{setting.description}"
            )

    if setting.kind == "bytes":
        # Both of the rules ``Config.from_env`` applies, applied here as well. Without them
        # this command writes a size the next start refuses — a service that will not come
        # back up after a restart, discovered whenever the next restart happens to be. That
        # is precisely the 06:00-on-a-Tuesday discovery `config set` exists to prevent.
        try:
            size = parse_bytes(value)
        except ValueError as exc:
            return f"{name}={value!r} is not a size — {exc}"
        if 0 < size < MINIMUM_WORK_DIR_MAX_BYTES:
            return (
                f"{name}={format_bytes(size)} is smaller than one ordinary recording needs "
                f"to be transcribed — an hour-long call is around 58 MB, and it is "
                f"downloaded and then split into pieces beside itself. The smallest "
                f"workable value is {format_bytes(MINIMUM_WORK_DIR_MAX_BYTES)}; 0 turns the "
                f"limit off altogether."
            )

    if setting.kind == "bool" and value.lower() not in (
        "1", "true", "yes", "on", "0", "false", "no", "off"
    ):
        return f"{name}={value!r} is not usable — write true or false"

    if name in _DATE_SETTINGS and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return f"{name}={value!r} is not an ISO date — write it as YYYY-MM-DD, e.g. 2028-08-27"
    if name in _DATE_SETTINGS:
        try:
            datetime.date.fromisoformat(value)
        except ValueError:
            return f"{name}={value!r} is not a real date"

    if name in ("ANALYSIS_MODEL_CHEAP", "ANALYSIS_MODEL_STRONG"):
        provider = _analysis_provider(env)
        if provider == "anthropic" and value not in ANALYSIS_MODELS:
            close = difflib.get_close_matches(value, ANALYSIS_MODELS, n=1, cutoff=0.5)
            suggestion = f" Did you mean {close[0]}?" if close else ""
            return (
                f"{name}={value!r} is not one of the model ids this service is documented "
                "against: " + ", ".join(ANALYSIS_MODELS) + "." + suggestion +
                " A model id that does not exist is not refused by anything until the "
                "first recording of the day is analysed, which is 06:00 on a Tuesday — so "
                "it is refused here instead. If you have deliberately pointed the analysis "
                "pass somewhere else, set ANALYSIS_PROVIDER first."
            )

    if name == "GATE_REVIEW_BASE_URL" and not value.startswith("https://"):
        return (
            "GATE_REVIEW_BASE_URL must start with https:// — approvals, and the held "
            "passages behind them, travel over it"
        )

    if name == "NAMING_APPLY" and value.lower() in ("1", "true", "yes", "on") and not str(
        env.get("NAMING_SITES_FILE") or ""
    ).strip():
        return (
            "NAMING_APPLY writes a worked-out name into the transcript's subject line, and "
            "the name can only come from the record's site list. Set NAMING_SITES_FILE "
            "first, to the file ops/build-site-book.py writes."
        )

    if name == "GATE_MODE" and value.lower() == "on" and not str(
        env.get("GATE_REVIEW_BASE_URL") or ""
    ).strip():
        return (
            "GATE_MODE=on holds sensitive passages back until somebody approves them, and "
            "GATE_REVIEW_BASE_URL is not set — there would be nowhere to approve them and "
            "nothing would ever be released. Set GATE_REVIEW_BASE_URL first, or leave the "
            "gate on shadow, where it records what it would have held and withholds nothing."
        )

    if name == "HEARTBEAT_URL" and not value.startswith("https://"):
        return (
            "HEARTBEAT_URL must start with https:// — it is the ping that tells something "
            "outside this machine that the service is still alive"
        )

    if name == "ANALYSIS_BASE_URL" and not value.startswith(("http://", "https://")):
        return f"ANALYSIS_BASE_URL={value!r} is not a url"

    if name == "TIMEZONE":
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(value)
        except Exception:  # noqa: BLE001 - any failure means the zone is not usable here
            return (
                f"TIMEZONE={value!r} is not a zone this machine knows. Write an IANA name "
                "such as Africa/Johannesburg."
            )

    if name in _LEGACY_FOLDER_SETTINGS and str(env.get("ROUTES") or "").strip():
        return (
            f"{name} is the single-folder setting from before routes existed, and this "
            "installation lists its folders in ROUTES — so the service would ignore "
            "whatever is written here. Change the folder with `transcriber routes edit "
            "<route>` instead."
        )

    return ""


def _unknown_key_message(name: str) -> str:
    close = difflib.get_close_matches(name.upper(), list(SETTINGS), n=3, cutoff=0.6)
    if _ROUTE_VAR_RE.match(name.upper()) or name.upper() == "ROUTES":
        return (
            f"{name} is a route's setting, not one of the service's. Routes are managed "
            "with `transcriber routes` — `transcriber routes` to see them, `transcriber "
            "routes edit <route>` to change one — so that the folders can be picked from "
            "your drive and checked against each other."
        )
    lines = [f"{name} is not a setting this service reads, so writing it would do nothing."]
    if close:
        lines.append("  Did you mean: " + ", ".join(close) + "?")
    lines.append("  `transcriber config list` prints every setting there is.")
    return "\n".join(lines)


def comments_would_be_lost(path: str) -> bool:
    """True when this ``.env`` carries comments of somebody's own.

    ``write_env_file`` rewrites the file in the standard grouped layout, which is what keeps
    the grouping and the 0600 mode intact — and which drops any note a person added by
    hand. Losing somebody's own comment silently would be a small betrayal of the same kind
    this service exists to stop, so it is said out loud instead.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return False
    header = {
        "The transcriber's settings. Written by `transcriber setup`.",
        "This file holds live credentials. It is chmod 0600 and .gitignore'd —",
        "keep it that way, and never paste its contents into a chat or an email.",
        "Re-run `python3 -m transcriber setup` to change any of it.",
    }
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("#") or stripped.startswith("# ---"):
            continue
        if stripped.lstrip("#").strip() in header or stripped == "#":
            continue
        return True
    return False


def _bullet(problem: str) -> str:
    """One problem as a bullet, with its continuation lines lined up under the first."""
    first, *rest = problem.splitlines()
    return "\n".join(["  \u2022 " + first] + ["    " + line.strip() for line in rest])


def _config_problems(env: Mapping[str, str]) -> list[str]:
    """Every complaint the real Config has about this environment, as plain lines."""
    try:
        Config.from_env(dict(env))
    except ConfigError as exc:
        return list(exc.problems)
    except Exception as exc:  # noqa: BLE001 - an unexpected failure is still a problem
        return [f"{type(exc).__name__}: {exc}"]
    return []


# ---------------------------------------------------------------------------------------
# display
# ---------------------------------------------------------------------------------------

def _shown(setting: Setting, value: str) -> str:
    if not value:
        if setting.default:
            return f"(not set — the service uses {setting.default})"
        return "(not set)"
    if setting.show == "masked":
        return mask(value)
    if setting.show == "private":
        return "set — not shown"
    return value


def _print_list(env_path: str, env: Mapping[str, str], out: Any) -> None:
    print(f"transcriber settings — {env_path}", file=out)
    width = max(len(name) for name in SETTINGS)
    for group, members in GROUPS:
        print(f"\n  {group}", file=out)
        for name in members:
            setting = SETTINGS[name]
            print(f"    {name:<{width}}  {_shown(setting, str(env.get(name) or ''))}", file=out)

    declared = [
        r.strip() for r in str(env.get("ROUTES") or "").replace("\n", ",").split(",") if r.strip()
    ]
    routes = routes_from_values(env)
    print("\n  routes", file=out)
    if declared:
        print(f"    ROUTES{' ' * (width - 6)}  " + ", ".join(declared), file=out)
        switched_on = sum(1 for r in routes if r.enabled)
        print(f"    {len(routes)} route(s), {switched_on} switched on, each with its own "
              "folders. `transcriber routes` shows them in full.", file=out)
    else:
        print("    none listed — this installation watches the one folder in "
              "SOURCE_FOLDER_ID, as one route called 'default'.", file=out)
        print("    `transcriber routes` shows it, and `transcriber routes add` adds another.",
              file=out)

    print(
        "\n  Keys are shown as their last four characters and addresses are not shown at "
        "all.\n  Change one with: python3 -m transcriber config set <NAME> <value>",
        file=out,
    )


# ---------------------------------------------------------------------------------------
# the commands
# ---------------------------------------------------------------------------------------

def _load_for_reading(env_path: str, out: Any) -> dict[str, str] | None:
    """The file if there is one, otherwise the process environment.

    ⛔ READING ONLY. `config set` still refuses when there is no file, because
    writing into a process environment changes nothing that survives the command.
    A deployed host has no `.env` — systemd hands the settings over through
    `EnvironmentFile=` — so refusing here made the two commands that answer "what
    is this set up to do" the only ones that did not work where the answer matters.
    """
    if os.path.exists(env_path):
        return load_env_file(env_path)
    from .config import environment_the_service_reads

    live = environment_the_service_reads()
    if not live:
        print(
            f"there is no {env_path} to read, and this process was not given any settings "
            "either. Run `python3 -m transcriber setup` to write one, or pass --env with "
            "the path to it.",
            file=out,
        )
        return None
    print(
        f"There is no {env_path}, so this is what THIS PROCESS was given — which on a "
        "deployed host is the environment file systemd hands over, and may differ from "
        "what the service itself is running with. Pass --env to read a file instead.\n",
        file=out,
    )
    return live


def _load(env_path: str, out: Any) -> dict[str, str] | None:
    if not os.path.exists(env_path):
        print(
            f"there is no {env_path} to read. Run `python3 -m transcriber setup` to write "
            "one, or pass --env with the path to it.",
            file=out,
        )
        return None
    return load_env_file(env_path)


def cmd_list(args: argparse.Namespace, out: Any = None) -> int:
    out = out or sys.stdout
    env = _load_for_reading(args.env, out)
    if env is None:
        return EXIT_FAILED
    _print_list(args.env, env, out)
    problems = _config_problems(env)
    if problems:
        print("\n  These settings are not usable as they stand:", file=out)
        for problem in problems:
            print(f"    • {problem}", file=out)
        return EXIT_FAILED
    return EXIT_OK


def cmd_get(args: argparse.Namespace, out: Any = None) -> int:
    out = out or sys.stdout
    env = _load_for_reading(args.env, out)
    if env is None:
        return EXIT_FAILED
    name = (args.key or "").strip().upper()
    setting = SETTINGS.get(name)
    if setting is None:
        print(_unknown_key_message(name or "(nothing)"), file=out)
        return EXIT_FAILED
    print(_shown(setting, str(env.get(name) or "")), file=out)
    return EXIT_OK


def _pending(args: argparse.Namespace, out: Any) -> list[tuple[str, str]] | None:
    """The changes this invocation asks for, as (NAME, value), or None if it asks for none."""
    changes: list[tuple[str, str]] = []
    for alias, name in ALIASES.items():
        value = getattr(args, alias.replace("-", "_"), None)
        if value is not None:
            changes.append((name, str(value)))
    if args.key:
        if args.value is None:
            print(
                f"`config set {args.key}` needs the value to set it to, e.g. "
                f"`config set {args.key.upper()} <value>`. Use `config get {args.key.upper()}` "
                "to see what it is now.",
                file=out,
            )
            return None
        changes.append(((args.key or "").strip().upper(), str(args.value)))
    elif args.value is not None:
        print("`config set` takes a setting name and then its value.", file=out)
        return None
    if not changes:
        print(
            "`config set` needs something to change: a setting name and a value, or one of "
            "the shorthands (" + ", ".join(f"--{a}" for a in ALIASES) + ").",
            file=out,
        )
        return None
    # A setting named twice in one invocation — `config set --model X ANALYSIS_MODEL_STRONG
    # Y` — is one change, the last one, not two contradictory lines in the report.
    deduped: dict[str, str] = {}
    for name, value in changes:
        deduped[name] = value
    return list(deduped.items())


def cmd_set(args: argparse.Namespace, out: Any = None) -> int:
    """Validate, then write. Never the other way round.

    Two gates, because they catch different mistakes. The first is the value on its own —
    an unknown key, a bad number, a model id nobody documented. The second re-reads the
    **whole file** through the real :class:`Config`, which is what catches a value that is
    fine by itself and wrong beside its neighbours. A file that was already broken before
    this change is reported and not held against the change, or a half-finished ``.env``
    would be uneditable by the one command meant to finish it.
    """
    out = out or sys.stdout
    env = _load(args.env, out)
    if env is None:
        return EXIT_FAILED

    changes = _pending(args, out)
    if changes is None:
        return EXIT_FAILED

    candidate = dict(env)
    for name, value in changes:
        candidate[name] = value.strip()

    problems: list[str] = []
    for name, value in changes:
        problem = check_value(name, value, candidate)
        if problem:
            problems.append(problem)
    if problems:
        print("Nothing was written. " + ("That setting cannot be used:" if len(problems) == 1
                                         else "Those settings cannot be used:"), file=out)
        for problem in problems:
            print(_bullet(problem), file=out)
        return EXIT_FAILED

    before = _config_problems(env)
    after = _config_problems(candidate)
    introduced = [p for p in after if p not in before]
    if introduced:
        print("Nothing was written. That change would stop the service starting:", file=out)
        for problem in introduced:
            print(_bullet(problem), file=out)
        return EXIT_FAILED

    for name, value in changes:
        if not value.strip():
            candidate.pop(name, None)

    lost_comments = comments_would_be_lost(args.env)
    write_env_file(
        args.env, candidate,
        header=[
            "The transcriber's settings. Written by `transcriber setup`.",
            "",
            "This file holds live credentials. It is chmod 0600 and .gitignore'd —",
            "keep it that way, and never paste its contents into a chat or an email.",
            "Re-run `python3 -m transcriber setup` to change any of it.",
        ],
    )

    for name, value in changes:
        setting = SETTINGS[name]
        was = _shown(setting, str(env.get(name) or ""))
        now = _shown(setting, value.strip())
        print(f"{name}: {was} -> {now}", file=out)
    print(f"\nWritten to {args.env}, readable only by you (0600).", file=out)
    if lost_comments:
        print("  Your own comments in that file were not kept — it is rewritten in the "
              "standard\n  grouped layout every time, which is what keeps the 0600 mode "
              "and the grouping.", file=out)
    print("The running service does not re-read this file — restart it to pick the change up.",
          file=out)
    if before:
        print("\n  Still to fix, from before this change:", file=out)
        for problem in before:
            print(f"    • {problem}", file=out)
    return EXIT_OK


_HANDLERS = {"list": cmd_list, "get": cmd_get, "set": cmd_set}


def run(args: argparse.Namespace, out: Any = None) -> int:
    action = (getattr(args, "action", None) or "list").strip().lower()
    handler = _HANDLERS.get(action)
    if handler is None:  # pragma: no cover - argparse constrains this
        print(f"`config {action}` is not something this command does: list, get, set.",
              file=out or sys.stdout)
        return EXIT_FAILED
    return handler(args, out)


def add_arguments(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Wire ``config`` onto the main parser. Kept here so the two stay in one place."""
    parser.add_argument(
        "action", nargs="?", default="list", choices=("list", "get", "set"),
        help="list every setting (the default), get one, or set one",
    )
    parser.add_argument("key", nargs="?", default=None, help="the setting's name, e.g. DIGEST_HOUR")
    parser.add_argument("value", nargs="?", default=None, help="what to set it to")
    parser.add_argument("--env", default=".env", help="path to the .env (default: .env)")
    for alias, name in ALIASES.items():
        parser.add_argument(
            f"--{alias}", default=None, metavar="VALUE",
            help=f"shorthand for `set {name}`",
        )
    return parser
