"""The page James actually taps yes or no on, rendered as one self-contained HTML file.

He is on a roof, in the sun, with a client on the line. That single sentence decides almost
everything here:

* **Seconds per item.** Everything needed to decide is on the page — which recording, which
  site, who was on the call, the held words with a little either side, and why they were
  caught — so nobody has to open a transcript to answer. The queue is grouped by recording,
  because five passages from one call are one act of remembering, not five.
* **Two buttons of exactly equal weight.** Same size, same type, same border, same tap
  target; only the words and a hairline of colour differ, and there is deliberately no
  "refuse everything" button anywhere. A page where refusing is the easy tap is a page that
  hollows out the record while looking like a gate, which is the failure this whole design
  is arranged against.
* **Nothing loads from anywhere.** No font, no script, no image, no favicon request: one
  document, inline style and inline script under a nonce, so it renders on a bad signal and
  so nothing about a held passage can be inferred from a request to somebody else's server.
  The one ``<link rel="icon" href="data:,">`` exists precisely to stop the browser asking
  for ``/favicon.ico``.
* **Legible in daylight.** Near-black on white, 17px base, 52px tap targets, real focus
  rings, no grey-on-grey. It follows the phone into dark mode, but the light palette is the
  one that was designed.

**What this module may be given, and what it may not.** :class:`Item` — the only shape that
carries words — is built by :mod:`transcriber.review_server` from
``WithheldStore.queue_for(<the signed-in reviewer>)``, which answers for one named person
and no other. :class:`Elsewhere` is what James sees of everybody else's queue, and it has no
field for text at all: not a blank one, not an ignored one — there is nowhere to put words,
so no template change and no future edit can put a staff member's sentence on his screen.
That is decision 6 made structural, because staff record voluntarily and a staff member who
learns their held words are read by the principal simply stops keeping a folder, and then
the recordings are gone.

Rendering rule, applied without exception: **every value is escaped on the way in**. There
is one helper, :func:`e`, every interpolation goes through it, and the held text is escaped
by the same one — a held passage is the most quotable text in the system and quite likely to
contain an ampersand, a bracket or an apostrophe.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime, timezone

try:  # pragma: no cover - present on every supported platform, absent on a stripped one
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - a missing tzdata must not take the page down
    ZoneInfo = None  # type: ignore[assignment]

__all__ = [
    "PAGE_TITLE",
    "Item",
    "Recording",
    "Elsewhere",
    "Flash",
    "Page",
    "render",
    "render_notice",
    "RELEASE_LABEL",
    "REFUSE_LABEL",
    "RELEASED_SAID",
    "REFUSED_SAID",
    "DEFAULT_TZ",
    "display_name",
    "friendly_when",
    "friendly_age",
    "e",
]

PAGE_TITLE = "Held for review"

#: The two answers, in the words the page uses. They are the same length on purpose: a
#: button that is visibly bigger is a button that gets pressed more, and the whole point of
#: this page is that approving and refusing cost the same.
RELEASE_LABEL = "Put it in the record"
REFUSE_LABEL = "Keep it out"

#: Where each answer leaves the passage, said plainly once it has been given.
RELEASED_SAID = "Going into the record"
REFUSED_SAID = "Kept out of the record"

DEFAULT_TZ = "Africa/Johannesburg"

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def e(value: object) -> str:
    """Escape anything on its way into the document. The only route text takes to HTML."""
    return html.escape("" if value is None else str(value), quote=True)


def display_name(address: str) -> str:
    """A person, named the way the page says it — never as an email address.

    Reviewers are configured as addresses (``ROUTE_<NAME>_REVIEWER``), and the house rule is
    that this service never types an email address anywhere. The local part identifies the
    person to anybody who would be reading this page in the first place, and the domain adds
    nothing but a deliverable address in a screenshot.
    """
    text = (address or "").strip()
    if "@" not in text:
        return text
    local = text.split("@", 1)[0]
    return local.replace(".", " ").replace("_", " ").strip() or text


def _parse(stamp: str) -> datetime | None:
    text = (stamp or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _zone(name: str):
    if ZoneInfo is None or not name:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - an unknown zone shows UTC rather than no page at all
        return timezone.utc


def friendly_when(stamp: str, tz_name: str = DEFAULT_TZ) -> str:
    """``Mon 24 Aug, 09:15`` — a person's way of saying when, in his own timezone."""
    when = _parse(stamp)
    if when is None:
        return ""
    local = when.astimezone(_zone(tz_name))
    return (
        f"{_DAYS[local.weekday()]} {local.day} {_MONTHS[local.month - 1]}, "
        f"{local.hour:02d}:{local.minute:02d}"
    )


def friendly_age(days: int) -> str:
    """How long it has been waiting, in words rather than a date arithmetic problem."""
    count = max(0, int(days))
    if count == 0:
        return "held today"
    if count == 1:
        return "waiting since yesterday"
    if count < 7:
        return f"waiting {count} days"
    if count < 14:
        return "waiting over a week"
    return f"waiting {count} days"


@dataclass(frozen=True)
class Item:
    """One held passage, with the words, on the screen of the person who owns the decision.

    Built only from a record :meth:`WithheldStore.queue_for` returned for the signed-in
    reviewer. ``before`` and ``after`` are the stored surrounds — stored rather than derived,
    because by the time anybody reads this the published transcript no longer contains the
    passage to derive them from.
    """

    hold_id: str
    ref: str
    what: str                 # "A staff matter" — the category, said plainly
    subject: str              # "a rate for the remedial" — the public noun phrase
    reason: str               # why the classifier caught it, in its own words
    before: str
    words: str
    after: str
    speaker: str = ""
    held_at: str = ""
    age_days: int = 0
    unsure: bool = False
    #: Set once the person has answered and the undo window is still open.
    answered: str = ""        # "" | "released" | "refused"
    undo_until_ms: int = 0

    @property
    def answered_said(self) -> str:
        if self.answered == "released":
            return RELEASED_SAID
        if self.answered == "refused":
            return REFUSED_SAID
        return ""


@dataclass(frozen=True)
class Recording:
    """One recording and every passage of it this reviewer has to answer."""

    item_id: str
    title: str                # the source filename, cleaned up for reading
    site: str = ""
    who: str = ""             # who was on the call, as far as anything knows
    recorded_at: str = ""
    route_label: str = ""
    items: tuple[Item, ...] = ()


@dataclass(frozen=True)
class Elsewhere:
    """Somebody else's queue as James is allowed to see it: a count, sites, an age.

    There is no field here for text, for context, for a category's quotation of the words,
    or for the classifier's reason — which quotes them. Decision 6 is not enforced by
    remembering not to render something; it is enforced by there being nothing to render.
    """

    who: str                  # already a display name, never an address
    count: int
    recordings: int = 0
    sites: tuple[str, ...] = ()
    oldest_days: int = 0


@dataclass(frozen=True)
class Flash:
    """One line at the top of the page saying what just happened, or did not."""

    text: str
    tone: str = "ok"          # "ok" | "warn"


@dataclass(frozen=True)
class Page:
    """Everything one rendering needs. No I/O happens below this line."""

    reviewer: str                       # display name
    csrf: str
    token: str = ""                     # carried in the form so a cookie-less phone still works
    action_answer: str = "/review/answer"
    action_undo: str = "/review/undo"
    recordings: tuple[Recording, ...] = ()
    elsewhere: tuple[Elsewhere, ...] = ()
    #: One line of aggregate, under the per-person counts: which sites have something
    #: waiting and how old the oldest is. Aggregate rather than per-person because the only
    #: query that could break it down by person is the one that also returns the words.
    elsewhere_summary: str = ""
    flashes: tuple[Flash, ...] = ()
    mode: str = "on"                    # off | shadow | on
    shadow_note: str = ""
    timezone_name: str = DEFAULT_TZ
    undo_seconds: int = 8
    is_principal: bool = False
    generated_at: str = ""

    @property
    def item_count(self) -> int:
        return sum(len(r.items) for r in self.recordings)

    @property
    def waiting_count(self) -> int:
        return sum(1 for r in self.recordings for i in r.items if not i.answered)


# --------------------------------------------------------------------------- the styling
#
# One stylesheet, inline, under a nonce. Colours are picked for a phone screen in direct
# sun: near-black on white, no mid-greys carrying meaning, and every border at least 1.5px.
# Dark mode follows the phone, but the light palette above is the one that was designed for
# the roof.

_CSS = """
:root{
  --ink:#111418; --ink-soft:#3d444d; --paper:#ffffff; --edge:#c4cad1;
  --held-bg:#fff4d6; --held-edge:#a97b00;
  --in:#0b6b3a; --out:#8a3300; --warn:#8a3300; --ok:#0b6b3a;
  --rule:#e4e8ec;
}
@media (prefers-color-scheme: dark){
  :root{
    --ink:#f2f4f6; --ink-soft:#c3cad2; --paper:#14171a; --edge:#4a525b;
    --held-bg:#3a3111; --held-edge:#e0b64a;
    --in:#7ddba3; --out:#ffb27a; --warn:#ffb27a; --ok:#7ddba3;
    --rule:#272c31;
  }
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0; background:var(--paper); color:var(--ink);
  font:17px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  padding:0 0 4rem;
}
.wrap{max-width:44rem;margin:0 auto;padding:0 1rem}
header{padding:1.1rem 0 .6rem;border-bottom:2px solid var(--rule)}
h1{font-size:1.35rem;margin:0 0 .2rem}
.who{color:var(--ink-soft);font-size:.95rem;margin:0}
.lede{font-size:1.05rem;margin:1rem 0 .2rem}
.hint{color:var(--ink-soft);font-size:.92rem;margin:.2rem 0 1rem}
.flash{border:2px solid var(--ok);border-radius:.5rem;padding:.7rem .8rem;margin:1rem 0;font-weight:600}
.flash.warn{border-color:var(--warn)}
.rec{margin:1.6rem 0 0;padding-top:1rem;border-top:2px solid var(--rule)}
.rec h2{font-size:1.05rem;margin:0 0 .15rem;word-break:break-word}
.meta{color:var(--ink-soft);font-size:.92rem;margin:0 0 .2rem}
.item{border:1.5px solid var(--edge);border-radius:.6rem;padding:.85rem;margin:.9rem 0;background:var(--paper)}
.what{font-weight:700;margin:0 0 .1rem}
.ref{color:var(--ink-soft);font-weight:400;font-size:.85rem;letter-spacing:.04em}
.why{color:var(--ink-soft);font-size:.93rem;margin:.15rem 0 .6rem}
blockquote{
  margin:0 0 .8rem; padding:.6rem .7rem; border-left:5px solid var(--held-edge);
  background:transparent; font-size:1rem; overflow-wrap:anywhere;
}
blockquote .ctx{color:var(--ink-soft)}
blockquote mark{background:var(--held-bg);color:var(--ink);padding:.06em .15em;border-radius:.2rem}
.speaker{color:var(--ink-soft);font-size:.88rem;margin:.35rem 0 0}
.decide{display:flex;gap:.6rem}
.decide button{
  flex:1 1 0; min-height:52px; padding:.7rem .5rem; font:inherit; font-weight:700;
  background:var(--paper); color:var(--ink); border:2px solid var(--edge);
  border-radius:.5rem; cursor:pointer; text-align:center;
}
.decide button.in{border-color:var(--in)}
.decide button.out{border-color:var(--out)}
.decide button:active{transform:translateY(1px)}
button:focus-visible,a:focus-visible{outline:3px solid var(--held-edge);outline-offset:2px}
.answered{display:flex;gap:.6rem;align-items:center;flex-wrap:wrap;min-height:52px}
.answered .said{font-weight:700;flex:1 1 auto}
.answered .said.out{color:var(--out)}
.answered .said.in{color:var(--in)}
.answered button{
  min-height:44px;padding:.5rem .9rem;font:inherit;font-weight:700;background:var(--paper);
  color:var(--ink);border:2px solid var(--edge);border-radius:.5rem;cursor:pointer;
}
.count{font-variant-numeric:tabular-nums}
.unsent{color:var(--warn);font-weight:700;font-size:.92rem;margin:.4rem 0 0}
.elsewhere{margin-top:2.2rem;padding-top:1rem;border-top:2px solid var(--rule)}
.elsewhere h2{font-size:1.05rem;margin:0 0 .3rem}
.elsewhere p.hint{margin-top:0}
.elsewhere li{margin:.45rem 0}
.elsewhere ul{padding-left:1.1rem;margin:.4rem 0}
.empty{padding:1.4rem 0;font-size:1.05rem}
.foot{margin-top:2.5rem;color:var(--ink-soft);font-size:.85rem}
.notice{padding:2.5rem 0}
.notice h1{margin-bottom:.6rem}
.shadow{border:2px dashed var(--edge);border-radius:.6rem;padding:.8rem;margin:1rem 0}
"""

# --------------------------------------------------------------------------- the script
#
# Progressive enhancement, and nothing more. With scripting off every button is an ordinary
# form submit that works exactly as it always did; with it on, the answer goes by fetch so
# the page does not reload under his thumb, and an answer that cannot be sent — a site with
# no signal, which is most of them — is held in memory and retried until it goes, saying so
# on screen the whole time rather than pretending it landed.
#
# It stores nothing. No localStorage, no cookie of its own: an unsent answer lives in this
# page for as long as the page is open, and if the page is closed the answer was never
# recorded and the passage is still waiting — which is the safe direction and is visible in
# tomorrow's email.

_JS = """
(function(){
  var pending = [];
  var timer = null;
  var busy = false;

  function setUnsent(card, on, text){
    var note = card.querySelector('.unsent');
    if(!note){ note = document.createElement('p'); note.className = 'unsent'; card.appendChild(note); }
    note.textContent = on ? (text || 'Not sent yet. Keep this page open, it will send itself.') : '';
    note.style.display = on ? '' : 'none';
  }

  function tick(){
    var now = Date.now();
    var live = document.querySelectorAll('[data-undo-until]');
    for(var i = 0; i < live.length; i++){
      var el = live[i];
      var left = Math.ceil((parseInt(el.getAttribute('data-undo-until'), 10) - now) / 1000);
      var label = el.querySelector('.count');
      if(left > 0){
        if(label){ label.textContent = left + 's'; }
      } else {
        el.removeAttribute('data-undo-until');
        var undo = el.querySelector('button');
        if(undo){ undo.disabled = true; undo.textContent = 'Recorded'; }
        if(label){ label.textContent = ''; }
      }
    }
  }

  function answeredMarkup(state, until){
    var word = state === 'released' ? RELEASED : REFUSED;
    var cls = state === 'released' ? 'in' : 'out';
    var wrap = document.createElement('div');
    wrap.className = 'answered';
    if(until){ wrap.setAttribute('data-undo-until', String(until)); }
    var said = document.createElement('span');
    said.className = 'said ' + cls;
    said.textContent = word;
    wrap.appendChild(said);
    var count = document.createElement('span');
    count.className = 'count';
    wrap.appendChild(count);
    return wrap;
  }

  function send(job){
    var body = new URLSearchParams();
    for(var key in job.fields){ body.append(key, job.fields[key]); }
    return fetch(job.url, {
      method: 'POST',
      body: body,
      headers: {'Accept': 'application/json', 'Content-Type': 'application/x-www-form-urlencoded'},
      credentials: 'same-origin',
      cache: 'no-store'
    }).then(function(response){
      if(!response.ok && response.status >= 500){ throw new Error('server'); }
      return response.json();
    });
  }

  function restore(card){
    var box = card.querySelector('.decide, .answered');
    var template = card.querySelector('template.buttons');
    if(template && box){ box.replaceWith(template.content.cloneNode(true)); wire(card); }
  }

  function apply(card, data){
    if(!card){ return; }
    setUnsent(card, false);
    // Anything the server would not take puts the two buttons back. Leaving the optimistic
    // "going into the record" on screen after a refusal would tell him something happened
    // that did not, which is the one thing this page may never do.
    if(!data.ok && data.state !== 'undone'){
      restore(card);
      setUnsent(card, true, data.message || 'That did not go through.');
      return;
    }
    if(data.state === 'undone'){
      restore(card);
      return;
    }
    var box = card.querySelector('.decide, .answered');
    if(data.state === 'queued' || data.state === 'recorded' || data.state === 'already'){
      var fresh = answeredMarkup(data.answer, data.undo_until_ms || 0);
      if(data.undo_until_ms){
        var undo = document.createElement('button');
        undo.type = 'button';
        undo.textContent = 'Undo';
        undo.addEventListener('click', function(){
          setUnsent(card, true, 'Undoing...');
          queue({url: UNDO, fields: {hold: card.getAttribute('data-hold'), csrf: CSRF, k: TOKEN}, card: card});
        });
        fresh.appendChild(undo);
      }
      if(box){ box.replaceWith(fresh); }
      return;
    }
    setUnsent(card, true, data.message || 'That did not go through.');
  }

  // One answer in flight at a time. Two overlapping sends would race each other's
  // completion handlers and shift the wrong job off the queue, which is how an answer
  // gets quietly dropped while the screen says it went.
  function drain(){
    if(busy || !pending.length){ return; }
    busy = true;
    var job = pending[0];
    send(job).then(function(data){
      busy = false;
      pending.shift();
      apply(job.card, data);
      drain();
    }).catch(function(){
      busy = false;
      setUnsent(job.card, true);
      if(!timer){ timer = setTimeout(function(){ timer = null; drain(); }, 8000); }
    });
  }

  function queue(job){
    pending.push(job);
    drain();
  }

  function wire(card){
    var form = card.querySelector('form.answer');
    if(!form || form.getAttribute('data-wired')){ return; }
    form.setAttribute('data-wired', '1');
    var chosen = '';
    var buttons = form.querySelectorAll('button');
    for(var i = 0; i < buttons.length; i++){
      buttons[i].addEventListener('click', function(){ chosen = this.value; });
    }
    form.addEventListener('submit', function(event){
      var answer = (event.submitter && event.submitter.value) || chosen;
      if(!answer){ return; }
      event.preventDefault();
      var fields = {};
      var data = new FormData(form);
      data.forEach(function(value, key){ fields[key] = value; });
      fields.answer = answer;
      var box = card.querySelector('.decide');
      if(box){ box.replaceWith(answeredMarkup(answer === 'release' ? 'released' : 'refused', 0)); }
      setUnsent(card, true, 'Sending...');
      queue({url: ANSWER, fields: fields, card: card});
    });
  }

  var cards = document.querySelectorAll('.item');
  for(var i = 0; i < cards.length; i++){ wire(cards[i]); }
  setInterval(tick, 250);
  window.addEventListener('online', drain);
  window.addEventListener('beforeunload', function(event){
    if(pending.length){ event.preventDefault(); event.returnValue = ''; }
  });
})();
"""


def _js_literal(value: str) -> str:
    """One value, safe to sit inside a ``<script>`` block.

    JSON quoting is not enough on its own: ``</script>`` inside a JSON string would still
    close the element. The three characters that can start something in an HTML parser are
    escaped as well, so no value — a token, a path, a label — can end the script early.
    """
    import json

    return (
        json.dumps(str(value))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _js(page: Page) -> str:
    """The script with the handful of values it needs, injected as escaped JS literals."""
    header = (
        f"var ANSWER={_js_literal(page.action_answer)};"
        f"var UNDO={_js_literal(page.action_undo)};"
        f"var CSRF={_js_literal(page.csrf)};"
        f"var TOKEN={_js_literal(page.token)};"
        f"var RELEASED={_js_literal(RELEASED_SAID)};"
        f"var REFUSED={_js_literal(REFUSED_SAID)};"
    )
    return header + _JS


def _head(nonce: str, title: str) -> list[str]:
    return [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">',
        '<meta name="referrer" content="no-referrer">',
        '<meta name="robots" content="noindex, nofollow, noarchive">',
        '<meta name="color-scheme" content="light dark">',
        f"<title>{e(title)}</title>",
        # Not decoration: without it the browser asks for /favicon.ico, and this page makes
        # no request to anything, ever.
        '<link rel="icon" href="data:,">',
        f'<style nonce="{e(nonce)}">{_CSS}</style>',
        "</head>",
        "<body>",
        '<div class="wrap">',
    ]


def _hidden(page: Page, hold_id: str) -> str:
    """The three fields every answer carries.

    ``k`` is the capability token itself. It rides in the body and never in a URL, so it
    stays out of the address bar, out of a Referer, out of a proxy log and out of ours — and
    it means the page still works on a phone that refuses cookies. Because a cross-site page
    cannot know it, it is also what makes forging one of these submissions impossible;
    ``csrf`` is the second lock on the same door.
    """
    return (
        f'<input type="hidden" name="hold" value="{e(hold_id)}">'
        f'<input type="hidden" name="csrf" value="{e(page.csrf)}">'
        f'<input type="hidden" name="k" value="{e(page.token)}">'
    )


def _buttons(page: Page, item: Item) -> str:
    """The two answers, side by side, identical but for the word and a hairline of colour."""
    return (
        f'<form class="answer" method="post" action="{e(page.action_answer)}">'
        f"{_hidden(page, item.hold_id)}"
        '<div class="decide">'
        f'<button class="in" type="submit" name="answer" value="release">{e(RELEASE_LABEL)}</button>'
        f'<button class="out" type="submit" name="answer" value="refuse">{e(REFUSE_LABEL)}</button>'
        "</div>"
        "</form>"
    )


def _answered(page: Page, item: Item) -> str:
    tone = "in" if item.answered == "released" else "out"
    parts = [f'<div class="answered" data-undo-until="{int(item.undo_until_ms)}">']
    parts.append(f'<span class="said {tone}">{e(item.answered_said)}</span>')
    parts.append('<span class="count"></span>')
    parts.append(
        f'<form method="post" action="{e(page.action_undo)}">'
        f"{_hidden(page, item.hold_id)}"
        '<button type="submit">Undo</button>'
        "</form>"
    )
    parts.append("</div>")
    return "".join(parts)


def _item_html(page: Page, item: Item) -> str:
    out = [f'<article class="item" id="h-{e(item.ref)}" data-hold="{e(item.hold_id)}">']
    out.append(
        f'<p class="what">{e(item.what)} <span class="ref">{e(item.ref)}</span></p>'
    )
    why = item.reason.strip()
    if item.unsure:
        why = (why + " " if why else "") + "The classifier was not sure about this one."
    if why:
        out.append(f'<p class="why">{e(why)}</p>')
    # The three pieces are contiguous slices of the transcript, so they are joined with
    # nothing between them: what he reads is exactly what was said, with an ellipsis
    # marking where the stored surround was cut.
    out.append(
        "<blockquote>"
        + (f'<span class="ctx">&hellip;{e(item.before)}</span>' if item.before else "")
        + f"<mark>{e(item.words)}</mark>"
        + (f'<span class="ctx">{e(item.after)}&hellip;</span>' if item.after else "")
        + "</blockquote>"
    )
    said = []
    if item.speaker:
        said.append(f"Said by {item.speaker}")
    if item.age_days >= 1:
        said.append(friendly_age(item.age_days))
    if said:
        out.append(f'<p class="speaker">{e(" · ".join(said))}</p>')
    # The buttons are kept in a template as well as rendered, so that an undo can put them
    # back exactly as they were without a page load.
    buttons = _buttons(page, item)
    if item.answered:
        out.append(_answered(page, item))
    else:
        out.append(buttons)
    out.append(f'<template class="buttons">{buttons}</template>')
    out.append("</article>")
    return "".join(out)


def _recording_html(page: Page, rec: Recording) -> str:
    out = ['<section class="rec">']
    out.append(f"<h2>{e(rec.title)}</h2>")
    line = " · ".join(
        part
        for part in (
            rec.site,
            friendly_when(rec.recorded_at, page.timezone_name),
            f"with {rec.who}" if rec.who else "",
            rec.route_label,
        )
        if part
    )
    if line:
        out.append(f'<p class="meta">{e(line)}</p>')
    count = len(rec.items)
    out.append(
        f'<p class="meta">{count} passage{"" if count == 1 else "s"} held from this recording</p>'
    )
    for item in rec.items:
        out.append(_item_html(page, item))
    out.append("</section>")
    return "".join(out)


def _elsewhere_html(page: Page) -> str:
    """Counts, sites and ages for the queues that are not his to read.

    Every value on this list came from :class:`Elsewhere`, which has no text on it. He can
    see that four passages from Beach Court have been waiting three days and whose they are;
    he cannot see a word of them, and there is no shape here that could carry one.
    """
    out = ['<section class="elsewhere">']
    out.append("<h2>Waiting with other people</h2>")
    out.append(
        '<p class="hint">Counts and sites only. A person reviews their own held passages, '
        "so their words are not shown here — not to you, not to anyone but them.</p>"
    )
    out.append("<ul>")
    for row in page.elsewhere:
        bits = [f"<strong>{e(row.who)}</strong>: {row.count} waiting"]
        if row.recordings:
            bits.append(f"across {row.recordings} recording{'' if row.recordings == 1 else 's'}")
        if row.sites:
            bits.append("at " + e(", ".join(row.sites[:4])))
        bits.append(friendly_age(row.oldest_days))
        out.append("<li>" + " · ".join(bits) + "</li>")
    out.append("</ul>")
    if page.elsewhere_summary:
        out.append(f'<p class="hint">{e(page.elsewhere_summary)}</p>')
    out.append("</section>")
    return "".join(out)


def render(page: Page, *, nonce: str) -> str:
    """The whole page, as one string. No I/O, no store, no request object."""
    out = _head(nonce, PAGE_TITLE)
    out.append("<header>")
    out.append(f"<h1>{e(PAGE_TITLE)}</h1>")
    out.append(f'<p class="who">Signed in as {e(page.reviewer)}</p>')
    out.append("</header>")

    for flash in page.flashes:
        tone = "warn" if flash.tone == "warn" else "ok"
        out.append(f'<p class="flash {tone}" role="status">{e(flash.text)}</p>')

    if page.mode == "shadow":
        out.append('<div class="shadow">')
        out.append(
            "<p><strong>The gate is in shadow.</strong> Nothing is being held back and "
            "nothing here needs an answer — every recording is going into the record in "
            "full, exactly as before.</p>"
        )
        if page.shadow_note:
            out.append(f"<p>{e(page.shadow_note)}</p>")
        out.append("</div>")

    waiting = page.waiting_count
    total = page.item_count
    if total:
        recordings = len(page.recordings)
        out.append(
            f'<p class="lede">{waiting} passage{"" if waiting == 1 else "s"} waiting for you, '
            f'from {recordings} recording{"" if recordings == 1 else "s"}.</p>'
        )
        out.append(
            '<p class="hint">Put it in the record and the words go back where they were said. '
            "Keep it out and they stay here and in the recording, and never reach the record. "
            "Neither answer is undone by anything but you.</p>"
        )
        for rec in page.recordings:
            out.append(_recording_html(page, rec))
    else:
        out.append('<p class="empty">Nothing is waiting for you. Nothing was held back.</p>')

    if page.is_principal and page.elsewhere:
        out.append(_elsewhere_html(page))

    out.append('<p class="foot">')
    out.append("Nothing here is decided by a timer, or by this service, or by anybody but you. ")
    out.append("An unanswered passage stays held, and is counted in tomorrow morning's email.")
    if page.generated_at:
        out.append(f" This page was built {e(friendly_when(page.generated_at, page.timezone_name))}.")
    out.append("</p>")

    out.append("</div>")
    out.append(f'<script nonce="{e(nonce)}">{_js(page)}</script>')
    out.append("</body></html>")
    return "".join(out)


def render_notice(title: str, message: str, *, nonce: str, detail: str = "") -> str:
    """A page with no queue on it: an expired link, a wrong one, or too many tries.

    Deliberately the same page for "this token was never issued" and "this token has
    expired". A page that distinguishes them tells whoever is guessing which half of the
    guess was right.
    """
    out = _head(nonce, title)
    out.append('<div class="notice">')
    out.append(f"<h1>{e(title)}</h1>")
    out.append(f"<p>{e(message)}</p>")
    if detail:
        out.append(f'<p class="hint">{e(detail)}</p>')
    out.append("</div></div></body></html>")
    return "".join(out)
