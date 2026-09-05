"""The hand-over to closea: one JSON envelope per recording, into a folder it reads.

⛔ A FOLDER, NOT A CALL. closea holds no credential and opens no socket — every one of its
ingestion sources reads a directory, and its own docs state that as a line the build is not
allowed to cross. So the seam is a folder this service writes and that service reads, which
also means a closea that is down, being replaced, or not yet installed DELAYS a recording
instead of losing it. Nothing here imports closea, and nothing here needs closea present.

⛔ THE ENVELOPE SHAPE IS NOT OURS TO CHOOSE. ``closea/ingest/drop.py`` accepts exactly one
schema string and refuses every other one by name rather than parsing it best-effort, and it
checks the item's field list in BOTH directions — an unknown key is a refusal, a missing key
is a refusal. So this module mirrors that contract rather than inventing one, and
``tests/test_the_estate_can_read_what_we_write.py`` drives closea's own shipped reader over
what we produce rather than trusting this paragraph.

⚠ THE SCHEMA STRING CARRIES ANOTHER SERVICE'S NAME, AND THAT IS DELIBERATE. It names the
WIRE FORMAT, which closea's mail service defined first; it does not claim we are that
service. Emitting anything else today means every envelope refused and nothing filed, which
is the one outcome worse than an awkward constant. If closea ever accepts a second name,
this is the line that changes.

⛔ WHAT WE DO NOT SEND, AND WHY EACH ONE IS A REFUSAL ON THEIR SIDE TOO:

  * ``site`` is always null. Which job a recording belongs to is a judgement over the whole
    estate, and the estate is not here. We have a reading of it — the candidates and their
    scores in the summary — and a reading is not a filing. closea refuses a non-null value
    outright, which is the correct answer to a service that has one recording.
  * ``revises`` is always null. It RETIRES a live record. A recording is evidence of what
    somebody said, never an instruction to withdraw something already filed.
  * ``source_fingerprint`` is never sent. It is a hash of the bytes closea itself read, so
    only closea can state it, and an envelope naming it is refused as an unknown field.

⭐ THE BODY IS THE READING, NOT THE DIALOGUE, and this is the one real design decision here.
closea's own transcript connector says it plainly: *the raw dialogue is never written into
memory — a transcript is per-conversation material; the estate holds what the business
knows.* Its mechanical marker-matching is described there as the placeholder for a real
extraction step. This service IS that step: a model reads the recording and returns
proposals, each carrying a quote checked verbatim against the transcript. So the envelope
carries the summary and those proposals with their quotes — the evidence travels inline,
attributable — and the transcript itself stays in OneDrive, named in ``origin``, as the
thing a person opens when they want the whole of what was said.

⚠ ONE ENVELOPE PER RECORDING, KEYED ON THE DRIVE ITEM ID. That id is stable across every
retry and every re-run, which is what makes closea's second pass file nothing. An envelope
per proposal would have been the other reasonable shape and was not taken: proposal text is
a model's output and is not stable between runs, so identity would drift and the same walk
would file again every time it was reprocessed.
"""

from __future__ import annotations

import json
import os
from typing import Any

#: The one envelope version closea reads. Anything else is refused by name — see the module
#: docstring on why this constant carries another service's name.
SCHEMA = "emailorio/incoming-item@1"

#: The key the item sits under. Everything outside it is ignored by a consumer that
#: constructs the dataclass, so nothing load-bearing may live out there.
ITEM_KEY = "item"

#: Exactly the fields closea accepts, in its order. Checked both ways on its side: an extra
#: key is a refusal, a missing key is a refusal.
ITEM_FIELDS = (
    "source_id", "title", "body", "occurred_at", "participants",
    "site", "kind", "tags", "revises", "origin",
)

#: The identity prefix. closea keys idempotency on `source_id` across every source at once,
#: so a prefix that another connector could also produce would make two different things one
#: record.
SOURCE_PREFIX = "recording:"

#: The record kind. closea's `Record` enforces a closed set and raises on anything outside
#: it — and that raise happens in its planning pass, which aborts the whole batch rather
#: than refusing one file. So this is a constant, never a value derived from a recording.
KIND = "fact"

#: What the estate tags this material with, so "show me the site walks" is a query.
TAGS = ("recording", "voice-note")


def _proposal_lines(extraction: Any) -> list[str]:
    """The proposals, each with the words that support it. Empty when there are none."""
    proposals = tuple(getattr(extraction, "proposals", ()) or ())
    if not proposals:
        return []
    out = ["", "## Proposed, not decided", "",
           "Each of these is a proposal a person confirms or it does not count. The quote "
           "under it is verbatim from the recording."]
    for number, proposal in enumerate(proposals, start=1):
        item = getattr(proposal, "item", proposal)
        statement = " ".join(str(getattr(item, "text", "") or "").split())
        quote = " ".join(str(getattr(item, "quote", "") or "").split())
        if not statement and not quote:
            continue
        out.append("")
        out.append(f"{number}. {statement or 'An item with no wording — see the quote'}")
        if quote:
            out.append(f"   > {quote}")
    return out


def body_for(ctx: Any) -> str:
    """What the estate is told this recording says.

    The summary and the proposals with their quotes — never the dialogue. See the module
    docstring: closea's own connector states that the estate holds what the business knows
    rather than what was said, and the transcript stays where a person can open it.
    """
    extraction = getattr(ctx, "extraction", None)
    summary = " ".join(str(getattr(extraction, "summary", "") or "").split())
    lines: list[str] = []
    if summary:
        lines += ["## What this recording says", "", summary]
    lines += _proposal_lines(extraction)
    if not lines:
        # A recording with no reading at all still travels, and says so. Silence here would
        # be indistinguishable from a recording that was never handed over.
        lines = ["## What this recording says", "",
                 "Nothing was read out of this recording beyond the words themselves. The "
                 "transcript named below is the whole of it."]
    source = str(getattr(ctx, "source_name", "") or "")
    names = getattr(ctx, "names", None)
    transcript = str(getattr(names, "transcript", "") or "")
    lines += ["", "## Where the evidence is", ""]
    if source:
        lines.append(f"- Recording: {source}")
    if transcript:
        lines.append(f"- Transcript: {transcript}")
    lines.append("- The transcript is the evidence of what was said. This is a reading of it.")
    return "\n".join(lines).strip() + "\n"


def _participants(ctx: Any) -> list[str]:
    """Everyone the recording names, as a person would write them down.

    ⚠ NEVER AN EMAIL ADDRESS. This service does not emit one anywhere, for any reason, and
    a field the estate indexes and searches is the last place to start.
    """
    out: list[str] = []
    extraction = getattr(ctx, "extraction", None)
    for who in tuple(getattr(extraction, "participants", ()) or ()):
        name = " ".join(str(getattr(who, "name", "") or who).split())
        if name and "@" not in name and name not in out:
            out.append(name)
    parsed = getattr(ctx, "parsed", None)
    party = " ".join(str(getattr(parsed, "party", "") or "").split())
    if party and "@" not in party and party not in out:
        out.append(party)
    return out


def item_for(ctx: Any, item_id: str) -> dict:
    """closea's `IncomingItem` fields, and exactly those."""
    recorded_at = getattr(ctx, "recorded_at", None)
    names = getattr(ctx, "names", None)
    return {
        "source_id": f"{SOURCE_PREFIX}{item_id}",
        # ⚠ `label` IS THE NAME THE RECORDING GOES BY, and it is the same string the
        # transcript's own subject line is built from — so the estate and the file in
        # OneDrive call this recording the same thing. It already carries the worked-out
        # name when one was applied, and the filename when it was not.
        "title": str(getattr(ctx, "label", "") or getattr(ctx, "source_name", "")
                     or f"Recording {item_id}"),
        "body": body_for(ctx),
        "occurred_at": recorded_at.isoformat() if recorded_at is not None else None,
        "participants": _participants(ctx),
        # ⛔ Always null. See the module docstring.
        "site": None,
        "kind": KIND,
        "tags": list(TAGS),
        # ⛔ Always null. See the module docstring.
        "revises": None,
        "origin": str(getattr(names, "transcript", "") or getattr(ctx, "source_name", "") or ""),
    }


def envelope(ctx: Any, item_id: str) -> dict:
    """One recording, in the shape closea reads."""
    item = item_for(ctx, item_id)
    missing = [f for f in ITEM_FIELDS if f not in item]
    extra = [k for k in item if k not in ITEM_FIELDS]
    if missing or extra:  # pragma: no cover - the tests below make this unreachable
        raise ValueError(
            f"the item does not carry closea's fields: missing {missing}, extra {extra}"
        )
    return {"schema": SCHEMA, ITEM_KEY: item}


def write(drop_dir: str, ctx: Any, item_id: str) -> str:
    """Publish one envelope. Atomic: closea sees all of it or none of it.

    ⛔ WRITTEN TO A TEMPORARY NAME IN THE SAME DIRECTORY, THEN RENAMED OVER. closea scans
    this folder and reads what it finds; a file written in place is one it can open halfway
    through, and its honest response to a truncated envelope is to report it unreadable —
    which turns our slow disk into their lost recording. The temporary name starts with a
    dot and does not end in ``.json``, so a reader globbing ``*.json`` never sees it, and
    the rename is within one directory so it cannot fail across filesystems.
    """
    target = os.path.expanduser(str(drop_dir or "").strip())
    os.makedirs(target, exist_ok=True)
    payload = json.dumps(envelope(ctx, item_id), indent=2, sort_keys=True, ensure_ascii=False)
    final = os.path.join(target, f"{item_id}.json")
    tmp = os.path.join(target, f".{item_id}.tmp-{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, final)
    return final


__all__ = [
    "SCHEMA", "ITEM_KEY", "ITEM_FIELDS", "SOURCE_PREFIX", "KIND", "TAGS",
    "body_for", "item_for", "envelope", "write",
]
