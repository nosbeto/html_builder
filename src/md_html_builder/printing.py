"""Print / PDF page layout: ``@page`` rules, running header/footer and page numbers.

The same HTML is used for the browser, for "Save as PDF" and for the PDF exporters, so
everything print-specific is expressed in CSS:

* :meth:`PrintLayout.stylesheet` (shared by all pages) sets the paper size and margins from
  :class:`~md_html_builder.config.PageConfig`, puts the page number into a bottom margin box
  with ``counter(page)`` / ``counter(pages)`` and keeps backgrounds (code colours) in print.
* :meth:`PrintLayout.region_css` (per page, emitted in ``<head>``) places the header and the
  footer text into the top/bottom page margins. Each margin box gets two ``content``
  declarations: a plain-text string, which Chromium-based browsers use, followed by
  ``element(...)``, which engines that support running elements (WeasyPrint) use instead,
  keeping the header's formatting.
* :meth:`PrintLayout.cover_page_css` empties every margin box of the named page
  ``mdh-cover`` (the cover page). It is emitted after the rules that fill those boxes
  (in both stylesheets above) because Chromium resolves margin boxes by source order rather
  than by page-selector specificity.
* :meth:`PrintLayout.regions_html` returns hidden copies of the header/footer placed at the
  start of ``<body>`` (``${print_regions}`` in the page template). Running elements must
  appear before the content so they repeat on every page; on screen they are not displayed
  and they are ``aria-hidden`` so assistive technology never reads them twice.

Browsers without margin-box support (Firefox, Safari at the time of writing) print the
content cleanly but without the running header, footer and page numbers.
"""

from __future__ import annotations

import html
import logging
import re
import string
from typing import ClassVar

from .config import Config
from .errors import ConfigError, MdHtmlError, TemplateError

__all__ = ['PrintLayout']

logger = logging.getLogger(__name__)

_TAG = re.compile(r'<[^>]*>')
_SPACE = re.compile(r'\s+')


class PrintLayout:
    """Builds the print-specific CSS and markup for a configuration.

    :param config: full configuration (``page``, ``page_numbers``, ``header``, ``footer``).
    """

    HEADER_BOX: ClassVar[str] = 'top-left'
    COVER_PAGE: ClassVar[str] = 'mdh-cover'
    MARGIN_BOXES: ClassVar[tuple[str, ...]] = (
        'top-left',
        'top-center',
        'top-right',
        'bottom-left',
        'bottom-center',
        'bottom-right',
    )
    HEADER_RUNNING: ClassVar[str] = 'mdh-running-header'
    FOOTER_RUNNING: ClassVar[str] = 'mdh-running-footer'

    def __init__(self, config: Config) -> None:
        try:
            if not isinstance(config, Config):
                raise ConfigError(f'PrintLayout needs a Config, got {type(config).__name__}')
            self.config = config
        except MdHtmlError:
            raise
        except Exception as exc:
            raise ConfigError(f'Unexpected error in PrintLayout.__init__: {exc}') from exc

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def css_string(text: str) -> str:
        """Quote ``text`` as a CSS string literal (safe inside a ``<style>`` element)."""
        try:
            escaped = (
                text.replace('\\', '\\\\')
                .replace('"', '\\"')
                .replace('\n', '\\a ')
                .replace('<', '\\3c ')
                .replace('>', '\\3e ')
            )
            return f'"{escaped}"'
        except Exception as exc:
            raise TemplateError(f'Could not quote CSS string {text!r}: {exc}') from exc

    @staticmethod
    def plain_text(fragment: str) -> str:
        """Reduce an HTML fragment to collapsed plain text (tags removed, entities decoded)."""
        try:
            text = html.unescape(_TAG.sub(' ', fragment))
            return _SPACE.sub(' ', text).strip()
        except Exception as exc:
            raise TemplateError(f'Could not convert {fragment!r} to text: {exc}') from exc

    @staticmethod
    def _mm(value: float) -> str:
        """Format a millimetre length without a trailing ``.0``."""
        try:
            return f'{value:g}mm'
        except MdHtmlError:
            raise
        except Exception as exc:
            raise ConfigError(f'Unexpected error in PrintLayout._mm: {exc}') from exc

    # ------------------------------------------------------------------ geometry

    def page_size(self) -> str:
        """Return the CSS ``size`` value, e.g. ``210mm 297mm``."""
        try:
            width, height = self.config.page.dimensions_mm()
            return f'{self._mm(width)} {self._mm(height)}'
        except ConfigError:
            raise
        except Exception as exc:
            raise ConfigError(f'Could not compute the page size: {exc}') from exc

    def page_variables(self) -> dict[str, str]:
        """Custom properties describing the sheet for the on-screen ``paged`` view."""
        try:
            width, height = self.config.page.dimensions_mm()
            top, right, bottom, left = self.config.page.margins()
            return {
                '--mdh-page-width': self._mm(width),
                '--mdh-page-height': self._mm(height),
                '--mdh-page-margin-top': top,
                '--mdh-page-margin-right': right,
                '--mdh-page-margin-bottom': bottom,
                '--mdh-page-margin-left': left,
            }
        except ConfigError:
            raise
        except Exception as exc:
            raise ConfigError(f'Could not compute page variables: {exc}') from exc

    def number_box(self) -> str:
        """Margin box that holds the printed page number (``bottom-<position>``)."""
        try:
            return f'bottom-{self.config.page_numbers.position}'
        except MdHtmlError:
            raise
        except Exception as exc:
            raise ConfigError(f'Unexpected error in PrintLayout.number_box: {exc}') from exc

    def footer_box(self) -> str:
        """Margin box for the footer text: the bottom corner not used by the page number."""
        try:
            numbers = self.config.page_numbers
            if numbers.enabled and numbers.print and numbers.position == 'left':
                return 'bottom-right'
            return 'bottom-left'
        except MdHtmlError:
            raise
        except Exception as exc:
            raise ConfigError(f'Unexpected error in PrintLayout.footer_box: {exc}') from exc

    # ------------------------------------------------------------------ CSS

    def counter_content(self) -> str:
        """Translate ``page_numbers.format`` into a CSS ``content`` value using counters."""
        try:
            pieces: list[str] = []
            for literal, field_name, _spec, _conv in string.Formatter().parse(
                self.config.page_numbers.format
            ):
                if literal:
                    pieces.append(self.css_string(literal))
                if field_name is None:
                    continue
                if field_name == 'page':
                    pieces.append('counter(page)')
                elif field_name == 'total_pages':
                    pieces.append('counter(pages)')
                else:
                    logger.debug(f'Page number variable {{{field_name}}} ignored in print CSS')
            return ' '.join(pieces) or 'counter(page)'
        except Exception as exc:
            raise ConfigError(f'Invalid page_numbers.format for print CSS: {exc}') from exc

    def cover_page_css(self) -> str:
        """``@page mdh-cover`` rule that empties every margin box on the cover page."""
        try:
            boxes = ' '.join(f'@{box} {{ content: none; }}' for box in self.MARGIN_BOXES)
            return f'@page {self.COVER_PAGE} {{ {boxes} }}\n'
        except MdHtmlError:
            raise
        except Exception as exc:
            raise ConfigError(f'Unexpected error in PrintLayout.cover_page_css: {exc}') from exc

    def stylesheet(self) -> str:
        """Return the shared print CSS: page box, page-number counter and print tweaks."""
        try:
            page = self.config.page
            numbers = self.config.page_numbers
            lines = ['@page {', f'  size: {self.page_size()};', f'  margin: {page.margin};']
            if numbers.enabled and numbers.print:
                lines.append(f'  @{self.number_box()} {{ content: {self.counter_content()}; }}')
            lines.append('}')
            lines.append('@media print {')
            lines.append('  html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }')
            if numbers.enabled and numbers.print:
                lines.append('  .mdh-page-number { display: none; }')
            if page.print_link_urls:
                lines.append(
                    '  .markdown-body a[href^="http"]:not(.header-anchor)::after '
                    '{ content: " (" attr(href) ")"; font-size: 0.85em; '
                    'color: var(--mdh-color-muted); word-break: break-all; }'
                )
            lines.append('}')
            return '\n'.join(lines) + '\n' + self.cover_page_css()
        except ConfigError:
            raise
        except Exception as exc:
            raise ConfigError(f'Could not build print CSS: {exc}') from exc

    def region_css(self, header_html: str | None, footer_html: str | None) -> str:
        """Per-page ``@page`` margin boxes for the running header and footer.

        :param header_html: rendered header template (``None`` when the header is off).
        :param footer_html: rendered footer template (``None`` when the footer is off).
        :return: CSS text, or ``''`` when there is nothing to place.
        """
        try:
            boxes: list[str] = []
            if header_html is not None:
                text = self.css_string(self.plain_text(header_html))
                boxes.append(
                    f'  @{self.HEADER_BOX} {{ content: {text}; '
                    f'content: element({self.HEADER_RUNNING}); width: 100%; }}'
                )
            if footer_html is not None:
                text = self.css_string(self.plain_text(footer_html))
                boxes.append(
                    f'  @{self.footer_box()} {{ content: {text}; '
                    f'content: element({self.FOOTER_RUNNING}); }}'
                )
            if not boxes:
                return ''
            return '@page {\n' + '\n'.join(boxes) + '\n}\n' + self.cover_page_css()
        except TemplateError:
            raise
        except Exception as exc:
            raise TemplateError(f'Could not build running header/footer CSS: {exc}') from exc

    def regions_html(self, header_html: str | None, footer_html: str | None) -> str:
        """Hidden running copies of the header/footer for print (``''`` if both are off)."""
        try:
            parts: list[str] = []
            if header_html is not None:
                parts.append(
                    f'<div class="{self.HEADER_RUNNING}">'
                    f'<div class="mdh-header-inner">{header_html}</div></div>'
                )
            if footer_html is not None:
                parts.append(
                    f'<div class="{self.FOOTER_RUNNING}">'
                    f'<div class="mdh-footer-text">{footer_html}</div></div>'
                )
            if not parts:
                return ''
            inner = '\n'.join(parts)
            return f'<div class="mdh-print-regions" aria-hidden="true">\n{inner}\n</div>'
        except Exception as exc:
            raise TemplateError(f'Could not build print regions: {exc}') from exc
