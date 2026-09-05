"""The AI pass: route every recording, extract from the substantive ones, verify every quote.

Two tiers, for one reason. A cheap model reads **every** recording and says whether it
carries anything worth keeping; a stronger model then reads only the ones that do. Nothing
is skipped on a guess about duration or file size, because the recording this service was
built for — a twelve-second *"ja, approved, go ahead on Beach Court"* — is exactly the one a
size heuristic throws away.

Three mechanisms in this module are deliberately machinery rather than judgement:

**The safety override** (:func:`route_precheck`). Any mention of a person, a site, a number,
a date, an amount, an approval or a promise forces ``substantive``, however short the
recording and whatever the model said. It is a regex pass over the transcript and it can
only ever ESCALATE — there is no path in this file by which a pre-check result makes a
recording less likely to be read. If the router model is unavailable, that too escalates.

**Quote verification** (:func:`verify_quote`). Every extracted item carries a quote, and the
quote is confirmed to genuinely appear in the transcript before the item is allowed near an
output. An item whose quote cannot be found does not reach the actions file at all; it is
diverted to the review list, where a person sees it. This is the guard against a misheard
word hardening into somebody's task. An exact match after normalisation is preferred; a
fuzzy match is accepted only above a high threshold, only when the words themselves are
mostly present, and the match ratio is recorded on the item either way. Where a match is
found, the quote written out is the transcript's own span, never the model's copy of it.

**No decisions, ever.** Every item is :class:`~transcriber.models.ExtractedItem`, whose
``observed_by`` can only be ``agent`` and which has no ``decided_by`` field to fill. The
downstream record demotes an agent observation to a question. That is correct, it is the
point, and nothing here tries to defeat it.

The provider is configurable. The default is the Anthropic Messages API; an
OpenAI-compatible chat-completions endpoint is the other built-in. Both are driven over
``urllib`` through the service's own retrying HTTP client, and both are asked for
JSON-schema-constrained output so the shape of the answer is guaranteed rather than hoped
for and then repaired.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Callable, Mapping, Sequence

from . import prompts
from .engines.base import (
    EngineAuthError,
    EngineError,
    EngineHTTPError,
    EngineResponseError,
    EngineTransportError,
    HttpClient,
    RetryPolicy,
)
from .models import (
    ITEM_KINDS,
    ExtractedItem,
    Hints,
    Transcript,
    contains_email,
    strip_dictated_emails,
    strip_emails,
    utc_now_iso,
)

__all__ = [
    "AnalysisError",
    "AnalysisConfigError",
    "AnalysisAuthError",
    "AnalysisHTTPError",
    "AnalysisTransportError",
    "AnalysisResponseError",
    "TranscriptTooLarge",
    "PROVIDERS",
    "DEFAULT_PROVIDER",
    "AnalysisSettings",
    "QuoteCheck",
    "normalise_for_match",
    "verify_quote",
    "SAFETY_CATEGORIES",
    "SafetyTrigger",
    "route_precheck",
    "Routing",
    "Participant",
    "Proposal",
    "ReviewItem",
    "UnclearPassage",
    "Extraction",
    "Extractor",
    "extract",
]

log = logging.getLogger("transcriber.extract")


# --------------------------------------------------------------------------------- errors


class AnalysisError(RuntimeError):
    """Base class. Every failure out of this module is one of these, and every one is loud."""


class AnalysisConfigError(AnalysisError):
    """The pass cannot be run as configured. Raised before any network call."""


class AnalysisAuthError(AnalysisError):
    """401/403 from the provider. Never carries the credential that failed."""


class AnalysisTransportError(AnalysisError):
    """The request never produced an HTTP response — DNS, TLS, timeout, reset connection."""


class AnalysisHTTPError(AnalysisError):
    """A non-retryable, or finally-failing, HTTP status from the provider."""

    def __init__(self, message: str, *, status: int = 0, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class AnalysisResponseError(AnalysisError):
    """The provider answered in a shape this module cannot read.

    Kept distinct from :class:`AnalysisHTTPError` because it is the failure that means the
    API changed under us — a refusal, a truncated answer, or JSON that does not match the
    schema we constrained it to. None of those may be papered over: a half-read recording
    filed as a success is the exact bug this service exists to remove.
    """


class TranscriptTooLarge(AnalysisError):
    """The transcript exceeds what one request may carry.

    Raised rather than truncated. Silently analysing the first half of a long site meeting
    and marking it done loses the second half forever and tells nobody.
    """


# ------------------------------------------------------------------------------ providers


@dataclass(frozen=True)
class _ProviderSpec:
    """How one provider is addressed. Everything provider-shaped lives in these two objects."""

    name: str
    base_url: str
    model_cheap: str
    model_strong: str
    supports_effort: bool


#: Model ids come from the bundled ``claude-api`` skill reference (Current Models table,
#: cached 2026-06-24), not from a guess. ``claude-haiku-4-5`` is the router tier and
#: ``claude-opus-5`` reads the substantive recordings. Both support JSON-schema structured
#: output. If these ever need changing, they are also settable per deployment via
#: ANALYSIS_MODEL_CHEAP / ANALYSIS_MODEL_STRONG, so a model rename is a config change and
#: not a code release.
ANTHROPIC = _ProviderSpec(
    name="anthropic",
    base_url="https://api.anthropic.com",
    model_cheap="claude-haiku-4-5",
    model_strong="claude-opus-5",
    # `output_config.effort` is rejected by the Haiku tier, so it is only ever sent on the
    # strong call. See _build_body.
    supports_effort=True,
)

#: Any OpenAI-compatible ``/chat/completions`` endpoint. The model ids have no defaults
#: worth hard-coding — a deployment pointing here names its own models in config — so these
#: are left as an explicit, obviously-unset value that fails loudly rather than a plausible
#: guess that quietly bills for the wrong thing.
OPENAI = _ProviderSpec(
    name="openai",
    base_url="https://api.openai.com/v1",
    model_cheap="",
    model_strong="",
    supports_effort=False,
)

PROVIDERS: dict[str, _ProviderSpec] = {ANTHROPIC.name: ANTHROPIC, OPENAI.name: OPENAI}

DEFAULT_PROVIDER = ANTHROPIC.name

#: Above this, a match is a match. Chosen high on purpose: the cost of accepting a wrong
#: quote is a fabricated task in somebody's week, and the cost of rejecting a right one is
#: a line on a review list a person reads anyway.
DEFAULT_FUZZY_THRESHOLD = 0.92

#: A fuzzy match on a handful of characters means nothing — "the roof" is 0.9 similar to
#: half a transcript. Short quotes must match exactly or not at all.
MIN_FUZZY_CHARS = 24
MIN_FUZZY_WORDS = 4

#: How much of the quote's own words must actually be present in the matched span. Ratio
#: alone can be fooled by two sentences of similar shape; this cannot.
MIN_TOKEN_COVERAGE = 0.8

#: Ceilings on the fuzzy search. They bound the work done to REJECT a quote, which is the
#: expensive direction: a fabricated quote made of ordinary site words matches nothing and
#: has to be compared against everything before that can be said. Raising them buys a
#: slightly better "nearest passage" on the review list and nothing else.
MAX_ANCHORS = 60
MAX_CANDIDATE_STARTS = 400


@dataclass
class AnalysisSettings:
    """Everything the pass needs, resolved once.

    Built either directly (tests, ``selftest``) or with :meth:`from_config`. Blank fields
    are filled from the provider's own defaults in :meth:`__post_init__`, so a caller may
    set only what it means to override.
    """

    provider: str = DEFAULT_PROVIDER
    base_url: str = ""
    #: ``repr=False`` on purpose. ``Config`` and ``GraphClient`` both define redacting
    #: reprs; this is the same rule, at the cost of one keyword. The root log formatter
    #: would catch it in production, but a ``print``, an exception message that interpolated
    #: the settings, or anything reached before ``configure()`` runs would not.
    api_key: str = field(default="", repr=False)
    model_cheap: str = ""
    model_strong: str = ""
    max_tokens_classify: int = 1024
    # Big enough that a long site meeting's extraction is not cut off mid-JSON. A truncated
    # answer is raised, never salvaged, so the only thing a small ceiling would buy is
    # quarantined recordings.
    max_tokens_extract: int = 16000
    effort_strong: str = "high"
    # The strong call reasons before it answers and can take minutes. The service-wide HTTP
    # timeout is sized for Graph calls; using it here would turn good answers into retries.
    timeout_s: int = 600
    max_attempts: int = 4
    max_transcript_chars: int = 600_000
    classify_excerpt_chars: int = 120_000
    fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD
    #: Whether the strong call is also asked the sensitivity question. True whenever
    #: ``GATE_MODE`` is not ``off``, including ``shadow`` — shadow's whole job is to measure
    #: the real classifier, and a shadow run that never asks the question measures nothing
    #: while reporting a number that reads like "this barely touches anything".
    #:
    #: False leaves the analysis pass byte-identical to the day before the gate existed: no
    #: extra field in the schema, no extra words in the system prompt. A gate that can break
    #: a transcription while switched off is not off.
    sensitivity: bool = False
    anthropic_version: str = "2023-06-01"
    extra_headers: Mapping[str, str] = field(default_factory=dict)
    vocabulary: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self.provider = (self.provider or DEFAULT_PROVIDER).strip().lower()
        if self.provider not in PROVIDERS:
            raise AnalysisConfigError(
                f"ANALYSIS_PROVIDER={self.provider!r} is not a provider this service can "
                "speak to — one of: " + ", ".join(sorted(PROVIDERS))
            )
        spec = PROVIDERS[self.provider]
        self.base_url = (self.base_url or spec.base_url).rstrip("/")
        self.model_cheap = self.model_cheap or spec.model_cheap
        self.model_strong = self.model_strong or spec.model_strong
        self.vocabulary = tuple(self.vocabulary)
        problems: list[str] = []
        if not self.api_key:
            problems.append("ANALYSIS_API_KEY is empty — the analysis pass cannot authenticate")
        if not self.model_cheap:
            problems.append(
                f"ANALYSIS_MODEL_CHEAP is not set and the {self.provider} provider has no "
                "default router model — name the model this deployment uses"
            )
        if not self.model_strong:
            problems.append(
                f"ANALYSIS_MODEL_STRONG is not set and the {self.provider} provider has no "
                "default extraction model — name the model this deployment uses"
            )
        if self.fuzzy_threshold < 0.8:
            problems.append(
                f"fuzzy_threshold={self.fuzzy_threshold} is below 0.8, which is low enough "
                "to admit a quote the recording does not contain"
            )
        if problems:
            raise AnalysisConfigError("; ".join(problems))

    @property
    def spec(self) -> _ProviderSpec:
        return PROVIDERS[self.provider]

    @classmethod
    def from_config(cls, config: Any, env: Mapping[str, str] | None = None) -> "AnalysisSettings":
        """Read a :class:`~transcriber.config.Config`, plus the provider override.

        The provider is settled in this order, most explicit first: ``ANALYSIS_PROVIDER``
        if an operator set it; otherwise the host in ``ANALYSIS_BASE_URL``, which is
        unambiguous about who is being called; otherwise the Anthropic default. Nothing is
        inferred from the model id — a model name is a string an operator can typo, and
        guessing a provider from one calls the wrong endpoint with the right key.

        Note that this module's own default is Anthropic (``AnalysisSettings()`` with
        nothing set), while ``Config`` supplies its own ``ANALYSIS_BASE_URL`` default. A
        deployment that wants the default here rather than the one in config sets
        ``ANALYSIS_PROVIDER=anthropic`` and leaves ``ANALYSIS_BASE_URL`` unset.
        """
        source = os.environ if env is None else env
        base_url = str(getattr(config, "analysis_base_url", "") or "").strip()
        provider = (source.get("ANALYSIS_PROVIDER") or "").strip().lower()
        if not provider:
            provider = _provider_from_url(base_url) or DEFAULT_PROVIDER
            if base_url and _provider_from_url(base_url) is None:
                log.warning(
                    "ANALYSIS_BASE_URL=%s names a host this service does not recognise; "
                    "assuming the %s API shape. Set ANALYSIS_PROVIDER to say so explicitly.",
                    base_url, provider,
                )
        # A base url belonging to a different provider is an operator error worth naming,
        # not something to quietly honour: it is how a request goes to one vendor with
        # another vendor's key and comes back 401 for a reason nobody can see.
        if base_url and provider in PROVIDERS and _provider_from_url(base_url) not in (None, provider):
            raise AnalysisConfigError(
                f"ANALYSIS_PROVIDER={provider!r} and ANALYSIS_BASE_URL={base_url!r} disagree "
                "about which API is being called"
            )
        if provider != _provider_from_url(base_url):
            # The default base url of the chosen provider is used unless the operator gave
            # one for that same provider.
            base_url = base_url if _provider_from_url(base_url) == provider else ""
        return cls(
            provider=provider,
            base_url=base_url,
            api_key=str(getattr(config, "analysis_api_key", "") or ""),
            model_cheap=str(getattr(config, "analysis_model_cheap", "") or ""),
            model_strong=str(getattr(config, "analysis_model_strong", "") or ""),
            timeout_s=max(int(getattr(config, "http_timeout_s", 60) or 60), 300),
            max_attempts=max(int(getattr(config, "max_retries", 4) or 4), 2),
            vocabulary=tuple(getattr(config, "vocabulary", ()) or ()),
            # The gate's own mode decides it, in one place. ``shadow`` — what ships — asks
            # the question and withholds nothing; ``off`` does not ask it at all.
            sensitivity=str(getattr(config, "gate_mode", "off") or "off").strip().lower() != "off",
        )


def _provider_from_url(url: str) -> str | None:
    host = (url or "").lower()
    if "anthropic" in host:
        return ANTHROPIC.name
    if "openai" in host or "azure.com" in host:
        return OPENAI.name
    return None


# ------------------------------------------------------------------- quote verification


_ZERO_WIDTH = {0x200B, 0x200C, 0x200D, 0xFEFF, 0x00AD}
_PUNCT_EQUIVALENTS = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "ʼ": "'", "`": "'",
    "“": '"', "”": '"', "„": '"', "«": '"', "»": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-",
    "−": "-", " ": " ",
}
#: Trimmed from the ends of a quote before matching. A model that wraps its quote in
#: speech marks, or ends it with an ellipsis, has not fabricated anything.
_EDGE_PUNCT = " \t\r\n\"'`.,;:!?-–—…()[]{}<>"


@dataclass(frozen=True)
class _Normalised:
    """Normalised text alongside the map back to where each character came from."""

    text: str
    source: tuple[int, ...]

    def span(self, original: str, start: int, end: int) -> str:
        """The original, unnormalised substring that produced ``text[start:end]``."""
        if start >= end or not self.source:
            return ""
        first = self.source[start]
        last = self.source[min(end, len(self.source)) - 1]
        return original[first : last + 1]


def _normalise(text: str) -> _Normalised:
    """Casefold, unify quote and dash shapes, collapse whitespace — and remember origins.

    The index map is the reason this is not two lines of ``re.sub``: once a quote is found
    in normalised space, the *transcript's own words* have to be recoverable so that what
    is written out is the recording's text and not the model's re-typing of it.
    """
    out: list[str] = []
    source: list[int] = []
    pending_space = False
    started = False
    for index, raw in enumerate(text or ""):
        if ord(raw) in _ZERO_WIDTH:
            continue
        char = _PUNCT_EQUIVALENTS.get(raw, raw)
        if char.isspace():
            pending_space = started
            continue
        if pending_space:
            out.append(" ")
            source.append(index)
            pending_space = False
        folded = unicodedata.normalize("NFKC", char).casefold()
        for piece in folded:
            out.append(piece)
            source.append(index)
        started = True
    return _Normalised("".join(out), tuple(source))


def normalise_for_match(text: str) -> str:
    """The comparison form of a string: lowercase, single-spaced, plain quotes and dashes."""
    return _normalise(text).text


_TOKEN_RE = re.compile(r"[0-9a-zà-ɏ]+")


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


def _token_coverage(needle: str, window: str) -> float:
    """How much of the quote's own vocabulary is present in the matched span.

    Similarity ratio alone can be fooled by two passages of the same shape; requiring the
    words themselves cannot. A fabricated quote fails this even when it reads plausibly.
    """
    wanted = _tokens(needle)
    if not wanted:
        return 0.0
    have = set(_tokens(window))
    return sum(1 for token in wanted if token in have) / len(wanted)


@dataclass(frozen=True)
class QuoteCheck:
    """The answer to 'does this quote genuinely appear in the transcript?'

    ``matched_text`` is the transcript's own span, so a caller that trusts this object can
    write out words the recording actually contains. ``ratio`` is recorded whether the
    check passed or failed — a near miss on the review list tells a person far more than a
    bare rejection.
    """

    ok: bool
    method: str          # "exact" | "fuzzy" | "none"
    ratio: float
    matched_text: str = ""
    reason: str = ""
    coverage: float = 0.0

    def __bool__(self) -> bool:
        return self.ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "method": self.method,
            "ratio": round(self.ratio, 4),
            "coverage": round(self.coverage, 4),
            "reason": self.reason,
        }


def verify_quote(
    quote: str,
    transcript: str,
    *,
    threshold: float = DEFAULT_FUZZY_THRESHOLD,
    allow_fuzzy: bool = True,
) -> QuoteCheck:
    """Confirm that ``quote`` genuinely appears in ``transcript``.

    Whitespace and case are normalised first, and so are the shapes of quote marks and
    dashes — a model that types a straight apostrophe where the transcript has a curly one
    has not invented anything. Nothing else is relaxed. In particular no words are dropped,
    reordered or stemmed, because every one of those turns "the roof leaks at unit 4" and
    "the roof leaks at unit 14" into the same string.

    An exact match after that normalisation is the preferred answer. A fuzzy match is
    accepted only above ``threshold``, only when the quote is long enough for similarity to
    mean anything, and only when most of the quote's own words are actually present.

    Returns a :class:`QuoteCheck`; ``ok`` false means the item that carried this quote must
    not reach an output.
    """
    if not (quote or "").strip():
        return QuoteCheck(False, "none", 0.0, reason="the item carried no quote at all")
    if not (transcript or "").strip():
        return QuoteCheck(False, "none", 0.0, reason="there is no transcript to check against")

    hay = _normalise(transcript)
    needle_text = _normalise(quote).text.strip(_EDGE_PUNCT).strip()
    if not needle_text:
        return QuoteCheck(False, "none", 0.0, reason="the quote is punctuation only")

    found = hay.text.find(needle_text)
    if found >= 0:
        return QuoteCheck(
            True,
            "exact",
            1.0,
            matched_text=hay.span(transcript, found, found + len(needle_text)),
            coverage=1.0,
        )

    if not allow_fuzzy:
        return QuoteCheck(False, "none", 0.0, reason="not found in the transcript")

    words = _tokens(needle_text)
    if len(needle_text) < MIN_FUZZY_CHARS or len(words) < MIN_FUZZY_WORDS:
        return QuoteCheck(
            False,
            "none",
            0.0,
            reason=(
                "not found in the transcript, and too short for a fuzzy match to mean "
                f"anything (needs {MIN_FUZZY_CHARS} characters and {MIN_FUZZY_WORDS} words)"
            ),
        )

    ratio, start, end = _best_window(needle_text, hay.text)
    window = hay.text[start:end]
    coverage = _token_coverage(needle_text, window)
    matched = hay.span(transcript, start, end)
    if ratio >= threshold and coverage >= MIN_TOKEN_COVERAGE:
        return QuoteCheck(True, "fuzzy", ratio, matched_text=matched, coverage=coverage)
    if ratio < threshold:
        reason = (
            f"not found in the transcript; the closest passage matches {ratio:.2f}, "
            f"below the {threshold:.2f} this service accepts"
        )
    else:
        reason = (
            f"the closest passage matches {ratio:.2f} but only {coverage:.0%} of the "
            "quoted words appear in it, so the wording is not the transcript's"
        )
    return QuoteCheck(False, "none", ratio, matched_text=matched, reason=reason, coverage=coverage)


def _best_window(needle: str, hay: str) -> tuple[float, int, int]:
    """Best similarity of ``needle`` against any span of ``hay``, with that span's bounds.

    Anchored on the quote's longer words so the scan is bounded on a long transcript: a
    quote that shares no four-letter word with the recording is fabricated, and the
    unanchored fallback scan exists only so that answer is measured rather than assumed.
    """
    n = len(needle)
    if not hay:
        return 0.0, 0, 0
    if n >= len(hay):
        return _ratio(needle, hay), 0, len(hay)

    lengths = sorted({max(8, int(n * 0.85)), n, min(len(hay), int(n * 1.2))})
    step = max(1, n // 8)
    starts = _candidate_starts(needle, hay, n, step)

    best = (0.0, 0, min(len(hay), n))
    for start in starts:
        for length in lengths:
            end = min(len(hay), start + length)
            if end <= start:
                continue
            score = _ratio(needle, hay[start:end])
            if score > best[0]:
                best = (score, start, end)
    if best[0] <= 0.0:
        return best

    # Refine: the coarse scan lands near the passage, not on it.
    lo = max(0, best[1] - step)
    hi = min(len(hay), best[1] + step)
    for start in range(lo, hi + 1):
        for length in lengths:
            end = min(len(hay), start + length)
            if end <= start:
                continue
            score = _ratio(needle, hay[start:end])
            if score > best[0]:
                best = (score, start, end)
    return best


def _candidate_starts(needle: str, hay: str, n: int, step: int) -> list[int]:
    """Where in the transcript this quote could plausibly sit.

    Anchored on the quote's RAREST words, not its longest. A word occurring twice in a
    forty-minute transcript says where the passage is; one occurring three hundred times
    says nothing and costs three hundred comparisons to learn that. The total is capped so
    that rejecting a fabricated quote — the case where every candidate has to be tried and
    none of them fits — stays a bounded cost rather than a slow one.
    """
    words = sorted({w for w in _tokens(needle) if len(w) >= 4}, key=len, reverse=True)[:8]
    by_rarity: list[tuple[int, list[int]]] = []
    for word in words:
        found: list[int] = []
        at = 0
        while len(found) <= MAX_CANDIDATE_STARTS:
            hit = hay.find(word, at)
            if hit < 0:
                break
            found.append(hit)
            at = hit + 1
        if found:
            by_rarity.append((len(found), found))
    by_rarity.sort(key=lambda pair: pair[0])

    anchors: list[int] = []
    for _, found in by_rarity:
        anchors.extend(found)
        if len(anchors) >= MAX_ANCHORS:
            break
    if not anchors:
        # The quote shares no word of any length with the transcript, which is itself the
        # answer. Scan coarsely anyway so the ratio is measured rather than assumed.
        stride = max(step, 1, (len(hay) - n) // MAX_CANDIDATE_STARTS + 1)
        return list(range(0, max(1, len(hay) - n + 1), stride))

    starts: set[int] = set()
    for anchor in sorted(anchors)[:MAX_ANCHORS]:
        for start in range(max(0, anchor - n), anchor + 1, step):
            starts.add(start)
        starts.add(anchor)
    ordered = sorted(starts)
    if len(ordered) > MAX_CANDIDATE_STARTS:
        stride = len(ordered) // MAX_CANDIDATE_STARTS + 1
        ordered = ordered[::stride]
    return ordered


def _ratio(needle: str, window: str) -> float:
    matcher = SequenceMatcher(None, needle, window, autojunk=False)
    if matcher.real_quick_ratio() < 0.8 or matcher.quick_ratio() < 0.8:
        return 0.0
    return matcher.ratio()


# ------------------------------------------------------------------- the safety override


SAFETY_CATEGORIES: tuple[str, ...] = (
    "person",
    "site",
    "number",
    "date",
    "amount",
    "approval",
    "promise",
    "trade",
)


@dataclass(frozen=True)
class SafetyTrigger:
    """One reason a recording is being treated as substantive, with the words that did it."""

    category: str
    term: str
    why: str

    def __str__(self) -> str:
        return f"{self.category} ({self.term!r})"


_I = re.IGNORECASE


def _p(pattern: str, flags: int = _I) -> re.Pattern[str]:
    return re.compile(pattern, flags)


#: The override, as data. Read it as: *any* of these forces ``substantive``. Nothing in this
#: table can make a recording less likely to be read — there is no "trivial" pattern, by
#: construction, so a bad regex here can only ever cost money, never a recording.
SAFETY_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "person",
        _p(r"\b(?:mr|mrs|ms|miss|dr|prof|mnr|mev|meneer|mevrou|juffrou|oom|tannie|"
           r"bhuti|bhut|sisi|sis|mama|baba|mnu|nkosi)\b\.?"),
        "names or addresses a person",
    ),
    (
        "person",
        _p(r"\b(?:tell|told|telling|phone|phoned|call|called|spoke to|speak to|speaking to|"
           r"ask(?:ed)?|meet(?:ing)? with|met with|se vir|gese vir|gesê vir|hy het|sy het|"
           r"hulle het|uthe|wathi|bathi|ndixelele|ngitshele)\b"),
        "refers to something a person said or must be told",
    ),
    (
        "site",
        _p(r"\b(?:site|sites|erf|stand|unit|units|block|section|phase|floor|storey|basement|"
           r"roof|building|house|flat|apartment|complex|estate|court|villas?|mews|manor|"
           r"heights|towers?|scheme|development|premises|perseel|gebou|huis|indawo)\b"),
        "names a place, building or part of one",
    ),
    (
        "number",
        _p(r"(?<![A-Za-z])\d+(?![A-Za-z])"),
        "contains a figure",
    ),
    (
        "number",
        _p(r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|twenty|"
           r"thirty|forty|fifty|sixty|hundred|thousand|million|half|quarter|dozen|"
           r"een|twee|drie|vier|vyf|ses|sewe|agt|nege|tien|honderd|duisend|miljoen|helfte|"
           r"kubili|kuthathu|amashumi|ikhulu|inkulungwane)\b"),
        "states a quantity in words",
    ),
    (
        "date",
        _p(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}\s*[/-]\s*\d{1,2}(?:\s*[/-]\s*\d{2,4})?\b"),
        "carries a date",
    ),
    (
        "date",
        _p(r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
           r"maandag|dinsdag|woensdag|donderdag|vrydag|saterdag|sondag|"
           r"january|february|march|april|june|july|august|september|october|november|"
           r"december|januarie|februarie|maart|mei|junie|julie|augustus|oktober|desember|"
           r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|okt|nov|dec|des)\b"),
        "names a day or a month",
    ),
    (
        "date",
        _p(r"\b(?:today|tonight|tomorrow|yesterday|this week|next week|last week|this month|"
           r"next month|month[- ]end|end of the month|end of the week|deadline|due|overdue|"
           r"asap|urgent(?:ly)?|by then|in the morning|first thing|"
           r"vandag|more|môre|gister|volgende week|verlede week|einde van die maand|"
           r"kusasa|namuhla|ngomso|namhlanje|izolo|kule veki)\b"),
        "places something in time",
    ),
    (
        "amount",
        _p(r"\bR\s?\d|\bZAR\b|\brand(?:s|e)?\b|\bimali\b|\bgeld\b|%|\bpercent\b|\bpersent\b"),
        "mentions money or a proportion",
    ),
    (
        "amount",
        _p(r"\b(?:price|prices|priced|quote|quoted|quotation|invoice[sd]?|cost(?:s|ing)?|"
           r"budget|deposit|retention|payment|pay|paid|betaal|prys|kwotasie|rekening|"
           r"variation|escalation|penalt(?:y|ies)|claim|certificate)\b"),
        "mentions a commercial matter",
    ),
    (
        "approval",
        _p(r"\b(?:approv(?:e|ed|es|al|als)|sign(?:ed)?[\s-]*off|signoff|go[\s-]*ahead|"
           r"green[\s-]*light|authoris(?:e|ed)|authoriz(?:e|ed)|permission|instruct(?:ed|ion|ions)?|"
           r"agree(?:d)?|accept(?:ed)?|confirm(?:ed)?|proceed|carry on|"
           r"goedgekeur|goedkeuring|toestemming|akkoord|gaan voort|maak so|reg so|"
           r"kulungile|sivumile|siyavuma|ndiyavuma|uvumile)\b"),
        "reports an approval, an agreement or an instruction",
    ),
    (
        "promise",
        _p(r"\b(?:i'?ll|we'?ll|he'?ll|she'?ll|they'?ll|i will|we will|he will|she will|"
           r"they will|will send|will do|will get|will call|will check|will sort|will fix|"
           r"will come|will be|going to|gonna|promise[sd]?|commit(?:s|ted|ment|ments)?|"
           r"undertake|undertook|must|need to|needs to|have to|has to|"
           r"ek sal|ons sal|sal ek|sal ons|gaan ek|ek gaan|ons gaan|moet|belowe|"
           r"ndiza|siza|ngizo|sizo|uzo|bazo|ngizokwenza)\b"),
        "somebody said something would be done",
    ),
)

#: Words a capitalised-word scan must not report as a name. Sentence starters, days and
#: months are already covered by their own patterns; language names and the pronoun "I"
#: would otherwise fire on every recording.
_NOT_A_NAME = frozenset(
    """the a an and but or so then now well okay ok yes no yeah yah ja nee ah oh hmm right
    this that these those there here what when where why how who which if because just also
    still even only very much more most some any all one two three first second third next
    last new old good bad big small right left top bottom front back side end start
    i i'm i'll i've we you he she they it its our your their his her them us me my
    monday tuesday wednesday thursday friday saturday sunday january february march april
    may june july august september october november december english afrikaans zulu xhosa
    south africa african sir madam hello hi hey thanks thank please sorry""".split()
)

_SENTENCE_SPLIT = re.compile(r"(?:[.!?…]+\s+|\n+)")
_CAPITALISED = re.compile(r"\b[A-Z][a-zA-Z'’-]{2,}(?:\s+[A-Z][a-zA-Z'’-]{2,})*\b")
_SPEAKER_LABEL = re.compile(r"^\s*([A-Z][\w .'’-]{1,40}?)\s*:", re.MULTILINE)


def _light_normalise(text: str) -> str:
    """Collapse whitespace and unify punctuation shapes, keeping case for the name scan."""
    out = []
    for char in text or "":
        if ord(char) in _ZERO_WIDTH:
            continue
        out.append(_PUNCT_EQUIVALENTS.get(char, char))
    return re.sub(r"[ \t ]+", " ", "".join(out))


def _proper_noun_triggers(text: str, limit: int = 8) -> list[SafetyTrigger]:
    """Capitalised words that are not sentence starters: a person or a place, we cannot tell.

    Deliberately reported as ``person`` rather than guessed at, because the override does
    not need to know which: both force ``substantive``. "Beach Court" is the case this
    exists for.
    """
    found: list[SafetyTrigger] = []
    seen: set[str] = set()

    for match in _SPEAKER_LABEL.finditer(text):
        name = match.group(1).strip()
        if name.lower() in _NOT_A_NAME or len(name) < 2:
            continue
        if name.lower() not in seen:
            seen.add(name.lower())
            found.append(SafetyTrigger("person", name, "the transcript labels a speaker by name"))
        if len(found) >= limit:
            return found

    for sentence in _SENTENCE_SPLIT.split(text):
        stripped = sentence.strip()
        if not stripped:
            continue
        for match in _CAPITALISED.finditer(stripped):
            # The first word of a sentence is capitalised because it is the first word.
            if match.start() == 0 and " " not in match.group(0):
                continue
            phrase = match.group(0).strip()
            if phrase.lower() in _NOT_A_NAME:
                continue
            if all(word.lower() in _NOT_A_NAME for word in phrase.split()):
                continue
            key = phrase.lower()
            if key in seen:
                continue
            seen.add(key)
            found.append(
                SafetyTrigger("person", phrase, "a capitalised name — a person or a place")
            )
            if len(found) >= limit:
                return found
    return found


def route_precheck(
    text: str,
    extra_terms: Sequence[str] = (),
    *,
    include_vocabulary: bool = True,
) -> tuple[SafetyTrigger, ...]:
    """The safety override, mechanically.

    Returns every reason this recording must be treated as substantive. An empty tuple
    means the pre-check found nothing — which is permission for the router model's answer
    to stand, and never an instruction to skip anything.

    ``extra_terms`` is the deployment's own vocabulary (site names, contractors) from
    config: a recording naming one of this consultancy's jobs is substantive whether or not
    a general-purpose model recognises the name.
    """
    if not (text or "").strip():
        return ()
    haystack = _light_normalise(text)
    triggers: list[SafetyTrigger] = []
    seen: set[tuple[str, str]] = set()

    def add(category: str, term: str, why: str) -> None:
        # The term is written into the actions file as the reason a recording was read, so
        # it goes through the same redaction as everything else that leaves this module.
        term = strip_emails(term)
        key = (category, term.lower())
        if key in seen:
            return
        seen.add(key)
        triggers.append(SafetyTrigger(category, term, why))

    for category, pattern, why in SAFETY_PATTERNS:
        match = pattern.search(haystack)
        if match:
            add(category, match.group(0).strip(), why)

    for trigger in _proper_noun_triggers(haystack):
        add(trigger.category, trigger.term, trigger.why)

    terms: list[str] = [t for t in extra_terms if t and len(t) > 2]
    if include_vocabulary:
        terms.extend(prompts.CONSTRUCTION_VOCABULARY)
    lowered = haystack.lower()
    for term in terms:
        needle = term.lower().strip()
        if not needle:
            continue
        if re.search(r"(?<![0-9a-z])" + re.escape(needle) + r"(?![0-9a-z])", lowered):
            add("trade", term, "names something specific to this trade or these jobs")
            if sum(1 for t in triggers if t.category == "trade") >= 6:
                break
    return tuple(triggers)


# --------------------------------------------------------------------------- the results


@dataclass(frozen=True)
class Spend:
    """What one model call used, as the provider reported it.

    TOKENS, NOT MONEY, and that is the whole point of this class. A token count is a fact
    the API told us about a request that happened. A rand or dollar figure is that fact
    multiplied by a price list which changes without telling us, and which nothing here can
    verify. Storing the money would mean a number in the record that quietly stops being
    true; storing the tokens means the arithmetic can be redone whenever the prices move.
    The morning email does that multiplication and names the price list it used.

    ``cache_write`` costs more than ordinary input and ``cache_read`` costs much less, so
    they are kept apart rather than folded into ``input``: a summary that added them
    together would report the same number whether the cache was working or not.
    """

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "input": self.input_tokens,
            "output": self.output_tokens,
            "cache_read": self.cache_read_tokens,
            "cache_write": self.cache_write_tokens,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Spend":
        return cls(
            model=str(raw.get("model") or ""),
            input_tokens=_whole(raw.get("input")),
            output_tokens=_whole(raw.get("output")),
            cache_read_tokens=_whole(raw.get("cache_read")),
            cache_write_tokens=_whole(raw.get("cache_write")),
        )


def _whole(value: Any) -> int:
    """A token count, or zero. Never raises: a usage block is telemetry, and a provider
    that starts sending a float or a string there must not fail a recording."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def spend_of(model: str, usage: Mapping[str, Any] | None) -> Spend:
    """One provider's usage block, read into the same shape.

    Anthropic reports ``input_tokens`` / ``output_tokens`` with cache counts beside them;
    OpenAI reports ``prompt_tokens`` / ``completion_tokens`` and hides the cached share
    inside ``prompt_tokens_details``. Both are accepted, and anything unrecognised reads as
    zero rather than as an error - a meter that could stop a transcription would be worse
    than no meter.

    OpenAI's ``prompt_tokens`` INCLUDES its cached tokens, where Anthropic's does not, so
    the cached share is subtracted out here. Without that the same recording would look
    more expensive on one provider than the other for no reason but the reporting.
    """
    raw = dict(usage or {})
    if "input_tokens" in raw or "output_tokens" in raw:
        return Spend(
            model=model,
            input_tokens=_whole(raw.get("input_tokens")),
            output_tokens=_whole(raw.get("output_tokens")),
            cache_read_tokens=_whole(raw.get("cache_read_input_tokens")),
            cache_write_tokens=_whole(raw.get("cache_creation_input_tokens")),
        )
    prompt = _whole(raw.get("prompt_tokens"))
    cached = _whole((raw.get("prompt_tokens_details") or {}).get("cached_tokens")
                    if isinstance(raw.get("prompt_tokens_details"), Mapping) else 0)
    return Spend(
        model=model,
        input_tokens=max(prompt - cached, 0),
        output_tokens=_whole(raw.get("completion_tokens")),
        cache_read_tokens=cached,
    )


@dataclass(frozen=True)
class Routing:
    """Where a recording was sent, why, and who said so."""

    label: str                                   # "substantive" | "trivial"
    forced: bool                                 # the pre-check overrode, or would have
    triggers: tuple[SafetyTrigger, ...] = ()
    model_label: str = ""                        # what the router model actually said
    model_reason: str = ""
    one_line: str = ""
    languages: tuple[str, ...] = ()
    escalated: bool = False                      # the model said trivial and was overridden
    model: str = ""
    notes: tuple[str, ...] = ()
    #: What the router call used. ``None`` when the router was never reached, which is a
    #: different fact from "used nothing" and is why this is not a zeroed Spend.
    spend: "Spend | None" = None

    @property
    def substantive(self) -> bool:
        return self.label == "substantive"

    @property
    def categories(self) -> tuple[str, ...]:
        seen: list[str] = []
        for trigger in self.triggers:
            if trigger.category not in seen:
                seen.append(trigger.category)
        return tuple(seen)

    def why(self) -> str:
        """Plain words for the actions file: why this recording was read, or was not.

        This sentence goes into the record and stays there, so it may not assert something
        that did not happen. Two ways it used to:

        A router OUTAGE sets ``model_label="unavailable"`` and escalates — the right
        behaviour, since a router that cannot be reached must never mean a recording is
        skipped — and this then reported it as "the router model called this trivial",
        which is a judgement no model made. And with no triggers to list, the clause
        collapsed mid-sentence: *"the safety check disagreed because it , so it was read in
        full"*, written into a file a person reads.
        """
        if self.model_label == "unavailable":
            return (
                "the router model could not be reached, so this was read in full rather "
                "than skipped"
            )
        if self.escalated:
            reasons = _join(t.why for t in self.triggers)
            if not reasons:
                # Escalated with nothing to name — the transcript was too long to send
                # whole, so it was read in full on those grounds rather than the router's.
                return "the router model called this trivial; it was read in full anyway"
            return (
                "the router model called this trivial; the safety check disagreed because "
                "it " + reasons + ", so it was read in full"
            )
        if self.substantive and self.triggers:
            return "read in full — it " + _join(t.why for t in self.triggers)
        if self.substantive:
            return "read in full — the router model judged it substantive"
        return "not read in full — nothing in it names a person, place, figure, date, amount, approval or promise"

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "forced": self.forced,
            "escalated": self.escalated,
            "model_label": self.model_label,
            "model": self.model,
            "categories": list(self.categories),
            "triggers": [{"category": t.category, "term": t.term} for t in self.triggers],
            "one_line": self.one_line,
            "languages": list(self.languages),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class Participant:
    """Somebody heard in, or spoken about in, the recording. Never an address."""

    name_or_role: str
    quote: str
    quote_check: QuoteCheck


@dataclass(frozen=True)
class Proposal:
    """One verified item, ready for a person to confirm or reject.

    ``category`` is the architecture's own list — decisions, commitments, money, materials,
    defects, safety, programme, questions — and ``item`` is the shared record type the rest
    of the service passes around. Both exist because the actions file groups by the first
    and the ledger stores the second.
    """

    category: str
    item: ExtractedItem
    quote_check: QuoteCheck

    @property
    def kind(self) -> str:
        return self.item.kind

    def to_dict(self) -> dict[str, Any]:
        out = self.item.to_dict()
        out["category"] = self.category
        out["quote_match"] = self.quote_check.to_dict()
        return out


@dataclass(frozen=True)
class ReviewItem:
    """An item that did not survive quote verification. It never reaches an output.

    It is not discarded either: a model producing quotes that are not in the transcript is
    itself a fault a person must see, and the only way to see it is for the rejection to be
    written down.
    """

    category: str
    summary: str
    offered_quote: str
    reason: str
    ratio: float = 0.0
    nearest: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "summary": self.summary,
            "offered_quote": self.offered_quote,
            "reason": self.reason,
            "ratio": round(self.ratio, 4),
            "nearest_passage_in_transcript": self.nearest,
        }


@dataclass(frozen=True)
class UnclearPassage:
    """A passage the model would not commit to. Kept as-is, never smoothed."""

    passage: str
    why: str
    in_transcript: bool


@dataclass
class Extraction:
    """Everything the AI pass produced for one recording.

    ``proposals`` are verified and may be written out. ``review`` may not — it is the list
    a person is shown so that a model's misses are visible rather than absent.
    """

    routing: Routing
    summary: str = ""
    languages: tuple[str, ...] = ()
    participants: tuple[Participant, ...] = ()
    site: str = ""
    site_quote: str = ""
    proposals: tuple[Proposal, ...] = ()
    review: tuple[ReviewItem, ...] = ()
    unclear: tuple[UnclearPassage, ...] = ()
    notes: tuple[str, ...] = ()
    models_used: tuple[str, ...] = ()
    usage: dict[str, Any] = field(default_factory=dict)
    #: One entry per model call this recording actually made - the router and, when it was
    #: read in full, the reader. A trivial recording has one entry, not none.
    spend: tuple[Spend, ...] = ()
    elapsed_s: float = 0.0
    analysed_at: str = ""
    trivial: bool = False
    redacted: bool = False
    #: The sensitivity gate's half of the same answer, exactly as the model returned it, or
    #: ``None`` when the question was not asked or was not answered.
    #:
    #: The distinction between ``None`` and ``()`` is load-bearing and is not a nicety:
    #: ``()`` means "asked, and there is nothing sensitive in this recording", while ``None``
    #: means "nobody asked" — the gate is off, the recording was trivial and never reached
    #: the strong model, or the model dropped the field. :meth:`transcriber.pipeline.Pipeline._assess`
    #: reads them differently, and collapsing the two would let a recording nothing ever
    #: classified count in the measurement as one the classifier cleared.
    #:
    #: ``repr=False`` because each entry carries a verbatim quote of the passage, which is
    #: the text the whole gate exists to keep out of a log line.
    sensitive_passages: tuple[Mapping[str, Any], ...] | None = field(default=None, repr=False)

    @property
    def items(self) -> tuple[ExtractedItem, ...]:
        """The verified items, in the shared record type, for the ledger and the outputs."""
        return tuple(p.item for p in self.proposals)

    @property
    def commitments(self) -> tuple[Proposal, ...]:
        return self.for_category("commitments")

    @property
    def questions(self) -> tuple[Proposal, ...]:
        return self.for_category("open_questions")

    def for_category(self, category: str) -> tuple[Proposal, ...]:
        return tuple(p for p in self.proposals if p.category == category)

    def by_category(self) -> dict[str, tuple[Proposal, ...]]:
        """Grouped in the order a person reads them, empty categories omitted."""
        grouped: dict[str, tuple[Proposal, ...]] = {}
        for category in prompts.EXTRACTION_CATEGORIES:
            found = self.for_category(category)
            if found:
                grouped[category] = found
        return grouped

    @property
    def needs_a_person(self) -> bool:
        """True when something happened that a person must look at, beyond the proposals."""
        return bool(self.review or self.notes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "analysed_at": self.analysed_at,
            "routing": self.routing.to_dict(),
            "summary": self.summary,
            "languages": list(self.languages),
            "participants": [
                {"name_or_role": p.name_or_role, "quote": p.quote} for p in self.participants
            ],
            "site": self.site,
            "site_quote": self.site_quote,
            "proposals": [p.to_dict() for p in self.proposals],
            "review": [r.to_dict() for r in self.review],
            "unclear": [
                {"passage": u.passage, "why": u.why, "in_transcript": u.in_transcript}
                for u in self.unclear
            ],
            "notes": list(self.notes),
            "models_used": list(self.models_used),
            "usage": dict(self.usage),
            "elapsed_s": round(self.elapsed_s, 3),
            "trivial": self.trivial,
            "redacted": self.redacted,
            # A count, never the entries. This dict is written into the ledger row and
            # printed by ``transcriber status``, and every entry carries a verbatim quote of
            # a passage that may be about to be withheld. ``None`` says the question was
            # never asked, which is a different fact from "nothing was found".
            "sensitive_passages_returned": (
                None if self.sensitive_passages is None else len(self.sensitive_passages)
            ),
            "observed_by": "agent",
        }


def _join(parts) -> str:
    items = [p for p in parts if p]
    if not items:
        return ""
    seen: list[str] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    if len(seen) == 1:
        return seen[0]
    return ", ".join(seen[:-1]) + " and " + seen[-1]


#: Which category becomes which ledger item kind. Kinds are the shared five in
#: :data:`transcriber.models.ITEM_KINDS`; the finer category rides on the proposal.
_CATEGORY_KIND = {
    "decisions": "observation",
    "commitments": "commitment",
    "money": "observation",
    "materials": "observation",
    "defects": "risk",
    "safety": "risk",
    "programme": "risk",
    "open_questions": "question",
    "follow_ups": "followup",
}
# Checked at import rather than asserted: a category the schema returns and this table has
# no kind for would silently drop every item in it, and `python -O` strips an assert.
if set(_CATEGORY_KIND) != set(prompts.EXTRACTION_CATEGORIES):
    raise RuntimeError(
        "the extraction schema and the category-to-kind table disagree: "
        f"schema-only={sorted(set(prompts.EXTRACTION_CATEGORIES) - set(_CATEGORY_KIND))} "
        f"table-only={sorted(set(_CATEGORY_KIND) - set(prompts.EXTRACTION_CATEGORIES))}"
    )
if not set(_CATEGORY_KIND.values()) <= set(ITEM_KINDS):
    raise RuntimeError(
        "the category-to-kind table names kinds the shared record type does not know: "
        f"{sorted(set(_CATEGORY_KIND.values()) - set(ITEM_KINDS))}"
    )


# ------------------------------------------------------------------------- the extractor


#: Signature of the injectable transport. Takes (url, headers, body) and returns the
#: decoded JSON response. ``selftest`` and the test suite pass one of these so the whole
#: The gate's field in the extraction answer. Named once, because it is the single
#: required field whose absence degrades the pass instead of failing it.
_SENSITIVITY_FIELD = "sensitive_passages"

#: pass runs offline with no credential and no network.
Caller = Callable[[str, Mapping[str, str], Mapping[str, Any]], Mapping[str, Any]]


class Extractor:
    """The two-tier pass, over ``urllib``, with the verification built in."""

    def __init__(
        self,
        settings: AnalysisSettings,
        *,
        http: HttpClient | None = None,
        caller: Caller | None = None,
    ) -> None:
        self.settings = settings
        self._caller = caller
        self._http = http or HttpClient(
            timeout_s=settings.timeout_s,
            policy=RetryPolicy(max_attempts=settings.max_attempts),
            secrets=(settings.api_key,),
        )

    # -- public ------------------------------------------------------------------

    @classmethod
    def from_config(cls, config: Any, **kwargs: Any) -> "Extractor":
        return cls(AnalysisSettings.from_config(config), **kwargs)

    def classify(self, transcript: Transcript | str, hints: Hints | None = None) -> Routing:
        """Route one recording. Called for every recording, without exception.

        The mechanical pre-check runs on the **whole** transcript first, and its answer can
        only escalate. The model's answer is then allowed to promote, never to demote: a
        router that says 'trivial' about a recording naming a site and an amount is
        overruled and the fact is recorded.
        """
        text = _text_of(transcript)
        triggers = route_precheck(text, self.settings.vocabulary)
        notes: list[str] = []

        excerpt, excerpted = self._excerpt_for_routing(text)
        if excerpted:
            notes.append(
                f"the transcript is {len(text):,} characters, so the router model was shown "
                "only its beginning and end; it was treated as substantive regardless"
            )

        model_label = ""
        model_reason = ""
        one_line = ""
        languages: tuple[str, ...] = ()
        try:
            payload = self._call(
                model=self.settings.model_cheap,
                system=prompts.CLASSIFIER_SYSTEM,
                user=prompts.build_classifier_user(
                    excerpt, self._context(hints), excerpted=excerpted
                ),
                schema=prompts.CLASSIFIER_SCHEMA,
                schema_name=prompts.CLASSIFIER_SCHEMA_NAME,
                max_tokens=self.settings.max_tokens_classify,
                effort="",
            )
        except AnalysisError as exc:
            # The router being down must never mean a recording is skipped. It means the
            # recording is read in full and the failure is written where a person sees it.
            notes.append(f"the router model could not be reached ({exc}); read in full instead")
            log.warning("router model unavailable, escalating to a full read: %s", exc)
            return Routing(
                label="substantive",
                forced=True,
                triggers=triggers,
                model_label="unavailable",
                model_reason=str(exc),
                escalated=True,
                model=self.settings.model_cheap,
                notes=tuple(notes),
                spend=None,   # nothing was spent: the call did not land
            )

        body = payload.get("data") or {}
        # The router's usage used to be dropped on the floor here. It is the cheaper of the
        # two calls but it runs on EVERY recording, including the trivial ones the reader
        # never sees, so a meter without it undercounts exactly the recordings that cost
        # least to analyse and are most numerous.
        router_spend = spend_of(self.settings.model_cheap, payload.get("usage"))
        model_label = str(body.get("label") or "").strip().lower()
        model_reason = strip_emails(str(body.get("reason") or "").strip())
        one_line = strip_emails(str(body.get("one_line") or "").strip())
        languages = tuple(str(x) for x in (body.get("languages") or ()) if str(x).strip())

        mentions = body.get("mentions") or {}
        for category, flagged in mentions.items():
            if flagged and not any(t.category == category for t in triggers):
                triggers = triggers + (
                    SafetyTrigger(str(category), "", "the router model saw it mentioned"),
                )

        forced = bool(triggers) or excerpted or model_label not in ("substantive", "trivial", "unclear")
        label = "substantive" if forced or model_label != "trivial" else "trivial"
        escalated = forced and model_label == "trivial"
        if escalated:
            log.info(
                "router said trivial; the safety check overrode it (%s)",
                ", ".join(str(t) for t in triggers[:4]),
            )
        if model_label not in ("substantive", "trivial", "unclear"):
            notes.append(
                f"the router model answered {model_label!r}, which is not one of its three "
                "labels; the recording was read in full"
            )
        return Routing(
            label=label,
            forced=forced,
            triggers=triggers,
            model_label=model_label,
            model_reason=model_reason,
            one_line=one_line,
            languages=languages,
            escalated=escalated,
            model=self.settings.model_cheap,
            notes=tuple(notes),
            spend=router_spend,
        )

    def extract(
        self,
        transcript: Transcript | str,
        hints: Hints | None = None,
        *,
        source_item_id: str | None = None,
    ) -> Extraction:
        """Route, then read, then verify every quote. The whole pass, for one recording."""
        started = time.monotonic()
        text = _text_of(transcript)
        if not text.strip():
            raise AnalysisConfigError(
                "the AI pass was given an empty transcript — an empty recording is the "
                "plausibility check's business (SKIPPED_EMPTY), not something to analyse"
            )
        if len(text) > self.settings.max_transcript_chars:
            raise TranscriptTooLarge(
                f"the transcript is {len(text):,} characters, more than the "
                f"{self.settings.max_transcript_chars:,} one analysis request may carry. "
                "It has not been analysed and has not been marked done; a person must split "
                "it or raise the limit deliberately."
            )

        routing = self.classify(text, hints)
        notes = list(routing.notes)
        models_used = [self.settings.model_cheap]

        if not routing.substantive:
            return Extraction(
                routing=routing,
                summary=routing.one_line or "Nothing was said that needs to be on the record.",
                languages=routing.languages,
                notes=tuple(notes),
                models_used=tuple(models_used),
                elapsed_s=time.monotonic() - started,
                analysed_at=utc_now_iso(),
                trivial=True,
                # A trivial recording still cost a router call. One entry, not none.
                spend=(routing.spend,) if routing.spend else (),
            )

        routing_note = _join(f"{t.category}" for t in routing.triggers)
        payload = self._call(
            model=self.settings.model_strong,
            # One call, two questions. The sensitivity half is asked here or it is asked
            # nowhere: the gate's mechanical rules find an explicit "don't write that down"
            # and a bare identifier, and nothing else. A staff matter, a person's health,
            # KBC's attorney strategy and its own cost-against-charge are visible only to
            # whatever reads the transcript, which is this call.
            system=prompts.extraction_system(sensitivity=self.settings.sensitivity),
            user=prompts.build_extraction_user(text, self._context(hints), routing_note),
            schema=prompts.extraction_schema(sensitivity=self.settings.sensitivity),
            schema_name=prompts.EXTRACTION_SCHEMA_NAME,
            max_tokens=self.settings.max_tokens_extract,
            effort=self.settings.effort_strong,
        )
        models_used.append(self.settings.model_strong)
        data = payload.get("data") or {}
        usage = dict(payload.get("usage") or {})

        extraction = self._assemble(text, data, routing, source_item_id)
        extraction.notes = tuple(notes) + extraction.notes
        extraction.models_used = tuple(models_used)
        extraction.usage = usage
        reader_spend = spend_of(self.settings.model_strong, usage)
        extraction.spend = tuple(x for x in (routing.spend, reader_spend) if x)
        extraction.elapsed_s = time.monotonic() - started
        extraction.analysed_at = utc_now_iso()
        if extraction.review:
            log.warning(
                "%d extracted item(s) were rejected because their quotes are not in the "
                "transcript; they are on the review list, not in the actions file",
                len(extraction.review),
            )
        return extraction

    # -- assembly ----------------------------------------------------------------

    def _assemble(
        self,
        text: str,
        data: Mapping[str, Any],
        routing: Routing,
        source_item_id: str | None,
    ) -> Extraction:
        proposals: list[Proposal] = []
        review: list[ReviewItem] = []
        notes: list[str] = []
        redacted = False

        summary = strip_dictated_emails(strip_emails(str(data.get("summary_en") or "").strip()))
        if summary != str(data.get("summary_en") or "").strip():
            redacted = True
            notes.append("an email address was removed from the summary")
        languages = tuple(
            str(x).strip() for x in (data.get("languages") or ()) if str(x).strip()
        ) or routing.languages

        participants: list[Participant] = []
        for entry in _as_list(data.get("participants")):
            name = strip_dictated_emails(strip_emails(str(entry.get("name_or_role") or "").strip()))
            quote = str(entry.get("quote") or "").strip()
            if not name:
                continue
            if contains_email(str(entry.get("name_or_role") or "")):
                redacted = True
                notes.append("an email address was removed from a participant's name")
            check = verify_quote(quote, text, threshold=self.settings.fuzzy_threshold)
            if check.ok:
                participants.append(Participant(name, strip_emails(check.matched_text), check))
            else:
                review.append(
                    ReviewItem(
                        "participants",
                        f"names {name} as involved",
                        strip_emails(quote),
                        check.reason,
                        check.ratio,
                        strip_emails(check.matched_text),
                    )
                )

        site_block = data.get("site") or {}
        site_name = strip_dictated_emails(strip_emails(str(site_block.get("name") or "").strip()))
        site_quote = ""
        if site_name:
            check = verify_quote(
                str(site_block.get("quote") or ""), text, threshold=self.settings.fuzzy_threshold
            )
            if check.ok:
                site_quote = strip_emails(check.matched_text)
            else:
                review.append(
                    ReviewItem(
                        "site",
                        f"says this recording is about {site_name}",
                        strip_emails(str(site_block.get("quote") or "")),
                        check.reason,
                        check.ratio,
                        strip_emails(check.matched_text),
                    )
                )
                # The name stays — it is still what the model read — but with nothing
                # behind it, so a reader can see there is no quote supporting it.
                notes.append(
                    f"the site was read as {site_name!r} but the words offered as evidence "
                    "are not in the transcript"
                )

        for category in prompts.EXTRACTION_CATEGORIES:
            for entry in _as_list(data.get(category)):
                proposal, rejected, was_redacted = self._one_item(
                    category, entry, text, site_name, source_item_id
                )
                redacted = redacted or was_redacted
                if proposal is not None:
                    proposals.append(proposal)
                if rejected is not None:
                    review.append(rejected)

        unclear: list[UnclearPassage] = []
        for entry in _as_list(data.get("unclear_passages")):
            passage = strip_emails(str(entry.get("passage") or "").strip())
            if not passage:
                continue
            check = verify_quote(passage, text, threshold=self.settings.fuzzy_threshold)
            unclear.append(
                UnclearPassage(passage, strip_emails(str(entry.get("why") or "").strip()), check.ok)
            )

        if not proposals and not review:
            notes.append(
                "this recording was read in full and nothing in it could be attached to a "
                "quote — the transcript is the record of what was said"
            )

        return Extraction(
            routing=routing,
            summary=summary,
            languages=languages,
            participants=tuple(participants),
            site=site_name,
            site_quote=site_quote,
            proposals=tuple(proposals),
            review=tuple(review),
            unclear=tuple(unclear),
            notes=tuple(notes),
            redacted=redacted,
            sensitive_passages=self._sensitive_passages(data),
        )

    def _sensitive_passages(self, data: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...] | None:
        """The gate's half of the answer, or ``None`` when it was not asked or not answered.

        Deliberately not verified, filtered or scored here. Every judgement about a passage —
        whether its category is one of the six held ones, whether its quote can be located in
        the transcript, whether its confidence clears the bar — belongs to
        :mod:`transcriber.sensitivity`, which owns the held band and is the only module that
        may widen or narrow it. This function's whole job is to carry the answer across
        without losing the difference between "no" and "not asked".
        """
        if not self.settings.sensitivity:
            return None
        raw = data.get("sensitive_passages")
        if not isinstance(raw, (list, tuple)):
            # The schema puts it in ``required``, so a missing field means the provider
            # stopped honouring the constraint. The gate then runs on its mechanical rules
            # and says so in its own notes rather than reading silence as "nothing here".
            return None
        return tuple(entry for entry in raw if isinstance(entry, Mapping))

    def _one_item(
        self,
        category: str,
        entry: Mapping[str, Any],
        text: str,
        default_site: str,
        source_item_id: str | None,
    ) -> tuple[Proposal | None, ReviewItem | None, bool]:
        """Verify one model-produced item and turn it into a proposal, or into a rejection."""
        quote = str(entry.get("quote") or "").strip()
        if category == "commitments":
            owner = str(entry.get("owner") or "").strip()
            what = str(entry.get("what") or "").strip()
            due = str(entry.get("by_when") or "").strip()
            summary = _commitment_sentence(owner, what)
        else:
            owner = ""
            due = ""
            summary = str(entry.get("summary") or "").strip()

        if not summary:
            return (
                None,
                ReviewItem(category, "(the model gave no description)", strip_emails(quote),
                           "the item had no description, so there is nothing to confirm"),
                False,
            )

        check = verify_quote(quote, text, threshold=self.settings.fuzzy_threshold)
        if not check.ok:
            return (
                None,
                ReviewItem(
                    category,
                    strip_emails(summary),
                    strip_emails(quote),
                    check.reason,
                    check.ratio,
                    strip_emails(check.matched_text),
                ),
                False,
            )

        speaker = str(entry.get("speaker") or "").strip() or None
        site = str(entry.get("site") or "").strip() or (default_site or None)
        confidence = _as_float(entry.get("confidence"))
        raw = " ".join(x for x in (summary, quote, speaker or "", site or "") if x)
        try:
            item = ExtractedItem(
                kind=_CATEGORY_KIND[category],
                text=summary,
                # The transcript's own span, not the model's retyping of it.
                quote=check.matched_text or quote,
                speaker=speaker,
                site=site,
                due=due or None,
                confidence=confidence,
                quote_verified=True,
                source_item_id=source_item_id,
            )
        except ValueError as exc:
            # ExtractedItem enforces the two house rules itself. A refusal here is a fault
            # worth showing a person, never something to work around.
            return (
                None,
                ReviewItem(category, strip_emails(summary), strip_emails(quote),
                           f"the item could not be recorded: {exc}"),
                False,
            )
        return Proposal(category, item, check), None, contains_email(raw)

    # -- transport ---------------------------------------------------------------

    def _context(self, hints: Hints | None) -> str:
        if hints is None:
            return prompts.context_block(vocabulary=self.settings.vocabulary)
        return prompts.context_block(
            source_name=hints.source_name or "",
            recorded_at=hints.recorded_at or "",
            duration_s=hints.duration_s,
            counterparty=hints.counterparty or "",
            languages=hints.languages or ((hints.language,) if hints.language else ()),
            vocabulary=hints.vocabulary or self.settings.vocabulary,
        )

    def _excerpt_for_routing(self, text: str) -> tuple[str, bool]:
        """Head and tail, with the gap marked, when a transcript is too long to route whole.

        Never a silent truncation: the caller escalates any excerpted recording to a full
        read, so the excerpt can only ever cost a model call.
        """
        cap = self.settings.classify_excerpt_chars
        if len(text) <= cap:
            return text, False
        half = cap // 2
        omitted = len(text) - (half * 2)
        return (
            text[:half]
            + f"\n\n[... {omitted:,} characters of this transcript are not shown ...]\n\n"
            + text[-half:],
            True,
        )

    def _call(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: Mapping[str, Any],
        schema_name: str,
        max_tokens: int,
        effort: str,
    ) -> dict[str, Any]:
        url, headers, body = self._request(
            model=model,
            system=system,
            user=user,
            schema=schema,
            schema_name=schema_name,
            max_tokens=max_tokens,
            effort=effort,
        )
        if self._caller is not None:
            payload = self._caller(url, headers, body)
        else:
            payload = self._post(url, headers, body)
        return self._read(payload, model=model, schema=schema)

    def _request(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: Mapping[str, Any],
        schema_name: str,
        max_tokens: int,
        effort: str,
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        settings = self.settings
        extra = {str(k): str(v) for k, v in (settings.extra_headers or {}).items()}
        if settings.provider == ANTHROPIC.name:
            url = f"{settings.base_url}/v1/messages"
            headers = {
                "x-api-key": settings.api_key,
                "anthropic-version": settings.anthropic_version,
                "content-type": "application/json",
            }
            headers.update(extra)
            output_config: dict[str, Any] = {
                "format": {"type": "json_schema", "schema": dict(schema)}
            }
            # Effort is only accepted by the models that implement it; the router tier
            # rejects it outright, which is why it is never sent on the cheap call.
            if effort:
                output_config["effort"] = effort
            body: dict[str, Any] = {
                "model": model,
                "max_tokens": max_tokens,
                # One cache breakpoint on the system prompt: it is byte-identical for every
                # recording, and the transcript that varies comes after it.
                "system": [
                    {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
                ],
                "messages": [{"role": "user", "content": [{"type": "text", "text": user}]}],
                "output_config": output_config,
            }
            return url, headers, body

        url = f"{settings.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {settings.api_key}",
            "content-type": "application/json",
        }
        headers.update(extra)
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": dict(schema)},
            },
        }
        return url, headers, body

    def _post(
        self, url: str, headers: Mapping[str, str], body: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        try:
            response = self._http.post(url, headers=dict(headers), json_body=dict(body), expected=(200,))
            return response.json()
        except EngineAuthError as exc:
            raise AnalysisAuthError(f"the analysis provider rejected our credential: {exc}") from exc
        except EngineHTTPError as exc:
            raise AnalysisHTTPError(
                f"the analysis provider answered {exc.status}: {exc}",
                status=exc.status,
                body=getattr(exc, "body", ""),
            ) from exc
        except EngineTransportError as exc:
            raise AnalysisTransportError(f"the analysis provider could not be reached: {exc}") from exc
        except EngineResponseError as exc:
            raise AnalysisResponseError(f"the analysis provider's answer was not JSON: {exc}") from exc
        except EngineError as exc:
            raise AnalysisError(f"the analysis call failed: {exc}") from exc

    def _read(
        self, payload: Mapping[str, Any], *, model: str, schema: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Pull the constrained JSON out of a provider response, or fail loudly."""
        if not isinstance(payload, Mapping):
            raise AnalysisResponseError(f"{model} answered with {type(payload).__name__}, not an object")

        if self.settings.provider == ANTHROPIC.name:
            stop = str(payload.get("stop_reason") or "")
            if stop == "refusal":
                details = payload.get("stop_details") or {}
                raise AnalysisResponseError(
                    f"{model} declined to answer (category "
                    f"{details.get('category') or 'unstated'}). The recording has not been "
                    "analysed and has not been marked done."
                )
            if stop == "max_tokens":
                raise AnalysisResponseError(
                    f"{model} ran out of output tokens before finishing the JSON, so the "
                    "answer is incomplete. Nothing partial is kept."
                )
            blocks = payload.get("content") or []
            text = "".join(
                str(block.get("text") or "")
                for block in blocks
                if isinstance(block, Mapping) and block.get("type") == "text"
            )
            usage = dict(payload.get("usage") or {})
        else:
            choices = payload.get("choices") or []
            if not choices:
                raise AnalysisResponseError(f"{model} answered with no choices at all")
            choice = choices[0] or {}
            finish = str(choice.get("finish_reason") or "")
            if finish == "length":
                raise AnalysisResponseError(
                    f"{model} ran out of output tokens before finishing the JSON, so the "
                    "answer is incomplete. Nothing partial is kept."
                )
            if finish == "content_filter":
                raise AnalysisResponseError(f"{model} declined to answer (content filter)")
            text = str((choice.get("message") or {}).get("content") or "")
            usage = dict(payload.get("usage") or {})

        if not text.strip():
            raise AnalysisResponseError(f"{model} answered with an empty body")
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise AnalysisResponseError(
                f"{model} was asked for JSON matching a schema and answered with something "
                f"that will not parse: {exc}"
            ) from exc
        if not isinstance(data, Mapping):
            raise AnalysisResponseError(f"{model} answered with a {type(data).__name__}, not an object")

        missing = [key for key in schema.get("required", ()) if key not in data]
        if _SENSITIVITY_FIELD in missing:
            # The one required field whose absence may not stop a recording. It is in
            # ``required`` because strict structured output demands that every property be,
            # but the gate is a passenger on this call: in ``shadow`` nothing is withheld at
            # all, and a provider dropping the field would otherwise quarantine a recording
            # for the sake of a measurement. The pass degrades to its mechanical rules and
            # ``sensitivity.assess`` says so in the report's own notes.
            missing = [key for key in missing if key != _SENSITIVITY_FIELD]
            log.warning(
                "%s did not return %s, so this recording was read for sensitive passages by "
                "the mechanical rules alone. Nothing about the transcript changed.",
                model, _SENSITIVITY_FIELD,
            )
        if missing:
            raise AnalysisResponseError(
                f"{model}'s answer is missing required field(s): {', '.join(missing)}. "
                "The schema constraint is not being honoured; nothing partial is kept."
            )
        return {"data": dict(data), "usage": usage}


# ------------------------------------------------------------------------------ helpers


def _text_of(transcript: Transcript | str) -> str:
    if isinstance(transcript, str):
        return transcript
    return getattr(transcript, "text", "") or ""


def _as_list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [entry for entry in value if isinstance(entry, Mapping)]


def _as_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return max(0.0, min(1.0, number))


def _commitment_sentence(owner: str, what: str) -> str:
    """Reported speech, always. 'X said they would…', never 'X will…'."""
    what = what.strip().rstrip(".")
    if not what:
        return ""
    if owner:
        return f"{owner} was said to be doing this: {what}"
    return f"Somebody undertook this, but the recording does not say who: {what}"


def extract(
    transcript: Transcript | str,
    settings: AnalysisSettings,
    hints: Hints | None = None,
    *,
    source_item_id: str | None = None,
    caller: Caller | None = None,
) -> Extraction:
    """One-shot convenience: build an extractor and run the pass."""
    return Extractor(settings, caller=caller).extract(
        transcript, hints, source_item_id=source_item_id
    )
