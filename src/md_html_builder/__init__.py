"""md_html_builder - convert Markdown files into customisable, styled HTML.

Quick start::

    from md_html_builder import Config, SiteBuilder, convert

    html = convert('# Hello\n\nSome *Markdown*.')          # full HTML page as a string
    SiteBuilder(Config()).build('docs/', output='site/')    # convert a directory
    SiteBuilder(Config()).build('notes.md', output='notes.pdf')  # make a PDF

See :mod:`md_html_builder.config` for all options and :mod:`md_html_builder.themes` for the
theme interface.
"""

from __future__ import annotations

__version__ = '0.1.0'

from .builder import BuildResult, MarkdownConverter, SiteBuilder  # noqa: E402
from .config import Config, ConfigLoader  # noqa: E402
from .errors import (  # noqa: E402
    BuildError,
    ConfigError,
    InputError,
    MdHtmlError,
    PdfBackendUnavailableError,
    PdfError,
    TemplateError,
    ThemeError,
)
from .pdf import PdfExporter, PdfResult  # noqa: E402
from .themes import Theme, ThemeRegistry  # noqa: E402

__all__ = [
    'BuildError',
    'BuildResult',
    'Config',
    'ConfigError',
    'ConfigLoader',
    'InputError',
    'MarkdownConverter',
    'MdHtmlError',
    'PdfBackendUnavailableError',
    'PdfError',
    'PdfExporter',
    'PdfResult',
    'SiteBuilder',
    'TemplateError',
    'Theme',
    'ThemeError',
    'ThemeRegistry',
    '__version__',
    'convert',
]


def convert(text: str, config: Config | None = None) -> str:
    """Convert Markdown ``text`` to a complete, styled HTML page.

    :param text: Markdown source.
    :param config: optional configuration (defaults to ``Config()``).
    :raises MdHtmlError: on invalid configuration or input.
    """
    try:
        return MarkdownConverter(config).convert_text(text)
    except MdHtmlError:
        raise
    except Exception as exc:
        raise BuildError(f'Could not convert Markdown: {exc}') from exc
