"""Front pages: an optional cover page and an optional printed contents page.

Both are placed before the document body (``${front_pages}`` in the page template) and each
ends with a page break when printed:

* :class:`CoverPage` renders ``section.mdh-cover`` from :class:`~md_html_builder.config.CoverConfig`
  merged with the page's front matter (``cover: true/false`` or a ``cover:`` mapping). The
  stylesheet puts it on the named page ``mdh-cover``, whose margin boxes are empty, so the
  printed cover has no running header, footer or page number.
* :class:`ContentsPage` renders ``nav.mdh-contents-page``: one link per heading down to
  ``contents_page.depth``. When ``contents_page.page_numbers`` is on, the print stylesheet
  adds a dotted leader and ``target-counter()`` page number to each entry (renderers without
  ``target-counter()`` support, such as Chrome, show the links only).
* :class:`FrontPages` combines the two for :class:`~md_html_builder.renderer.PageRenderer`.

All text is plain text and is HTML-escaped.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Mapping
from datetime import date
from typing import Any, ClassVar

from .config import Config, CoverConfig
from .errors import ConfigError, InputError, MdHtmlError, TemplateError
from .parser import ParsedDocument

__all__ = ['ContentsPage', 'CoverPage', 'FrontPages']

logger = logging.getLogger(__name__)


class CoverPage:
    """Builds the cover page for a document.

    :param config: full configuration (``cover`` and root ``author``).
    """

    FRONT_MATTER_KEY: ClassVar[str] = 'cover'

    def __init__(self, config: Config) -> None:
        try:
            if not isinstance(config, Config):
                raise ConfigError(f'CoverPage needs a Config, got {type(config).__name__}')
            self.config = config
        except MdHtmlError:
            raise
        except Exception as exc:
            raise ConfigError(f'Unexpected error in CoverPage.__init__: {exc}') from exc

    def settings(self, document: ParsedDocument) -> CoverConfig | None:
        """Effective cover settings for ``document``, or ``None`` when there is no cover.

        Front matter ``cover: false`` / ``true`` switches the cover off / on; a ``cover:``
        mapping overrides individual fields and switches it on unless it sets
        ``enabled: false``.

        :raises InputError: if the front matter value is not a boolean or a valid mapping.
        """
        try:
            base = self.config.cover
            value = document.meta.get(self.FRONT_MATTER_KEY)
            if value is None:
                return base if base.enabled else None
            if isinstance(value, bool):
                if not value:
                    return None
                return CoverConfig.from_dict({**base.to_dict(), 'enabled': True})
            if isinstance(value, dict):
                value = dict(value)
                if isinstance(value.get('date'), date):  # YAML reads 2024-05-01 as a date
                    value['date'] = value['date'].strftime(self.config.build.date_format)
                merged = {**base.to_dict(), 'enabled': True, **value}
                settings = CoverConfig.from_dict(merged, 'cover')
                return settings if settings.enabled else None
            raise InputError(
                f'{document.source}: front matter "cover" must be true, false or a mapping, '
                f'got {type(value).__name__}'
            )
        except ConfigError as exc:
            raise InputError(f'{document.source}: invalid front matter: {exc}') from exc
        except MdHtmlError:
            raise
        except Exception as exc:
            raise InputError(f'{document.source}: could not read the cover: {exc}') from exc

    def authors(self, settings: CoverConfig, document: ParsedDocument) -> list[str]:
        """Author lines: ``cover.authors``, else front matter ``author``, else root ``author``."""
        try:
            if settings.authors:
                return list(settings.authors)
            meta_author = document.meta.get('author')
            if isinstance(meta_author, list):
                return [str(name) for name in meta_author if str(name).strip()]
            if meta_author is not None and str(meta_author).strip():
                return [str(meta_author)]
            if self.config.author:
                return [self.config.author]
            return []
        except MdHtmlError:
            raise
        except Exception as exc:
            raise TemplateError(f'Could not determine the cover authors: {exc}') from exc

    @staticmethod
    def _lines(css_class: str, lines: list[str]) -> str:
        """A ``<ul>`` of escaped lines ('' when empty)."""
        try:
            if not lines:
                return ''
            items = ''.join(f'<li>{html.escape(line)}</li>' for line in lines)
            return f'<ul class="{css_class}">{items}</ul>'
        except Exception as exc:
            raise TemplateError(f'Could not render the cover {css_class} lines: {exc}') from exc

    def render(
        self,
        settings: CoverConfig,
        document: ParsedDocument,
        variables: Mapping[str, Any],
        logo_src: str | None = None,
    ) -> str:
        """Return the cover ``<section>``.

        :param settings: effective settings from :meth:`settings`.
        :param document: the page (for the author fallback).
        :param variables: page template variables (``title`` and ``date`` are the defaults).
        :param logo_src: final URL of the logo (already resolved for the output location).
        """
        try:
            title = settings.title or str(variables.get('title') or '')
            if not title.strip():
                raise TemplateError('The cover needs a title: set cover.title or a page title')
            date_text = settings.date
            if date_text is None:
                date_text = str(variables.get('date', ''))
            main = []
            if logo_src:
                main.append(
                    f'<img class="mdh-cover-logo" src="{html.escape(logo_src, quote=True)}" alt="">'
                )
            main.append(f'<p class="mdh-cover-title">{html.escape(title)}</p>')
            if settings.subtitle:
                main.append(f'<p class="mdh-cover-subtitle">{html.escape(settings.subtitle)}</p>')
            meta = [
                self._lines('mdh-cover-authors', self.authors(settings, document)),
                self._lines('mdh-cover-details', settings.details),
            ]
            if date_text.strip():
                meta.append(f'<p class="mdh-cover-date">{html.escape(date_text)}</p>')
            meta_html = '\n'.join(part for part in meta if part)
            return (
                '<section class="mdh-cover" aria-label="Cover">\n'
                '<div class="mdh-cover-main">\n' + '\n'.join(main) + '\n</div>\n'
                f'<div class="mdh-cover-meta">\n{meta_html}\n</div>\n</section>'
            )
        except MdHtmlError:
            raise
        except Exception as exc:
            raise TemplateError(f'Could not render the cover page: {exc}') from exc


class ContentsPage:
    """Builds the printed contents page.

    :param config: full configuration (``contents_page``).
    """

    FRONT_MATTER_KEY: ClassVar[str] = 'contents_page'

    def __init__(self, config: Config) -> None:
        try:
            if not isinstance(config, Config):
                raise ConfigError(f'ContentsPage needs a Config, got {type(config).__name__}')
            # Imported here: renderer.py imports this module.
            from .renderer import TocBuilder

            self.config = config
            self.toc_builder = TocBuilder(config.contents_page.depth)
        except MdHtmlError:
            raise
        except Exception as exc:
            raise ConfigError(f'Unexpected error in ContentsPage.__init__: {exc}') from exc

    def enabled(self, document: ParsedDocument) -> bool:
        """Whether ``document`` gets a contents page (front matter overrides the config).

        :raises InputError: if front matter ``contents_page`` is not a boolean.
        """
        try:
            value = document.meta.get(self.FRONT_MATTER_KEY)
            if value is None:
                return self.config.contents_page.enabled
            if not isinstance(value, bool):
                raise InputError(
                    f'{document.source}: front matter "contents_page" must be true or false, '
                    f'got {value!r}'
                )
            return value
        except MdHtmlError:
            raise
        except Exception as exc:
            raise InputError(f'{document.source}: could not read contents_page: {exc}') from exc

    def render(self, document: ParsedDocument) -> str:
        """Return the contents ``<nav>``, or '' when the document has no usable headings."""
        try:
            entries = self.toc_builder.entries(document.headings)
            if not entries:
                logger.warning(f'{document.source}: contents page skipped, no headings to list')
                return ''
            settings = self.config.contents_page
            top = min(heading.level for heading in entries)
            items = []
            for heading in entries:
                anchor = html.escape(heading.anchor or '', quote=True)
                indent = heading.level - top
                items.append(
                    f'<li class="mdh-contents-h{heading.level} mdh-contents-indent-{indent}">'
                    f'<a href="#{anchor}">{html.escape(heading.text)}</a></li>'
                )
            classes = 'mdh-contents-page'
            if settings.page_numbers:
                classes += ' mdh-contents-numbered'
            title = html.escape(settings.title)
            return (
                f'<nav class="{classes}" aria-label="{html.escape(settings.title, quote=True)}">\n'
                f'<p class="mdh-contents-title">{title}</p>\n'
                f'<ul class="mdh-contents-list">\n' + '\n'.join(items) + '\n</ul>\n</nav>'
            )
        except MdHtmlError:
            raise
        except Exception as exc:
            raise TemplateError(f'Could not render the contents page: {exc}') from exc


class FrontPages:
    """Cover page and contents page together.

    :param config: full configuration.
    """

    def __init__(self, config: Config) -> None:
        try:
            self.cover = CoverPage(config)
            self.contents = ContentsPage(config)
        except MdHtmlError:
            raise
        except Exception as exc:
            raise ConfigError(f'Unexpected error in FrontPages.__init__: {exc}') from exc

    def logo(self, document: ParsedDocument) -> str | None:
        """The cover logo as configured (unresolved), or ``None``."""
        try:
            settings = self.cover.settings(document)
            return settings.logo if settings is not None else None
        except MdHtmlError:
            raise
        except Exception as exc:
            raise InputError(f'{document.source}: could not read the cover logo: {exc}') from exc

    def render(
        self,
        document: ParsedDocument,
        variables: Mapping[str, Any],
        logo_src: str | None = None,
    ) -> tuple[str, list[str]]:
        """Return ``(html, body classes)`` for the front pages ('' and [] when none)."""
        try:
            parts: list[str] = []
            classes: list[str] = []
            settings = self.cover.settings(document)
            if settings is not None:
                parts.append(self.cover.render(settings, document, variables, logo_src))
                classes.append('mdh-has-cover')
            if self.contents.enabled(document):
                contents = self.contents.render(document)
                if contents:
                    parts.append(contents)
                    classes.append('mdh-has-contents-page')
            return '\n'.join(parts), classes
        except MdHtmlError:
            raise
        except Exception as exc:
            raise TemplateError(f'Could not render the front pages: {exc}') from exc
