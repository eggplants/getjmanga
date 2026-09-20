"""Every error this package raises, under one base class the CLI catches."""

from __future__ import annotations


class GetjmangaError(Exception):
    """Base class for every error this package raises."""


class UnsupportedUrlError(GetjmangaError):
    """No extractor takes the URL, or the one asked for does not take it."""


class UnknownExtractorError(GetjmangaError):
    """No extractor goes by the name."""


class NotAnEpisodePageError(GetjmangaError):
    """The fetched page describes no episode the extractor can read."""


class LoginError(GetjmangaError):
    """The site refused the credentials, or the extractor cannot sign in at all."""
