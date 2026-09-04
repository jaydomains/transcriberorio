"""Which site a recording is about, decided by the record's own rules rather than ours.

A recording that arrives under the voice recorder's own name — ``Voice 260806_162219.m4a``
— has nothing in its name saying where it was made. The site is in the recording, said out
loud, and :mod:`transcriber.autoname` proposes it as the transcript's title.

**This module exists so that the title cannot disagree with the filing.** The downstream
record binds every document to a site by scoring the document's own text against a
vocabulary it learns from itself, and that binding is what puts a note into a site's
correspondence log. If this service worked out the site by some other means — the model's
own answer, a prefix match, a fuzzy match — it could confidently title a note ``BEACH
COURT`` that the record then files under Forest Hill, and nobody would ever look at the
two together. Worse, the title becomes part of the bytes the record scores, so a title
naming the wrong site can *move* a filing that was previously right.

So the record's rules are vendored here, verbatim, and run over the exact bytes the record
will be handed. Not an equivalent of them — a copy of them, under the discipline
``tests/vendored_ingest.py`` already states:

    **Copied with its behaviour intact, including the parts that look like bugs.**

Copied rather than imported because that repository is read-only to this service, is not on
the path in CI, and an import would make this service's behaviour depend on a checkout that
is not ours. ``tests/test_sitebook_is_faithful.py`` runs both copies over the real spine and
fails the build when they disagree.

**Everything here fails towards fewer names, never towards different ones.** A missing
book, an unreadable one, one written against a contract this code does not know, a site the
record has no folder for — every one of them ends in "no name proposed", which is exactly
today's behaviour. :func:`load` never raises, because a naming feature that could stop a
transcript being written would be worse than no naming feature at all.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

log = logging.getLogger(__name__)

__all__ = [
    "CONTRACT",
    "SiteCandidate",
    "SiteEvidence",
    "evidence_for",
    "STOPWORDS",
    "ADDR_RE",
    "VOCAB_FIELDS",
    "SiteBook",
    "EMPTY",
    "site_vocab",
    "bind_site",
    "load",
]

#: The shape of the file this module reads. The generator stamps the same integer, and a
#: mismatch empties the book rather than guessing at a field that moved. If the record ever
#: changes which fields feed the vocabulary it bumps this, the book goes empty, and the
#: morning email says so — the failure is fewer names, never different ones.
CONTRACT = 1

#: The eight fields the record's ``site_vocab`` actually reads. Named here because the
#: generator projects exactly these out of the record's 7.8 MB spine and nothing else: the
#: projection is 80 KB, and ``site_vocab(projection) == site_vocab(spine)`` is asserted by
#: the test suite against the real file, so the artifact cannot quietly lose a field.
VOCAB_FIELDS = (
    "title",
    "monday_item_id",
    "status_raw",
    "contractors_raw",
    "client_org_raw",
    "supervisor_raw",
    "timeline_raw",
    "kbc_owners_raw",
)

# --- vendored verbatim from kbc-site-memory/tools/ingest.py, as of 2026-08-28 ----------

STOPWORDS = set("the of and for a to at on in bc body corporate project works site phase "
                "pty ltd trust".split())

ADDR_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")


def site_vocab(spine: Mapping[str, Any]) -> dict[str, set[str]]:
    """What each site can be recognised by: its name, its slug, its monday id, the firms
    and the client named on it. Learned from the record, never typed in here."""
    vocab: dict[str, set[str]] = {}
    for slug, s in spine["sites"].items():
        terms = set()
        for tok in re.findall(r"[A-Za-z0-9]+", s["title"].lower()):
            if len(tok) > 3 and tok not in STOPWORDS:
                terms.add(tok)
        terms.add(slug.replace("-", " "))
        if s.get("monday_item_id"):
            terms.add(s["monday_item_id"])
        for raw in (s.get("contractors_raw"), s.get("client_org_raw")):
            for frag in re.split(r"[,;·—\-–(]", raw or "")[:4]:
                frag = frag.strip()
                if 4 < len(frag) < 34 and not ADDR_RE.search(frag):
                    terms.add(frag.lower())
        vocab[slug] = {t for t in terms if len(t) > 3}
    # A term that names several sites names none of them. "point", "road", "growthpoint",
    # "reno" - all true of a dozen jobs, so all useless for saying which one this is.
    #
    # Counting how many TITLES a term appears in is not enough. Exactly one site is called
    # "House Swart Snags", so "snags" survived that test - and then an email about
    # Chepstow with the word "snags" in the subject scored 2 for each site, tied, and was
    # left unbound. A word is generic because of how the record USES it, not because of
    # how many titles happen to contain it. So the count is over how many sites mention
    # the term anywhere in what is recorded about them.
    mentions: dict[str, int] = {}
    for slug, s_ in spine["sites"].items():
        blob = " ".join(str(s_.get(k) or "") for k in
                        ("title", "status_raw", "contractors_raw", "client_org_raw",
                         "supervisor_raw", "timeline_raw", "kbc_owners_raw")).lower()
        for t in {t for terms in vocab.values() for t in terms}:
            if t in blob:
                mentions[t] = mentions.get(t, 0) + 1
    common = {t for t, n in mentions.items() if n > 2}
    return {slug: terms - common for slug, terms in vocab.items()}


def bind_site(text: str, vocab: Mapping[str, Any]) -> tuple[str | None, dict[str, int]]:
    """Which site this text belongs to. ``None`` when the text does not say.

    The record's :func:`bind_site` reduced to the only path a transcript can take through
    it. The full form scores an address already bound to a site at 6 before it scores any
    name, because an address was put there by a person — but a transcript has no addresses:
    :func:`transcriber.models.strip_emails` removes them before a file is ever written, and
    the record's ``from_addr``/``to``/``cc`` are empty for every file this service produces.
    ``tests/test_sitebook_is_faithful.py`` asserts this reduced form equals the record's
    full form under exactly that condition, so the reduction is checked rather than assumed.

    The two behaviours that matter are kept exactly:

    * a term scores 4 when it is all digits (a monday id is not a coincidence) and 2
      otherwise;
    * **a tie is not an answer.** Two sites scoring the same means the text does not say
      which, and the answer is ``None``. This is why a title naming the wrong site is
      destructive rather than merely useless: it can tie a binding that was clean.
    """
    lowered = (text or "").lower()
    scores: dict[str, int] = {}
    for slug, terms in vocab.items():
        for t in terms:
            if t and t in lowered:
                scores[slug] = scores.get(slug, 0) + (4 if t.isdigit() else 2)
    if not scores:
        return None, scores
    best = max(scores, key=lambda k: scores[k])
    rank = sorted(scores.values(), reverse=True)
    # A tie is not an answer. Two sites scoring the same means the message does not say.
    if len(rank) > 1 and rank[0] == rank[1]:
        return None, scores
    return best, scores


# --- ours -----------------------------------------------------------------------------


#: Compiled whole-word patterns, keyed by term. The book holds ~56 sites with a handful of
#: terms each and is re-read only when the file changes, so this is bounded by the book.
_WORD_RE_CACHE: dict[str, "re.Pattern[str]"] = {}


def _stands_as_a_word(term: str, text: str) -> bool:
    """Whether ``term`` appears in ``text`` as a whole word rather than inside one.

    Deliberately NOT what the record does — the record's own matching is a substring test,
    and that is right for it: an email mentioning "Beachwood" probably is about Beach Court.
    It is wrong for a title, which is read by a person as a claim about where he was.
    """
    return re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text) is not None


@dataclass(frozen=True)
class SiteBook:
    """The sites the record knows about, and the vocabulary that recognises them.

    Falsey when it holds no sites, which is the state on every failure. Code that reads it
    is expected to ask ``if not book`` and stop, not to catch anything.
    """

    sites: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    vocab: Mapping[str, Any] = field(default_factory=dict)
    generated_at: str = ""
    path: str = ""
    mtime: float = 0.0
    #: Plain English, empty when the book loaded cleanly. Printed in the morning email, so
    #: a book that has silently gone missing says so rather than looking like a quiet week.
    fault: str = ""

    def __bool__(self) -> bool:
        return bool(self.sites)

    @property
    def size(self) -> int:
        return len(self.sites)

    def bind(self, text: str) -> tuple[str | None, dict[str, int]]:
        """The site the record would file this text under, and every site's score."""
        if not self.vocab:
            return None, {}
        return bind_site(text, self.vocab)

    def spoken_names(self, limit: int = 80) -> tuple[str, ...]:
        """The job names as a person says them, for the transcription engine's hint list.

        This is the fix for the thing that makes everything downstream a guessing game: an
        engine that has never heard of Lonehill writes down "on loan", and no amount of
        matching afterwards recovers a word that was never transcribed. Two real examples
        from one site walk — "wrong on loan" and "the same issue at lo" — both Lonehill,
        both invisible to a matcher, both obvious to a person.

        The record already knows every job's name. It was only ever read here to *name* a
        recording, never to help transcribe one, so the book that could have prevented the
        mishearing sat one function call away from the engine.

        Titles only, deliberately. :func:`site_vocab`'s terms are tokens chosen to
        *discriminate between* sites once the words exist — contractor fragments, monday
        ids, half-words. As a spoken hint they are noise at best, and a monday id read out
        as a phrase is worse. What helps an engine is the name somebody actually says.

        Ordered longest first: a hint list is capped downstream by
        :func:`transcriber.engines.base.safe_vocabulary`, and if something has to be
        dropped it should be the one-word names, which are the ones an engine is least
        likely to get wrong and most likely to hear anyway.
        """
        names = []
        for entry in self.sites.values():
            title = str((entry or {}).get("title") or "").strip()
            if title:
                names.append(title)
        names.sort(key=lambda t: (-len(t), t.lower()))
        return tuple(names[:max(0, int(limit))])

    def sites_named_by(self, span: str) -> frozenset[str]:
        """Every site this span actually names. Two conditions, both learned the hard way.

        The guard against naming a recording after something that is not a site. The model
        may answer ``"House"``, ``"North"`` or ``"Green"``, all of which appear in real site
        titles — but the record drops any term it uses of more than two sites, so none of
        them discriminates anything and none of them names anywhere.

        **The term has to stand as a word.** This was a substring test, and an adversarial
        sweep of the record's own correspondence found what that costs: ``SHARON`` became a
        title for *277 Imam Haron Road*; ``DURBANVILLE`` — which is Orion Concrete Yard's own
        address, eleven occurrences in his own emails — for *Urban Artisan*; ``PRINCESS
        COURT`` for *Prince Court*; ``BEACHWOOD`` for *Beach Court*. Not invented cases:
        every one is a word that appears in his correspondence and happens to contain a
        site's term.

        **And it has to carry at least half of the site's own name.** ``CONCRETE``,
        ``FLOORING``, ``CLADDING``, ``STEELWORKS``, ``GARDENS`` — the ordinary nouns of the
        trade — each match exactly one site through a contractor or client entry while
        sharing nothing with what the site is actually called, and each would otherwise be
        published as a title on the record's most-read surface.

        Half rather than most, deliberately: ``CANTERBURY`` is one word of *Canterbury
        Square* and is exactly what he writes on his own files — ``CANTERBURY SNAG WALK 14
        AUGUST``, ``CANTERBURY 6 AUGUST``. A rule that refused his own naming convention
        would be tuned for the tests rather than for him.

        **What this still lets through, stated rather than hidden:** a trade noun that is
        itself half of a site's title — ``FLOORING`` for *BLSA Flooring*, ``CLADDING`` for
        *Roggebaai Cladding*, ``STEELWORKS`` for *Ashton Steelworks*. Structurally identical
        to ``CANTERBURY`` for *Canterbury Square*, so nothing here can separate them without
        a hand-kept list of trade words, which is the maintained vocabulary this design
        exists to avoid. The cost is bounded and different in kind from the cases above:
        the site is the RIGHT one and the title is merely terse, where ``SHARON`` for *277
        Imam Haron Road* named nowhere at all. Pinned by a test so it is a decision rather
        than a surprise.
        """
        lowered = " ".join((span or "").lower().split())
        if not lowered:
            return frozenset()

        words = {w for w in re.findall(r"[a-z0-9]+", lowered) if w not in STOPWORDS}
        hits: set[str] = set()
        for slug, terms in self.vocab.items():
            if not any(t and _stands_as_a_word(t, lowered) for t in terms):
                continue
            # More than half of what the record itself calls this site. A title carrying one
            # word of it is a coincidence; carrying most of it is a naming.
            title_words = {
                w for w in re.findall(r"[a-z0-9]+", str((self.sites.get(slug) or {}).get("title") or "").lower())
                if len(w) > 3 and w not in STOPWORDS
            }
            if not title_words:
                continue
            shared = title_words & words
            if len(shared) * 2 < len(title_words):
                continue
            hits.add(slug)
        return frozenset(hits)

    def title_of(self, slug: str) -> str:
        """The site's name as a person would say it, for anything he reads.

        Never the slug. ``beach-court-bc`` is how the record files things and is exactly
        the kind of thing that must not appear in his morning email.
        """
        entry = self.sites.get(slug) or {}
        return str(entry.get("title") or "").strip() or str(slug or "")

    def mentions_of_each(self, text: str, *, spans: Any = None) -> dict[str, int]:
        """How much of this recording is about each site, by how often it is named.

        The record's own :func:`bind_site` cannot answer this and is not built to: it scores
        a site by how many *distinct* vocabulary terms appear anywhere in the document, once
        each, never by how often. So a passing call about "Ashton Steelworks" — three terms,
        ``ashton``, ``ashton steelworks``, ``steelworks`` — outscores an hour spent at Eagle
        House, which carries one. That is the right answer for an email, which is what the
        record was built to file, and the wrong answer for a recording, where the question
        is not *is this site mentioned* but *is this recording about it*.

        Counting names spoken is the question a person would ask, and it is what he asked
        for: *"any decent api call would easily be able to infer the site name from the
        majority of conversation."* This is the mechanical half of that — the model's answer
        still has to win the count, so a model naming a site that was said twice against one
        said forty times is refused rather than believed.
        """
        counts: dict[str, int] = {}
        lowered = (text or "").lower()
        for slug, terms in self.vocab.items():
            best = 0
            for term in terms:
                if not term or len(term) < 4:
                    continue
                # Whole words, for the same reason :func:`sites_named_by` counts that way.
                # A plain substring count was inflating a site's score with words nobody
                # said: "durbanville" scores for *Urban* Artisan, "princess court" scores
                # for *Prince* Court, "imam haron road" scores for anyone called *Sharon*.
                # The number this returns decides the majority test in N7 — "is this
                # recording ABOUT this site" — so a term counted inside another word is a
                # vote cast by a word that was never spoken.
                found = len(_WORD_RE_CACHE.setdefault(term, re.compile(
                    r"(?<!\w)" + re.escape(term) + r"(?!\w)")).findall(lowered))
                if found > best:
                    best = found
            if best:
                counts[slug] = best
        return counts

    def line(self) -> str:
        """The one line the morning email prints, every day, including the bad ones.

        Printed on a day with nothing to report as well, so that silence never means
        "working" and "the site list vanished a fortnight ago" at the same time.
        """
        if not self.sites:
            return (
                f"site list: not loaded — {self.fault}, so nothing is being named"
                if self.fault
                else "site list: empty, so nothing is being named"
            )
        when = f", written {self.generated_at}" if self.generated_at else ""
        tail = f" ({self.fault})" if self.fault else ""
        return f"site list: {self.size} sites, from the record's nightly build{when}{tail}"


#: The book when there is no book. Every failure path returns this or a copy of it carrying
#: a ``fault``, so no caller ever has to handle ``None``.
EMPTY = SiteBook()


@dataclass(frozen=True)
class SiteCandidate:
    """One job the recording might be about, and how strongly."""

    slug: str
    title: str
    score: int


@dataclass(frozen=True)
class SiteEvidence:
    """Which jobs a recording appears to concern — as EVIDENCE, never as a filing.

    A site walk covers the job somebody is standing on and the two that are bothering
    them. One real recording discussed HQ, Lonehill and Pick n Pay, and everything
    downstream had to work that out again from raw text, every time, from scratch.

    The work was already being done here. :meth:`SiteBook.bind` runs the record's own
    scoring — vendored verbatim — over the exact bytes the record will be handed, and
    returns a score for *every* site. Only the winner was ever used, to propose a title.
    The rest was thrown away, and downstream started the same hunt over.

    **This is evidence, and the distinction is the whole point.** The record binds a
    document by scoring the document's own text, and this module's own docstring says why
    that must not be pre-empted: a name asserted here that is wrong can *move* a filing
    that was previously right. So what travels is candidates, scores, and the words they
    were matched on — the workings, not the answer. The record still decides.

    ``by_quote`` maps a proposal's verbatim quote to the slugs that quote itself names.
    Keyed by the quote rather than by position because the actions file renders proposals
    grouped by category, not in extraction order, and a lookup that depends on the caller
    iterating in the right order is a lookup that will eventually be wrong. Two proposals
    sharing a quote share an answer, which is correct rather than a collision.

    Empty for most quotes, and that is not a failure: silence is the honest answer to
    "which job is this line about" when the line does not say.
    """

    candidates: tuple[SiteCandidate, ...] = ()
    by_quote: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    book_size: int = 0
    #: Empty when the book loaded cleanly. Carried so a reader can tell "no jobs matched"
    #: from "the job list could not be read", which are very different facts.
    fault: str = ""

    def __bool__(self) -> bool:
        return bool(self.candidates) or any(self.by_quote.values())

    def slugs_for(self, quote: str) -> tuple[str, ...]:
        """The jobs this one line names. Empty when it names none — most lines."""
        return tuple(self.by_quote.get(_quote_key(quote), ()))

    @property
    def named_by_items(self) -> tuple[str, ...]:
        """Every slug some individual line names, in a stable order."""
        seen: list[str] = []
        for slugs in self.by_quote.values():
            for slug in slugs:
                if slug not in seen:
                    seen.append(slug)
        return tuple(sorted(seen))

    def title_of(self, slug: str) -> str:
        for candidate in self.candidates:
            if candidate.slug == slug:
                return candidate.title
        return slug


def evidence_for(book: "SiteBook", text: str, quotes: Sequence[str] = ()) -> SiteEvidence:
    """Read the site book over one recording. Never raises; never files anything.

    ``text`` is the bytes the record will score — hand it the same thing, or the candidate
    scores describe a document nobody will ever ingest.

    ``quotes`` are the proposals' verbatim quotes, in order. Each is matched with
    :meth:`SiteBook.sites_named_by`, which carries the two guards learned the hard way —
    a term must stand as a whole word, and must carry half of the site's own name — so a
    line mentioning a Sharon does not name *277 Imam Haron Road*.
    """
    if not book:
        return SiteEvidence(fault=book.fault or "no site list is configured")
    try:
        _winner, scores = book.bind(text or "")
    except Exception:  # noqa: BLE001 - evidence is a nicety; a recording is not
        log.warning("site-evidence", "the site list could not be scored for this recording")
        return SiteEvidence(book_size=book.size, fault="the site list could not be scored")

    ranked = sorted(
        ((slug, int(n)) for slug, n in (scores or {}).items() if n),
        key=lambda pair: (-pair[1], pair[0]),
    )
    candidates = tuple(
        SiteCandidate(slug=slug,
                      title=str((book.sites.get(slug) or {}).get("title") or slug),
                      score=score)
        for slug, score in ranked
    )

    by_quote: dict[str, tuple[str, ...]] = {}
    for quote in quotes:
        key = _quote_key(quote)
        if not key or key in by_quote:
            continue
        try:
            by_quote[key] = tuple(sorted(book.sites_named_by(quote or "")))
        except Exception:  # noqa: BLE001
            by_quote[key] = ()
    return SiteEvidence(candidates=candidates, by_quote=by_quote,
                        book_size=book.size, fault=book.fault)


def _quote_key(quote: str) -> str:
    """One spelling of a quote, so the lookup is not defeated by whitespace."""
    return " ".join(str(quote or "").split()).lower()


def load(path: str) -> SiteBook:
    """Read the site book. **Never raises.**

    Every fault comes back as an empty book carrying a plain-English ``fault``, because the
    only thing this file can do for a recording is give it a nicer title, and nothing about
    a nicer title is worth a transcript. A missing file is not even an error: it is the
    shipped default, and it means no recording is ever named.
    """
    target = (path or "").strip()
    if not target:
        return EMPTY

    try:
        stat = os.stat(target)
        with open(target, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return SiteBook(path=target, fault=f"{os.path.basename(target)} is not there yet")
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return SiteBook(path=target, fault=f"{os.path.basename(target)} could not be read ({exc})")

    if not isinstance(raw, dict):
        return SiteBook(path=target, fault=f"{os.path.basename(target)} is not a site list")

    contract = raw.get("vocab_contract")
    if contract != CONTRACT:
        return SiteBook(
            path=target,
            fault=(
                f"{os.path.basename(target)} was written for a different version "
                f"({contract!r}, this service reads {CONTRACT}), so nothing is being named "
                f"until the two agree"
            ),
        )

    sites = raw.get("sites")
    if not isinstance(sites, dict) or not sites:
        return SiteBook(path=target, fault=f"{os.path.basename(target)} lists no sites")

    # One malformed site must not cost the other fifty-five. A site without a title cannot
    # contribute vocabulary anyway, so it is dropped rather than raising.
    usable = {
        slug: entry for slug, entry in sites.items()
        if isinstance(slug, str) and isinstance(entry, dict) and str(entry.get("title") or "").strip()
    }
    if not usable:
        return SiteBook(path=target, fault=f"{os.path.basename(target)} lists no usable sites")

    try:
        vocab = site_vocab({"sites": usable})
    except Exception as exc:                                        # pragma: no cover
        return SiteBook(path=target, fault=f"{os.path.basename(target)} could not be read ({exc})")

    dropped = len(sites) - len(usable)
    return SiteBook(
        sites=usable,
        vocab=vocab,
        generated_at=str(raw.get("generated_at") or ""),
        path=target,
        mtime=stat.st_mtime,
        fault=(f"{dropped} of {len(sites)} sites had no name and were skipped" if dropped else ""),
    )
