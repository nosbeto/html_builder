"""Markdown parsing and body rendering built on markdown-it-py.

Supported syntax (CommonMark + extensions):

* GFM tables (with column alignment), strikethrough (``~~x~~`` / ``~x~``), task lists,
  GitHub alerts (``> [!NOTE]``), autolinks for bare URLs and e-mail addresses
* footnotes (``[^1]`` and inline ``^[note]``), definition lists
* YAML front matter (``---`` block at the top of the file)
* heading ``id`` anchors (used by the table of contents), optional permalinks
* ``$inline$`` / ``$$block$$`` math and ````math`` fences (passthrough or MathJax)
* fenced / indented code blocks highlighted by :class:`~md_html_builder.highlighter.CodeHighlighter`
* raw HTML passthrough (disabled in safe mode)
* figures: an image with a title that stands alone in a paragraph becomes
  ``<figure class="mdh-figure"><img ...><figcaption>title</figcaption></figure>``

Parsing (:meth:`MarkdownParser.parse`) and rendering (:meth:`MarkdownParser.render`) are
separate steps so the builder can rewrite link/image tokens in between.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import yaml
from markdown_it import MarkdownIt
from markdown_it.token import Token
from mdit_py_plugins.anchors import anchors_plugin
from mdit_py_plugins.deflist import deflist_plugin
from mdit_py_plugins.dollarmath import dollarmath_plugin
from mdit_py_plugins.footnote import footnote_plugin
from mdit_py_plugins.front_matter import front_matter_plugin
from mdit_py_plugins.gfm_autolink import gfm_autolink_plugin

from .config import MarkdownConfig
from .errors import InputError, MdHtmlError
from .highlighter import CodeHighlighter

__all__ = ['FrontMatterParser', 'Heading', 'MarkdownParser', 'ParsedDocument']

#: Tables with at least this many columns get compact print styles.
WIDE_TABLE_COLUMNS = 7
#: Tables with at least this many columns get the most compact print styles.
XWIDE_TABLE_COLUMNS = 11

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Heading:
    """A heading found in a document.

    :ivar level: 1-6.
    :ivar text: plain text (formatting stripped, not escaped).
    :ivar anchor: the heading's ``id`` attribute, or ``None`` if anchors are disabled.
    """

    level: int
    text: str
    anchor: str | None


@dataclass
class ParsedDocument:
    """Result of parsing one Markdown source.

    :ivar tokens: markdown-it token stream (may be mutated before rendering).
    :ivar env: markdown-it environment (holds footnotes/references; reuse when rendering).
    :ivar meta: front-matter mapping (empty if none).
    :ivar headings: all headings in document order.
    :ivar source: label of the source (file path or ``<string>``) for messages.
    """

    tokens: list[Token]
    env: dict[str, Any]
    meta: dict[str, Any] = field(default_factory=dict)
    headings: list[Heading] = field(default_factory=list)
    source: str = '<string>'

    @staticmethod
    def count_columns(tokens: list[Token], start: int) -> int:
        """Number of cells in the first row of the table opened at ``tokens[start]``."""
        try:
            count = 0
            for token in tokens[start + 1 :]:
                if token.type in ('th_open', 'td_open'):
                    count += 1
                elif token.type in ('tr_close', 'table_close'):
                    break
            return count
        except Exception as exc:
            raise InputError(f'Could not count table columns: {exc}') from exc

    @property
    def table_columns(self) -> list[int]:
        """Column count of every table in the document, in order."""
        try:
            return [
                self.count_columns(self.tokens, index)
                for index, token in enumerate(self.tokens)
                if token.type == 'table_open'
            ]
        except MdHtmlError:
            raise
        except Exception as exc:
            raise InputError(f'Could not inspect tables in {self.source}: {exc}') from exc

    @property
    def first_h1(self) -> str | None:
        """Text of the first level-1 heading, if any."""
        try:
            return next((h.text for h in self.headings if h.level == 1 and h.text), None)
        except MdHtmlError:
            raise
        except Exception as exc:
            raise InputError(f'Unexpected error in ParsedDocument.first_h1: {exc}') from exc


class FrontMatterParser:
    """Parses YAML front matter into a mapping with JSON-friendly values."""

    def parse(self, content: str, source: str) -> dict[str, Any]:
        """Parse front matter ``content``.

        Dates are converted to ISO strings. An empty block gives ``{}``.

        :raises InputError: if the YAML is malformed or is not a mapping.
        """
        try:
            data = yaml.safe_load(content) if content.strip() else {}
            if data is None:
                return {}
            if not isinstance(data, dict):
                raise InputError(
                    f'{source}: front matter must be a mapping of keys to values, '
                    f'got {type(data).__name__}'
                )
            return {str(k): self._normalise(v) for k, v in data.items()}
        except InputError:
            raise
        except yaml.YAMLError as exc:
            raise InputError(f'{source}: malformed YAML front matter: {exc}') from exc
        except Exception as exc:
            raise InputError(f'{source}: could not read front matter: {exc}') from exc

    def _normalise(self, value: Any) -> Any:
        """Convert dates to ISO strings, recursively."""
        try:
            if isinstance(value, datetime | date):
                return value.isoformat()
            if isinstance(value, list):
                return [self._normalise(v) for v in value]
            if isinstance(value, dict):
                return {str(k): self._normalise(v) for k, v in value.items()}
            return value
        except MdHtmlError:
            raise
        except Exception as exc:
            raise InputError(f'Unexpected error in FrontMatterParser._normalise: {exc}') from exc


class MarkdownParser:
    """Configured markdown-it-py pipeline.

    :param config: Markdown options.
    :param highlighter: code highlighter used for fences and indented code.
    """

    def __init__(self, config: MarkdownConfig, highlighter: CodeHighlighter) -> None:
        try:
            self.config = config
            self.highlighter = highlighter
            self.front_matter = FrontMatterParser()
            self.md = self._build()
        except InputError:
            raise
        except Exception as exc:
            raise InputError(f'Could not initialise the Markdown parser: {exc}') from exc

    def _build(self) -> MarkdownIt:
        """Create and configure the MarkdownIt instance."""
        try:
            cfg = self.config
            md = MarkdownIt(
                'commonmark',
                {'html': cfg.allow_html, 'typographer': cfg.typographer, 'breaks': cfg.breaks},
            )
            md.enable(['table', 'strikethrough'])
            if cfg.typographer:
                md.enable(['replacements', 'smartquotes'])
            md.options['tasklists'] = True
            md.options['tasklists_editable'] = False
            md.options['alerts'] = True
            md.options['strikethrough_single_tilde'] = True
            md.use(front_matter_plugin)
            md.use(footnote_plugin, inline=True)
            md.use(deflist_plugin)
            if cfg.autolinks:
                md.use(gfm_autolink_plugin)
            if cfg.math:
                md.use(
                    dollarmath_plugin,
                    allow_space=False,
                    allow_digits=False,
                    allow_blank_lines=False,
                    renderer=self._math_content,
                )
            if cfg.heading_anchors:
                md.use(
                    anchors_plugin,
                    min_level=1,
                    max_level=6,
                    permalink=cfg.heading_permalinks,
                    permalinkSymbol='#',
                )
            md.core.ruler.push('mdh_task_state', self._task_state_rule)
            if cfg.figures:
                md.core.ruler.push('mdh_figures', self._figure_rule)
                self._default_image = md.renderer.rules['image']
                md.add_render_rule('image', self._render_image)
            md.add_render_rule('fence', self._render_fence)
            md.add_render_rule('code_block', self._render_code_block)
            md.add_render_rule('table_open', self._render_table_open)
            md.add_render_rule('table_close', self._render_table_close)
            return md
        except MdHtmlError:
            raise
        except Exception as exc:
            raise InputError(f'Unexpected error in MarkdownParser._build: {exc}') from exc

    @staticmethod
    def _task_state_rule(state: Any) -> None:
        """Add ``is-checked`` to completed task-list items so themes can style the item."""
        try:
            for token in state.tokens:
                if token.type == 'list_item_open' and token.meta.get('checked') is True:
                    token.attrJoin('class', 'is-checked')
        except Exception as exc:
            raise InputError(f'Could not mark completed tasks: {exc}') from exc

    @staticmethod
    def _figure_rule(state: Any) -> None:
        """Mark paragraphs that contain only a titled image so they render as figures."""
        try:
            tokens = state.tokens
            for index in range(len(tokens) - 2):
                opening, inline, closing = tokens[index : index + 3]
                if not (
                    opening.type == 'paragraph_open'
                    and inline.type == 'inline'
                    and closing.type == 'paragraph_close'
                ):
                    continue
                children = [
                    child
                    for child in inline.children or []
                    if not (child.type == 'text' and not child.content.strip())
                    and child.type != 'softbreak'
                ]
                if len(children) != 1 or children[0].type != 'image':
                    continue
                if not str(children[0].attrGet('title') or '').strip():
                    continue
                opening.hidden = closing.hidden = True
                children[0].meta['figure'] = True
        except Exception as exc:
            raise InputError(f'Could not detect figures: {exc}') from exc

    def _render_image(
        self, tokens: list[Token], idx: int, options: Any, env: dict[str, Any]
    ) -> str:
        """Render an image; titled stand-alone images become ``<figure>`` with a caption."""
        try:
            image = self._default_image(tokens, idx, options, env)
            token = tokens[idx]
            if not token.meta.get('figure'):
                return image
            caption = html.escape(str(token.attrGet('title') or ''))
            return (
                f'<figure class="mdh-figure">{image}<figcaption>{caption}</figcaption></figure>\n'
            )
        except Exception as exc:
            source = env.get('source', '<string>')
            raise InputError(f'{source}: could not render image: {exc}') from exc

    def _math_content(self, content: str, options: dict[str, Any]) -> str:
        """Return escaped math (in MathJax delimiters if enabled) without a wrapper element.

        This is the ``renderer`` given to the dollarmath plugin, which adds the
        ``<span class="math inline">`` / ``<div class="math block">`` wrapper itself.
        """
        try:
            display = bool(options.get('display_mode'))
            text = html.escape(content.strip() if display else content)
            if self.config.math_renderer == 'mathjax':
                text = f'\\[{text}\\]' if display else f'\\({text}\\)'
            return text
        except Exception as exc:
            raise InputError(f'Could not render math {content!r}: {exc}') from exc

    def _render_math(self, content: str, options: dict[str, Any]) -> str:
        """Render a complete math span/block, wrapper included (used for ````math`` fences)."""
        try:
            text = self._math_content(content, options)
            if options.get('display_mode'):
                return f'<div class="math block">\n{text}\n</div>\n'
            return f'<span class="math inline">{text}</span>'
        except InputError:
            raise
        except Exception as exc:
            raise InputError(f'Could not render math {content!r}: {exc}') from exc

    def _render_fence(
        self, tokens: list[Token], idx: int, options: Any, env: dict[str, Any]
    ) -> str:
        """Render a fenced code block (``math`` fences become math blocks)."""
        try:
            token = tokens[idx]
            lang = CodeHighlighter.parse_info(token.info)
            if lang == 'math' and self.config.math:
                return self._render_math(token.content, {'display_mode': True})
            return self.highlighter.highlight(token.content, token.info)
        except InputError as exc:
            raise InputError(f'{env.get("source", "<string>")}: {exc}') from exc
        except MdHtmlError:
            raise
        except Exception as exc:
            raise InputError(
                f'{env.get("source", "<string>")}: could not render code block: {exc}'
            ) from exc

    def _render_code_block(
        self, tokens: list[Token], idx: int, options: Any, env: dict[str, Any]
    ) -> str:
        """Render an indented code block as plain text."""
        try:
            return self.highlighter.highlight(tokens[idx].content, '')
        except InputError as exc:
            raise InputError(f'{env.get("source", "<string>")}: {exc}') from exc
        except MdHtmlError:
            raise
        except Exception as exc:
            raise InputError(
                f'{env.get("source", "<string>")}: could not render code block: {exc}'
            ) from exc

    @staticmethod
    def table_width_class(columns: int) -> str:
        """Wrapper classes for a table with ``columns`` columns.

        Wide tables get compact print styles (smaller text and padding) so they fit the
        page instead of being cut off at its edge.
        """
        try:
            if columns >= XWIDE_TABLE_COLUMNS:
                return 'mdh-table-wrap mdh-table-wide mdh-table-xwide'
            if columns >= WIDE_TABLE_COLUMNS:
                return 'mdh-table-wrap mdh-table-wide'
            return 'mdh-table-wrap'
        except Exception as exc:
            raise InputError(f'Could not classify table width: {exc}') from exc

    def _render_table_open(
        self, tokens: list[Token], idx: int, options: Any, env: dict[str, Any]
    ) -> str:
        """Wrap tables in a scroll container so wide tables do not break the layout."""
        try:
            table = self.md.renderer.renderToken(tokens, idx, options, env)
            columns = ParsedDocument.count_columns(tokens, idx)
            css_class = self.table_width_class(columns)
            return f'<div class="{css_class}" data-columns="{columns}">\n{table}'
        except Exception as exc:
            raise InputError(f'Could not render table: {exc}') from exc

    def _render_table_close(
        self, tokens: list[Token], idx: int, options: Any, env: dict[str, Any]
    ) -> str:
        """Close the table scroll container."""
        try:
            return f'{self.md.renderer.renderToken(tokens, idx, options, env)}</div>\n'
        except Exception as exc:
            raise InputError(f'Could not render table: {exc}') from exc

    def parse(self, text: str, source: str = '<string>') -> ParsedDocument:
        """Parse Markdown ``text`` into a :class:`ParsedDocument`.

        :raises InputError: on invalid input or malformed front matter.
        """
        try:
            if not isinstance(text, str):
                raise InputError(f'{source}: expected text, got {type(text).__name__}')
            env: dict[str, Any] = {'source': source}
            tokens = self.md.parse(text, env)
            meta: dict[str, Any] = {}
            for token in tokens:
                if token.type == 'front_matter':
                    meta = self.front_matter.parse(token.content, source)
                    break
            headings = self.extract_headings(tokens)
            return ParsedDocument(
                tokens=tokens, env=env, meta=meta, headings=headings, source=source
            )
        except InputError:
            raise
        except Exception as exc:
            raise InputError(f'{source}: could not parse Markdown: {exc}') from exc

    def render(self, document: ParsedDocument) -> str:
        """Render a parsed document's body to HTML.

        :raises InputError: if rendering fails (e.g. unknown language in strict mode).
        """
        try:
            return self.md.renderer.render(document.tokens, self.md.options, document.env)
        except InputError:
            raise
        except Exception as exc:
            raise InputError(f'{document.source}: could not render Markdown: {exc}') from exc

    @staticmethod
    def inline_text(token: Token) -> str:
        """Return the plain text of an inline token (formatting and raw HTML stripped)."""
        try:
            parts: list[str] = []
            in_permalink = False
            for child in token.children or []:
                if child.type == 'link_open' and 'header-anchor' in str(
                    child.attrGet('class') or ''
                ):
                    in_permalink = True
                elif child.type == 'link_close' and in_permalink:
                    in_permalink = False
                elif in_permalink:
                    continue
                elif child.type in ('text', 'code_inline', 'math_inline', 'image'):
                    parts.append(child.content)
                elif child.type in ('softbreak', 'hardbreak'):
                    parts.append(' ')
            return ''.join(parts).strip()
        except Exception as exc:
            raise InputError(f'Could not extract heading text: {exc}') from exc

    @classmethod
    def extract_headings(cls, tokens: list[Token]) -> list[Heading]:
        """Collect all headings (level, plain text, anchor id) from a token stream."""
        try:
            headings: list[Heading] = []
            for index, token in enumerate(tokens):
                if token.type != 'heading_open':
                    continue
                inline = tokens[index + 1] if index + 1 < len(tokens) else None
                text = cls.inline_text(inline) if inline is not None else ''
                anchor = token.attrGet('id')
                headings.append(Heading(int(token.tag[1]), text, str(anchor) if anchor else None))
            return headings
        except InputError:
            raise
        except Exception as exc:
            raise InputError(f'Could not extract headings: {exc}') from exc
