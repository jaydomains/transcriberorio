"""transcriber — voice notes from OneDrive to the site-memory record.

Stdlib only, on purpose: a service that must run unattended for years should have nothing
underneath it that can rot. Nothing in this package decides anything — it transcribes, it
summarises what was said, and it surfaces commitments and questions as proposals carrying a
verbatim quote, for a person to confirm.
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
