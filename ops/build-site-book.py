#!/usr/bin/env python3
"""Write the site book the naming feature reads, from the record's own build.

    ops/build-site-book.py /path/to/kbc-site-memory /var/lib/transcriber/sites.json

The record's ``build/spine.json`` is 7.8 MB and rebuilt nightly. Loading it on every
recording would be silly, and pointing the service at a file inside somebody else's
repository would couple a running service to a git checkout. So this projects out the
**eight fields the record's own vocabulary function actually reads** — 80 KB for 56 sites —
and writes them somewhere the service owns.

It lives here, in this repository, rather than in the record's, for one reason: the record
is read-only to this service. Nothing about naming may require a change over there, and
nothing here may write there. This reads one file and writes one file.

Run it from the same cron entry that rebuilds the record, after the rebuild:

    30 4 * * *  cd /srv/kbc-site-memory && make build && \\
                /srv/transcriber/ops/build-site-book.py . /var/lib/transcriber/sites.json

**Nothing breaks if it never runs.** A stale book names slightly fewer recordings; a
missing one names none. Both are today's behaviour, and the morning email prints the book's
date every day so a book that quietly stopped being written says so.
"""

from __future__ import annotations

import datetime
import json
import os
import sys

#: Must equal transcriber.sitebook.CONTRACT. If the record ever changes which fields feed
#: its vocabulary, bump both — the service empties its book on a mismatch rather than
#: reading a field that has moved, so the failure is fewer names, never different ones.
CONTRACT = 1

#: Exactly the fields kbc-site-memory/tools/ingest.py:site_vocab reads, plus the slug.
#: A test in this repository asserts that the vocabulary built from this projection is
#: identical to the one built from the whole spine, against the real file.
FIELDS = (
    "slug",
    "title",
    "monday_item_id",
    "status_raw",
    "contractors_raw",
    "client_org_raw",
    "supervisor_raw",
    "timeline_raw",
    "kbc_owners_raw",
)


def project(spine: dict) -> dict:
    sites = spine.get("sites") or {}
    return {
        "vocab_contract": CONTRACT,
        "generated_at": datetime.date.today().isoformat(),
        "sites": {
            slug: {f: entry.get(f) for f in FIELDS}
            for slug, entry in sites.items()
            if str((entry or {}).get("title") or "").strip()
        },
    }


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    repo, out = argv[1], argv[2]

    source = os.path.join(repo, "build", "spine.json")
    try:
        with open(source, "r", encoding="utf-8") as handle:
            spine = json.load(handle)
    except (OSError, ValueError) as exc:
        print(f"could not read {source}: {exc}", file=sys.stderr)
        return 1

    book = project(spine)
    if not book["sites"]:
        # Refuse to overwrite a good book with an empty one. A half-finished build is the
        # likely cause, and yesterday's list is worth more than none.
        print(f"{source} lists no usable sites; leaving {out} as it is", file=sys.stderr)
        return 1

    # Written whole and moved into place, so the service never reads half a file. It polls
    # by modification time and re-reads on a change; a partial read would empty its book
    # for a day.
    tmp = f"{out}.tmp"
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(book, handle, ensure_ascii=False)
    os.replace(tmp, out)

    print(f"{len(book['sites'])} sites -> {out} ({os.path.getsize(out)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
