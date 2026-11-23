"""Assembles complete HTML documents from rendered Markdown bodies.

:class:`PageRenderer` combines the body HTML with the ``<head>`` (metadata, stylesheet),
optional header/footer/page number, table of contents, multi-page navigation and pager,
then fills the page template: a theme's own ``template.html``, or the built-in
``templates/paged.html`` (``layout.view: paged``, the document as a sheet of paper) or
``templates/page.html`` (``layout.view: web``).

Template placeholders (``string.Template`` syntax, all values are ready-to-use HTML):
see :data:`TEMPLATE_PLACEHOLDERS`. Use ``$$`` for a literal dollar sign.
"""

from __future__ import annotations

import html
import logging
import os
import string
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import __version__
from .config import Config
from .errors import MdHtmlError, TemplateError, ThemeError
from .frontpages import FrontPages
from .parser import Heading, ParsedDocument
from .printing import PrintLayout
from .templating import VariableTemplate
from .themes import Theme

__all__ = ['TEMPLATE_PLACEHOLDERS', 'NavEntry', 'PageContext', 'PageRenderer', 'TocBuilder']

logger = logging.getLogger(__name__)

TEMPLATE_PLACEHOLDERS: dict[str, str] = {
    'lang': 'language tag for <html lang>',
    'theme_mode': 'light | dark | auto (for <html data-theme>)',
    'toc_position': 'none | left | right | top (effective, after empty-TOC check)',
    'title': 'escaped page title',
    'head': 'contents of <head>: meta tags, <title>, stylesheet, extra head HTML',
    'body_class': 'space-separated classes for <body>',
    'header': '<header class="mdh-header"> or empty',
    'site_nav': '<nav class="mdh-site-nav"> (multi-page) or empty',
    'toc': '<aside class="mdh-toc"> or empty',
    'front_pages': 'cover page and printed contents page, or empty (prepended to the content '
    'when a template lacks it)',
    'content': 'rendered Markdown body',
    'pager': '<nav class="mdh-pager"> previous/next links or empty',
    'footer': '<footer class="mdh-footer"> (footer text and/or page number) or empty',
    'print_regions': 'hidden running header/footer copies for print (place right after <body>)',
}

TEMPLATES_DIR = Path(__file__).resolve().parent / 'templates'
DEFAULT_TEMPLATE_PATH = TEMPLATES_DIR / 'page.html'
VIEW_TEMPLATES: dict[str, Path] = {
    'web': DEFAULT_TEMPLATE_PATH,
    'paged': TEMPLATES_DIR / 'paged.html',
}
MATHJAX_URL = 'https://cdn.jsdelivr.net/npm/mathjax@3.2.2/es5/tex-chtml.js'


@dataclass(frozen=True)
class NavEntry:
    """A page in a multi-page build (for navigation and pager links)."""

    title: str
    output: Path


@dataclass
class PageContext:
    """Per-page information supplied by the builder.

    :ivar page: 1-based page number.
    :ivar total_pages: number of pages in the build.
    :ivar source: source Markdown path (``None`` for in-memory text).
    :ivar output: output HTML path (``None`` for in-memory rendering).
    :ivar nav: all pages, in order (empty for single pages).
    :ivar prev: previous page, if any.
    :ivar next: next page, if any.
    :ivar stylesheet_href: link to a shared stylesheet instead of inlining it.
    :ivar build_date: date used for ``{build_date}`` (and ``{date}`` fallback).
    :ivar cover_logo_src: cover logo URL, already resolved for the output location.
    """

    page: int = 1
    total_pages: int = 1
    source: Path | None = None
    output: Path | None = None
    nav: list[NavEntry] = field(default_factory=list)
    prev: NavEntry | None = None
    next: NavEntry | None = None
    stylesheet_href: str | None = None
    build_date: date = field(default_factory=date.today)
    cover_logo_src: str | None = None


class TocBuilder:
    """Builds a nested ``<ul>`` table of contents from headings.

    :param max_depth: deepest heading level to include.
    """

    def __init__(self, max_depth: int = 3) -> None:
        try:
            if not 1 <= max_depth <= 6:
                raise TemplateError(f'TOC depth must be between 1 and 6, got {max_depth}')
            self.max_depth = max_depth
        except MdHtmlError:
            raise
        except Exception as exc:
            raise TemplateError(f'Unexpected error in TocBuilder.__init__: {exc}') from exc

    def entries(self, headings: list[Heading]) -> list[Heading]:
        """Select headings for the TOC.

        Headings without anchors or deeper than ``max_depth`` are skipped. A single leading
        H1 (the page title) is omitted when other headings exist.
        """
        try:
            usable = [h for h in headings if h.anchor and h.text and h.level <= self.max_depth]
            h1s = [h for h in usable if h.level == 1]
            if len(h1s) == 1 and usable and usable[0] is h1s[0] and len(usable) > 1:
                usable = usable[1:]
            return usable
        except Exception as exc:
            raise TemplateError(f'Could not select TOC entries: {exc}') from exc

    def render(self, headings: list[Heading]) -> str:
        """Return nested ``<ul class="mdh-toc-list">`` markup ('' if nothing to show)."""
        try:
            items = self.entries(headings)
            if not items:
                return ''
            out: list[str] = []
            stack: list[int] = []
            for heading in items:
                level = heading.level
                if not stack:
                    out.append('<ul class="mdh-toc-list">')
                    stack.append(level)
                elif level > stack[-1]:
                    out.append('<ul>')
                    stack.append(level)
                else:
                    out.append('</li>')
                    while len(stack) > 1 and level <= stack[-2]:
                        out.append('</ul></li>')
                        stack.pop()
                anchor = html.escape(heading.anchor or '', quote=True)
                text = html.escape(heading.text)
                out.append(f'<li class="mdh-toc-h{level}"><a href="#{anchor}">{text}</a>')
            out.append('</li>')
            while len(stack) > 1:
                out.append('</ul></li>')
                stack.pop()
            out.append('</ul>')
            return '\n'.join(out)
        except TemplateError:
            raise
        except Exception as exc:
            raise TemplateError(f'Could not render TOC: {exc}') from exc


class PageRenderer:
    """Renders full HTML pages.

    :param config: configuration.
    :param theme: theme (its optional template replaces the built-in page skeleton).
    """

    BUILTIN_VARIABLES = frozenset(
        {'title', 'site_title', 'page', 'total_pages', 'build_date', 'year', 'filename'}
    )

    def __init__(self, config: Config, theme: Theme) -> None:
        try:
            self.config = config
            self.theme = theme
            self.template = string.Template(
                theme.template or self._default_template(config.layout.view)
            )
            self._check_template()
            self.toc_builder = TocBuilder(config.layout.toc_depth)
            self.header_template = VariableTemplate(config.header.template, 'header.template')
            self.footer_template = VariableTemplate(config.footer.template, 'footer.template')
            self.page_number_template = VariableTemplate(
                config.page_numbers.format, 'page_numbers.format'
            )
            self.print_layout = PrintLayout(config)
            self.front_pages = FrontPages(config)
        except (ThemeError, TemplateError):
            raise
        except Exception as exc:
            raise ThemeError(f'Could not initialise page renderer: {exc}') from exc

    @staticmethod
    def _default_template(view: str = 'web') -> str:
        """Read the built-in page template for ``view`` (``paged`` or ``web``)."""
        try:
            path = VIEW_TEMPLATES.get(view)
            if path is None:
                raise ThemeError(f'Unknown layout view {view!r}; expected {sorted(VIEW_TEMPLATES)}')
            return path.read_text(encoding='utf-8')
        except OSError as exc:
            raise ThemeError(f'Built-in page template missing: {exc}') from exc

    def _check_template(self) -> None:
        """Fail fast if the template uses unknown placeholders or invalid ``$`` syntax."""
        try:
            identifiers = set(self.template.get_identifiers())
            if not self.template.is_valid():
                raise ThemeError(
                    f'Theme {self.theme.name!r}: page template has an invalid "$" placeholder '
                    '(use $$ for a literal dollar sign)'
                )
            unknown = identifiers - set(TEMPLATE_PLACEHOLDERS)
            if unknown:
                raise ThemeError(
                    f'Theme {self.theme.name!r}: unknown template placeholders {sorted(unknown)}; '
                    f'allowed: {sorted(TEMPLATE_PLACEHOLDERS)}'
                )
            if 'content' not in identifiers:
                raise ThemeError(f'Theme {self.theme.name!r}: page template lacks ${{content}}')
        except ThemeError:
            raise
        except Exception as exc:
            raise ThemeError(f'Invalid page template: {exc}') from exc

    # ------------------------------------------------------------------ metadata

    def page_title(self, document: ParsedDocument, source: Path | None) -> str:
        """Resolve the page title: front matter, first H1, site title, filename, 'Untitled'."""
        try:
            meta_title = document.meta.get('title')
            if meta_title is not None and str(meta_title).strip():
                return str(meta_title).strip()
            if document.first_h1:
                return document.first_h1
            if self.config.title:
                return self.config.title
            if source is not None:
                return source.stem
            return 'Untitled'
        except Exception as exc:
            raise TemplateError(f'Could not determine page title: {exc}') from exc

    def _format_date(self, value: Any, fallback: date) -> str:
        """Format a front-matter date (ISO string) or ``fallback`` with ``date_format``.

        A value that is not an ISO date is used verbatim (e.g. ``date: Spring 2024``).
        """
        try:
            fmt = self.config.build.date_format
            if value is None or value == '':
                return fallback.strftime(fmt)
            text = str(value)
            try:
                return datetime.fromisoformat(text).strftime(fmt)
            except ValueError:
                logger.debug(f'Front matter date {text!r} is not ISO formatted; using it verbatim')
                return text
        except MdHtmlError:
            raise
        except Exception as exc:
            raise TemplateError(f'Could not format date {value!r}: {exc}') from exc

    def variables(self, document: ParsedDocument, context: PageContext) -> dict[str, Any]:
        """Return the template variables for header/footer/page-number templates."""
        try:
            title = self.page_title(document, context.source)
            meta = document.meta
            variables: dict[str, Any] = {
                'author': self.config.author or '',
                'description': self.config.description or '',
            }
            for key, value in meta.items():
                if key.isidentifier() and key not in self.BUILTIN_VARIABLES:
                    if isinstance(value, list):
                        value = ', '.join(str(v) for v in value)
                    elif isinstance(value, dict):
                        continue
                    variables[key] = '' if value is None else value
            variables.update(
                {
                    'title': title,
                    'site_title': self.config.title or title,
                    'date': self._format_date(meta.get('date'), context.build_date),
                    'build_date': context.build_date.strftime(self.config.build.date_format),
                    'year': context.build_date.year,
                    'page': context.page,
                    'total_pages': context.total_pages,
                    'filename': context.source.name if context.source else '',
                }
            )
            return variables
        except TemplateError:
            raise
        except Exception as exc:
            raise TemplateError(f'Could not compute template variables: {exc}') from exc

    # ------------------------------------------------------------------ regions

    def _head(
        self,
        title: str,
        document: ParsedDocument,
        context: PageContext,
        css: str,
        body: str,
        region_css: str = '',
    ) -> str:
        """Build the contents of ``<head>``."""
        try:
            meta = document.meta
            lines = [
                '<meta charset="utf-8">',
                '<meta name="viewport" content="width=device-width, initial-scale=1">',
                f'<meta name="generator" content="md-html-builder {__version__}">',
            ]
            scheme = {'light': 'light', 'dark': 'dark', 'auto': 'light dark'}[
                self.config.style.mode
            ]
            lines.append(f'<meta name="color-scheme" content="{scheme}">')
            description = meta.get('description') or self.config.description
            author = meta.get('author') or self.config.author
            keywords = meta.get('keywords')
            if isinstance(keywords, list):
                keywords = ', '.join(str(k) for k in keywords)
            for name, value in (
                ('description', description),
                ('author', author),
                ('keywords', keywords),
            ):
                if value:
                    lines.append(
                        f'<meta name="{name}" content="{html.escape(str(value), quote=True)}">'
                    )
            lines.append(f'<title>{html.escape(title)}</title>')
            lines.extend(self._font_links())
            if context.stylesheet_href:
                href = html.escape(context.stylesheet_href, quote=True)
                lines.append(f'<link rel="stylesheet" href="{href}">')
            else:
                safe_css = css.replace('</style', '<\\/style')
                lines.append(f'<style id="mdh-styles">\n{safe_css}\n</style>')
            if region_css:
                safe_region = region_css.replace('</style', '<\\/style')
                lines.append(f'<style id="mdh-print-regions" media="print">\n{safe_region}</style>')
            if self.config.markdown.math_renderer == 'mathjax' and 'class="math ' in body:
                lines.append(f'<script defer src="{MATHJAX_URL}"></script>')
            if self.config.head_html:
                lines.append(self.config.head_html)
            return '\n'.join(lines)
        except MdHtmlError:
            raise
        except Exception as exc:
            raise TemplateError(f'Unexpected error in PageRenderer._head: {exc}') from exc

    def _font_links(self) -> list[str]:
        """``<link>`` tags for configured web-font stylesheets (e.g. Google Fonts)."""
        try:
            urls = self.config.style.font_url
            if not urls:
                return []
            lines: list[str] = []
            if any(url.startswith('https://fonts.googleapis.com/') for url in urls):
                lines.append('<link rel="preconnect" href="https://fonts.googleapis.com">')
                lines.append('<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>')
            for url in urls:
                lines.append(f'<link rel="stylesheet" href="{html.escape(url, quote=True)}">')
            return lines
        except MdHtmlError:
            raise
        except Exception as exc:
            raise TemplateError(f'Unexpected error in PageRenderer._font_links: {exc}') from exc

    def _toc(self, document: ParsedDocument) -> str:
        """Build the TOC aside ('' when disabled or empty)."""
        try:
            position = self.config.layout.toc
            if position == 'none' or document.meta.get('toc') is False:
                return ''
            items = self.toc_builder.render(document.headings)
            if not items:
                return ''
            title = html.escape(self.config.layout.toc_title)
            return (
                f'<aside class="mdh-toc mdh-toc-{position}">\n'
                f'<nav aria-label="{html.escape(self.config.layout.toc_title, quote=True)}">\n'
                f'<p class="mdh-toc-title">{title}</p>\n{items}\n</nav>\n</aside>'
            )
        except MdHtmlError:
            raise
        except Exception as exc:
            raise TemplateError(f'Unexpected error in PageRenderer._toc: {exc}') from exc

    @staticmethod
    def _href(target: Path, context: PageContext) -> str:
        """Relative URL from the current page to ``target``."""
        try:
            if context.output is None:
                rel = target.name
            else:
                rel = Path(os.path.relpath(target, context.output.parent)).as_posix()
            # Percent-encode so names containing '#', '?', '%' or spaces stay one path.
            return html.escape(quote(rel, safe='/'), quote=True)
        except MdHtmlError:
            raise
        except Exception as exc:
            raise TemplateError(f'Unexpected error in PageRenderer._href: {exc}') from exc

    def _site_nav(self, context: PageContext) -> str:
        """Build the multi-page navigation list."""
        try:
            if not (self.config.layout.navigation and context.total_pages > 1 and context.nav):
                return ''
            items = []
            for index, entry in enumerate(context.nav, start=1):
                title = html.escape(entry.title)
                href = self._href(entry.output, context)
                if index == context.page:
                    items.append(
                        f'<li class="is-current">'
                        f'<a href="{href}" aria-current="page">{title}</a></li>'
                    )
                else:
                    items.append(f'<li><a href="{href}">{title}</a></li>')
            site = html.escape(self.config.title, quote=False) if self.config.title else ''
            heading = f'<p class="mdh-site-nav-title">{site}</p>\n' if site else ''
            return (
                '<nav class="mdh-site-nav" aria-label="Pages">\n<div class="mdh-site-nav-inner">\n'
                f'{heading}<ul class="mdh-site-nav-list">\n'
                + '\n'.join(items)
                + '\n</ul>\n</div>\n</nav>'
            )
        except MdHtmlError:
            raise
        except Exception as exc:
            raise TemplateError(f'Unexpected error in PageRenderer._site_nav: {exc}') from exc

    def _pager(self, context: PageContext) -> str:
        """Build previous/next links."""
        try:
            if not (self.config.layout.navigation and context.total_pages > 1):
                return ''
            parts = []
            if context.prev:
                prev_href = self._href(context.prev.output, context)
                parts.append(
                    f'<a class="mdh-pager-prev" rel="prev" href="{prev_href}">'
                    f'<span class="mdh-pager-label">Previous</span> '
                    f'<span class="mdh-pager-title">{html.escape(context.prev.title)}</span></a>'
                )
            if context.next:
                next_href = self._href(context.next.output, context)
                parts.append(
                    f'<a class="mdh-pager-next" rel="next" href="{next_href}">'
                    f'<span class="mdh-pager-label">Next</span> '
                    f'<span class="mdh-pager-title">{html.escape(context.next.title)}</span></a>'
                )
            if not parts:
                return ''
            return (
                '<nav class="mdh-pager" aria-label="Pagination">\n' + '\n'.join(parts) + '\n</nav>'
            )
        except MdHtmlError:
            raise
        except Exception as exc:
            raise TemplateError(f'Unexpected error in PageRenderer._pager: {exc}') from exc

    def _header_text(self, variables: Mapping[str, Any], document: ParsedDocument) -> str | None:
        """Rendered header template, or ``None`` when the header is disabled."""
        try:
            if not self.config.header.enabled or document.meta.get('header') is False:
                return None
            return self.header_template.render(variables)
        except MdHtmlError:
            raise
        except Exception as exc:
            raise TemplateError(f'Unexpected error in PageRenderer._header_text: {exc}') from exc

    def _footer_text(self, variables: Mapping[str, Any], document: ParsedDocument) -> str | None:
        """Rendered footer template, or ``None`` when the footer is disabled."""
        try:
            if not self.config.footer.enabled or document.meta.get('footer') is False:
                return None
            return self.footer_template.render(variables)
        except MdHtmlError:
            raise
        except Exception as exc:
            raise TemplateError(f'Unexpected error in PageRenderer._footer_text: {exc}') from exc

    @staticmethod
    def _header(text: str | None) -> str:
        """Build the header region from the rendered header text."""
        try:
            if text is None:
                return ''
            return (
                '<header class="mdh-header">\n'
                f'<div class="mdh-header-inner">{text}</div>\n</header>'
            )
        except MdHtmlError:
            raise
        except Exception as exc:
            raise TemplateError(f'Unexpected error in PageRenderer._header: {exc}') from exc

    def _footer(self, variables: Mapping[str, Any], text: str | None) -> str:
        """Build the footer region (footer text and/or page number)."""
        try:
            show_number = self.config.page_numbers.enabled
            if text is None and not show_number:
                return ''
            parts = []
            if text is not None:
                parts.append(f'<div class="mdh-footer-text">{text}</div>')
            if show_number:
                number = self.page_number_template.render(variables)
                parts.append(f'<div class="mdh-page-number">{number}</div>')
            inner = '\n'.join(parts)
            return (
                f'<footer class="mdh-footer">\n<div class="mdh-footer-inner">\n{inner}\n</div>\n'
                '</footer>'
            )
        except MdHtmlError:
            raise
        except Exception as exc:
            raise TemplateError(f'Unexpected error in PageRenderer._footer: {exc}') from exc

    # ------------------------------------------------------------------ page

    def render(
        self, document: ParsedDocument, body: str, context: PageContext, css: str = ''
    ) -> str:
        """Render a complete HTML document.

        :param document: parsed document (meta, headings).
        :param body: rendered Markdown body HTML.
        :param context: page/build context.
        :param css: stylesheet text to inline (ignored when ``context.stylesheet_href`` is set).
        :raises TemplateError: on template variable errors.
        :raises ThemeError: on page template errors.
        """
        try:
            title = self.page_title(document, context.source)
            variables = self.variables(document, context)
            toc = self._toc(document)
            toc_position = self.config.layout.toc if toc else 'none'
            site_nav = self._site_nav(context)
            header_text = self._header_text(variables, document)
            footer_text = self._footer_text(variables, document)
            classes = [
                'mdh-body',
                f'mdh-view-{self.config.layout.view}',
                f'mdh-toc-{toc_position}',
                f'mdh-align-{self.config.layout.align}',
            ]
            if self.config.page_numbers.enabled:
                classes.append(f'mdh-pagenum-{self.config.page_numbers.position}')
            if site_nav:
                classes.append('mdh-has-nav')
            front_pages, front_classes = self.front_pages.render(
                document, variables, context.cover_logo_src
            )
            classes.extend(front_classes)
            content = body.strip()
            if front_pages and 'front_pages' not in self.template.get_identifiers():
                content = f'{front_pages}\n{content}'
            lang = str(document.meta.get('lang') or self.config.lang)
            values = {
                'lang': html.escape(lang, quote=True),
                'theme_mode': self.config.style.mode,
                'toc_position': toc_position,
                'title': html.escape(title),
                'head': self._head(
                    title,
                    document,
                    context,
                    css,
                    body,
                    self.print_layout.region_css(header_text, footer_text),
                ),
                'body_class': ' '.join(classes),
                'header': self._header(header_text),
                'site_nav': site_nav,
                'toc': toc,
                'front_pages': front_pages,
                'content': content,
                'pager': self._pager(context),
                'footer': self._footer(variables, footer_text),
                'print_regions': self.print_layout.regions_html(header_text, footer_text),
            }
            return self.template.substitute(values)
        except MdHtmlError:
            raise
        except (KeyError, ValueError) as exc:
            raise ThemeError(f'Page template error in theme {self.theme.name!r}: {exc}') from exc
        except Exception as exc:
            raise TemplateError(f'Could not render page: {exc}') from exc
