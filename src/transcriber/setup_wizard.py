"""``transcriber setup`` — the interactive wizard that writes ``.env``.

The person deploying this is a building consultant, not an engineer. Handing him a file of
forty variables and a comment each is a way of saying "you work it out". So this asks one
question at a time, in plain words, and — the part that actually matters — **checks each
answer against the real service before moving on**. A wrong tenant id found here costs
thirty seconds; found at 06:00 on a Tuesday it costs a day of recordings.

Two rules the whole module obeys:

* **A secret is never echoed, never logged, never written anywhere but ``.env``.** Typed
  input is read with :func:`getpass.getpass`, existing values are shown only as their last
  four characters, and every failure message is scrubbed before it is printed.
* **A failed check is never fatal by itself.** He may be setting this up before the admin
  has granted consent, or on a laptop with no route to SMTP. Every check offers "carry on
  anyway", because a wizard that refuses to finish is a wizard that gets abandoned
  half-done — and a half-written ``.env`` is worse than none.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from . import config as config_mod

__all__ = ["run_setup", "load_env_file", "write_env_file"]


# ---------------------------------------------------------------------------------------
# terminal helpers
# ---------------------------------------------------------------------------------------

def _supports_colour(stream: Any) -> bool:
    return bool(getattr(stream, "isatty", lambda: False)()) and os.environ.get("TERM") != "dumb"


class _Style:
    def __init__(self, on: bool) -> None:
        self.on = on

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.on else text

    def bold(self, t: str) -> str:
        return self._wrap("1", t)

    def dim(self, t: str) -> str:
        return self._wrap("2", t)

    def green(self, t: str) -> str:
        return self._wrap("32", t)

    def red(self, t: str) -> str:
        return self._wrap("31", t)

    def yellow(self, t: str) -> str:
        return self._wrap("33", t)

    def cyan(self, t: str) -> str:
        return self._wrap("36", t)


def mask(value: str) -> str:
    """Show enough of a secret to recognise it, never enough to use it."""
    if not value:
        return ""
    if len(value) <= 4:
        return "•" * len(value)
    return "•" * 8 + value[-4:]


# ---------------------------------------------------------------------------------------
# .env read / write
# ---------------------------------------------------------------------------------------

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def load_env_file(path: str) -> dict[str, str]:
    """Parse a ``.env``. Tolerant on purpose — a hand-edited file must still load."""
    out: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return out
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _ENV_LINE.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            # Undo the escaping _quote() applies. Without this a client secret containing a
            # quote or a backslash round-trips to something subtly different, and the only
            # symptom is an authentication failure that names nothing useful.
            value = value[1:-1]
            if value and "\\" in value:
                value = re.sub(r"\\(.)", r"\1", value)
        else:  # strip a trailing comment only when the value is not quoted
            value = value.split("  #", 1)[0].strip()
        out[key] = value
    return out


def _quote(value: str) -> str:
    if value == "" or re.search(r"[\s#\"'$`\\]", value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def write_env_file(path: str, values: dict[str, str], *, header: Sequence[str] = ()) -> None:
    """Write ``.env`` at 0600, and only ever at 0600.

    Created with :func:`os.open` rather than written then chmod'd — between those two calls
    a world-readable file holding a client secret exists on disk, and on a shared host that
    window is the whole vulnerability.
    """
    lines: list[str] = []
    for line in header:
        lines.append(f"# {line}" if line else "#")
    if header:
        lines.append("")

    written: set[str] = set()
    for group, members in _GROUPS:
        present = [v for v in members if v in values]
        if not present:
            continue
        lines.append(f"# --- {group} " + "-" * max(0, 70 - len(group)))
        for name in present:
            lines.append(f"{name}={_quote(values[name])}")
            written.add(name)
        lines.append("")
    extra = sorted(k for k in values if k not in written)
    if extra:
        lines.append("# --- other " + "-" * 64)
        for name in extra:
            lines.append(f"{name}={_quote(values[name])}")
        lines.append("")

    body = "\n".join(lines).rstrip() + "\n"
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)
    os.chmod(path, 0o600)


_GROUPS: list[tuple[str, list[str]]] = [
    ("Microsoft / OneDrive", [
        "GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET", "GRAPH_USER_ID",
        "GRAPH_SECRET_EXPIRES_ON",
        "SOURCE_FOLDER_ID", "OUTPUT_FOLDER_ID", "ARCHIVE_FOLDER_ID", "ORPHAN_FOLDER_ID",
    ]),
    ("transcription", [
        "TRANSCRIBE_ENGINE", "OPENAI_API_KEY", "ELEVENLABS_API_KEY",
        "AZURE_SPEECH_KEY", "AZURE_SPEECH_REGION", "ENGINE_BASE_URL",
        "ENGINE_KEY_EXPIRES_ON",
    ]),
    ("the analysis pass", [
        "ANALYSIS_PROVIDER", "ANALYSIS_API_KEY", "ANALYSIS_BASE_URL",
        "ANALYSIS_MODEL_CHEAP", "ANALYSIS_MODEL_STRONG", "ANALYSIS_KEY_EXPIRES_ON",
    ]),
    ("the morning email", [
        "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM", "SMTP_TO",
        "DIGEST_HOUR",
    ]),
    ("staying alive", ["HEARTBEAT_URL"]),
    ("local", ["LEDGER_PATH", "WORK_DIR", "LOG_FORMAT"]),
]

#: Written masked into any transcript of the run, and never printed back.
SECRET_VARS = {
    "GRAPH_CLIENT_SECRET", "OPENAI_API_KEY", "ELEVENLABS_API_KEY",
    "AZURE_SPEECH_KEY", "ANALYSIS_API_KEY", "SMTP_PASSWORD",
}


# ---------------------------------------------------------------------------------------
# the wizard
# ---------------------------------------------------------------------------------------

@dataclass
class _Ctx:
    values: dict[str, str]
    style: _Style
    verify: bool = True
    assume_yes: bool = False
    notes: list[str] = field(default_factory=list)
    scrub: list[str] = field(default_factory=list)

    def out(self, text: str = "") -> None:
        print(self._clean(text))

    def _clean(self, text: str) -> str:
        for secret in self.scrub:
            if secret and len(secret) >= 6:
                text = text.replace(secret, "••••")
        return text

    def remember_secret(self, value: str) -> None:
        if value and value not in self.scrub:
            self.scrub.append(value)

    # -- prompts ------------------------------------------------------------------------

    def ask(
        self,
        name: str,
        question: str,
        *,
        secret: bool = False,
        default: str = "",
        required: bool = True,
        help_text: str = "",
        validate: Callable[[str], str] | None = None,
    ) -> str:
        current = self.values.get(name, "") or default
        shown = mask(current) if (secret and current) else current
        suffix = f" [{self.style.dim(shown)}]" if shown else ""
        while True:
            self.out()
            self.out(self.style.bold(question) + suffix)
            if help_text:
                self.out(self.style.dim("  " + help_text))
            try:
                if secret:
                    raw = getpass.getpass("  > ").strip()
                else:
                    raw = input("  > ").strip()
            except EOFError:
                if not current and required:
                    raise SystemExit(
                        "\n  Input ended before that question was answered. Run this in a "
                        "terminal, or pipe an answer for every question in order.\n"
                    )
                raw = ""
            if not raw and current:
                raw = current
            if not raw:
                if not required:
                    self.values[name] = ""
                    return ""
                self.out(self.style.red("  That one is needed. Try again, or press Ctrl-C to stop."))
                continue
            if validate:
                problem = validate(raw)
                if problem:
                    self.out(self.style.red("  " + problem))
                    continue
            if secret:
                self.remember_secret(raw)
            self.values[name] = raw
            return raw

    def choose(self, question: str, options: Sequence[tuple[str, str]], *, current: str = "") -> str:
        self.out()
        self.out(self.style.bold(question))
        for i, (value, label) in enumerate(options, 1):
            marker = self.style.green(" (current)") if value == current else ""
            self.out(f"  {i}. {label}{marker}")
        while True:
            try:
                raw = input("  > ").strip()
            except EOFError:
                raw = ""
            if not raw and current:
                return current
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                return options[int(raw) - 1][0]
            for value, _ in options:
                if raw.lower() == value.lower():
                    return value
            self.out(self.style.red(f"  Pick a number from 1 to {len(options)}."))

    def confirm(self, question: str, *, default: bool = True) -> bool:
        if self.assume_yes:
            return default
        hint = "Y/n" if default else "y/N"
        while True:
            try:
                raw = input(f"  {question} [{hint}] ").strip().lower()
            except EOFError:
                return default
            if not raw:
                return default
            if raw in ("y", "yes"):
                return True
            if raw in ("n", "no"):
                return False

    # -- check reporting ----------------------------------------------------------------

    def check(self, label: str, fn: Callable[[], str]) -> bool:
        """Run one live check. Returns False only if the user chooses to stop."""
        if not self.verify:
            self.out(self.style.dim(f"  (skipped: {label})"))
            return True
        self.out(self.style.dim(f"  checking {label} …"))
        try:
            detail = fn()
        except Exception as exc:  # noqa: BLE001 - any failure is reportable, none is fatal
            msg = self._clean(f"{type(exc).__name__}: {exc}")
            self.out(self.style.red(f"  ✗ {label} — {msg}"))
            self.notes.append(f"{label} did not pass: {msg}")
            return self.confirm("Carry on anyway and fix it later?", default=True)
        self.out(self.style.green(f"  ✓ {label}") + (f" — {self._clean(detail)}" if detail else ""))
        return True


def _http_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    method: str = "GET",
    timeout: float = 30.0,
) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            body = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        body = exc.read()
        status = exc.code
    try:
        doc = json.loads(body.decode("utf-8", "replace")) if body else {}
    except ValueError:
        doc = {"_raw": body[:400].decode("utf-8", "replace")}
    return status, doc if isinstance(doc, dict) else {"_list": doc}


def _api_error(doc: dict[str, Any]) -> str:
    for path in (("error", "message"), ("error", "code"), ("message",), ("error_description",)):
        node: Any = doc
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, str) and node:
            return node[:300]
    return json.dumps(doc)[:300]


# ---------------------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------------------

def _section(ctx: _Ctx, number: int, title: str, blurb: str = "") -> None:
    ctx.out()
    ctx.out(ctx.style.cyan("─" * 74))
    ctx.out(ctx.style.cyan(ctx.style.bold(f" {number}. {title}")))
    if blurb:
        ctx.out(ctx.style.dim(" " + blurb))
    ctx.out(ctx.style.cyan("─" * 74))


def _iso_date_or_blank(raw: str) -> str:
    if not raw:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return ""
    return "Write it as YYYY-MM-DD, for example 2028-08-27 — or leave it empty."


def _microsoft(ctx: _Ctx) -> None:
    _section(
        ctx, 1, "Microsoft / OneDrive",
        "From the app registration in SETUP.md step 1. Nothing works without this.",
    )
    ctx.ask("GRAPH_TENANT_ID", "Directory (tenant) ID",
            help_text="On the app registration's Overview page. Not a secret.")
    ctx.ask("GRAPH_CLIENT_ID", "Application (client) ID",
            help_text="Same page, just above it. Not a secret.")
    ctx.ask("GRAPH_CLIENT_SECRET", "Client secret VALUE", secret=True,
            help_text="The Value column, not the Secret ID. Typing is hidden.")
    ctx.ask("GRAPH_USER_ID", "Whose OneDrive holds the recordings?",
            help_text="The sign-in address, e.g. james@yourcompany.co.za")
    ctx.ask("GRAPH_SECRET_EXPIRES_ON", "When does that secret expire? (YYYY-MM-DD)",
            required=False, validate=_iso_date_or_blank,
            help_text="Optional but worth it — the morning email counts down to it. "
                      "An expired secret is the commonest way a service like this dies quietly.")

    client = _graph_client(ctx)
    if client is None:
        # --no-verify, or the client would not build. Either way there is no drive to list,
        # so the ids have to be typed. Skipping them here is how the wizard used to finish
        # "successfully" and still leave a .env the service refuses to start from.
        _ask_folder_ids_by_hand(ctx)
        return

    if not ctx.check("Microsoft sign-in and drive access", lambda: _probe_graph(client)):
        return
    _pick_folders(ctx, client)


def _ask_folder_ids_by_hand(ctx: _Ctx) -> None:
    """The fallback when the drive cannot be listed for you.

    Finding a driveItem id by hand is genuinely unpleasant, so say where they come from
    rather than just demanding one.
    """
    ctx.out()
    ctx.out(ctx.style.yellow(
        "  Without a live connection the folders cannot be listed for you, so their ids\n"
        "  have to be typed. Re-run without --no-verify once the app registration is\n"
        "  consented and you can pick them from a list instead."))
    for name, question, blurb in (
        ("SOURCE_FOLDER_ID", "Recordings folder id",
         "the folder your phone uploads into, usually CALLS"),
        ("OUTPUT_FOLDER_ID", "Transcripts folder id",
         "must be a different folder, or the service would read its own output"),
        ("ARCHIVE_FOLDER_ID", "Archive folder id",
         "where recordings move after 60 days, once their transcripts are confirmed"),
    ):
        ctx.ask(name, question, help_text=blurb)

    chosen = [ctx.values.get(k, "") for k in
              ("SOURCE_FOLDER_ID", "OUTPUT_FOLDER_ID", "ARCHIVE_FOLDER_ID")]
    if len(set(chosen)) != len(chosen):
        ctx.out(ctx.style.red("  ✗ Those must be three different folders."))
        ctx.notes.append("The source, output and archive folders are not all different.")


def _graph_client(ctx: _Ctx) -> Any:
    if not ctx.verify:
        return None
    try:
        from .graph import GraphClient
    except Exception:  # pragma: no cover - import guard only
        return None

    class _Shim:
        tenant_id = ctx.values.get("GRAPH_TENANT_ID", "")
        client_id = ctx.values.get("GRAPH_CLIENT_ID", "")
        client_secret = ctx.values.get("GRAPH_CLIENT_SECRET", "")
        user_id = ctx.values.get("GRAPH_USER_ID", "")
        drive_id = ""

    try:
        return GraphClient.from_config(_Shim())
    except Exception as exc:  # noqa: BLE001
        ctx.out(ctx.style.red(f"  ✗ could not build the client — {exc}"))
        return None


def _probe_graph(client: Any) -> str:
    items = client.list_children(None)
    folders = [i for i in items if not i.is_file]
    return f"{len(folders)} folders visible at the top of the drive"


def _pick_folders(ctx: _Ctx, client: Any) -> None:
    """Let him choose folders from a list. Nobody should have to hunt for a driveItem id."""
    try:
        items = client.list_children(None)
    except Exception as exc:  # noqa: BLE001
        ctx.out(ctx.style.yellow(f"  could not list the drive ({exc}); enter ids by hand."))
        for name, question in (
            ("SOURCE_FOLDER_ID", "driveItem id of the recordings folder"),
            ("OUTPUT_FOLDER_ID", "driveItem id of the folder for transcripts"),
            ("ARCHIVE_FOLDER_ID", "driveItem id of the archive folder"),
        ):
            ctx.ask(name, question)
        return

    folders = sorted((i for i in items if not i.is_file), key=lambda i: i.name.lower())
    if not folders:
        ctx.out(ctx.style.yellow("  no folders found at the top of that drive."))
        return

    options = [(f.id, f.name) for f in folders]
    by_id = {f.id: f.name for f in folders}

    def pick(name: str, question: str, blurb: str) -> None:
        current = ctx.values.get(name, "")
        ctx.out()
        ctx.out(ctx.style.dim("  " + blurb))
        chosen = ctx.choose(question, options, current=current)
        ctx.values[name] = chosen
        ctx.out(ctx.style.green(f"  → {by_id.get(chosen, chosen)}"))

    pick("SOURCE_FOLDER_ID", "Which folder do your recordings land in?",
         "The one your phone uploads to — usually CALLS.")
    pick("OUTPUT_FOLDER_ID", "Which folder should transcripts be written to?",
         "A different folder from the recordings. Make it in OneDrive first if it is not listed.")
    pick("ARCHIVE_FOLDER_ID", "Which folder should recordings older than 60 days move to?",
         "Only ever moved once their transcripts are confirmed present. Nothing is deleted.")

    chosen = [ctx.values.get(k, "") for k in ("SOURCE_FOLDER_ID", "OUTPUT_FOLDER_ID", "ARCHIVE_FOLDER_ID")]
    if len(set(chosen)) != len(chosen):
        ctx.out(ctx.style.red(
            "  ✗ Those must be three different folders. Writing transcripts into the folder "
            "being watched would make the service read its own output."))
        ctx.notes.append("The source, output and archive folders are not all different — re-run setup.")


_ENGINES = [
    ("openai", "OpenAI — what you asked for; strong multilingual, splits files over 25 MB"),
    ("elevenlabs", "ElevenLabs Scribe — takes whole files, claims all four SA languages"),
    ("azure", "Azure Speech — stays inside your tenant, but has no isiXhosa"),
]


def _transcription(ctx: _Ctx) -> None:
    _section(ctx, 2, "Transcription", "Which service turns the audio into words.")
    engine = ctx.choose("Which transcription engine?", _ENGINES,
                        current=ctx.values.get("TRANSCRIBE_ENGINE", "openai"))
    ctx.values["TRANSCRIBE_ENGINE"] = engine

    if engine == "openai":
        ctx.ask("OPENAI_API_KEY", "OpenAI API key", secret=True,
                help_text="platform.openai.com → API keys. Starts sk-.")
        ctx.check("the OpenAI key", lambda: _probe_openai(ctx.values["OPENAI_API_KEY"]))
    elif engine == "elevenlabs":
        ctx.ask("ELEVENLABS_API_KEY", "ElevenLabs API key", secret=True,
                help_text="elevenlabs.io → Developers → API keys.")
        ctx.check("the ElevenLabs key", lambda: _probe_elevenlabs(ctx.values["ELEVENLABS_API_KEY"]))
    else:
        ctx.ask("AZURE_SPEECH_KEY", "Azure Speech key", secret=True)
        ctx.ask("AZURE_SPEECH_REGION", "Azure Speech region",
                help_text="e.g. southafricanorth, or westeurope.")


def _probe_openai(key: str) -> str:
    status, doc = _http_json("https://api.openai.com/v1/models",
                             headers={"Authorization": f"Bearer {key}"})
    if status != 200:
        raise RuntimeError(_api_error(doc))
    n = len(doc.get("data") or [])
    return f"key accepted, {n} models available"


def _probe_elevenlabs(key: str) -> str:
    status, doc = _http_json("https://api.elevenlabs.io/v1/user",
                             headers={"xi-api-key": key})
    if status != 200:
        raise RuntimeError(_api_error(doc))
    return "key accepted"


#: Model ids from the bundled claude-api reference, not from memory. Priced per million
#: tokens. The router runs on every recording so nothing is skipped on a guess; the strong
#: model reads only the substantive ones.
_ANALYSIS_TIERS = [
    ("balanced", "Haiku 4.5 sorts everything, Opus 5 reads what matters  (recommended)",
     "claude-haiku-4-5", "claude-opus-5"),
    ("thorough", "Opus 5 does both passes — most careful, costs more",
     "claude-opus-5", "claude-opus-5"),
    ("light", "Haiku 4.5 does both passes — cheapest, less careful on long meetings",
     "claude-haiku-4-5", "claude-haiku-4-5"),
]


def _analysis(ctx: _Ctx) -> None:
    _section(
        ctx, 3, "The AI pass",
        "Reads each transcript and pulls out who promised what. Separate from transcription.",
    )
    ctx.values["ANALYSIS_PROVIDER"] = "anthropic"
    ctx.values["ANALYSIS_BASE_URL"] = "https://api.anthropic.com"

    ctx.ask("ANALYSIS_API_KEY", "Anthropic API key", secret=True,
            help_text="console.anthropic.com → API keys. Starts sk-ant-.")

    current = ctx.values.get("ANALYSIS_MODEL_CHEAP", "")
    tier_now = ""
    for key, _label, cheap, strong in _ANALYSIS_TIERS:
        if cheap == current and strong == ctx.values.get("ANALYSIS_MODEL_STRONG", ""):
            tier_now = key
    tier = ctx.choose(
        "How careful should the AI pass be?",
        [(k, label) for k, label, _c, _s in _ANALYSIS_TIERS],
        current=tier_now or "balanced",
    )
    for key, _label, cheap, strong in _ANALYSIS_TIERS:
        if key == tier:
            ctx.values["ANALYSIS_MODEL_CHEAP"] = cheap
            ctx.values["ANALYSIS_MODEL_STRONG"] = strong

    ctx.check("the Anthropic key", lambda: _probe_anthropic(
        ctx.values["ANALYSIS_API_KEY"], ctx.values["ANALYSIS_MODEL_CHEAP"]))


def _probe_anthropic(key: str, model: str) -> str:
    body = json.dumps({
        "model": model,
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "Reply with the single word: ready"}],
    }).encode()
    status, doc = _http_json(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        data=body,
        method="POST",
    )
    if status != 200:
        raise RuntimeError(_api_error(doc))
    blocks = doc.get("content") or []
    said = ""
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            said = str(block.get("text", "")).strip()
            break
    return f"{doc.get('model', model)} answered {said[:24]!r}"


def _email(ctx: _Ctx) -> None:
    _section(
        ctx, 4, "The morning email",
        "One message at 06:00 — sent on good days too, so silence always means something is wrong.",
    )
    ctx.ask("SMTP_HOST", "Mail server", default="smtp.office365.com",
            help_text="smtp.office365.com for Microsoft 365.")
    ctx.ask("SMTP_PORT", "Port", default="587",
            validate=lambda v: "" if v.isdigit() else "Numbers only — usually 587.")
    ctx.ask("SMTP_USER", "Sign-in address for that mailbox")
    ctx.ask("SMTP_PASSWORD", "Its password (an app password, if the account has 2FA)", secret=True)
    ctx.ask("SMTP_FROM", "Send the report FROM", default=ctx.values.get("SMTP_USER", ""))
    ctx.ask("SMTP_TO", "Send the report TO",
            help_text="Your own address. Several are fine, separated by commas.")
    ctx.ask("DIGEST_HOUR", "What hour should it arrive? (0-23)", default="6",
            validate=lambda v: "" if v.isdigit() and 0 <= int(v) <= 23 else "A number from 0 to 23.")

    if ctx.verify and ctx.confirm("Send a test email now?", default=True):
        ctx.check("the morning email", lambda: _probe_smtp(ctx))


def _probe_smtp(ctx: _Ctx) -> str:
    import smtplib
    from email.message import EmailMessage

    host = ctx.values["SMTP_HOST"]
    port = int(ctx.values.get("SMTP_PORT", "587"))
    recipients = [a.strip() for a in ctx.values["SMTP_TO"].split(",") if a.strip()]

    msg = EmailMessage()
    msg["Subject"] = "Recordings: setup test"
    msg["From"] = ctx.values["SMTP_FROM"]
    msg["To"] = ", ".join(recipients)
    msg.set_content(
        "This is the transcriber's setup test.\n\n"
        "If you can read this, the morning report can reach you. From now on it arrives "
        "every day at the hour you chose — including on days when nothing went wrong, "
        "because a report that only turns up when something breaks looks exactly like a "
        "service that has died.\n"
    )

    if port == 465:
        server: Any = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
    try:
        if port != 465:
            server.starttls(context=ssl.create_default_context())
        server.login(ctx.values["SMTP_USER"], ctx.values["SMTP_PASSWORD"])
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:  # noqa: BLE001
            pass
    return f"sent to {', '.join(recipients)} — go and look"


def _heartbeat(ctx: _Ctx) -> None:
    _section(
        ctx, 5, "Staying alive",
        "The one alarm that still works when the service itself is dead.",
    )
    ctx.out()
    ctx.out(ctx.style.dim(
        "  Sign up free at healthchecks.io, add a check, and paste its ping URL. If the\n"
        "  transcriber stops pinging it, healthchecks emails you — which is how you find\n"
        "  out in two hours instead of four days."))
    url = ctx.ask(
        "HEARTBEAT_URL", "Ping URL",
        help_text="Required, deliberately — it is the only alarm that still works when the "
                  "service itself is dead.",
        validate=lambda v: "" if v.startswith("https://") else "Must start with https://")
    if url and ctx.verify:
        ctx.check("the heartbeat", lambda: _probe_heartbeat(url))


def _probe_heartbeat(url: str) -> str:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=20, context=ssl.create_default_context()) as resp:
        resp.read()
        return f"pinged, {resp.status} — the check should now show as up"


def _local(ctx: _Ctx) -> None:
    _section(ctx, 6, "Where it keeps its notes", "Sensible defaults; press Enter through these.")
    ctx.ask("LEDGER_PATH", "Where should the ledger live?", default="./transcriber.db",
            help_text="The permanent record of every recording and what happened to it. "
                      "Back this up — it is the memory that stops a loss going unnoticed.")
    ctx.ask("WORK_DIR", "Scratch folder for downloads", default="./work")


# ---------------------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------------------

_BANNER = """
  This asks for everything the transcriber needs, checks each answer against the real
  service, and writes it to .env — so a wrong value costs you thirty seconds now instead
  of a day of recordings later.

  Press Enter to keep an existing answer. Ctrl-C stops without writing anything.
  Nothing you type is printed back, logged, or sent anywhere but that one file.
"""


def run_setup(
    *,
    env_path: str = ".env",
    verify: bool = True,
    assume_yes: bool = False,
    stream: Any = None,
) -> int:
    stream = stream or sys.stdout
    style = _Style(_supports_colour(stream))
    existing = load_env_file(env_path)
    ctx = _Ctx(values=dict(existing), style=style, verify=verify, assume_yes=assume_yes)
    for name in SECRET_VARS:
        ctx.remember_secret(existing.get(name, ""))

    ctx.out(style.bold("\n  Setting up the transcriber"))
    ctx.out(style.dim(_BANNER.rstrip()))
    if existing:
        ctx.out(style.green(f"  Found an existing {env_path} — its answers are the defaults."))
    if not verify:
        ctx.out(style.yellow("  Running with --no-verify: nothing will be checked against a real service."))

    try:
        _microsoft(ctx)
        _transcription(ctx)
        _analysis(ctx)
        _email(ctx)
        _heartbeat(ctx)
        _local(ctx)
    except KeyboardInterrupt:
        ctx.out(style.yellow("\n\n  Stopped. Nothing was written.\n"))
        return 130

    # Drop keys belonging to engines that were not chosen, so a stale key cannot be picked
    # up later by a config change nobody remembers making.
    engine = ctx.values.get("TRANSCRIBE_ENGINE", "")
    for name, owner in (("ELEVENLABS_API_KEY", "elevenlabs"), ("AZURE_SPEECH_KEY", "azure")):
        if engine != owner:
            ctx.values.pop(name, None)
    if engine != "azure":
        ctx.values.pop("AZURE_SPEECH_REGION", None)

    ctx.out()
    ctx.out(style.cyan("─" * 74))
    ctx.out(style.bold("  Writing " + env_path))
    write_env_file(
        env_path, ctx.values,
        header=[
            "The transcriber's settings. Written by `transcriber setup`.",
            "",
            "This file holds live credentials. It is chmod 0600 and .gitignore'd —",
            "keep it that way, and never paste its contents into a chat or an email.",
            "Re-run `python3 -m transcriber setup` to change any of it.",
        ],
    )
    ctx.out(style.green(f"  ✓ written, readable only by you (0600)"))

    problems = _validate(env_path, ctx)
    ctx.out()
    ctx.out(style.cyan("─" * 74))
    if ctx.notes:
        ctx.out(style.yellow(style.bold("  Worth going back to:")))
        for note in ctx.notes:
            ctx.out(style.yellow(f"    • {note}"))
        ctx.out()
    if problems:
        ctx.out(style.red(style.bold("  The settings are not complete yet:")))
        for problem in problems:
            ctx.out(style.red(f"    • {problem}"))
        ctx.out()
        ctx.out("  Re-run " + style.bold("python3 -m transcriber setup") + " to fill in the rest.")
        return 1

    ctx.out(style.green(style.bold("  Ready. Three commands, in this order:")))
    ctx.out()
    ctx.out("    " + style.bold("python3 -m transcriber selftest"))
    ctx.out(style.dim("      Proves the code is sane. Touches nothing."))
    ctx.out("    " + style.bold("python3 -m transcriber once --dry-run"))
    ctx.out(style.dim("      Looks at your OneDrive and writes nothing. Confirms the permissions."))
    ctx.out("    " + style.bold("python3 -m transcriber once --limit 3"))
    ctx.out(style.dim("      Does three recordings for real. Read them before letting it near the rest."))
    ctx.out()
    ctx.out("  Then " + style.bold("python3 -m transcriber run") + " to start the loop.")
    ctx.out()
    return 0


def _validate(env_path: str, ctx: _Ctx) -> list[str]:
    """Load what we just wrote through the real Config, so the wizard cannot disagree with it."""
    try:
        merged = dict(os.environ)
        merged.update(load_env_file(env_path))
        config_mod.Config.from_env(merged)
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        lines = [ln.strip(" -•\t") for ln in text.splitlines() if ln.strip()]
        return [ln for ln in lines if ln][:20] or [text[:300]]
    return []
