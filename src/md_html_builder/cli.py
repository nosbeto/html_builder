"""Command-line interface: ``md2html`` (or ``python -m md_html_builder``).

Examples::

    md2html README.md                          # writes README.html next to it
    md2html docs/ -o site/ --theme default --toc left --page-numbers
    md2html notes.md --header-template '{title}' --footer --page-numbers
    md2html notes.md -c md2html.yaml --color accent=#e8590c --set layout.max_width=60rem
    md2html report.md -o report.pdf            # PDF instead of HTML
    md2html docs/ -o site/ --format both       # HTML pages plus a PDF of each

Precedence (later wins): defaults, ``--config`` file, dedicated flags, ``--set`` assignments.
Exit status: 0 on success, 1 on a conversion/config error, 2 on usage errors.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, get_args

import yaml

from . import __version__
from .builder import SiteBuilder
from .config import (
    Align,
    AssetMode,
    Config,
    ConfigLoader,
    LayoutView,
    OutputFormat,
    PageConfig,
    PageOrientation,
    PdfConfig,
    ThemeMode,
    TocPosition,
)
from .errors import ConfigError, MdHtmlError
from .highlighter import CodeHighlighter
from .themes import ThemeRegistry

_URL_PREFIX = re.compile(r'^([A-Za-z][A-Za-z0-9+.-]+:|//)')

__all__ = ['CommandLine', 'main']

logger = logging.getLogger('md_html_builder')


class _CliFormatter(logging.Formatter):
    """Plain messages for INFO, ``level: message`` for everything else."""

    def format(self, record: logging.LogRecord) -> str:
        """Format a log record for the terminal."""
        try:
            message = super().format(record)
            if record.levelno == logging.INFO:
                return message
            return f'{record.levelname.lower()}: {message}'
        except MdHtmlError:
            raise
        except Exception as exc:
            raise RuntimeError(f'Unexpected error in _CliFormatter.format: {exc}') from exc


class CommandLine:
    """The ``md2html`` command.

    :param stdout: stream for normal output (listings, printed config).
    """

    def __init__(self, stdout: Any = None) -> None:
        try:
            self.stdout = stdout or sys.stdout
            self.loader = ConfigLoader()
            self.parser = self.build_parser()
        except MdHtmlError:
            raise
        except Exception as exc:
            raise RuntimeError(f'Unexpected error in CommandLine.__init__: {exc}') from exc

    @staticmethod
    def build_parser() -> argparse.ArgumentParser:
        """Create the argument parser."""
        try:
            parser = argparse.ArgumentParser(
                prog='md2html',
                description='Convert Markdown files into customisable, styled HTML.',
                epilog=(
                    'Any option can also be set with --set KEY=VALUE, e.g. --set layout.toc=left'
                ),
            )
            parser.add_argument('inputs', nargs='*', help='Markdown files and/or directories')
            parser.add_argument(
                '-o',
                '--output',
                help='output .html/.pdf file or directory (a .pdf file implies --format pdf)',
            )
            parser.add_argument(
                '-c', '--config', help='configuration file (.yaml, .yml, .json, .toml)'
            )
            parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
            verbosity = parser.add_mutually_exclusive_group()
            verbosity.add_argument('-v', '--verbose', action='store_true', help='debug output')
            verbosity.add_argument(
                '-q', '--quiet', action='store_true', help='only warnings and errors'
            )

            info = parser.add_argument_group('information')
            info.add_argument('--list-themes', action='store_true', help='list built-in themes')
            info.add_argument(
                '--list-code-styles', action='store_true', help='list Pygments styles'
            )
            info.add_argument('--print-config', action='store_true', help='print effective config')

            page = parser.add_argument_group('page')
            page.add_argument('--title', help='site title (fallback page title)')
            page.add_argument('--lang', help='document language, e.g. en')
            page.add_argument('--author', help='default author')

            style = parser.add_argument_group('style')
            style.add_argument('--theme', help='theme name or path to theme directory/.css')
            style.add_argument('--mode', choices=get_args(ThemeMode), help='colour scheme')
            style.add_argument('--font-body', help='body font stack')
            style.add_argument('--font-heading', help='heading font stack')
            style.add_argument('--font-code', help='code font stack')
            style.add_argument('--font-size', help='base font size, e.g. 17px')
            style.add_argument('--line-height', help='base line height, e.g. 1.7')
            style.add_argument('--code-style', help='Pygments style for code (light)')
            style.add_argument('--code-style-dark', help='Pygments style for code (dark)')
            style.add_argument(
                '--color', action='append', default=[], metavar='KEY=VALUE', help='colour override'
            )
            style.add_argument(
                '--dark-color',
                action='append',
                default=[],
                metavar='KEY=VALUE',
                help='dark-mode colour override',
            )
            style.add_argument(
                '--var',
                action='append',
                default=[],
                metavar='NAME=VALUE',
                help='CSS custom property override',
            )
            style.add_argument(
                '--css',
                action='append',
                default=[],
                metavar='PATH',
                help='extra CSS file (repeatable)',
            )
            style.add_argument(
                '--font-url',
                action='append',
                default=[],
                metavar='URL',
                help='web-font stylesheet URL, e.g. Google Fonts (repeatable)',
            )

            layout = parser.add_argument_group('layout')
            layout.add_argument(
                '--view',
                choices=get_args(LayoutView),
                help='paged: document shown as a printed sheet (default); web: classic web page',
            )
            layout.add_argument(
                '--page-size',
                choices=list(PageConfig.SIZES_MM),
                help='paper size for the paged view, printing and PDF',
            )
            layout.add_argument(
                '--orientation', choices=get_args(PageOrientation), help='paper orientation'
            )
            layout.add_argument('--margin', help='page margins (CSS shorthand), e.g. "20mm 18mm"')
            layout.add_argument('--toc', choices=get_args(TocPosition), help='TOC position')
            layout.add_argument('--toc-depth', type=int, help='deepest heading level in the TOC')
            layout.add_argument('--max-width', help='content column width, e.g. 60rem')
            layout.add_argument('--align', choices=get_args(Align), help='page alignment')
            layout.add_argument(
                '--nav',
                action=argparse.BooleanOptionalAction,
                default=None,
                help='multi-page navigation and previous/next links',
            )

            bars = parser.add_argument_group('header, footer and page numbers')
            for bar, example in (('header', '{title}'), ('footer', '{date}')):
                bars.add_argument(
                    f'--{bar}',
                    action=argparse.BooleanOptionalAction,
                    default=None,
                    help=f'show the {bar} (with its configured or default template)',
                )
                bars.add_argument(
                    f'--{bar}-template',
                    metavar='TEMPLATE',
                    help=f'{bar} text/HTML, e.g. "{example}"; implies --{bar}',
                )
            bars.add_argument(
                '--page-numbers',
                action=argparse.BooleanOptionalAction,
                default=None,
                help='show page numbers (screen and print)',
            )

            front = parser.add_argument_group('cover page and contents page')
            front.add_argument(
                '--cover',
                action=argparse.BooleanOptionalAction,
                default=None,
                help='add a cover page (title, subtitle, authors, details, date, logo)',
            )
            front.add_argument('--cover-title', metavar='TEXT', help='cover title; implies --cover')
            front.add_argument(
                '--cover-subtitle', metavar='TEXT', help='line under the title; implies --cover'
            )
            front.add_argument(
                '--cover-author',
                action='append',
                default=[],
                metavar='NAME',
                help='author on the cover (repeatable); implies --cover',
            )
            front.add_argument(
                '--cover-detail',
                action='append',
                default=[],
                metavar='TEXT',
                help='extra cover line, e.g. a course (repeatable); implies --cover',
            )
            front.add_argument(
                '--cover-date', metavar='TEXT', help='date line ("" hides it); implies --cover'
            )
            front.add_argument(
                '--cover-logo', metavar='PATH', help='image above the title; implies --cover'
            )
            front.add_argument(
                '--contents-page',
                action=argparse.BooleanOptionalAction,
                default=None,
                help='add a printed contents page with page numbers',
            )
            front.add_argument(
                '--contents-depth',
                type=int,
                metavar='N',
                help='deepest heading level on the contents page; implies --contents-page',
            )

            md = parser.add_argument_group('markdown')
            md.add_argument('--safe', action='store_true', help='escape raw HTML (untrusted input)')
            md.add_argument(
                '--no-raw-html', action='store_true', help='do not pass raw HTML through'
            )
            md.add_argument(
                '--line-numbers', action='store_true', help='line numbers in code blocks'
            )

            build = parser.add_argument_group('build')
            build.add_argument(
                '--assets',
                choices=get_args(AssetMode),
                help='local asset handling',
            )
            build.add_argument(
                '--link-css',
                action='store_true',
                help='write one shared CSS file instead of inlining',
            )
            build.add_argument('--strict', action='store_true', help='treat warnings as errors')
            build.add_argument(
                '--no-recursive', action='store_true', help='do not descend into sub-directories'
            )
            output = parser.add_argument_group('output format and PDF')
            output.add_argument(
                '--format',
                choices=get_args(OutputFormat),
                help='generate HTML (default), PDF only, or both',
            )
            output.add_argument(
                '--pdf-backend',
                choices=PdfConfig.backend_names(),
                help='PDF renderer (auto: WeasyPrint if installed, else headless Chrome)',
            )
            output.add_argument(
                '--chrome-path',
                metavar='PATH',
                help='Chrome/Chromium/Edge executable for PDF export',
            )
            output.add_argument(
                '--pdf-timeout',
                type=float,
                metavar='SECONDS',
                help='time limit per PDF page export',
            )

            build.add_argument(
                '--set',
                action='append',
                default=[],
                metavar='KEY=VALUE',
                dest='assignments',
                help='set any config value by dotted key (repeatable)',
            )
            return parser
        except MdHtmlError:
            raise
        except Exception as exc:
            raise RuntimeError(f'Unexpected error in CommandLine.build_parser: {exc}') from exc

    @staticmethod
    def _pairs(items: list[str], option: str) -> dict[str, str]:
        """Parse repeated ``KEY=VALUE`` options."""
        try:
            result: dict[str, str] = {}
            for item in items:
                key, sep, value = item.partition('=')
                if not sep or not key.strip():
                    raise ConfigError(f'{option} expects KEY=VALUE, got {item!r}')
                result[key.strip()] = value.strip()
            return result
        except MdHtmlError:
            raise
        except Exception as exc:
            raise ConfigError(f'Unexpected error in CommandLine._pairs: {exc}') from exc

    @staticmethod
    def _front_pages(args: argparse.Namespace, put: Any) -> None:
        """Add the cover and contents page options; a detail option implies its page."""
        try:
            logo = args.cover_logo
            if logo is not None and logo.strip() and not _URL_PREFIX.match(logo):
                logo = str(Path(logo).expanduser().resolve())  # relative to where md2html runs
            cover = {
                'title': args.cover_title,
                'subtitle': args.cover_subtitle,
                'authors': args.cover_author or None,
                'details': args.cover_detail or None,
                'date': args.cover_date,
                'logo': logo,
            }
            enabled = args.cover
            if any(value is not None for value in cover.values()):
                if enabled is False:
                    raise ConfigError('--cover-* options and --no-cover cannot be combined')
                enabled = True
            for key, value in cover.items():
                put(f'cover.{key}', value)
            put('cover.enabled', enabled)
            contents = args.contents_page
            if args.contents_depth is not None:
                if contents is False:
                    raise ConfigError('--contents-depth and --no-contents-page cannot be combined')
                contents = True
                put('contents_page.depth', args.contents_depth)
            put('contents_page.enabled', contents)
        except MdHtmlError:
            raise
        except Exception as exc:
            raise ConfigError(f'Unexpected error in CommandLine._front_pages: {exc}') from exc

    def overrides(self, args: argparse.Namespace) -> dict[str, Any]:
        """Translate parsed arguments into a nested override mapping."""
        try:
            data: dict[str, Any] = {}

            def put(path: str, value: Any) -> None:
                try:
                    if value is None:
                        return
                    cursor = data
                    parts = path.split('.')
                    for part in parts[:-1]:
                        cursor = cursor.setdefault(part, {})
                    cursor[parts[-1]] = value
                except MdHtmlError:
                    raise
                except Exception as exc:
                    raise ConfigError(
                        f'Unexpected error in CommandLine.overrides.put: {exc}'
                    ) from exc

            put('title', args.title)
            put('lang', args.lang)
            put('author', args.author)
            put('style.theme', args.theme)
            put('style.mode', args.mode)
            put('style.font_body', args.font_body)
            put('style.font_heading', args.font_heading)
            put('style.font_code', args.font_code)
            put('style.font_size', args.font_size)
            put('style.line_height', args.line_height)
            put('style.code_style', args.code_style)
            put('style.code_style_dark', args.code_style_dark)
            put('style.colors', self._pairs(args.color, '--color') or None)
            put('style.colors_dark', self._pairs(args.dark_color, '--dark-color') or None)
            put('style.variables', self._pairs(args.var, '--var') or None)
            put('style.font_url', args.font_url or None)
            put('layout.view', args.view)
            put('page.size', args.page_size)
            put('page.orientation', args.orientation)
            put('page.margin', args.margin)
            put('layout.toc', args.toc)
            put('layout.toc_depth', args.toc_depth)
            put('layout.max_width', args.max_width)
            put('layout.align', args.align)
            put('layout.navigation', args.nav)
            for bar in ('header', 'footer'):
                enabled = getattr(args, bar)
                template = getattr(args, f'{bar}_template')
                if template is not None:
                    if enabled is False:
                        raise ConfigError(f'--{bar}-template and --no-{bar} cannot be combined')
                    enabled = True
                    put(f'{bar}.template', template)
                put(f'{bar}.enabled', enabled)
            put('page_numbers.enabled', args.page_numbers)
            self._front_pages(args, put)
            if args.safe:
                put('markdown.safe_mode', True)
            if args.no_raw_html:
                put('markdown.raw_html', False)
            if args.line_numbers:
                put('markdown.code_line_numbers', True)
            put('build.assets', args.assets)
            if args.link_css:
                put('build.inline_css', False)
            if args.strict:
                put('build.strict', True)
            if args.no_recursive:
                put('build.recursive', False)
            put('build.format', args.format)
            put('pdf.backend', args.pdf_backend)
            put('pdf.chrome_path', args.chrome_path)
            put('pdf.timeout', args.pdf_timeout)
            for assignment in args.assignments:
                data = ConfigLoader.deep_merge(data, ConfigLoader.parse_assignment(assignment))
            return data
        except ConfigError:
            raise
        except Exception as exc:
            raise ConfigError(f'Invalid command-line options: {exc}') from exc

    def load_config(self, args: argparse.Namespace) -> Config:
        """Build the effective configuration from the config file and CLI options."""
        try:
            base = self.loader.load(args.config) if args.config else Config()
            config = base.with_overrides(self.overrides(args))
            if args.css:
                extra = [str(Path(p).expanduser().resolve()) for p in args.css]
                config = config.with_overrides(
                    {'style': {'custom_css': [*config.style.custom_css, *extra]}}
                )
            return config
        except ConfigError:
            raise
        except Exception as exc:
            raise ConfigError(f'Could not build configuration: {exc}') from exc

    @staticmethod
    def configure_logging(verbose: bool, quiet: bool) -> None:
        """Send package logs to stderr at the requested verbosity."""
        try:
            level = logging.DEBUG if verbose else logging.WARNING if quiet else logging.INFO
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(_CliFormatter('%(message)s'))
            logger.handlers[:] = [handler]
            logger.setLevel(level)
            logger.propagate = False
        except MdHtmlError:
            raise
        except Exception as exc:
            raise RuntimeError(f'Unexpected error in CommandLine.configure_logging: {exc}') from exc

    def _print(self, text: str) -> None:
        """Write a line to stdout."""
        try:
            print(text, file=self.stdout)
        except MdHtmlError:
            raise
        except Exception as exc:
            raise RuntimeError(f'Unexpected error in CommandLine._print: {exc}') from exc

    def run(self, argv: Sequence[str] | None = None) -> int:
        """Run the command and return the exit status."""
        args = self.parser.parse_args(argv)
        self.configure_logging(args.verbose, args.quiet)
        try:
            if args.list_themes:
                for name, description in ThemeRegistry().describe().items():
                    self._print(f'{name}\t{description}'.rstrip())
                return 0
            if args.list_code_styles:
                self._print('\n'.join(CodeHighlighter.available_styles()))
                return 0
            config = self.load_config(args)
            if args.print_config:
                self._print(yaml.safe_dump(config.to_dict(), sort_keys=False).rstrip())
                return 0
            if not args.inputs:
                self.parser.error('at least one input file or directory is required')
            result = SiteBuilder(config).build(args.inputs, args.output)
            noun = 'page' if len(result.pages) == 1 else 'pages'
            kind = {'html': '', 'pdf': ' as PDF', 'both': ' as HTML and PDF'}[result.format]
            logger.info(f'Built {len(result.pages)} {noun}{kind}')
            return 0
        except MdHtmlError as exc:
            logger.error(str(exc))
            logger.debug('Traceback:', exc_info=True)
            return 1
        except KeyboardInterrupt:
            logger.error('Interrupted')
            return 130


def main(argv: Sequence[str] | None = None) -> int:
    """Console-script entry point."""
    try:
        return CommandLine().run(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 2)
