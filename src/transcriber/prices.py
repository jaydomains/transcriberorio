"""What the analysis pass costs, and the one place a price is written down.

The ledger stores tokens, never money — see :class:`transcriber.extract.Spend` for why.
This is the other half: the price list that turns those tokens into a figure for the
morning email, kept in one file so there is exactly one thing to change when prices move.

**Three rules, and the second is the one that matters.**

**A price carries the date it was checked.** ``CHECKED_ON`` is printed next to every figure
the digest reports. A cost with no provenance is how the setup guide came to say "a few
dollars a month" about something nearer eighty: nobody could see how old the number was, so
nobody knew to doubt it.

**An unknown model is UNPRICED, never zero.** If this deployment is pointed at a model that
is not in the table below, the digest says so and reports the tokens without a figure.
Returning zero would read as "that was free", which is the one wrong answer a cost meter
can give. Switching models is a one-line config change, so this will happen.

**Cache reads and writes are priced separately.** A cache read is a tenth of ordinary
input and a cache write is a quarter more than it, so folding them together would report
the same money whether the cache was working or not — and the cache is the difference
between the system prompt costing something once a day or once a recording.

Prices are USD per million tokens, from the published rates. They are not negotiated,
not per-customer, and not guaranteed: this file is a record of what was published on
``CHECKED_ON``, which is a different claim from what the invoice will say.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

__all__ = [
    "CHECKED_ON",
    "PRICES",
    "ModelPrice",
    "cost_of",
    "cost_of_all",
    "price_for",
]

#: The day the rates below were last read off the published price list. Printed beside
#: every figure the digest reports, so a stale table is visible rather than assumed.
CHECKED_ON = "2026-06-24"


class ModelPrice:
    """USD per million tokens, for one model, in the four ways tokens are billed."""

    __slots__ = ("input", "output", "cache_read", "cache_write")

    def __init__(self, input: float, output: float,
                 cache_read: float | None = None, cache_write: float | None = None) -> None:
        self.input = float(input)
        self.output = float(output)
        # The usual multipliers on the input rate: a cache read is a tenth of it, a cache
        # write a quarter more than it. Stated per model so a model that prices its cache
        # differently can say so instead of being quietly wrong.
        self.cache_read = float(input) * 0.1 if cache_read is None else float(cache_read)
        self.cache_write = float(input) * 1.25 if cache_write is None else float(cache_write)

    def __repr__(self) -> str:
        return (f"ModelPrice(input={self.input}, output={self.output}, "
                f"cache_read={self.cache_read}, cache_write={self.cache_write})")


#: Keyed by the model id this service actually sends. A dated snapshot, not a contract.
PRICES: dict[str, ModelPrice] = {
    "claude-haiku-4-5": ModelPrice(input=1.00, output=5.00),
    "claude-haiku-4-5-20251001": ModelPrice(input=1.00, output=5.00),
    "claude-opus-5": ModelPrice(input=5.00, output=25.00),
    "claude-sonnet-5": ModelPrice(input=3.00, output=15.00),
}

_PER_MILLION = 1_000_000.0


def price_for(model: str) -> ModelPrice | None:
    """The price list entry for a model, or None when there is no entry.

    None is a real answer and callers must handle it: it means "this deployment is using a
    model this table does not know", which is a thing to report, not a thing to treat as
    free. Matching is exact on purpose — guessing that ``claude-opus-6`` prices like
    ``claude-opus-5`` is how a table starts lying.
    """
    return PRICES.get((model or "").strip())


def cost_of(spend: Any) -> float | None:
    """USD for one model call, or None when its model is not in the table.

    ``spend`` is anything with the five attributes :class:`transcriber.extract.Spend` has;
    it is read by attribute rather than imported so this module stays a leaf and can be
    read, and priced, without pulling in the analysis pass.
    """
    price = price_for(getattr(spend, "model", ""))
    if price is None:
        return None
    return (
        getattr(spend, "input_tokens", 0) * price.input
        + getattr(spend, "output_tokens", 0) * price.output
        + getattr(spend, "cache_read_tokens", 0) * price.cache_read
        + getattr(spend, "cache_write_tokens", 0) * price.cache_write
    ) / _PER_MILLION


def cost_of_all(spends: Iterable[Any]) -> tuple[float, tuple[str, ...]]:
    """Total USD, and the models that could not be priced.

    Returns both because either alone would mislead. The total without the unpriced list
    would be an undercount presented as a total; the list without the total would be a
    warning with nothing to weigh it against. The digest prints both.
    """
    total = 0.0
    unpriced: list[str] = []
    for spend in spends:
        amount = cost_of(spend)
        if amount is None:
            model = str(getattr(spend, "model", "") or "an unnamed model")
            if model not in unpriced:
                unpriced.append(model)
            continue
        total += amount
    return total, tuple(unpriced)
