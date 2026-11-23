"""Themes: loading CSS themes and assembling the final stylesheet.

Theme interface contract
========================

A theme is either

* a directory ``<name>/`` containing:

  - ``theme.css`` (required) - the stylesheet;
  - ``theme.yaml`` (optional) - metadata with the keys ``name``, ``description``,
    ``code_style`` and ``code_style_dark`` (Pygments style names used unless the user
    configures ``style.code_style`` / ``style.code_style_dark``);
  - ``template.html`` (optional) - replaces the built-in page skeleton
    (``md_html_builder/templates/page.html``); it uses ``string.Template`` placeholders,
    see :data:`md_html_builder.renderer.TEMPLATE_PLACEHOLDERS`;

* or a single ``<name>.css`` file.

Built-in themes live in ``md_html_builder/themes/``; ``style.theme`` may also be a path to a
theme directory or ``.css`` file anywhere on disk.

Shared base stylesheet
----------------------
Every theme is layered on top of ``themes/_shared/base.css``, which implements the complete
layout (``paged`` sheet view and ``web`` view), all Markdown components, accessibility
helpers and the print stylesheet, driven entirely by custom properties. A theme therefore
usually only sets variables and adds a few flourishes. Set ``base: false`` in
``theme.yaml`` to opt out and style everything from scratch.

Besides the variables below, ``base.css`` reads these optional theme variables (defaults in
brackets): ``--mdh-heading-weight`` [600; use multiples of 100],
``--mdh-heading-line-height`` [1.25], ``--mdh-heading-letter-spacing`` [-0.011em],
``--mdh-h1-size`` .. ``--mdh-h6-size``,
``--mdh-code-font-size`` [0.875em], ``--mdh-text-align`` [left], ``--mdh-hyphens``
[manual], ``--mdh-block-spacing`` [1em], ``--mdh-radius`` [8px], ``--mdh-gutter`` [2rem],
``--mdh-sheet-shadow``, ``--mdh-bar-font-size`` [0.8rem] (header/footer text),
``--mdh-table-frame`` [1px solid border colour; ``none`` for frameless tables], plus the
colour keys ``desk`` (backdrop behind the paged sheet), ``selection``, ``mark``, ``focus``
and the alert colours ``note``, ``tip``, ``important``, ``warning``, ``caution``.

CSS custom properties
---------------------
Themes must express every customisable value as a CSS custom property on ``:root`` so user
configuration can override it. The builder appends an override block *after* the theme CSS:

==========================  ======================================================
Config key                  CSS custom property
==========================  ======================================================
``style.font_body``         ``--mdh-font-body``
``style.font_heading``      ``--mdh-font-heading``
``style.font_code``         ``--mdh-font-code``
``style.font_size``         ``--mdh-font-size``
``style.line_height``       ``--mdh-line-height``
``layout.max_width``        ``--mdh-content-max-width``
``layout.sidebar_width``    ``--mdh-sidebar-width``
``style.colors.<key>``      ``--mdh-color-<key>`` (``_`` becomes ``-``), every mode
``style.colors_dark.<key>`` ``--mdh-color-<key>``, dark mode only
``style.variables.<name>``  ``--<name>`` (verbatim; leading ``--`` optional)
``page.size``/``orientation`` ``--mdh-page-width`` / ``--mdh-page-height`` (mm)
``page.margin``             ``--mdh-page-margin-top|right|bottom|left``
==========================  ======================================================

Themes should define at least the colours in :data:`CORE_COLOR_KEYS`.

Light / dark mode
-----------------
``<html>`` carries ``data-theme="light|dark|auto"``. Themes define the light palette on
``:root``, the dark palette on ``:root[data-theme="dark"]`` and repeat the dark palette inside
``@media screen and (prefers-color-scheme: dark) { :root[data-theme="auto"] { ... } }``.
The ``screen`` condition keeps ``auto`` pages light when printed or exported to PDF.

Page structure & classes
------------------------
``<body>`` gets ``mdh-body``, ``mdh-view-<paged|web>``, ``mdh-toc-<none|left|right|top>``,
``mdh-align-<center|left>`` and ``mdh-has-nav`` (multi-page). In the ``paged`` view the
header, navigation, TOC, content and footer sit inside ``div.mdh-sheet`` (the paper); see
``templates/paged.html``. ``div.mdh-print-regions`` holds hidden running copies of the
header/footer for print (see :mod:`md_html_builder.printing`).
Regions: ``header.mdh-header``, ``div.mdh-layout``,
``nav.mdh-site-nav`` (multi-page navigation), ``aside.mdh-toc`` (with ``.mdh-toc-title`` and
nested ``ul.mdh-toc-list``), ``main.mdh-content > article.mdh-article.markdown-body``,
``nav.mdh-pager`` (``a.mdh-pager-prev`` / ``a.mdh-pager-next``), ``footer.mdh-footer``
(``.mdh-footer-text``, ``.mdh-page-number``). Markdown output classes include
``div.mdh-code`` (``data-lang``, ``span.mdh-code-lang``, Pygments ``div.highlight``),
``div.mdh-table-wrap > table``, ``ul.contains-task-list``, ``li.task-list-item``,
``div.markdown-alert.markdown-alert-<type>``, ``section.footnotes``, ``a.header-anchor``,
``span.math.inline`` / ``div.math.block``, ``dl/dt/dd``, ``figure.mdh-figure > img +
figcaption`` (an image with a title alone in a paragraph).

Print
-----
:class:`~md_html_builder.printing.PrintLayout` emits ``@page`` size/margins, page-number
counters and running header/footer boxes. Themes may style the margin boxes with
``@page { @top-left { ... } }`` and should keep screen-only chrome out of print.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import yaml

from .config import Config
from .errors import ConfigError, MdHtmlError, ThemeError
from .highlighter import CodeHighlighter
from .printing import PrintLayout

__all__ = ['BASE_CSS_PATH', 'CORE_COLOR_KEYS', 'StylesheetBuilder', 'Theme', 'ThemeRegistry']

logger = logging.getLogger(__name__)

CORE_COLOR_KEYS: tuple[str, ...] = (
    'bg',
    'surface',
    'text',
    'muted',
    'heading',
    'link',
    'accent',
    'border',
    'code-bg',
    'code-text',
    'table-header-bg',
    'table-stripe',
    'blockquote-border',
    'header-bg',
    'header-text',
    'footer-bg',
    'footer-text',
)

_THEME_META_KEYS = frozenset({'name', 'description', 'code_style', 'code_style_dark', 'base'})
BASE_CSS_PATH = Path(__file__).resolve().parent / 'themes' / '_shared' / 'base.css'
_THEME_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]*$')


@dataclass(frozen=True)
class Theme:
    """A loaded theme.

    :ivar name: theme name.
    :ivar path: theme directory or CSS file.
    :ivar css: stylesheet text.
    :ivar description: human-readable description.
    :ivar code_style: preferred light Pygments style (or ``None``).
    :ivar code_style_dark: preferred dark Pygments style (or ``None``).
    :ivar template: optional page template text (``None`` = built-in skeleton).
    :ivar base: layer the theme on top of the shared ``base.css`` (default ``True``).
    """

    name: str
    path: Path
    css: str
    description: str = ''
    code_style: str | None = None
    code_style_dark: str | None = None
    template: str | None = None
    base: bool = True

    def defined_variables(self) -> set[str]:
        """Return the set of CSS custom property names defined in the theme CSS."""
        try:
            return set(re.findall(r'(--[A-Za-z0-9_-]+)\s*:', self.css))
        except Exception as exc:
            raise ThemeError(f'Could not inspect theme {self.name!r}: {exc}') from exc


class ThemeRegistry:
    """Finds and loads themes from the built-in directory or arbitrary paths.

    :param builtin_dir: directory of built-in themes (defaults to the package's ``themes/``).
    """

    CSS_FILE: ClassVar[str] = 'theme.css'
    META_FILE: ClassVar[str] = 'theme.yaml'
    TEMPLATE_FILE: ClassVar[str] = 'template.html'

    def __init__(self, builtin_dir: Path | None = None) -> None:
        try:
            self.builtin_dir = builtin_dir or Path(__file__).resolve().parent / 'themes'
        except MdHtmlError:
            raise
        except Exception as exc:
            raise ThemeError(f'Unexpected error in ThemeRegistry.__init__: {exc}') from exc

    def available(self) -> list[str]:
        """Return the sorted names of built-in themes."""
        try:
            if not self.builtin_dir.is_dir():
                return []
            names = set()
            for entry in self.builtin_dir.iterdir():
                if entry.is_dir() and (entry / self.CSS_FILE).is_file():
                    names.add(entry.name)
                elif entry.is_file() and entry.suffix == '.css':
                    names.add(entry.stem)
            return sorted(names)
        except OSError as exc:
            raise ThemeError(f'Could not list themes in {self.builtin_dir}: {exc}') from exc

    def describe(self) -> dict[str, str]:
        """Return ``{name: description}`` for all built-in themes."""
        try:
            return {name: self.load(name).description for name in self.available()}
        except ThemeError:
            raise
        except Exception as exc:
            raise ThemeError(f'Could not describe themes: {exc}') from exc

    def load(self, name_or_path: str) -> Theme:
        """Load a theme by built-in name or by path.

        :raises ThemeError: if the theme does not exist or is malformed.
        """
        try:
            if not name_or_path or not name_or_path.strip():
                raise ThemeError('Theme name must not be empty')
            candidate = Path(name_or_path).expanduser()
            looks_like_path = (
                '/' in name_or_path or '\\' in name_or_path or name_or_path.endswith('.css')
            )
            if looks_like_path:
                if candidate.is_dir():
                    return self._load_dir(candidate)
                if candidate.is_file():
                    return self._load_file(candidate)
                raise ThemeError(f'Theme path not found: {candidate}')
            if not _THEME_NAME.match(name_or_path):
                raise ThemeError(f'Invalid theme name {name_or_path!r}')
            theme_dir = self.builtin_dir / name_or_path
            if theme_dir.is_dir():
                return self._load_dir(theme_dir)
            theme_file = self.builtin_dir / f'{name_or_path}.css'
            if theme_file.is_file():
                return self._load_file(theme_file)
            available = ', '.join(self.available()) or '(none)'
            raise ThemeError(f'Unknown theme {name_or_path!r}. Available themes: {available}')
        except ThemeError:
            raise
        except Exception as exc:
            raise ThemeError(f'Could not load theme {name_or_path!r}: {exc}') from exc

    def _read(self, path: Path) -> str:
        """Read a UTF-8 theme file."""
        try:
            return path.read_text(encoding='utf-8-sig')
        except (OSError, UnicodeDecodeError) as exc:
            raise ThemeError(f'Could not read theme file {path}: {exc}') from exc

    def _load_file(self, path: Path) -> Theme:
        """Load a single-file CSS theme."""
        try:
            if path.suffix.lower() != '.css':
                raise ThemeError(f'Theme file must be a .css file: {path}')
            return Theme(name=path.stem, path=path, css=self._read(path))
        except ThemeError:
            raise
        except Exception as exc:
            raise ThemeError(f'Could not load theme file {path}: {exc}') from exc

    def _load_dir(self, path: Path) -> Theme:
        """Load a directory theme (theme.css + optional theme.yaml/template.html)."""
        try:
            css_path = path / self.CSS_FILE
            if not css_path.is_file():
                raise ThemeError(f'Theme directory {path} has no {self.CSS_FILE}')
            meta = self._read_meta(path / self.META_FILE)
            template_path = path / self.TEMPLATE_FILE
            template = self._read(template_path) if template_path.is_file() else None
            return Theme(
                name=str(meta.get('name') or path.name),
                path=path,
                css=self._read(css_path),
                description=str(meta.get('description') or ''),
                code_style=meta.get('code_style'),
                code_style_dark=meta.get('code_style_dark'),
                template=template,
                base=meta.get('base', True),
            )
        except ThemeError:
            raise
        except Exception as exc:
            raise ThemeError(f'Could not load theme directory {path}: {exc}') from exc

    def _read_meta(self, path: Path) -> dict[str, Any]:
        """Read and validate ``theme.yaml`` (missing file gives ``{}``)."""
        try:
            if not path.is_file():
                return {}
            data = yaml.safe_load(self._read(path)) or {}
            if not isinstance(data, dict):
                raise ThemeError(f'{path}: theme metadata must be a mapping')
            unknown = set(data) - _THEME_META_KEYS
            if unknown:
                allowed = ', '.join(sorted(_THEME_META_KEYS))
                raise ThemeError(f'{path}: unknown keys {sorted(unknown)}; allowed: {allowed}')
            for key in ('code_style', 'code_style_dark'):
                if data.get(key) is not None and not isinstance(data[key], str):
                    raise ThemeError(f'{path}: {key} must be a string')
            if 'base' in data and not isinstance(data['base'], bool):
                raise ThemeError(f'{path}: base must be true or false')
            return data
        except ThemeError:
            raise
        except yaml.YAMLError as exc:
            raise ThemeError(f'{path}: malformed theme metadata: {exc}') from exc


class StylesheetBuilder:
    """Assembles the page stylesheet: theme, code highlighting, overrides and custom CSS.

    Order (later wins): shared base CSS (unless the theme opts out), theme CSS, Pygments CSS,
    configuration overrides (custom properties), print/page rules, user ``custom_css`` files.

    :param config: full configuration.
    :param theme: loaded theme.
    :param highlighter: highlighter whose stylesheet is included.
    """

    def __init__(self, config: Config, theme: Theme, highlighter: CodeHighlighter) -> None:
        try:
            self.config = config
            self.theme = theme
            self.highlighter = highlighter
            self.print_layout = PrintLayout(config)
        except MdHtmlError:
            raise
        except Exception as exc:
            raise ThemeError(f'Unexpected error in StylesheetBuilder.__init__: {exc}') from exc

    @staticmethod
    def base_css() -> str:
        """Return the shared base stylesheet that all themes build on."""
        try:
            return BASE_CSS_PATH.read_text(encoding='utf-8')
        except OSError as exc:
            raise ThemeError(f'Built-in base stylesheet missing: {exc}') from exc

    @staticmethod
    def color_var(key: str) -> str:
        """Map a colour key to its custom property (``code_bg`` -> ``--mdh-color-code-bg``)."""
        try:
            return f'--mdh-color-{key.replace("_", "-")}'
        except MdHtmlError:
            raise
        except Exception as exc:
            raise ThemeError(f'Unexpected error in StylesheetBuilder.color_var: {exc}') from exc

    @staticmethod
    def custom_var(name: str) -> str:
        """Normalise a free-form variable name to ``--name``."""
        try:
            return name if name.startswith('--') else f'--{name}'
        except MdHtmlError:
            raise
        except Exception as exc:
            raise ThemeError(f'Unexpected error in StylesheetBuilder.custom_var: {exc}') from exc

    def base_declarations(self) -> dict[str, str]:
        """Return custom properties derived from layout, font and colour settings."""
        try:
            style = self.config.style
            layout = self.config.layout
            decls: dict[str, str] = {
                '--mdh-content-max-width': layout.max_width,
                '--mdh-sidebar-width': layout.sidebar_width,
            }
            mapping = {
                '--mdh-font-body': style.font_body,
                '--mdh-font-heading': style.font_heading,
                '--mdh-font-code': style.font_code,
                '--mdh-font-size': style.font_size,
                '--mdh-line-height': style.line_height,
            }
            decls.update({k: v for k, v in mapping.items() if v is not None})
            decls.update(self.print_layout.page_variables())
            decls.update({self.color_var(k): v for k, v in style.colors.items()})
            decls.update({self.custom_var(k): v for k, v in style.variables.items()})
            return decls
        except Exception as exc:
            raise ConfigError(f'Could not compute style overrides: {exc}') from exc

    def warn_unknown_colors(self) -> None:
        """Log a warning for colour keys the theme does not define (likely typos)."""
        try:
            defined = self.theme.defined_variables()
            if self.theme.base:
                defined |= set(re.findall(r'(--[A-Za-z0-9_-]+)\s*:', self.base_css()))
            style = self.config.style
            for key in {**style.colors, **style.colors_dark}:
                var = self.color_var(key)
                if var not in defined:
                    logger.warning(
                        f'Colour {key!r} ({var}) is not used by theme {self.theme.name!r}'
                    )
        except Exception as exc:
            raise ThemeError(f'Could not check colour overrides: {exc}') from exc

    @staticmethod
    def _block(selector: str, decls: dict[str, str]) -> str:
        """Format a CSS rule block."""
        try:
            body = ''.join(f'  {name}: {value};\n' for name, value in decls.items())
            return f'{selector} {{\n{body}}}\n'
        except MdHtmlError:
            raise
        except Exception as exc:
            raise ThemeError(f'Unexpected error in StylesheetBuilder._block: {exc}') from exc

    def overrides_css(self) -> str:
        """Return the configuration override CSS (custom properties)."""
        try:
            parts = [self._block(':root, :root[data-theme]', self.base_declarations())]
            dark = {self.color_var(k): v for k, v in self.config.style.colors_dark.items()}
            if dark:
                parts.append(self._block(':root[data-theme="dark"]', dark))
                auto = self._block('  :root[data-theme="auto"]', dark)
                parts.append(f'@media screen and (prefers-color-scheme: dark) {{\n{auto}}}\n')
            return ''.join(parts)
        except ConfigError:
            raise
        except Exception as exc:
            raise ConfigError(f'Could not build override CSS: {exc}') from exc

    def print_css(self) -> str:
        """Return the ``@page`` rules (size, margins, page-number counters) and print tweaks."""
        try:
            return self.print_layout.stylesheet()
        except ConfigError:
            raise
        except Exception as exc:
            raise ConfigError(f'Could not build print CSS: {exc}') from exc

    def custom_css(self) -> str:
        """Return the concatenated contents of ``style.custom_css`` files.

        :raises ConfigError: if a file is missing or unreadable.
        """
        try:
            chunks = []
            for item in self.config.style.custom_css:
                path = Path(item).expanduser()
                if not path.is_file():
                    raise ConfigError(f'Custom CSS file not found: {path}')
                chunks.append(f'/* custom: {path.name} */\n{path.read_text(encoding="utf-8-sig")}')
            return '\n'.join(chunks)
        except ConfigError:
            raise
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigError(f'Could not read custom CSS: {exc}') from exc

    def build(self) -> str:
        """Return the complete stylesheet text."""
        try:
            self.warn_unknown_colors()
            sections = [
                ('base', self.base_css() if self.theme.base else ''),
                (f'theme: {self.theme.name}', self.theme.css),
                ('code highlighting', self.highlighter.stylesheet()),
                ('configuration overrides', self.overrides_css()),
                ('print', self.print_css()),
                ('custom css', self.custom_css()),
            ]
            return '\n'.join(
                f'/* ---- {label} ---- */\n{css.strip()}\n'
                for label, css in sections
                if css.strip()
            )
        except (ConfigError, ThemeError):
            raise
        except Exception as exc:
            raise ThemeError(f'Could not build stylesheet: {exc}') from exc
