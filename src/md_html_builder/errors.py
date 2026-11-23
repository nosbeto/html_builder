"""Exception hierarchy for md_html_builder.

Every error raised deliberately by the package derives from :class:`MdHtmlError`
so callers (and the CLI) can catch a single base class and report a clear message.
"""

from __future__ import annotations

__all__ = [
    'BuildError',
    'ConfigError',
    'InputError',
    'MdHtmlError',
    'PdfBackendUnavailableError',
    'PdfError',
    'TemplateError',
    'ThemeError',
]


class MdHtmlError(Exception):
    """Base class for all md_html_builder errors."""


class ConfigError(MdHtmlError):
    """Raised when configuration is missing, malformed or contains invalid values."""


class ThemeError(MdHtmlError):
    """Raised when a theme cannot be found or is malformed."""


class TemplateError(MdHtmlError):
    """Raised when a header/footer/page template is invalid or references unknown variables."""


class InputError(MdHtmlError):
    """Raised when an input Markdown file (or a referenced asset) is missing or unreadable."""


class BuildError(MdHtmlError):
    """Raised when building or writing output fails."""


class PdfError(BuildError):
    """Raised when exporting a page to PDF fails."""


class PdfBackendUnavailableError(PdfError):
    """Raised when the requested PDF backend (WeasyPrint, Chrome) is not installed or usable.

    The message always says how to fix it (what to install or which option to change).
    """
