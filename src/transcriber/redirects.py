"""One rule about redirects, kept in one place: a request carrying a credential never follows one.

Why this is a module of its own rather than a helper inside the Graph client: urllib's
redirect handling copies the request's headers onto the new host, stripping only
``Content-Length`` and ``Content-Type``. Every credential this service holds travels in a
header — the Graph bearer token, OpenAI's ``Authorization``, ElevenLabs' ``xi-api-key``,
Azure's ``ocp-apim-subscription-key`` — so one 302 from anything answering on a provider's
address is enough to hand that credential to whoever sent the redirect, and nothing in any
log says it happened. The Graph client and the engines' HTTP client need exactly the same
rule, and a rule written twice is a rule that only gets fixed once.

Refusing to follow costs nothing. urllib turns an unfollowed 3xx into an
``urllib.error.HTTPError`` that still carries the status and the response headers, so a
caller that genuinely wants the redirect target — resolving a pre-authenticated OneDrive
download URL is the only such caller in this service — reads the ``Location`` off the
refusal and fetches it deliberately, with no credential attached to that second request.
"""

from __future__ import annotations

import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Hand a 3xx back to the caller instead of following it.

    Returning ``None`` from ``redirect_request`` is urllib's way of saying "this one is not
    followed": the response travels on to the default handler, which raises ``HTTPError``
    with the 3xx status and the headers intact. Every redirect status funnels through this
    single method — 301, 302, 303, 307 and 308, and for every request method, POST
    included — so there is no combination left that quietly follows one.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102 - urllib API
        return None


def no_redirect_opener(*handlers: urllib.request.BaseHandler) -> urllib.request.OpenerDirector:
    """An opener that refuses redirects, plus whatever else the caller needs installed.

    The extra handlers are for the caller that must also pin an SSL context; without this
    they would have to choose between the context and the refusal, and the refusal is the
    one that must not be optional.
    """
    return urllib.request.build_opener(NoRedirect(), *handlers)


def host_of(url: str) -> str:
    """The host part of a URL, or an empty string if it has none we can read."""
    try:
        return urllib.parse.urlsplit((url or "").strip()).netloc
    except ValueError:
        return ""


def redirect_host(location: str) -> str:
    """The host a ``Location`` header points at, phrased for an error a person will read.

    The host and nothing else: a redirect target is routinely a pre-authenticated URL whose
    query string is itself a credential, and this string ends up in exception messages and
    log lines.
    """
    return host_of(location) or "an address it did not name"
