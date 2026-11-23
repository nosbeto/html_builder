"""Pygments-based syntax highlighting for fenced and indented code blocks.

The :class:`CodeHighlighter` turns code plus a fence info string (e.g. ``python``,
``js {linenos}``) into HTML of the form::

    <div class="mdh-code" data-lang="python">
      <span class="mdh-code-lang">python</span>
      <div class="highlight"><pre><span></span><code>...</code></pre></div>
    </div>

Unknown languages fall back to plain (escaped) text with a warning, or raise
:class:`~md_html_builder.errors.InputError` in strict mode. :meth:`CodeHighlighter.stylesheet`
produces the Pygments CSS for the configured light/dark styles.
"""

from __future__ import annotations

import html
import logging
from typing import ClassVar

from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexer import Lexer
from pygments.lexers import get_lexer_by_name, guess_lexer
from pygments.lexers.special import TextLexer
from pygments.styles import get_all_styles, get_style_by_name
from pygments.util import ClassNotFound

from .errors import ConfigError, InputError, MdHtmlError

__all__ = ['CodeHighlighter']

logger = logging.getLogger(__name__)


class CodeHighlighter:
    """Highlights code with Pygments and generates the matching stylesheet.

    :param style: Pygments style for light mode.
    :param dark_style: Pygments style for dark mode.
    :param mode: ``light``, ``dark`` or ``auto`` (controls which CSS is emitted).
    :param line_numbers: render line numbers.
    :param guess_language: guess the lexer when no language is given.
    :param strict: raise instead of warning on unknown languages.
    :raises ConfigError: if a style name is unknown.
    """

    CSS_CLASS: ClassVar[str] = 'highlight'
    PLAIN_LANGS: ClassVar[frozenset[str]] = frozenset({'', 'text', 'plain', 'plaintext', 'txt'})

    def __init__(
        self,
        style: str = 'default',
        dark_style: str = 'monokai',
        mode: str = 'auto',
        line_numbers: bool = False,
        guess_language: bool = False,
        strict: bool = False,
    ) -> None:
        try:
            self.style = self._check_style(style)
            self.dark_style = self._check_style(dark_style)
            if mode not in ('light', 'dark', 'auto'):
                raise ConfigError(f'mode must be light, dark or auto, got {mode!r}')
            self.mode = mode
            self.line_numbers = line_numbers
            self.guess_language = guess_language
            self.strict = strict
            self._lexer_cache: dict[str, Lexer | None] = {}
        except ConfigError:
            raise
        except Exception as exc:
            raise ConfigError(f'Could not initialise code highlighter: {exc}') from exc

    @staticmethod
    def available_styles() -> list[str]:
        """Return the sorted names of all installed Pygments styles."""
        try:
            return sorted(get_all_styles())
        except Exception as exc:
            raise ConfigError(f'Could not list Pygments styles: {exc}') from exc

    @classmethod
    def _check_style(cls, name: str) -> str:
        """Return ``name`` if it is an installed Pygments style, else raise ConfigError."""
        try:
            get_style_by_name(name)
            return name
        except ClassNotFound as exc:
            styles = ', '.join(cls.available_styles())
            raise ConfigError(f'Unknown code style {name!r}. Available styles: {styles}') from exc

    @staticmethod
    def parse_info(info: str) -> str:
        """Extract the language from a fence info string (``'py {x}'`` -> ``'py'``)."""
        try:
            info = (info or '').strip()
            if not info:
                return ''
            first = info.split(maxsplit=1)[0]
            first = first.strip('{}').lstrip('.')
            return first.split('{', 1)[0].lower()
        except Exception as exc:
            raise InputError(f'Could not parse code fence info {info!r}: {exc}') from exc

    def _lexer_for(self, lang: str, code: str) -> Lexer | None:
        """Return a lexer for ``lang`` or ``None`` for plain text."""
        try:
            if lang in self.PLAIN_LANGS:
                if lang == '' and self.guess_language and code.strip():
                    try:
                        return guess_lexer(code)
                    except ClassNotFound:
                        return None
                return None
            if lang in self._lexer_cache:
                return self._lexer_cache[lang]
            try:
                lexer: Lexer | None = get_lexer_by_name(lang, stripnl=False, ensurenl=False)
            except ClassNotFound:
                message = f'Unknown code language {lang!r}; rendering as plain text'
                if self.strict:
                    raise InputError(message) from None
                logger.warning(message)
                lexer = None
            self._lexer_cache[lang] = lexer
            return lexer
        except InputError:
            raise
        except Exception as exc:
            raise InputError(f'Could not select a lexer for {lang!r}: {exc}') from exc

    def _formatter(self) -> HtmlFormatter:
        """Build a Pygments HTML formatter with the configured options."""
        try:
            return HtmlFormatter(
                cssclass=self.CSS_CLASS,
                wrapcode=True,
                linenos='table' if self.line_numbers else False,
            )
        except MdHtmlError:
            raise
        except Exception as exc:
            raise ConfigError(f'Unexpected error in CodeHighlighter._formatter: {exc}') from exc

    def highlight(self, code: str, info: str = '') -> str:
        """Return the HTML for one code block.

        :param code: raw code text.
        :param info: fence info string; the first word is the language.
        """
        try:
            lang = self.parse_info(info)
            lexer = self._lexer_for(lang, code)
            body = highlight(code, lexer or TextLexer(stripnl=False), self._formatter())
            safe_lang = html.escape(lang, quote=True)
            label = f'<span class="mdh-code-lang">{safe_lang}</span>' if lang else ''
            lang_attr = f' data-lang="{safe_lang}"' if lang else ''
            plain = '' if lexer else ' mdh-code-plain'
            return f'<div class="mdh-code{plain}"{lang_attr}>{label}{body}</div>\n'
        except InputError:
            raise
        except Exception as exc:
            raise InputError(f'Could not highlight code block ({info!r}): {exc}') from exc

    @staticmethod
    def _style_rules(style: str, scopes: list[str]) -> str:
        """Return Pygments token + background CSS for ``style`` scoped to ``scopes``."""
        try:
            formatter = HtmlFormatter(style=style, cssclass=CodeHighlighter.CSS_CLASS)
            selectors = [f'{scope} .{CodeHighlighter.CSS_CLASS}'.strip() for scope in scopes]
            rules = [
                *formatter.get_background_style_defs(selectors),
                *formatter.get_token_style_defs(selectors),
            ]
            return '\n'.join(rules)
        except MdHtmlError:
            raise
        except Exception as exc:
            raise ConfigError(f'Unexpected error in CodeHighlighter._style_rules: {exc}') from exc

    def stylesheet(self) -> str:
        """Return CSS for the configured code styles.

        * ``light``: the light style, unscoped.
        * ``dark``: the dark style, unscoped.
        * ``auto``: the light style, plus the dark style scoped to
          ``:root[data-theme="dark"]`` and to ``:root[data-theme="auto"]`` inside a
          ``prefers-color-scheme: dark`` media query.
        """
        try:
            linenos = '\n'.join(HtmlFormatter(style=self.style).get_linenos_style_defs())
            if self.mode == 'light':
                return f'{linenos}\n{self._style_rules(self.style, [""])}\n'
            if self.mode == 'dark':
                return f'{linenos}\n{self._style_rules(self.dark_style, [""])}\n'
            light = self._style_rules(self.style, [''])
            dark_forced = self._style_rules(self.dark_style, [':root[data-theme="dark"]'])
            dark_auto = self._style_rules(self.dark_style, [':root[data-theme="auto"]'])
            return (
                f'{linenos}\n{light}\n{dark_forced}\n'
                f'@media screen and (prefers-color-scheme: dark) {{\n{dark_auto}\n}}\n'
            )
        except Exception as exc:
            raise ConfigError(f'Could not generate code highlighting CSS: {exc}') from exc
