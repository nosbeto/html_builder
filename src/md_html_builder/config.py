"""Configuration model, loading and validation.

Configuration is a tree of dataclasses (:class:`Config` at the root). It can be built from:

* defaults (``Config()``),
* a YAML / JSON / TOML file (:meth:`ConfigLoader.load`),
* CLI overrides, expressed as a nested mapping and deep-merged on top
  (:meth:`Config.with_overrides`, :meth:`ConfigLoader.parse_assignment`).

Every section validates its own values and raises :class:`~md_html_builder.errors.ConfigError`
with the dotted key path of the offending value, so mistakes are reported early and clearly.

Example YAML::

    title: My Docs
    style:
      theme: default
      mode: auto
      colors: {accent: '#0b7285'}
    layout: {toc: left, max_width: 60rem}
    header: {enabled: true, template: '{site_title} | {title}'}
    page_numbers: {enabled: true}
"""

from __future__ import annotations

import copy
import dataclasses
import difflib
import json
import logging
import math
import re
import string
import tomllib
import types
import typing
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, ClassVar, Literal, Self, get_args, get_origin, get_type_hints

import yaml

from .errors import ConfigError, MdHtmlError

__all__ = [
    'BuildConfig',
    'Config',
    'ConfigLoader',
    'ConfigSection',
    'ContentsPageConfig',
    'CoverConfig',
    'FooterConfig',
    'HeaderConfig',
    'LayoutConfig',
    'MarkdownConfig',
    'PageConfig',
    'PageNumberConfig',
    'PdfConfig',
    'StyleConfig',
    'validate_css_value',
    'validate_template_syntax',
]

logger = logging.getLogger(__name__)

_UNSAFE_CSS = re.compile(r'[;{}<>\r\n]')
_CSS_IDENT = re.compile(r'^(--)?[A-Za-z][A-Za-z0-9_-]*$')
_COLOR_KEY = re.compile(r'^[a-z][a-z0-9_-]*$')
_DOTTED_KEY = re.compile(r'^[A-Za-z_][A-Za-z0-9_-]*(\.[A-Za-z_][A-Za-z0-9_-]*)*$')
_URL_SCHEME = re.compile(r'^([A-Za-z][A-Za-z0-9+.-]+:|//)')


def validate_css_value(value: str, key: str) -> str:
    """Return ``value`` unchanged if it is safe to embed inside a CSS declaration.

    Values may not contain ``; { } < >`` or newlines, which could break out of the
    declaration or the surrounding ``<style>`` element.

    :raises ConfigError: if the value is empty or unsafe.
    """
    try:
        if not isinstance(value, str):
            raise ConfigError(f'{key}: expected a CSS value string, got {type(value).__name__}')
        if not value.strip():
            raise ConfigError(f'{key}: CSS value must not be empty')
        if _UNSAFE_CSS.search(value):
            raise ConfigError(
                f'{key}: CSS value {value!r} contains forbidden characters (; {{ }} < > newline)'
            )
        return value
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f'{key}: could not validate CSS value {value!r}: {exc}') from exc


def validate_template_syntax(template: str, key: str) -> str:
    """Check that ``template`` is a well-formed ``str.format``-style template.

    Only plain identifiers are allowed as fields (``{title}``), never attribute or index
    access (``{title.__class__}``), and conversion/format specs are rejected.

    :raises ConfigError: if the template is malformed.
    """
    try:
        for _literal, field_name, format_spec, conversion in string.Formatter().parse(template):
            if field_name is None:
                continue
            if not field_name.isidentifier():
                raise ConfigError(
                    f'{key}: invalid template field {{{field_name}}}; '
                    'only simple names such as {title} are allowed'
                )
            if format_spec or conversion:
                raise ConfigError(
                    f'{key}: format specs/conversions are not allowed in {{{field_name}}}'
                )
        return template
    except ConfigError:
        raise
    except ValueError as exc:
        raise ConfigError(
            f'{key}: malformed template {template!r} ({exc}); use {{{{ and }}}} for literal braces'
        ) from exc


class ConfigSection:
    """Mixin giving dataclass config sections strict, typed construction from mappings.

    Subclasses are dataclasses; :meth:`from_dict` coerces and type-checks every value against
    the field annotations, rejects unknown keys (with a "did you mean" hint) and then calls
    :meth:`validate` for semantic checks.
    """

    _path: ClassVar[str] = ''

    @classmethod
    def from_dict(cls, data: Any, path: str = '') -> Self:
        """Build the section from a mapping, validating types and values.

        :param data: mapping of field names to values (``None`` means all defaults).
        :param path: dotted prefix used in error messages.
        :raises ConfigError: on unknown keys, wrong types or invalid values.
        """
        try:
            if data is None:
                data = {}
            if not isinstance(data, dict):
                where = path or 'config'
                raise ConfigError(f'{where}: expected a mapping, got {type(data).__name__}')
            hints = get_type_hints(cls)
            known = {f.name: f for f in dataclasses.fields(cls) if f.init}  # type: ignore[arg-type]
            kwargs: dict[str, Any] = {}
            for raw_key, value in data.items():
                if not isinstance(raw_key, str):
                    raise ConfigError(f'{path or "config"}: keys must be strings, got {raw_key!r}')
                key = raw_key.replace('-', '_')
                key_path = f'{path}.{key}' if path else key
                if key not in known:
                    suggestion = difflib.get_close_matches(key, list(known), n=1)
                    hint = f' (did you mean {suggestion[0]!r}?)' if suggestion else ''
                    raise ConfigError(f'Unknown configuration key {key_path!r}{hint}')
                kwargs[key] = _coerce(value, hints[key], key_path)
            instance = cls(**kwargs)
            instance.validate(path)
            return instance
        except ConfigError:
            raise
        except Exception as exc:
            raise ConfigError(f'{path or "config"}: invalid configuration ({exc})') from exc

    def validate(self, path: str = '') -> None:
        """Semantic validation hook; subclasses override and raise :class:`ConfigError`."""

    def to_dict(self) -> dict[str, Any]:
        """Return a plain (JSON/YAML serialisable) dict of this section."""
        try:
            return dataclasses.asdict(self)  # type: ignore[call-overload]
        except Exception as exc:
            raise ConfigError(f'Could not serialise configuration: {exc}') from exc


def _coerce(value: Any, annotation: Any, key: str) -> Any:
    """Coerce/validate ``value`` against a type ``annotation`` (used by ConfigSection)."""
    try:
        origin = get_origin(annotation)
        args = get_args(annotation)
        if origin is Literal:
            if value not in args:
                choices = ', '.join(repr(a) for a in args)
                raise ConfigError(f'{key}: {value!r} is not one of {choices}')
            return value
        if origin in (typing.Union, types.UnionType):
            if value is None:
                if type(None) in args:
                    return None
                raise ConfigError(f'{key}: value must not be null')
            errors: list[str] = []
            for arg in args:
                if arg is type(None):
                    continue
                try:
                    return _coerce(value, arg, key)
                except ConfigError as exc:
                    errors.append(str(exc))
            raise ConfigError(errors[0] if errors else f'{key}: invalid value {value!r}')
        if origin is list:
            (item_type,) = args or (Any,)
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, list | tuple):
                raise ConfigError(f'{key}: expected a list, got {type(value).__name__}')
            return [_coerce(item, item_type, f'{key}[{i}]') for i, item in enumerate(value)]
        if origin is dict:
            key_type, value_type = args or (str, Any)
            if not isinstance(value, dict):
                raise ConfigError(f'{key}: expected a mapping, got {type(value).__name__}')
            return {
                _coerce(k, key_type, f'{key} key'): _coerce(v, value_type, f'{key}.{k}')
                for k, v in value.items()
            }
        if isinstance(annotation, type) and issubclass(annotation, ConfigSection):
            return annotation.from_dict(value, key)
        if annotation is bool:
            if not isinstance(value, bool):
                raise ConfigError(f'{key}: expected true/false, got {value!r}')
            return value
        if annotation is int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigError(f'{key}: expected an integer, got {value!r}')
            return value
        if annotation is float:
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ConfigError(f'{key}: expected a number, got {value!r}')
            return float(value)
        if annotation is str:
            if isinstance(value, bool) or value is None:
                raise ConfigError(f'{key}: expected a string, got {value!r}')
            if isinstance(value, int | float):
                return str(value)
            if not isinstance(value, str):
                raise ConfigError(f'{key}: expected a string, got {type(value).__name__}')
            return value
        if annotation is Any:
            return value
        raise ConfigError(f'{key}: unsupported config type {annotation!r}')
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f'{key}: invalid value {value!r} ({exc})') from exc


TocPosition = Literal['none', 'left', 'right', 'top']
ThemeMode = Literal['light', 'dark', 'auto']
AssetMode = Literal['copy', 'rewrite', 'embed', 'keep']
LayoutView = Literal['paged', 'web']
PageSize = Literal['A3', 'A4', 'A5', 'B5', 'Letter', 'Legal', 'Tabloid']
PageOrientation = Literal['portrait', 'landscape']
BoxPosition = Literal['left', 'center', 'right']
OutputFormat = Literal['html', 'pdf', 'both']
Align = Literal['center', 'left']
MathRenderer = Literal['none', 'mathjax']

_FONT_URL = re.compile(r'^https://[^\s"\'<>]+$')


@dataclass
class LayoutConfig(ConfigSection):
    """Page layout options.

    :ivar max_width: CSS width of the content column (``--mdh-content-max-width``).
    :ivar sidebar_width: CSS width of the TOC/navigation sidebars (``--mdh-sidebar-width``).
    :ivar toc: table-of-contents position: ``none``, ``left``, ``right`` or ``top``.
    :ivar toc_depth: deepest heading level (1-6) included in the TOC.
    :ivar toc_title: heading shown above the TOC.
    :ivar align: ``center`` the page or keep it ``left`` aligned.
    :ivar navigation: show site navigation and previous/next links in multi-page builds.
    :ivar view: ``paged`` renders the document as a printed sheet of paper (page size and
        margins from :class:`PageConfig`) on a neutral backdrop, so it looks like the PDF;
        ``web`` renders a classic full-width web page (``max_width`` applies).
    """

    view: LayoutView = 'paged'
    max_width: str = '48rem'
    sidebar_width: str = '16rem'
    toc: TocPosition = 'right'
    toc_depth: int = 3
    toc_title: str = 'Contents'
    align: Align = 'center'
    navigation: bool = True

    def validate(self, path: str = '') -> None:
        """Validate widths and TOC depth."""
        try:
            prefix = f'{path}.' if path else ''
            validate_css_value(self.max_width, f'{prefix}max_width')
            validate_css_value(self.sidebar_width, f'{prefix}sidebar_width')
            if not 1 <= self.toc_depth <= 6:
                raise ConfigError(
                    f'{prefix}toc_depth must be between 1 and 6, got {self.toc_depth}'
                )
        except ConfigError:
            raise
        except Exception as exc:
            raise ConfigError(f'{path}: invalid layout configuration ({exc})') from exc


@dataclass
class StyleConfig(ConfigSection):
    """Visual style options; most map directly onto theme CSS custom properties.

    :ivar theme: built-in theme name, or a path to a theme directory / ``.css`` file.
    :ivar mode: colour scheme: ``light``, ``dark`` or ``auto`` (follows the OS preference).
    :ivar font_body: body font stack (``--mdh-font-body``).
    :ivar font_heading: heading font stack (``--mdh-font-heading``).
    :ivar font_code: monospace font stack (``--mdh-font-code``).
    :ivar font_size: base font size (``--mdh-font-size``).
    :ivar line_height: base line height (``--mdh-line-height``).
    :ivar colors: colour overrides applied in every mode, ``{key: value}`` maps to
        ``--mdh-color-<key>`` (underscores become hyphens).
    :ivar colors_dark: colour overrides applied only in dark mode (take precedence).
    :ivar variables: arbitrary CSS custom property overrides (``name`` or ``--name``).
    :ivar code_style: Pygments style for code blocks (defaults to the theme's choice).
    :ivar code_style_dark: Pygments style used in dark mode.
    :ivar custom_css: CSS files appended after the theme (highest priority).
    :ivar font_url: web-font stylesheet URLs (``https://`` only), e.g. a Google Fonts
        ``css2`` link, loaded with ``<link rel="stylesheet">`` before the theme CSS.
    """

    theme: str = 'default'
    mode: ThemeMode = 'auto'
    font_body: str | None = None
    font_heading: str | None = None
    font_code: str | None = None
    font_size: str | None = None
    line_height: str | None = None
    colors: dict[str, str] = field(default_factory=dict)
    colors_dark: dict[str, str] = field(default_factory=dict)
    variables: dict[str, str] = field(default_factory=dict)
    code_style: str | None = None
    code_style_dark: str | None = None
    custom_css: list[str] = field(default_factory=list)
    font_url: list[str] = field(default_factory=list)

    def validate(self, path: str = '') -> None:
        """Validate that all CSS-bound values are safe and keys are well formed."""
        try:
            prefix = f'{path}.' if path else ''
            if not self.theme.strip():
                raise ConfigError(f'{prefix}theme must not be empty')
            for name in ('font_body', 'font_heading', 'font_code', 'font_size', 'line_height'):
                value = getattr(self, name)
                if value is not None:
                    validate_css_value(value, f'{prefix}{name}')
            for section in ('colors', 'colors_dark'):
                for key, value in getattr(self, section).items():
                    if not _COLOR_KEY.match(key):
                        raise ConfigError(
                            f'{prefix}{section}: invalid colour key {key!r} '
                            '(use lowercase letters, digits, - or _)'
                        )
                    validate_css_value(value, f'{prefix}{section}.{key}')
            for key, value in self.variables.items():
                if not _CSS_IDENT.match(key):
                    raise ConfigError(f'{prefix}variables: invalid CSS variable name {key!r}')
                validate_css_value(value, f'{prefix}variables.{key}')
            for name in ('code_style', 'code_style_dark'):
                value = getattr(self, name)
                if value is not None and not value.strip():
                    raise ConfigError(f'{prefix}{name} must not be empty')
            for index, url in enumerate(self.font_url):
                if not _FONT_URL.match(url):
                    raise ConfigError(
                        f'{prefix}font_url[{index}]: {url!r} must be an https:// URL '
                        'without spaces or quotes'
                    )
        except ConfigError:
            raise
        except Exception as exc:
            raise ConfigError(f'{path}: invalid style configuration ({exc})') from exc


@dataclass
class _BarConfig(ConfigSection):
    """Shared shape for header and footer bars."""

    enabled: bool = False
    template: str = ''

    def validate(self, path: str = '') -> None:
        """Validate template syntax."""
        try:
            validate_template_syntax(self.template, f'{path}.template' if path else 'template')
        except ConfigError:
            raise
        except Exception as exc:
            raise ConfigError(f'{path}: invalid template configuration ({exc})') from exc


@dataclass
class HeaderConfig(_BarConfig):
    """Optional page header.

    ``template`` is trusted HTML with ``{variable}`` placeholders (values are HTML-escaped).
    Available variables: ``title``, ``site_title``, ``date``, ``build_date``, ``year``,
    ``page``, ``total_pages``, ``filename``, ``author``, ``description`` plus any scalar
    front-matter key.
    """

    template: str = '{site_title}'


@dataclass
class FooterConfig(_BarConfig):
    """Optional page footer (same template variables as :class:`HeaderConfig`)."""

    template: str = '{title} &middot; {date}'


@dataclass
class PageNumberConfig(ConfigSection):
    """Page numbering.

    :ivar enabled: render a page number (``format``) in the footer area.
    :ivar format: template for the on-screen page number.
    :ivar print: emit CSS ``@page`` counters so printed/PDF output is numbered.
    :ivar position: horizontal position of the printed page number in the bottom page
        margin: ``left``, ``center`` or ``right`` (the footer text takes the other side).
    """

    enabled: bool = False
    format: str = 'Page {page} of {total_pages}'
    print: bool = True
    position: BoxPosition = 'right'

    def validate(self, path: str = '') -> None:
        """Validate the page number template."""
        try:
            validate_template_syntax(self.format, f'{path}.format' if path else 'format')
        except ConfigError:
            raise
        except Exception as exc:
            raise ConfigError(f'{path}: invalid page number configuration ({exc})') from exc


@dataclass
class CoverConfig(ConfigSection):
    """Optional cover page: the first printed page, without running header, footer or number.

    Every text value is plain text (HTML-escaped). A page's front matter can override any
    field with a ``cover:`` mapping, or turn the cover on or off with ``cover: true/false``.

    :ivar enabled: render the cover page.
    :ivar title: cover title (default: the page title).
    :ivar subtitle: line shown under the title.
    :ivar authors: author names, one per line (default: front matter ``author``, then the
        root ``author`` setting).
    :ivar details: extra lines such as a course, organisation or version.
    :ivar date: date line (default: the ``{date}`` value; an empty string hides it).
    :ivar logo: image shown above the title. A relative path in a config file is resolved
        against that file; in front matter it is relative to the Markdown file.
    """

    enabled: bool = False
    title: str | None = None
    subtitle: str | None = None
    authors: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)
    date: str | None = None
    logo: str | None = None

    def validate(self, path: str = '') -> None:
        """Reject blank titles, logos and list entries (an empty ``date`` hides the date)."""
        try:
            prefix = f'{path}.' if path else ''
            for name in ('title', 'subtitle', 'logo'):
                value = getattr(self, name)
                if value is not None and not value.strip():
                    raise ConfigError(f'{prefix}{name} must not be empty (omit it instead)')
            for name in ('authors', 'details'):
                for index, item in enumerate(getattr(self, name)):
                    if not item.strip():
                        raise ConfigError(f'{prefix}{name}[{index}] must not be empty')
        except ConfigError:
            raise
        except Exception as exc:
            raise ConfigError(f'{path}: invalid cover configuration ({exc})') from exc


@dataclass
class ContentsPageConfig(ConfigSection):
    """Optional printed table of contents on its own page (after the cover, if any).

    Unlike ``layout.toc`` (a sidebar or box for reading on screen), this is part of the
    document itself. With a renderer that supports CSS ``target-counter()`` (WeasyPrint),
    each entry shows a dotted leader and its page number; elsewhere the entries are links.
    Front matter ``contents_page: true/false`` turns it on or off per page.

    :ivar enabled: render the contents page.
    :ivar title: heading of the contents page.
    :ivar depth: deepest heading level (1-6) listed.
    :ivar page_numbers: print page numbers next to the entries.
    """

    enabled: bool = False
    title: str = 'Contents'
    depth: int = 2
    page_numbers: bool = True

    def validate(self, path: str = '') -> None:
        """Validate the title and depth."""
        try:
            prefix = f'{path}.' if path else ''
            if not self.title.strip():
                raise ConfigError(f'{prefix}title must not be empty')
            if not 1 <= self.depth <= 6:
                raise ConfigError(f'{prefix}depth must be between 1 and 6, got {self.depth}')
        except ConfigError:
            raise
        except Exception as exc:
            raise ConfigError(f'{path}: invalid contents page configuration ({exc})') from exc


@dataclass
class PageConfig(ConfigSection):
    """Physical page used for printing / PDF and for the on-screen ``paged`` view.

    :ivar size: paper size: ``A3``, ``A4``, ``A5``, ``B5``, ``Letter``, ``Legal`` or
        ``Tabloid``.
    :ivar orientation: ``portrait`` or ``landscape``.
    :ivar margin: page margins as a CSS ``margin`` shorthand (1-4 lengths), e.g. ``20mm``
        or ``22mm 18mm 25mm``. Header and footer are printed inside the top/bottom margins.
    :ivar print_link_urls: print the target URL after external links (useful on paper).
    """

    SIZES_MM: ClassVar[dict[str, tuple[float, float]]] = {
        'A3': (297, 420),
        'A4': (210, 297),
        'A5': (148, 210),
        'B5': (176, 250),
        'Letter': (215.9, 279.4),
        'Legal': (215.9, 355.6),
        'Tabloid': (279.4, 431.8),
    }

    size: PageSize = 'A4'
    orientation: PageOrientation = 'portrait'
    margin: str = '20mm 18mm'
    print_link_urls: bool = False

    def validate(self, path: str = '') -> None:
        """Validate the margin shorthand."""
        try:
            prefix = f'{path}.' if path else ''
            validate_css_value(self.margin, f'{prefix}margin')
            parts = self.margin.split()
            if not 1 <= len(parts) <= 4:
                raise ConfigError(
                    f'{prefix}margin: expected 1 to 4 CSS lengths, got {self.margin!r}'
                )
        except ConfigError:
            raise
        except Exception as exc:
            raise ConfigError(f'{path}: invalid page configuration ({exc})') from exc

    def dimensions_mm(self) -> tuple[float, float]:
        """Return ``(width, height)`` of the page in millimetres, honouring orientation."""
        try:
            width, height = self.SIZES_MM[self.size]
            if self.orientation == 'landscape':
                return height, width
            return width, height
        except KeyError as exc:
            raise ConfigError(f'Unknown page size {self.size!r}') from exc

    def margins(self) -> tuple[str, str, str, str]:
        """Expand ``margin`` into ``(top, right, bottom, left)`` like CSS does."""
        try:
            parts = self.margin.split()
            if len(parts) == 1:
                return parts[0], parts[0], parts[0], parts[0]
            if len(parts) == 2:
                return parts[0], parts[1], parts[0], parts[1]
            if len(parts) == 3:
                return parts[0], parts[1], parts[2], parts[1]
            return parts[0], parts[1], parts[2], parts[3]
        except Exception as exc:
            raise ConfigError(f'Invalid page margin {self.margin!r}: {exc}') from exc


@dataclass
class MarkdownConfig(ConfigSection):
    """Markdown parsing options.

    :ivar raw_html: pass raw HTML in the Markdown through to the output.
    :ivar safe_mode: escape all raw HTML regardless of ``raw_html`` (for untrusted input).
    :ivar typographer: smart quotes, dashes and ellipses.
    :ivar breaks: treat single newlines as ``<br>``.
    :ivar autolinks: turn bare URLs / e-mail addresses into links (GFM autolinks).
    :ivar heading_anchors: add ``id`` attributes to headings (needed for the TOC).
    :ivar heading_permalinks: add a visible permalink symbol to each heading.
    :ivar math: recognise ``$inline$`` and ``$$block$$`` math.
    :ivar math_renderer: ``none`` keeps math as styled text; ``mathjax`` loads MathJax.
    :ivar code_line_numbers: show line numbers in fenced code blocks.
    :ivar guess_language: guess the language of fences without an info string.
    :ivar figures: render a titled image that stands alone in a paragraph as a ``<figure>``
        whose ``<figcaption>`` is the title (``![alt](src "Caption")``).
    """

    raw_html: bool = True
    safe_mode: bool = False
    typographer: bool = False
    breaks: bool = False
    autolinks: bool = True
    heading_anchors: bool = True
    heading_permalinks: bool = False
    math: bool = True
    math_renderer: MathRenderer = 'none'
    code_line_numbers: bool = False
    guess_language: bool = False
    figures: bool = True

    @property
    def allow_html(self) -> bool:
        """Whether raw HTML is passed through (``raw_html`` and not ``safe_mode``)."""
        try:
            return self.raw_html and not self.safe_mode
        except MdHtmlError:
            raise
        except Exception as exc:
            raise ConfigError(f'Unexpected error in MarkdownConfig.allow_html: {exc}') from exc


@dataclass
class BuildConfig(ConfigSection):
    """Build / output options.

    :ivar output: output file or directory (default: next to each source file).
    :ivar recursive: descend into sub-directories of directory inputs.
    :ivar assets: what to do with local images/files referenced by pages:
        ``copy`` them next to the output, ``rewrite`` paths to point at the originals,
        ``embed`` images as data URIs, or ``keep`` paths untouched.
    :ivar rewrite_md_links: rewrite relative links to ``*.md`` files to ``*.html``.
    :ivar inline_css: inline the stylesheet in every page (otherwise write a shared CSS file).
    :ivar date_format: ``strftime`` format for ``{date}``/``{build_date}``.
    :ivar strict: turn warnings (missing images, unknown code languages) into errors.
    :ivar exclude: glob patterns (matched against relative paths) to skip in directories.
    :ivar format: what to generate: ``html`` (default), ``pdf`` (only PDF files; the HTML
        is rendered to a temporary file and removed) or ``both``. PDF export uses the
        ``pdf`` section.
    """

    output: str | None = None
    format: OutputFormat = 'html'
    recursive: bool = True
    assets: AssetMode = 'copy'
    rewrite_md_links: bool = True
    inline_css: bool = True
    date_format: str = '%Y-%m-%d'
    strict: bool = False
    exclude: list[str] = field(default_factory=lambda: ['node_modules/*', '_build/*'])

    def validate(self, path: str = '') -> None:
        """Validate the date format."""
        try:
            prefix = f'{path}.' if path else ''
            if not self.date_format:
                raise ConfigError(f'{prefix}date_format must not be empty')
            date(2000, 1, 2).strftime(self.date_format)
        except ConfigError:
            raise
        except Exception as exc:
            raise ConfigError(f'{path}.date_format: invalid strftime format ({exc})') from exc


@dataclass
class PdfConfig(ConfigSection):
    """PDF export options (used when ``build.format`` is ``pdf`` or ``both``).

    The PDF is printed from the generated HTML with the theme's print stylesheet, so it has
    the same page size, margins, running header/footer and page numbers as the browser's
    "Save as PDF".

    :ivar backend: ``auto`` (WeasyPrint if usable, else headless Chrome/Chromium; backends
        that run JavaScript first when MathJax is on), ``weasyprint``, ``chrome`` or the
        name of any other registered :class:`~md_html_builder.pdf.PdfBackend`.
    :ivar chrome_path: Chrome/Chromium/Edge executable (a path or a command on ``PATH``);
        auto-detected when unset.
    :ivar timeout: seconds allowed for one page export before it is aborted (applies to
        Chrome and to WeasyPrint running in a child process; WeasyPrint rendering inside
        the current process cannot be interrupted).
    """

    backend: str = 'auto'
    chrome_path: str | None = None
    timeout: float = 120.0

    @staticmethod
    def backend_names() -> list[str]:
        """Valid ``backend`` values: ``auto`` plus every registered PDF backend."""
        try:
            from .pdf import PdfBackend  # local import: pdf.py imports this module

            return ['auto', *PdfBackend.registry]
        except Exception as exc:
            raise ConfigError(f'Could not list the PDF backends: {exc}') from exc

    def validate(self, path: str = '') -> None:
        """Validate the backend name, the timeout and the Chrome path."""
        try:
            prefix = f'{path}.' if path else ''
            names = self.backend_names()
            if self.backend not in names:
                raise ConfigError(
                    f'{prefix}backend: {self.backend!r} is not one of {", ".join(names)}'
                )
            if not math.isfinite(self.timeout) or not 0 < self.timeout <= 86400:
                raise ConfigError(
                    f'{prefix}timeout must be between 0 and 86400 seconds, got {self.timeout}'
                )
            if self.chrome_path is not None and not self.chrome_path.strip():
                raise ConfigError(f'{prefix}chrome_path must not be empty')
        except ConfigError:
            raise
        except Exception as exc:
            raise ConfigError(f'{path}: invalid PDF configuration ({exc})') from exc


@dataclass
class Config(ConfigSection):
    """Root configuration object.

    :ivar title: site title (``{site_title}``) and fallback page title.
    :ivar lang: ``<html lang>`` value (front matter ``lang`` overrides per page).
    :ivar author: default author meta / ``{author}``.
    :ivar description: default description meta / ``{description}``.
    :ivar head_html: trusted raw HTML injected at the end of ``<head>``.
    """

    title: str | None = None
    lang: str = 'en'
    author: str | None = None
    description: str | None = None
    head_html: str = ''
    layout: LayoutConfig = field(default_factory=LayoutConfig)
    style: StyleConfig = field(default_factory=StyleConfig)
    header: HeaderConfig = field(default_factory=HeaderConfig)
    footer: FooterConfig = field(default_factory=FooterConfig)
    page_numbers: PageNumberConfig = field(default_factory=PageNumberConfig)
    cover: CoverConfig = field(default_factory=CoverConfig)
    contents_page: ContentsPageConfig = field(default_factory=ContentsPageConfig)
    page: PageConfig = field(default_factory=PageConfig)
    markdown: MarkdownConfig = field(default_factory=MarkdownConfig)
    build: BuildConfig = field(default_factory=BuildConfig)
    pdf: PdfConfig = field(default_factory=PdfConfig)

    def validate(self, path: str = '') -> None:
        """Validate root-level values."""
        try:
            if not re.match(r'^[A-Za-z]{1,8}(-[A-Za-z0-9]{1,8})*$', self.lang):
                raise ConfigError(f'lang: {self.lang!r} is not a valid language tag')
        except ConfigError:
            raise
        except Exception as exc:
            raise ConfigError(f'invalid root configuration ({exc})') from exc

    def with_overrides(self, overrides: dict[str, Any]) -> Config:
        """Return a new Config with ``overrides`` (a nested mapping) deep-merged on top.

        :raises ConfigError: if the merged result is invalid.
        """
        try:
            merged = ConfigLoader.deep_merge(self.to_dict(), overrides)
            return Config.from_dict(merged)
        except ConfigError:
            raise
        except Exception as exc:
            raise ConfigError(f'Could not apply configuration overrides: {exc}') from exc


class ConfigLoader:
    """Reads configuration files and turns CLI-style assignments into override mappings."""

    SUPPORTED_SUFFIXES: ClassVar[tuple[str, ...]] = ('.yaml', '.yml', '.json', '.toml')
    _PATH_KEYS: ClassVar[tuple[tuple[str, str], ...]] = (('build', 'output'), ('cover', 'logo'))

    def load(self, path: str | Path) -> Config:
        """Load and validate a configuration file.

        Relative paths inside the file (``style.custom_css``, ``build.output`` and a
        path-like ``style.theme``) are resolved relative to the file's directory.

        :raises ConfigError: if the file is missing, unreadable, malformed or invalid.
        """
        try:
            config_path = Path(path)
            data = self.read_mapping(config_path)
            self.resolve_paths(data, config_path.resolve().parent)
            config = Config.from_dict(data)
            logger.debug(f'Loaded configuration from {config_path}')
            return config
        except ConfigError as exc:
            raise ConfigError(f'{path}: {exc}') from exc
        except Exception as exc:
            raise ConfigError(f'Could not load configuration {path}: {exc}') from exc

    def read_mapping(self, path: Path) -> dict[str, Any]:
        """Parse a config file into a dict (empty file gives ``{}``).

        :raises ConfigError: on a missing file, unsupported suffix or parse error.
        """
        try:
            if not path.is_file():
                raise ConfigError(f'configuration file not found: {path}')
            suffix = path.suffix.lower()
            if suffix not in self.SUPPORTED_SUFFIXES:
                supported = ', '.join(self.SUPPORTED_SUFFIXES)
                raise ConfigError(f'unsupported config format {suffix!r} (use {supported})')
            text = path.read_text(encoding='utf-8-sig')
            if suffix == '.json':
                data = json.loads(text) if text.strip() else {}
            elif suffix == '.toml':
                data = tomllib.loads(text)
            else:
                data = yaml.safe_load(text)
            if data is None:
                data = {}
            if not isinstance(data, dict):
                raise ConfigError(f'top level must be a mapping, got {type(data).__name__}')
            return data
        except ConfigError:
            raise
        except (yaml.YAMLError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f'malformed configuration file: {exc}') from exc
        except UnicodeDecodeError as exc:
            raise ConfigError(f'configuration file is not valid UTF-8: {exc}') from exc
        except OSError as exc:
            raise ConfigError(f'could not read configuration file: {exc}') from exc

    @staticmethod
    def resolve_path(value: str, base_dir: Path) -> str:
        """Expand ``~`` in ``value`` and resolve it against ``base_dir`` if it is relative."""
        try:
            return str((base_dir / Path(value).expanduser()).resolve())
        except Exception as exc:
            raise ConfigError(f'could not resolve path {value!r}: {exc}') from exc

    @staticmethod
    def resolve_paths(data: dict[str, Any], base_dir: Path) -> None:
        """Resolve relative file paths in raw config ``data`` against ``base_dir`` in place."""
        try:
            style = data.get('style')
            if isinstance(style, dict):
                css = style.get('custom_css')
                if isinstance(css, str):
                    css = [css]
                if isinstance(css, list):
                    style['custom_css'] = [
                        ConfigLoader.resolve_path(item, base_dir) if isinstance(item, str) else item
                        for item in css
                    ]
                theme = style.get('theme')
                if isinstance(theme, str) and ('/' in theme or theme.endswith('.css')):
                    style['theme'] = ConfigLoader.resolve_path(theme, base_dir)
            for section, key in ConfigLoader._PATH_KEYS:
                block = data.get(section)
                value = block.get(key) if isinstance(block, dict) else None
                # URLs (https://..., data:...) are not files; leave them untouched.
                if isinstance(value, str) and value.strip() and not _URL_SCHEME.match(value):
                    block[key] = ConfigLoader.resolve_path(value, base_dir)
            pdf = data.get('pdf')
            if isinstance(pdf, dict) and isinstance(pdf.get('chrome_path'), str):
                # A bare command name ('chromium') is looked up on PATH; only relative
                # paths with a directory part are resolved against the config file.
                chrome = pdf['chrome_path']
                has_dir = '/' in chrome or '\\' in chrome
                if has_dir:
                    pdf['chrome_path'] = ConfigLoader.resolve_path(chrome, base_dir)
        except Exception as exc:
            raise ConfigError(f'could not resolve paths in configuration: {exc}') from exc

    @staticmethod
    def parse_assignment(assignment: str) -> dict[str, Any]:
        """Turn ``'layout.max_width=60rem'`` into ``{'layout': {'max_width': '60rem'}}``.

        The value is parsed as YAML so ``true``, ``3`` and ``[a, b]`` get proper types.

        :raises ConfigError: if the assignment is malformed.
        """
        try:
            if '=' not in assignment:
                raise ConfigError(f'invalid --set {assignment!r}; expected KEY=VALUE')
            key, raw_value = assignment.split('=', 1)
            key = key.strip()
            if not _DOTTED_KEY.match(key):
                raise ConfigError(f'invalid --set key {key!r}; expected dotted.name')
            try:
                value = yaml.safe_load(raw_value) if raw_value.strip() else ''
            except yaml.YAMLError:
                value = raw_value
            if isinstance(value, date):
                value = raw_value
            elif isinstance(value, dict):
                # Only an explicit flow mapping such as {accent: red} is a mapping; text like
                # 'My: Docs' or a template like '{date}' stays a string.
                explicit = raw_value.strip().startswith('{')
                if not explicit or any(v is None for v in value.values()):
                    value = raw_value
            result: dict[str, Any] = {}
            cursor = result
            parts = key.split('.')
            for part in parts[:-1]:
                cursor = cursor.setdefault(part, {})
            cursor[parts[-1]] = value
            return result
        except ConfigError:
            raise
        except Exception as exc:
            raise ConfigError(f'could not parse --set {assignment!r}: {exc}') from exc

    @staticmethod
    def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
        """Recursively merge ``override`` into a copy of ``base`` and return it."""
        try:
            result = copy.deepcopy(base)
            for key, value in override.items():
                norm = key.replace('-', '_') if isinstance(key, str) else key
                if isinstance(value, dict) and isinstance(result.get(norm), dict):
                    result[norm] = ConfigLoader.deep_merge(result[norm], value)
                else:
                    result[norm] = copy.deepcopy(value)
            return result
        except Exception as exc:
            raise ConfigError(f'could not merge configuration: {exc}') from exc
