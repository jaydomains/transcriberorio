"""A copy of the downstream record's own parser, so the contract is tested against it.

Lifted from ``kbc-site-memory/tools/ingest.py`` (``parse_texty``, ``ADDR_RE``, ``_addrs``)
and ``kbc-site-memory/tools/gen_common.py`` (``parse_date``, ``MONTHS``). Copied rather
than imported: that repository is read-only to this service and is not on the path in CI,
and an import would make this suite pass or fail depending on a checkout that is not ours.

**Copied with its behaviour intact, including the parts that look like bugs.**

  * a non-matching, non-blank line inside the header block is swallowed — it reaches
    neither the header nor the body;
  * ``kind`` is ``email`` the moment a ``From:`` header appears;
  * ``parse_date`` matches ``\\b20\\d\\d-\\d\\d-\\d\\d\\b`` first and otherwise falls through
    to a year-month pattern that answers the *first of the month*.

The point of these tests is what the record will actually do with our file, not what it
ought to do. If this copy is ever "corrected", it stops testing anything.

Faithful to ingest.py as of 2026-08-27.
"""

from __future__ import annotations

import os
import re

__all__ = ["ADDR_RE", "HEADER_RE", "MONTHS", "parse_date", "parse_texty"]

# --- from tools/ingest.py -------------------------------------------------------------

ADDR_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")

HEADER_RE = re.compile(r"^(from|to|cc|subject|date|sent)\s*:\s*(.*)$", re.I)

# --- from tools/gen_common.py ---------------------------------------------------------

MONTHS = {m: i + 1 for i, m in enumerate(
    "january february march april may june july august september october november december".split())}
MONTHS.update({m[:3]: i + 1 for i, m in enumerate(
    "january february march april may june july august september october november december".split())})
MONTHS["sept"] = 9


def parse_date(s):
    """First real date on a string -> ISO, else None. Never guesses a missing component."""
    if not s:
        return None
    m = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", s)
    if m:
        return m.group(0)
    m = re.search(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(20\d{2})\b", s)
    if m and m.group(2).lower() in MONTHS:
        return f"{m.group(3)}-{MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
    m = re.search(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(20\d{2})\b", s)
    if m and m.group(1).lower() in MONTHS:
        return f"{m.group(3)}-{MONTHS[m.group(1).lower()]:02d}-{int(m.group(2)):02d}"
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b", s)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    m = re.search(r"\b(20\d{2})-(\d{2})\b", s)
    if m:
        return f"{m.group(0)}-01"
    return None


def _addrs(v):
    return [a.lower() for a in ADDR_RE.findall(v or "")]


def parse_texty(path):
    """A .txt or .md drop: an optional header block, then the body. Also a .vtt transcript."""
    # The only departure from the original: the handle is closed. Upstream leaks it, which
    # is harmless in a script that exits and noisy in a test suite that runs it hundreds of
    # times. Nothing about the parsing below differs.
    with open(path, encoding="utf-8", errors="replace") as handle:
        raw = handle.read()
    if path.lower().endswith(".vtt") or raw.lstrip().startswith("WEBVTT"):
        lines = [l for l in raw.split("\n")
                 if l.strip() and "-->" not in l and not l.strip().isdigit()
                 and not l.startswith("WEBVTT")]
        return {"kind": "transcript", "date": parse_date(os.path.basename(path)) or "",
                "subject": os.path.splitext(os.path.basename(path))[0],
                "from_addr": "", "from_name": "", "to": [], "cc": [], "message_id": "",
                "body": "\n".join(lines)}
    head, body, seen = {}, raw, False
    lines = raw.split("\n")
    for i, l in enumerate(lines):
        m = HEADER_RE.match(l.strip())
        if m:
            head[m.group(1).lower()] = m.group(2).strip()
            seen = True
            continue
        if seen and not l.strip():
            body = "\n".join(lines[i + 1:])
            break
        if not seen and i > 6:
            break
    kind = "transcript" if not head.get("from") else "email"
    return {"kind": kind,
            "date": parse_date(head.get("date") or head.get("sent") or "")
                    or parse_date(os.path.basename(path)) or "",
            "subject": head.get("subject") or os.path.splitext(os.path.basename(path))[0],
            "from_addr": (_addrs(head.get("from", "")) or [""])[0],
            "from_name": re.sub(r"<[^>]*>", "", head.get("from", "")).strip(' "'),
            "to": _addrs(head.get("to", "")), "cc": _addrs(head.get("cc", "")),
            "message_id": "", "body": body}
