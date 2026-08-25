"""The contract every job source implements."""
from __future__ import annotations

from typing import Protocol


class SourceError(RuntimeError):
    """A source could not produce listings."""


class Source(Protocol):
    name: str

    def gather(self, config: dict) -> list:
        """Return a list of Job for the searches in `config`."""
        ...


def get_source(config: dict):
    """Resolve `source:` in the config to a backend instance."""
    name = (config.get("source") or "local").lower().strip()

    if name == "local":
        from .local import LocalSource
        return LocalSource()

    if name == "apify":
        from .apify import ApifySource
        return ApifySource()

    raise SourceError(
        f"Unknown source {name!r}. Use 'local' (Playwright on this machine) "
        "or 'apify' (Apify actor in the cloud)."
    )
