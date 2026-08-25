"""Where job listings come from.

Two backends, same return type - a list of `Job`. The scan pipeline does not
know or care which one ran, so scoring, deduping and export are written once.

    local   Playwright on your machine, using your own logged-in session.
            Works out of the box. Needs Python + Chromium locally.

    apify   An Apify actor doing the same navigation in the cloud.
            Needs an Apify account, a token, and your session uploaded there.

`local` is the default and the one to reach for. Read docs/apify.md before
choosing `apify` - it carries real constraints that are not obvious, chiefly
that Naukri's search endpoints are request-signed and cannot be called over
plain HTTP, so the actor still runs a full browser rather than a cheap fetch.
"""
from __future__ import annotations

from .base import SourceError, get_source

__all__ = ["SourceError", "get_source"]
