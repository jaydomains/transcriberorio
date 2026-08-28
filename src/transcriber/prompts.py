"""Prompt text and JSON schemas for the two-tier analysis pass.

Kept out of :mod:`transcriber.extract` because the wording is the part of this service most
likely to be revised by a person who is not editing code, and a prompt buried three call
frames deep is a prompt nobody reviews.

Three obligations come from outside this module and are why the wording is what it is:

* The recordings are South African site speech — English, Afrikaans, isiXhosa and isiZulu,
  routinely switching inside one sentence. The model is told to keep the original words and
  to summarise separately, never to translate a passage into confident English it did not
  actually hear.
* Every item must carry a quote copied character for character, because
  :func:`transcriber.extract.verify_quote` checks it mechanically and drops any item whose
  quote cannot be found in the transcript.
* Nothing here may ask the model to conclude anything. The vocabulary throughout is "was
  said", never "is" — this pipeline reports speech and cannot decide a business fact.

Everything in this module is data: strings, dicts and pure formatting helpers. No I/O.
"""

from __future__ import annotations

import copy
from typing import Any, Sequence

__all__ = [
    "CONSTRUCTION_VOCABULARY",
    "LANGUAGE_NOTE",
    "HOUSE_RULES",
    "CLASSIFIER_SYSTEM",
    "CLASSIFIER_SCHEMA",
    "CLASSIFIER_SCHEMA_NAME",
    "EXTRACTION_SYSTEM",
    "EXTRACTION_SCHEMA",
    "EXTRACTION_SCHEMA_NAME",
    "EXTRACTION_CATEGORIES",
    "SENSITIVITY_CATEGORIES",
    "SENSITIVITY_NOTE",
    "SENSITIVE_PASSAGE_SCHEMA",
    "extraction_schema",
    "extraction_system",
    "build_classifier_user",
    "build_extraction_user",
    "context_block",
]


# --------------------------------------------------------------------------- vocabulary

#: Words this trade actually uses, on these sites, in this country. It does two jobs: it
#: goes into both prompts so a model reading a mangled transcript can recognise what was
#: meant, and :mod:`transcriber.extract` compiles it into the safety pre-check, so a
#: recording that says "torch-on" is substantive whether or not a model agrees.
#:
#: Terms are lowercase and matched on a word boundary. Keep them specific: a term so
#: general that every recording contains it (site, work, job) buys nothing and is left out.
CONSTRUCTION_VOCABULARY: tuple[str, ...] = (
    # waterproofing and roofing
    "waterproofing", "torch-on", "torch on", "membrane", "flashing", "damp course", "dpc",
    "rising damp", "screed", "fall", "ponding", "gutter", "downpipe", "chromadek",
    "ibr", "corrugated", "purlin", "truss", "ridge", "valley", "parapet", "coping",
    # structure and finishes
    "rebar", "brickforce", "shutter", "shuttering", "formwork", "slab", "lintel",
    "plaster", "skimming", "screeding", "bagging", "grout", "tiling", "cornice",
    "cill", "sill", "glazing", "balustrade", "expansion joint", "sealant",
    # process and contract
    "boq", "bill of quantities", "snag", "snags", "snagging", "snag list", "defect",
    "defects liability", "practical completion", "final completion", "handover",
    "retention", "payment certificate", "certificate", "variation order", "variation",
    "extension of time", "penalty", "penalties", "jbcc", "nhbrc", "cidb", "sans",
    "occupation certificate", "engineer's certificate", "method statement",
    "programme", "critical path", "float", "site instruction", "site meeting",
    "site agent", "foreman", "subcontractor", "sub-contractor", "main contractor",
    "quantity surveyor", "principal agent", "clerk of works",
    # client side
    "body corporate", "trustees", "trustee", "managing agent", "levy", "special levy",
    "agm", "sectional title", "hoa", "homeowners association", "insurance claim",
    # money and commerce
    "quote", "quotation", "invoice", "provisional sum", "prime cost", "escalation",
    "preliminaries", "p&g", "vat", "deposit", "progress payment",
    # health and safety
    "scaffold", "scaffolding", "harness", "fall arrest", "ppe", "barricade",
    "safety file", "incident", "near miss", "lockout",
)

#: Afrikaans, isiXhosa and isiZulu words that carry meaning this pipeline must not lose in
#: translation. Listed for the model's benefit — the transcript keeps the original words.
_LANGUAGE_EXAMPLES = (
    "Afrikaans: ja, nee, môre (tomorrow), vandag (today), volgende week (next week), "
    "klaar (finished), nog nie (not yet), sal (will), lek (leak), dak (roof), muur (wall), "
    "regmaak (fix), betaal (pay), geld (money), baie (a lot), gou (quickly), "
    "goedgekeur (approved), oor (about).",
    "isiZulu: yebo (yes), cha (no), kusasa (tomorrow), namuhla (today), imali (money), "
    "umsebenzi (the work), sizoqeda (we will finish), kulungile (it is fine / agreed), "
    "angikaqedi (I have not finished yet).",
    "isiXhosa: ewe (yes), hayi (no), ngomso (tomorrow), namhlanje (today), imali (money), "
    "umsebenzi (the work), ndiza- (I will), sivumile (we have agreed), "
    "andikaqedi (I have not finished yet).",
)

LANGUAGE_NOTE = (
    "These recordings are South African construction speech. Expect English, Afrikaans, "
    "isiXhosa and isiZulu, and expect a speaker to switch language inside a single "
    "sentence. That is normal and is not an error in the transcript.\n\n"
    "Rules for language, which override any instinct to tidy the text:\n"
    "- Quote non-English passages VERBATIM, in the language they were spoken, exactly as "
    "they appear in the transcript. Never translate inside a quote.\n"
    "- Write your summaries and descriptions in English.\n"
    "- Where a passage is unclear, misheard or ambiguous, say so and put it in "
    "unclear_passages with the words as they appear. Do NOT smooth it into confident "
    "English. A confident wrong sentence is worse here than an admitted gap, because a "
    "person can act on the gap and cannot see through the confidence.\n"
    "- A word that looks like nonsense is often a trade term or a place name heard "
    "imperfectly. Say that you are unsure rather than substituting a word that fits.\n\n"
    + "\n".join(_LANGUAGE_EXAMPLES)
)

HOUSE_RULES = (
    "Absolute rules. These are not preferences.\n"
    "1. You are reporting what was SAID. You never conclude, decide, close, approve or "
    "confirm anything yourself. Write 'James said the slab was signed off', never 'the "
    "slab is signed off'. If the recording reports someone else's decision, that is a "
    "report of a decision and you record it as such.\n"
    "2. Every single item you return carries a quote copied CHARACTER FOR CHARACTER from "
    "the transcript, including its original language, punctuation and any oddity. The "
    "quote is checked mechanically against the transcript afterwards. An item whose quote "
    "cannot be found is thrown away and a person is told the model produced an unverifiable "
    "item. If you cannot copy an exact quote for something, leave the item out.\n"
    "3. Quotes are short — one sentence, or the few words that carry the point. Long enough "
    "to be unambiguous, never a paragraph.\n"
    "4. Never output an email address, anywhere, for any reason. Not in a quote, not in a "
    "summary, not as a participant. If the recording contains one, describe the person by "
    "name or role instead and choose a different quote.\n"
    "5. Never invent an owner, a date, an amount or a site. If the recording does not say "
    "who or by when, leave that field empty. An empty field is a true statement; a guessed "
    "one is a task assigned to the wrong person.\n"
    "6. Do not infer that something is complete because it was discussed. Do not infer "
    "agreement from silence.\n"
    "7. Prefer leaving an item out to including one you are unsure of — except where the "
    "uncertainty is itself the point, in which case it belongs in open_questions or "
    "unclear_passages."
)


# --------------------------------------------------------------------------- tier one

CLASSIFIER_SCHEMA_NAME = "recording_routing"

CLASSIFIER_SYSTEM = (
    "You are the first-pass router for a voice-note pipeline used by a South African "
    "building consultancy. Every recording made on site passes through you. Your only job "
    "is to say whether this recording carries anything a person would want on the record.\n\n"
    "Choose ONE label:\n"
    "- 'substantive': it mentions a person, a site or building, a number, a date, an "
    "amount of money, an approval, an instruction, a promise, a defect, a delay, a safety "
    "matter, or anything else somebody might later need to look up. When in any doubt at "
    "all, choose this.\n"
    "- 'trivial': it carries none of the above. A test recording, a pocket recording, "
    "someone checking the microphone, a fragment with no content.\n"
    "- 'unclear': you genuinely cannot tell — the transcript is too garbled to read.\n\n"
    "Length is not evidence. A twelve-second recording saying 'ja, approved, go ahead on "
    "Beach Court' is substantive: it names a site and an approval. A two-minute recording "
    "of road noise is trivial. Judge content, never duration.\n\n"
    "Downstream, 'trivial' means no further reading happens, so the cost of calling "
    "something trivial by mistake is that it disappears from the record. The cost of "
    "calling something substantive by mistake is a few cents. Route accordingly.\n\n"
    "Also fill in 'mentions': one boolean per category, true if the recording mentions "
    "that kind of thing at all. Be generous — these are used to double-check your label, "
    "never to reduce it.\n\n"
    + LANGUAGE_NOTE
)

CLASSIFIER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "label": {
            "type": "string",
            "enum": ["substantive", "trivial", "unclear"],
        },
        "one_line": {
            "type": "string",
            "description": "One sentence in English saying what this recording is. No conclusions.",
        },
        "languages": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Languages heard, e.g. ['English', 'Afrikaans'].",
        },
        "mentions": {
            "type": "object",
            "properties": {
                "person": {"type": "boolean"},
                "site": {"type": "boolean"},
                "number": {"type": "boolean"},
                "date": {"type": "boolean"},
                "amount": {"type": "boolean"},
                "approval": {"type": "boolean"},
                "promise": {"type": "boolean"},
            },
            "required": ["person", "site", "number", "date", "amount", "approval", "promise"],
            "additionalProperties": False,
        },
        "reason": {
            "type": "string",
            "description": "Why this label, in one short sentence.",
        },
    },
    "required": ["label", "one_line", "languages", "mentions", "reason"],
    "additionalProperties": False,
}


# --------------------------------------------------------------------------- tier two

EXTRACTION_SCHEMA_NAME = "site_recording_extraction"

#: The categories the architecture asks for, in the order a person reads them. The value is
#: what the field is called in :data:`EXTRACTION_SCHEMA`; :mod:`transcriber.extract` maps
#: each one onto a ledger item kind.
EXTRACTION_CATEGORIES: tuple[str, ...] = (
    "decisions",
    "commitments",
    "money",
    "materials",
    "defects",
    "safety",
    "programme",
    "open_questions",
    "follow_ups",
)

EXTRACTION_SYSTEM = (
    "You are reading the transcript of a voice note recorded by a building consultant "
    "walking a South African site. He is thinking out loud: on the phone, in front of a "
    "contractor, or dictating to himself between buildings.\n\n"
    "Your job is to surface what a person would want on the record, each piece attached to "
    "the words that support it, so that a human being can confirm or reject it in seconds. "
    "You are not summarising for its own sake and you are not writing minutes. You are "
    "producing proposals, every one of which will be shown to a person next to its quote.\n\n"
    + HOUSE_RULES
    + "\n\nWhat to look for:\n"
    "- participants: who is speaking or being spoken about, by name or by role. Never an "
    "email address.\n"
    "- site: which building, site, unit, block or scheme this is about, if it is said.\n"
    "- decisions: a decision REPORTED in the recording — something approved, agreed, "
    "instructed, rejected or signed off, by whoever made it. You are recording that it was "
    "said, not making it true.\n"
    "- commitments: somebody said they would do something. Capture owner (who), what, and "
    "by_when — leaving any of them empty when it was not said.\n"
    "- money: amounts, quotes, invoices, certificates, retention, variations, penalties, "
    "escalation, anything with a rand figure or a commercial consequence.\n"
    "- materials: materials, products, specifications, deliveries and quantities.\n"
    "- defects: snags, defects, damage, leaks, cracks, poor workmanship, remedial work.\n"
    "- safety: anything about site safety, an incident, an unsafe method, missing PPE, "
    "scaffolding, an unsafe excavation.\n"
    "- programme: implications for timing — delays, sequence, a date pulled in or pushed "
    "out, waiting on someone, an extension of time.\n"
    "- open_questions: anything left hanging, asked and not answered, or that a person "
    "clearly needs to resolve. Be generous here. This is the most useful list in the file.\n"
    "- follow_ups: something that must be checked or chased where nobody actually "
    "committed to it.\n"
    "- unclear_passages: any passage you could not read confidently. Copy it as it appears "
    "and say what you are unsure about.\n\n"
    "An item may only appear in one category — choose the one a person would look under. "
    "If nothing in a category was discussed, return an empty list for it. An empty list is "
    "a correct answer and is much better than a padded one.\n\n"
    "Write summary_en as a short plain-English account of what was discussed: what this "
    "recording is about and what was said, in the order it was said. No headings, no "
    "bullet symbols, no bold. Somebody reads it once and knows what is in the audio.\n\n"
    + LANGUAGE_NOTE
)


def _item_schema(description: str) -> dict[str, Any]:
    """One extracted observation. Every category shares this shape but `commitments`."""
    return {
        "type": "object",
        "description": description,
        "properties": {
            "summary": {
                "type": "string",
                "description": "What was said, in English, as reported speech. One or two sentences.",
            },
            "quote": {
                "type": "string",
                "description": (
                    "Verbatim from the transcript, in the original language, copied character "
                    "for character. Checked mechanically; an item whose quote is not found is "
                    "discarded."
                ),
            },
            "speaker": {
                "type": "string",
                "description": "Who said it, if the transcript labels or names them. Empty string if not.",
            },
            "site": {
                "type": "string",
                "description": "The site or building this item is about, if said. Empty string if not.",
            },
            "confidence": {
                "type": "number",
                "description": "0.0 to 1.0 — how sure you are you read this correctly.",
            },
        },
        "required": ["summary", "quote", "speaker", "site", "confidence"],
        "additionalProperties": False,
    }


_COMMITMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Somebody said they would do something.",
    "properties": {
        "owner": {
            "type": "string",
            "description": (
                "Who undertook it, by name or role, exactly as identified in the recording. "
                "Empty string if the recording does not say. Never guess."
            ),
        },
        "what": {"type": "string", "description": "What they said they would do, in English."},
        "by_when": {
            "type": "string",
            "description": (
                "When, in the words used ('Friday', 'end of the month', 'ngomso'). Empty "
                "string if no time was given. Never convert a vague phrase into a date."
            ),
        },
        "quote": {
            "type": "string",
            "description": "Verbatim from the transcript, copied character for character.",
        },
        "speaker": {"type": "string", "description": "Who said it, if labelled. Empty string if not."},
        "site": {"type": "string", "description": "Site or building, if said. Empty string if not."},
        "confidence": {"type": "number", "description": "0.0 to 1.0."},
    },
    "required": ["owner", "what", "by_when", "quote", "speaker", "site", "confidence"],
    "additionalProperties": False,
}

EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary_en": {
            "type": "string",
            "description": "Plain-English account of what was discussed. Reported speech throughout.",
        },
        "languages": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Languages heard in the recording.",
        },
        "participants": {
            "type": "array",
            "description": "People speaking or spoken about. Names or roles only — never an email address.",
            "items": {
                "type": "object",
                "properties": {
                    "name_or_role": {"type": "string"},
                    "quote": {
                        "type": "string",
                        "description": "Verbatim words that show this person is involved.",
                    },
                },
                "required": ["name_or_role", "quote"],
                "additionalProperties": False,
            },
        },
        "site": {
            "type": "object",
            "description": "The site or building discussed. Empty strings if the recording does not say.",
            "properties": {
                "name": {"type": "string"},
                "quote": {"type": "string", "description": "Verbatim words naming it."},
            },
            "required": ["name", "quote"],
            "additionalProperties": False,
        },
        "decisions": {
            "type": "array",
            "items": _item_schema("A decision REPORTED in the recording, made by a person, not by you."),
        },
        "commitments": {"type": "array", "items": _COMMITMENT_SCHEMA},
        "money": {"type": "array", "items": _item_schema("An amount, quote, invoice, certificate or commercial consequence.")},
        "materials": {"type": "array", "items": _item_schema("A material, product, specification, delivery or quantity.")},
        "defects": {"type": "array", "items": _item_schema("A snag, defect, damage, leak or piece of remedial work.")},
        "safety": {"type": "array", "items": _item_schema("A site safety matter, incident or unsafe method.")},
        "programme": {"type": "array", "items": _item_schema("An implication for timing, sequence or duration.")},
        "open_questions": {"type": "array", "items": _item_schema("Something left hanging that a person must resolve.")},
        "follow_ups": {"type": "array", "items": _item_schema("Something to be checked or chased that nobody committed to.")},
        "unclear_passages": {
            "type": "array",
            "description": "Passages you could not read confidently. Never smoothed into confident English.",
            "items": {
                "type": "object",
                "properties": {
                    "passage": {"type": "string", "description": "The words as they appear in the transcript."},
                    "why": {"type": "string", "description": "What you are unsure of, in English."},
                },
                "required": ["passage", "why"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "summary_en",
        "languages",
        "participants",
        "site",
        "decisions",
        "commitments",
        "money",
        "materials",
        "defects",
        "safety",
        "programme",
        "open_questions",
        "follow_ups",
        "unclear_passages",
    ],
    "additionalProperties": False,
}


# ------------------------------------------------------------------- the sensitivity gate

#: The taxonomy, exactly as he settled it on 2026-08-28 (``docs/GATE-DECISIONS.md`` §5).
#: The first six are the held band — the same six names, spelled the same way, as
#: :data:`transcriber.withheld.CATEGORIES`, which is what the store validates against and
#: what the marker's wording is keyed on — and the last two are let through with a label. **What
#: each one means for the record is decided in code**, not here — :mod:`transcriber.sensitivity`
#: owns the category-to-disposition table, so a model cannot widen the held band by
#: inventing a disposition. This tuple is only the vocabulary the model is allowed to use.
SENSITIVITY_CATEGORIES: tuple[str, ...] = (
    # held
    "do_not_write_down",
    "staff_matter",
    "personal_circumstances",
    "legal_exposure",
    "bare_identifier",
    "own_margin",
    # let through, labelled
    "commercial_figure",
    "conduct_or_quality",
)

#: The wording of the gate, and the most carefully tuned text in this module. Two failures
#: are possible and they are not symmetrical: a passage wrongly held costs an approval he
#: has to clear on a phone on a roof, and ten of those a day is a gate he stops opening —
#: which loses the record. A passage wrongly let through costs a leak. He has decided which
#: risk he is taking on which content, and this block states that decision rather than
#: inviting the model to re-derive it.
SENSITIVITY_NOTE = (
    "SENSITIVE PASSAGES — a separate job, done on the same reading.\n\n"
    "Alongside everything above, list the passages of this transcript that would harm "
    "somebody if they were repeated. Put them in sensitive_passages.\n\n"
    "The question is 'WHO IS HARMED IF THIS IS REPEATED?' — never 'does this mention "
    "money'. Most recordings harm nobody. **An empty list is the right answer most of the "
    "time and is a good answer.** These are voice notes about roofs, deliveries and dates.\n\n"
    "This is a South African building consultancy. The firm is KBC. Its people record while "
    "they walk sites, with clients, contractors and staff on the line.\n\n"
    "HELD — the words are cut out of the record until a person approves them. Six things, "
    "and only these six:\n"
    "- do_not_write_down: somebody asks that something not be written down, not be minuted, "
    "not go in the report, or says it is off the record or just between us. **In any "
    "language.** This outranks every other judgement here: if you hear it, return it, "
    "whatever the passage is about. Quote the instruction and the words it is about.\n"
    "- staff_matter: a KBC employee's warning, hearing, pay, performance, dismissal, or a "
    "complaint about one of their own people as an employee.\n"
    "- personal_circumstances: an identifiable person's health, illness, bereavement, "
    "family or money troubles — anything about their life rather than their work.\n"
    "- legal_exposure: an admission that KBC itself is liable or at fault, what its "
    "attorney or insurer is planning, or a statement that something must not leave the "
    "firm.\n"
    "- bare_identifier: an identity number, bank or card details, or a home address. Quote "
    "the identifier itself and nothing around it.\n"
    "- own_margin: KBC's OWN cost set against its OWN charge, in one breath — what it "
    "raised or charged next to what it will actually spend or land at. This is the one "
    "money case that is held.\n\n"
    "LET THROUGH, LABELLED — these are published; the label rides with them so that a "
    "later client-facing check can see what they are. Two:\n"
    "- commercial_figure: a price quoted to or by a client, a supplier's rate, an invoice, "
    "a fee, a contract sum, a payment certificate, retention.\n"
    "- conduct_or_quality: a defect, poor workmanship, a complaint about a company, work "
    "being criticised.\n\n"
    "NOT SENSITIVE AT ALL — return NOTHING for these. Materials, deliveries, programme "
    "dates, building defects with nobody blamed, and a named person straightforwardly doing "
    "their job. **PRICES FLOW.** A rand figure on its own is ordinary business and is not a "
    "reason to return anything: about one line in sixteen of this record carries one. Only "
    "return a commercial_figure when the figure is genuinely the point of the passage, and "
    "never return one merely because a number was said.\n\n"
    "How to judge, and it matters more than the categories:\n"
    "- Precision over completeness, deliberately. Return a passage only if you would defend "
    "holding it to the man who has to approve it on his phone on a roof. If you are "
    "reaching for a reason, there is no reason.\n"
    "- One entry per passage. If two categories fit, return the single entry whose category "
    "is the more serious one.\n"
    "- Quote the SMALLEST span that carries the sensitive fact. The words you quote are the "
    "words that get cut out.\n"
    "- The quote is located mechanically, character for character. A passage whose words "
    "cannot be found in the transcript is NOT withheld — so an inexact copy is a leak, not "
    "a rejected item.\n"
    "- confidence: how sure you are that this genuinely harms somebody. Below 0.75 the "
    "passage is not withheld, so do not use a low confidence as a way of raising a "
    "possibility — either it is one of these six things or it is not.\n"
    "- what_it_is is PUBLISHED IN PLACE OF THE WORDS, on a page a client may read. It names "
    "the KIND of thing and carries none of the detail: 'a rate', 'a staff matter', 'a "
    "person's health', 'our own cost against what we charged'. No names, no figures, no "
    "dates, no company names.\n"
    "- reason is one line of plain English for the person approving it. Say why without "
    "repeating the sensitive words.\n\n"
    "Worked examples, from these sites:\n"
    "1. 'The quote to the body corporate is R4,500 for the torch-on repair.' -> "
    "commercial_figure. A price to a client. Published with a label. what_it_is: 'a price "
    "quoted to a client'.\n"
    "2. 'We raised R1.65m and we'll land at R1.604m, so there's a bit in it.' -> own_margin. "
    "HELD: that is what KBC charged next to what it will spend, in one breath. what_it_is: "
    "'our own cost against what we charged'.\n"
    "3. 'Sipho's disciplinary is on Thursday, it's the second warning.' -> staff_matter.\n"
    "4. 'Mrs Naidoo isn't answering because her husband is in hospital.' -> "
    "personal_circumstances.\n"
    "5. 'Don't write this down, but the engineer signed off a slab he never inspected.' -> "
    "do_not_write_down. Quote the instruction together with what it is about.\n"
    "6. 'Ja, maar moenie dit neerskryf nie.' -> do_not_write_down. Afrikaans, same rule.\n"
    "7. 'Ungakubhali oku, uThabo akaphangeli namhlanje kuba unyana wakhe ugula.' -> "
    "do_not_write_down. isiXhosa: he is asking that it not be written.\n"
    "8. 'Die prys vir die dak is R12,000, maar moenie vir hulle sê wat ons betaal het nie.' "
    "-> commercial_figure ONLY. 'Don't tell them what we paid' is an instruction about what "
    "to say to a client, not a request that something not be written down, and one figure "
    "is not a margin. Do not return do_not_write_down here.\n"
    "9. 'His ID is 8203155009089 for the site register.' -> bare_identifier. Quote only the "
    "number.\n"
    "10. 'Between you and me, the insurer says we mustn't admit the beam was "
    "under-designed.' -> legal_exposure.\n"
    "11. 'The waterproofing at unit 12 is lifting again, their workmanship is poor.' -> "
    "conduct_or_quality. Published with a label.\n"
    "12. 'The chromadek arrives Tuesday and the scaffold comes down Friday.' -> nothing.\n"
    "13. 'Thabo finished the flashing on block C.' -> nothing. A named person doing their "
    "job is not sensitive.\n"
    "14. 'The supplier rate is R92 a square and the contract sum is R840,000.' -> one "
    "commercial_figure. Ordinary business, published with a label."
)

#: One entry in ``sensitive_passages``. Note what is NOT here: the model does not say
#: whether a passage is held or labelled. That follows from the category, in code, so the
#: held band cannot be widened by a model having a strong day.
SENSITIVE_PASSAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "One passage that would harm somebody if it were repeated.",
    "properties": {
        "quote": {
            "type": "string",
            "description": (
                "Verbatim from the transcript, character for character, in the language it "
                "was spoken. The smallest span that carries the sensitive fact — for an "
                "identity number, the number itself. These are the words that get cut out, "
                "and they are located mechanically: a quote that cannot be found is not "
                "withheld at all."
            ),
        },
        "category": {
            "type": "string",
            "enum": list(SENSITIVITY_CATEGORIES),
            "description": "Which of the eight kinds this is.",
        },
        "who_is_harmed": {
            "type": "string",
            "description": (
                "Who is harmed if this is repeated, in a few words: 'the staff member', "
                "'the client', 'KBC itself'. If the honest answer is nobody, do not return "
                "the passage at all."
            ),
        },
        "what_it_is": {
            "type": "string",
            "description": (
                "A short plain-English noun phrase naming the KIND of thing, carrying none "
                "of the detail — it is published in place of the words on a page a client "
                "may read. 'a rate', 'a staff matter', 'a person's health'. No names, no "
                "figures, no dates."
            ),
        },
        "reason": {
            "type": "string",
            "description": (
                "One line of plain English for the person approving it, saying why without "
                "repeating the sensitive words."
            ),
        },
        "confidence": {
            "type": "number",
            "description": "0.0 to 1.0 — how sure you are that repeating this harms somebody.",
        },
    },
    "required": ["quote", "category", "who_is_harmed", "what_it_is", "reason", "confidence"],
    "additionalProperties": False,
}

_SENSITIVE_PASSAGES_PROPERTY: dict[str, Any] = {
    "type": "array",
    "description": (
        "Passages that would harm somebody if repeated. Empty is the usual and correct "
        "answer."
    ),
    "items": SENSITIVE_PASSAGE_SCHEMA,
}


def extraction_schema(*, sensitivity: bool = False) -> dict[str, Any]:
    """The extraction schema, with the gate's field added when the gate is running.

    The field is added to ``required`` as well as to ``properties``, because the
    OpenAI-compatible path sends ``strict: true`` — which demands that every property be
    required — and because :meth:`transcriber.extract.Extractor._read` checks the required
    list to catch a provider that has quietly stopped honouring the constraint.

    It is a parameter rather than a permanent part of the schema for one reason: with
    ``GATE_MODE=off`` the gate is not merely inactive, it is **not in the way**. No extra
    field is asked for, no extra words are sent, and nothing about the analysis pass differs
    from the day before the gate existed. A gate that can break the transcription of a
    recording while switched off is not off.
    """
    schema = copy.deepcopy(EXTRACTION_SCHEMA)
    if sensitivity:
        schema["properties"]["sensitive_passages"] = copy.deepcopy(_SENSITIVE_PASSAGES_PROPERTY)
        schema["required"] = list(schema["required"]) + ["sensitive_passages"]
    return schema


def extraction_system(*, sensitivity: bool = False) -> str:
    """The extraction system prompt, with the gate's instructions when the gate is running.

    One call, not two. A second model call per recording would double the cost and add a
    second thing that can fail between a recording and its transcript, for an answer the
    model reading the transcript is already holding in its head.
    """
    if not sensitivity:
        return EXTRACTION_SYSTEM
    return EXTRACTION_SYSTEM + "\n\n" + SENSITIVITY_NOTE


# --------------------------------------------------------------------------- rendering

def context_block(
    *,
    source_name: str = "",
    recorded_at: str = "",
    duration_s: float | None = None,
    counterparty: str = "",
    languages: Sequence[str] = (),
    vocabulary: Sequence[str] = (),
) -> str:
    """What is known about the recording before anybody listens to it.

    Every line is framed as provenance, never as fact: the filename is a filename, not
    evidence of who was on the call. A model told 'this is a call with X' will find X in
    the audio whether or not X is there.
    """
    lines: list[str] = []
    if source_name:
        lines.append(f"- Original file name (a file name only, not evidence): {source_name}")
    if recorded_at:
        lines.append(f"- Recorded at (from the file's own metadata): {recorded_at}")
    if duration_s:
        lines.append(f"- Audio duration: {duration_s:.0f} seconds")
    if counterparty:
        lines.append(
            f"- The file name suggests this may involve: {counterparty}. Treat that as a "
            "hint about spelling only. Do not report it as a participant unless the "
            "transcript itself supports it."
        )
    if languages:
        lines.append("- Languages expected on these recordings: " + ", ".join(languages))
    terms = [t for t in vocabulary if t]
    if terms:
        lines.append(
            "- Terms that occur on these jobs, for recognition only: " + ", ".join(terms[:120])
        )
    if not lines:
        return ""
    return "Context\n" + "\n".join(lines)


def _wrap_transcript(transcript: str) -> str:
    """Fence the transcript so its own text cannot close the fence early.

    The tags are the only boundary between content this service does not control — an engine
    echoing markup, somebody dictating XML, a hallucinated tag out of a garbled passage — and
    the instructions above them. A literal ``</transcript>`` in the words would end the
    delimiter and everything after it would read as top-level prompt. Quote verification
    stops an injected instruction manufacturing a *proposal*, since every item must carry
    words that are genuinely in the transcript; ``summary_en`` is free text and has no such
    guard, which is what this closes.
    """
    body = transcript.strip("\n").replace("</transcript>", "<\u200b/transcript>")
    return "<transcript>\n" + body + "\n</transcript>"


def build_classifier_user(transcript: str, context: str = "", excerpted: bool = False) -> str:
    """The router's user turn.

    ``excerpted`` is passed when the transcript was too long to send whole. It says so in
    the prompt rather than quietly shortening the input, because a router that silently
    reads half a recording and answers 'trivial' is the failure this service exists to
    remove — and the caller escalates such a recording regardless of what comes back.
    """
    parts: list[str] = []
    if context:
        parts.append(context)
    if excerpted:
        parts.append(
            "NOTE: this transcript was too long to send whole; you are seeing the "
            "beginning and the end, with a marked gap. Judge only what you can see, and "
            "do not treat the gap as empty."
        )
    parts.append(_wrap_transcript(transcript))
    parts.append(
        "Route this recording. Return only the JSON object described by the schema."
    )
    return "\n\n".join(parts)


def build_extraction_user(transcript: str, context: str = "", routing_note: str = "") -> str:
    """The extraction turn.

    ``routing_note`` carries what the mechanical pre-check found — 'this mentions an
    amount and a date' — so the strong model knows what a machine already saw. It is a
    prompt for attention, never an instruction to agree: the model is told plainly that
    the pre-check is crude and may be wrong.
    """
    parts: list[str] = []
    if context:
        parts.append(context)
    if routing_note:
        parts.append(
            "A crude keyword pre-check flagged this recording for: "
            + routing_note
            + ". That check is mechanical and often wrong. Use it only as a prompt to look "
            "carefully; do not manufacture an item to satisfy it, and do not skip anything "
            "it failed to notice."
        )
    parts.append(_wrap_transcript(transcript))
    parts.append(
        "Read the whole transcript, then return only the JSON object described by the "
        "schema. Every item must carry a quote copied character for character from the "
        "transcript above."
    )
    return "\n\n".join(parts)
