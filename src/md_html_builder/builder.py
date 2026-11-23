"""Build orchestration: single conversions and multi-file sites.

* :class:`MarkdownConverter` wires the theme, highlighter, parser, stylesheet and page
  renderer together and converts one Markdown text to a complete HTML page.
* :class:`SourceCollector` expands input files/directories into an ordered list of sources.
* :class:`SiteBuilder` converts one or many files, decides output paths, numbers pages,
  builds navigation, handles assets and writes the results as HTML, PDF or both
  (``build.format``; PDF export is delegated to :class:`~md_html_builder.pdf.PdfExporter`).
"""

from __future__ import annotations

import fnmatch
import logging
import math
import os
import re
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .assets import MARKDOWN_SUFFIXES, AssetResolver, LinkProcessor
from .config import Config
from .errors import BuildError, InputError, MdHtmlError
from .highlighter import CodeHighlighter
from .parser import MarkdownParser, ParsedDocument
from .pdf import PdfExporter, PdfResult
from .renderer import NavEntry, PageContext, PageRenderer
from .themes import StylesheetBuilder, Theme, ThemeRegistry

__all__ = ['BuildResult', 'MarkdownConverter', 'SiteBuilder', 'SourceCollector', 'SourceFile']

logger = logging.getLogger(__name__)

HTML_SUFFIXES = ('.html', '.htm')
PDF_SUFFIX = '.pdf'
#: A table is reported as too wide for print when it has more columns than the page
#: width in millimetres divided by this (A4 portrait: 11 columns, landscape: 16).
WIDE_TABLE_MM_PER_COLUMN = 18
SHARED_STYLESHEET = Path('assets') / 'md-html-builder.css'
_URL_PREFIX = re.compile(r'^([A-Za-z][A-Za-z0-9+.-]+:|//)')


@dataclass(frozen=True)
class SourceFile:
    """A Markdown source and its path relative to the build root."""

    path: Path
    relative: Path


@dataclass
class BuildResult:
    """Outcome of a build.

    :ivar pages: ``(source, output)`` pairs in page order. The output is the HTML file, or
        the PDF file when only PDF was generated (``build.format: pdf``).
    :ivar stylesheet: path of the shared stylesheet, if one was written.
    :ivar output_root: root output directory.
    :ivar format: what was generated: ``html``, ``pdf`` or ``both``.
    :ivar pdfs: PDF export results in page order (empty for ``html``).
    """

    pages: list[tuple[Path, Path]] = field(default_factory=list)
    stylesheet: Path | None = None
    output_root: Path | None = None
    format: str = 'html'
    pdfs: list[PdfResult] = field(default_factory=list)

    @property
    def outputs(self) -> list[Path]:
        """Primary output paths in page order (HTML, or PDF for PDF-only builds)."""
        try:
            return [out for _src, out in self.pages]
        except MdHtmlError:
            raise
        except Exception as exc:
            raise BuildError(f'Unexpected error in BuildResult.outputs: {exc}') from exc

    @property
    def pdf_outputs(self) -> list[Path]:
        """Written PDF files in page order."""
        try:
            return [pdf.path for pdf in self.pdfs]
        except MdHtmlError:
            raise
        except Exception as exc:
            raise BuildError(f'Unexpected error in BuildResult.pdf_outputs: {exc}') from exc


class MarkdownConverter:
    """Converts Markdown text into complete HTML documents using one configuration.

    :param config: configuration (defaults to ``Config()``).
    :param registry: theme registry (defaults to built-in themes).
    """

    def __init__(self, config: Config | None = None, registry: ThemeRegistry | None = None) -> None:
        try:
            self.config = config or Config()
            self.registry = registry or ThemeRegistry()
            self.theme: Theme = self.registry.load(self.config.style.theme)
            style = self.config.style
            self.highlighter = CodeHighlighter(
                style=style.code_style or self.theme.code_style or 'default',
                dark_style=style.code_style_dark or self.theme.code_style_dark or 'monokai',
                mode=style.mode,
                line_numbers=self.config.markdown.code_line_numbers,
                guess_language=self.config.markdown.guess_language,
                strict=self.config.build.strict,
            )
            self.parser = MarkdownParser(self.config.markdown, self.highlighter)
            self.renderer = PageRenderer(self.config, self.theme)
            self._stylesheet: str | None = None
        except MdHtmlError:
            raise
        except Exception as exc:
            raise BuildError(f'Could not initialise converter: {exc}') from exc

    @property
    def stylesheet(self) -> str:
        """The complete stylesheet (built once, then cached)."""
        try:
            if self._stylesheet is None:
                self._stylesheet = StylesheetBuilder(
                    self.config, self.theme, self.highlighter
                ).build()
            return self._stylesheet
        except MdHtmlError:
            raise
        except Exception as exc:
            raise BuildError(f'Could not build stylesheet: {exc}') from exc

    def parse(self, text: str, source: str = '<string>') -> ParsedDocument:
        """Parse Markdown text (see :meth:`MarkdownParser.parse`)."""
        try:
            return self.parser.parse(text, source)
        except MdHtmlError:
            raise
        except Exception as exc:
            raise BuildError(f'Unexpected error in MarkdownConverter.parse: {exc}') from exc

    def cover_logo(
        self, document: ParsedDocument, context: PageContext, links: LinkProcessor
    ) -> str | None:
        """The cover logo URL for the page being written, or ``None`` without a logo.

        URLs are used as they are. A relative path is relative to the Markdown file; an
        absolute one (a config-file logo is made absolute when the config is loaded) is
        turned into a path relative to the Markdown file, so ``build.assets`` copies, embeds
        or rewrites it like any other image.

        :raises InputError: if the logo is a local file that does not exist.
        """
        try:
            raw = self.renderer.front_pages.logo(document)
            if raw is None:
                return None
            if _URL_PREFIX.match(raw):
                return raw
            path = Path(raw).expanduser()
            target = path if path.is_absolute() else None
            if target is None and context.source is not None:
                target = context.source.parent / path
            if target is not None and not target.is_file():
                raise InputError(f'{document.source}: cover logo not found: {target}')
            if not path.is_absolute():
                return links.resolve_image(quote(path.as_posix()), context.source, context.output)
            if context.source is None:
                return path.resolve().as_uri()
            try:
                relative = os.path.relpath(path.resolve(), context.source.parent.resolve())
            except ValueError:  # another drive on Windows: no relative path exists
                return path.resolve().as_uri()
            url = quote(Path(relative).as_posix())
            return links.resolve_image(url, context.source, context.output)
        except MdHtmlError:
            raise
        except Exception as exc:
            raise BuildError(f'{document.source}: could not resolve the cover logo: {exc}') from exc

    def render_document(
        self,
        document: ParsedDocument,
        context: PageContext | None = None,
        links: LinkProcessor | None = None,
    ) -> str:
        """Render a parsed document to a full HTML page.

        :param document: parsed document (its tokens are rewritten by ``links``).
        :param context: page context (defaults to a single standalone page).
        :param links: link processor; defaults to ``.md`` link rewriting without assets.
        """
        try:
            context = context or PageContext()
            links = links or LinkProcessor(None, self.config.build.rewrite_md_links)
            links.process(document, context.source, context.output)
            logo = self.cover_logo(document, context, links)
            if logo is not None:
                context = replace(context, cover_logo_src=logo)
            body = self.parser.render(document)
            css = '' if context.stylesheet_href else self.stylesheet
            return self.renderer.render(document, body, context, css)
        except MdHtmlError:
            raise
        except Exception as exc:
            raise BuildError(f'{document.source}: could not render page: {exc}') from exc

    def convert_text(self, text: str, context: PageContext | None = None) -> str:
        """Convert Markdown ``text`` to a complete HTML page."""
        try:
            source = str(context.source) if context and context.source else '<string>'
            return self.render_document(self.parse(text, source), context)
        except MdHtmlError:
            raise
        except Exception as exc:
            raise BuildError(f'Could not convert Markdown: {exc}') from exc

    def convert_fragment(self, text: str) -> str:
        """Convert Markdown ``text`` to an HTML body fragment (no page shell, no CSS)."""
        try:
            document = self.parse(text)
            LinkProcessor(None, self.config.build.rewrite_md_links).process(document, None, None)
            return self.parser.render(document)
        except MdHtmlError:
            raise
        except Exception as exc:
            raise BuildError(f'Could not convert Markdown fragment: {exc}') from exc


class SourceCollector:
    """Expands inputs into Markdown sources.

    :param recursive: descend into sub-directories.
    :param exclude: glob patterns matched against paths relative to each input directory.
    """

    def __init__(self, recursive: bool = True, exclude: list[str] | None = None) -> None:
        try:
            self.recursive = recursive
            self.exclude = list(exclude or [])
        except MdHtmlError:
            raise
        except Exception as exc:
            raise BuildError(f'Unexpected error in SourceCollector.__init__: {exc}') from exc

    def _excluded(self, relative: Path) -> bool:
        """True for hidden paths or paths matching an exclude pattern."""
        try:
            posix = relative.as_posix()
            if any(part.startswith('.') for part in relative.parts):
                return True
            return any(
                fnmatch.fnmatch(posix, pattern) or fnmatch.fnmatch(relative.name, pattern)
                for pattern in self.exclude
            )
        except MdHtmlError:
            raise
        except Exception as exc:
            raise BuildError(f'Unexpected error in SourceCollector._excluded: {exc}') from exc

    def _scan(self, directory: Path) -> list[Path]:
        """Return Markdown files in ``directory``."""
        try:
            pattern = '**/*' if self.recursive else '*'
            found = []
            for path in directory.glob(pattern):
                if not path.is_file() or path.suffix.lower() not in MARKDOWN_SUFFIXES:
                    continue
                if self._excluded(path.relative_to(directory)):
                    continue
                found.append(path)
            return found
        except OSError as exc:
            raise InputError(f'Could not scan directory {directory}: {exc}') from exc

    def collect(self, inputs: list[str | Path]) -> tuple[list[SourceFile], Path]:
        """Return ``(sources, root)`` where ``root`` is the common base directory.

        :raises InputError: for missing inputs or when no Markdown files are found.
        """
        try:
            if not inputs:
                raise InputError('No input files given')
            files: list[Path] = []
            bases: list[Path] = []
            for item in inputs:
                path = Path(item).expanduser()
                if not path.exists():
                    raise InputError(f'Input not found: {path}')
                path = path.resolve()
                if path.is_dir():
                    bases.append(path)
                    files.extend(self._scan(path))
                elif path.is_file():
                    if path.suffix.lower() not in MARKDOWN_SUFFIXES:
                        logger.warning(
                            f'{path} does not have a Markdown extension; converting anyway'
                        )
                    bases.append(path.parent)
                    files.append(path)
                else:
                    raise InputError(f'Input is not a regular file or directory: {path}')
            unique = sorted(set(files))
            if not unique:
                raise InputError(f'No Markdown files found in: {", ".join(map(str, inputs))}')
            root = Path(os.path.commonpath([str(b) for b in bases]))
            return [SourceFile(p, p.relative_to(root)) for p in unique], root
        except InputError:
            raise
        except Exception as exc:
            raise InputError(f'Could not collect input files: {exc}') from exc


class SiteBuilder:
    """Builds HTML for one or many Markdown files.

    :param config: configuration.
    :param converter: optional pre-built converter (mainly for testing).
    :param pdf_exporter: optional PDF exporter (defaults to one built from ``config.pdf``
        the first time a PDF is needed; injectable for tests and custom backends).
    """

    def __init__(
        self,
        config: Config | None = None,
        converter: MarkdownConverter | None = None,
        pdf_exporter: PdfExporter | None = None,
    ) -> None:
        try:
            self.config = config or Config()
            self.converter = converter or MarkdownConverter(self.config)
            self.collector = SourceCollector(self.config.build.recursive, self.config.build.exclude)
            self._pdf_exporter = pdf_exporter
        except MdHtmlError:
            raise
        except Exception as exc:
            raise BuildError(f'Could not initialise builder: {exc}') from exc

    @property
    def pdf_exporter(self) -> PdfExporter:
        """The PDF exporter (created on first use from ``config.pdf``)."""
        try:
            if self._pdf_exporter is None:
                markdown = self.config.markdown
                needs_javascript = markdown.math and markdown.math_renderer == 'mathjax'
                self._pdf_exporter = PdfExporter(self.config.pdf, needs_javascript=needs_javascript)
            return self._pdf_exporter
        except MdHtmlError:
            raise
        except Exception as exc:
            raise BuildError(f'Could not create the PDF exporter: {exc}') from exc

    # ------------------------------------------------------------------ planning

    def resolve_format(self, output: str | Path | None) -> str:
        """Return what to generate: ``build.format``, or ``pdf`` for a ``*.pdf`` output.

        An output path ending in ``.pdf`` with the default ``html`` format means "make a
        PDF", so ``md2html doc.md -o doc.pdf`` works without ``--format pdf``.

        :raises InputError: for a ``pdf`` build with an ``.html`` output file.
        """
        try:
            fmt = self.config.build.format
            suffix = Path(output).suffix.lower() if output is not None else ''
            if fmt == 'html' and suffix == PDF_SUFFIX:
                logger.debug(f'Output {output} ends in .pdf; generating PDF')
                return 'pdf'
            if fmt == 'pdf' and suffix in HTML_SUFFIXES:
                raise InputError(
                    f'Output {output} is an HTML file but build.format is "pdf"; '
                    'use a .pdf file, a directory, or --format both'
                )
            return fmt
        except InputError:
            raise
        except Exception as exc:
            raise InputError(f'Could not determine the output format: {exc}') from exc

    @staticmethod
    def pdf_path(html_path: Path) -> Path:
        """The PDF written for the HTML output ``html_path`` (same name, ``.pdf``)."""
        try:
            return html_path.with_suffix(PDF_SUFFIX)
        except ValueError as exc:
            raise BuildError(f'Cannot derive a PDF path from {html_path}: {exc}') from exc

    @staticmethod
    def _read(path: Path) -> str:
        """Read a Markdown source as UTF-8 (BOM tolerated)."""
        try:
            return path.read_text(encoding='utf-8-sig')
        except UnicodeDecodeError as exc:
            raise InputError(f'{path}: file is not valid UTF-8 ({exc})') from exc
        except OSError as exc:
            raise InputError(f'{path}: could not read file ({exc})') from exc

    def plan_outputs(
        self,
        sources: list[SourceFile],
        root: Path,
        output: str | Path | None,
        fmt: str = 'html',
    ) -> tuple[dict[Path, Path], Path]:
        """Map each source to its output path; return ``(mapping, output_root)``.

        * no output: ``page.md`` -> ``page.html`` next to the source;
        * output ending in ``.html``/``.htm``/``.pdf``: only valid for a single source (a
          ``.pdf`` file maps to the HTML page of the same name; see :meth:`pdf_path`);
        * otherwise output is a directory mirroring the source tree.

        The mapping always holds HTML paths; PDFs are written next to them. ``fmt`` (``html``,
        ``pdf`` or ``both``) decides which of those files are really written, and so which
        ones are checked against the inputs.

        Paths are compared the way a case-insensitive file system (macOS, Windows) sees
        them, so ``Guide.html`` and ``guide.html`` clash everywhere, and an output that is
        the same file as an input (by identity, e.g. ``Report.HTML``) is refused.

        :raises InputError: for a file output with several sources, clashing outputs, or an
            output that would overwrite one of the inputs.
        """
        try:
            mapping: dict[Path, Path] = {}
            if output is None:
                for src in sources:
                    mapping[src.path] = src.path.with_suffix('.html')
                out_root = root
            else:
                out = Path(output).expanduser().resolve()
                file_suffixes = (*HTML_SUFFIXES, PDF_SUFFIX)
                if out.suffix.lower() in file_suffixes and not out.is_dir():
                    if len(sources) != 1:
                        raise InputError(
                            f'Output {out} is a file but {len(sources)} inputs were found; '
                            'use an output directory'
                        )
                    is_pdf = out.suffix.lower() == PDF_SUFFIX
                    mapping[sources[0].path] = out.with_suffix('.html') if is_pdf else out
                    out_root = out.parent
                else:
                    if out.exists() and not out.is_dir():
                        raise InputError(f'Output {out} exists and is not a directory')
                    for src in sources:
                        mapping[src.path] = (out / src.relative).with_suffix('.html')
                    out_root = out
            self._check_outputs(mapping, fmt)
            return mapping, out_root
        except InputError:
            raise
        except Exception as exc:
            raise InputError(f'Could not plan output paths: {exc}') from exc

    def written_paths(self, dest: Path, fmt: str) -> list[Path]:
        """Files a build in format ``fmt`` really writes for the HTML output ``dest``."""
        try:
            if fmt == 'pdf':
                return [self.pdf_path(dest)]
            if fmt == 'both':
                return [dest, self.pdf_path(dest)]
            return [dest]
        except MdHtmlError:
            raise
        except Exception as exc:
            raise BuildError(f'Could not list output files for {dest}: {exc}') from exc

    @staticmethod
    def _same_file(target: Path, inputs: list[Path]) -> Path | None:
        """The input that ``target`` would overwrite, compared by file identity."""
        try:
            if not target.exists():
                return None
            return next((path for path in inputs if os.path.samefile(target, path)), None)
        except OSError as exc:
            raise InputError(f'Could not compare {target} with the inputs: {exc}') from exc

    def _check_outputs(self, mapping: dict[Path, Path], fmt: str) -> None:
        """Refuse clashing outputs and outputs that would overwrite an input."""
        try:
            inputs = list(mapping)
            seen: dict[str, Path] = {}
            for src, dest in mapping.items():
                for target in self.written_paths(dest, fmt):
                    key = os.path.normcase(str(target)).casefold()
                    if key in seen:
                        raise InputError(
                            f'{src} and {seen[key]} would both be written to {target} '
                            '(file names that differ only in case are the same file on '
                            'macOS and Windows)'
                        )
                    seen[key] = src
                    overwritten = self._same_file(target, inputs)
                    if overwritten is not None:
                        raise InputError(
                            f'Converting {src} would overwrite the input file {overwritten}; '
                            'rename the input or choose another output with -o'
                        )
        except MdHtmlError:
            raise
        except Exception as exc:
            raise InputError(f'Could not check output paths: {exc}') from exc

    @staticmethod
    def _order_value(document: ParsedDocument) -> float:
        """Return the numeric front-matter ``order`` (``inf`` when absent)."""
        try:
            value = document.meta.get('order')
            if value is None:
                return math.inf
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise InputError(
                    f'{document.source}: front matter "order" must be a number, got {value!r}'
                )
            return float(value)
        except MdHtmlError:
            raise
        except Exception as exc:
            raise BuildError(f'Unexpected error in SiteBuilder._order_value: {exc}') from exc

    def _sort_key(self, item: tuple[SourceFile, ParsedDocument]) -> tuple[Any, ...]:
        """Order by front-matter ``order``, then directory, index/README first, then name."""
        try:
            src, document = item
            stem = src.relative.stem.lower()
            return (
                self._order_value(document),
                tuple(part.lower() for part in src.relative.parent.parts),
                0 if stem in ('index', 'readme') else 1,
                src.relative.name.lower(),
            )
        except MdHtmlError:
            raise
        except Exception as exc:
            raise BuildError(f'Unexpected error in SiteBuilder._sort_key: {exc}') from exc

    # ------------------------------------------------------------------ build

    def _write(self, path: Path, content: str) -> None:
        """Write ``content`` to ``path``, creating parent directories."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
        except OSError as exc:
            raise BuildError(f'Could not write {path}: {exc}') from exc

    def _parse_sorted(self, sources: list[SourceFile]) -> list[tuple[SourceFile, ParsedDocument]]:
        """Read and parse every source, in page order (see :meth:`_sort_key`)."""
        try:
            parsed = [
                (src, self.converter.parse(self._read(src.path), str(src.path))) for src in sources
            ]
            parsed.sort(key=self._sort_key)
            return parsed
        except MdHtmlError:
            raise
        except Exception as exc:
            raise BuildError(f'Could not parse the input files: {exc}') from exc

    def _navigation(
        self,
        parsed: list[tuple[SourceFile, ParsedDocument]],
        mapping: dict[Path, Path],
        pdf_only: bool,
    ) -> list[NavEntry]:
        """One navigation entry per page, pointing at the PDFs for PDF-only builds."""
        try:
            return [
                NavEntry(
                    self.converter.renderer.page_title(document, src.path),
                    self.pdf_path(mapping[src.path]) if pdf_only else mapping[src.path],
                )
                for src, document in parsed
            ]
        except MdHtmlError:
            raise
        except Exception as exc:
            raise BuildError(f'Could not build the navigation: {exc}') from exc

    def _link_processor(self, out_root: Path, pdf_only: bool) -> LinkProcessor:
        """Asset/link handling; PDF-only builds read images in place and link to PDFs."""
        try:
            asset_mode = self.config.build.assets
            if pdf_only and asset_mode == 'copy':
                asset_mode = 'rewrite'
            resolver = AssetResolver(asset_mode, out_root, self.config.build.strict)
            return LinkProcessor(
                resolver,
                self.config.build.rewrite_md_links,
                lazy_images=not pdf_only,
                link_suffix=PDF_SUFFIX if pdf_only else '.html',
            )
        except MdHtmlError:
            raise
        except Exception as exc:
            raise BuildError(f'Could not set up link processing: {exc}') from exc

    def warn_wide_tables(self, parsed: list[tuple[SourceFile, ParsedDocument]]) -> None:
        """Warn about tables likely too wide for the printed page.

        Wide tables are printed with compact styles and cells may wrap anywhere, so nothing
        is cut off, but very wide ones read better on a wider page.
        """
        try:
            width_mm, _height = self.config.page.dimensions_mm()
            limit = int(width_mm // WIDE_TABLE_MM_PER_COLUMN)
            for src, document in parsed:
                widest = max(document.table_columns, default=0)
                if widest > limit:
                    page = f'{self.config.page.size} {self.config.page.orientation}'
                    logger.warning(
                        f'{src.path}: a table has {widest} columns, more than fit comfortably '
                        f'on a {page} page; cells will be compressed. Consider '
                        '--orientation landscape or a larger --page-size.'
                    )
        except MdHtmlError:
            raise
        except Exception as exc:
            raise BuildError(f'Could not check table widths: {exc}') from exc

    def _export_pdf_from_html(self, page: str, pdf_path: Path, source: Path) -> PdfResult:
        """Print an HTML string to ``pdf_path`` (staged next to it, then removed)."""
        try:
            pdf_path.parent.mkdir(parents=True, exist_ok=True)
            pdf = self.pdf_exporter.export_html(page, pdf_path, pdf_path.parent)
            self._log_pdf(pdf)
            return pdf
        except MdHtmlError as exc:
            raise type(exc)(f'{source}: {exc}') from exc
        except Exception as exc:
            raise BuildError(f'{source}: could not write {pdf_path}: {exc}') from exc

    def _export_pdf_from_file(self, html_path: Path, source: Path) -> PdfResult:
        """Print the written HTML page ``html_path`` to a PDF next to it."""
        try:
            pdf = self.pdf_exporter.export_file(html_path, self.pdf_path(html_path))
            self._log_pdf(pdf)
            return pdf
        except MdHtmlError as exc:
            raise type(exc)(f'{source}: {exc}') from exc
        except Exception as exc:
            raise BuildError(f'{source}: could not export {html_path} to PDF: {exc}') from exc

    @staticmethod
    def _log_pdf(pdf: PdfResult) -> None:
        """Report a written PDF."""
        try:
            pages = f'{pdf.pages} page{"" if pdf.pages == 1 else "s"}, ' if pdf.pages else ''
            logger.info(f'Wrote {pdf.path} ({pages}{pdf.backend})')
        except MdHtmlError:
            raise
        except Exception as exc:
            raise BuildError(f'Unexpected error in SiteBuilder._log_pdf: {exc}') from exc

    def _emit_page(self, fmt: str, src: Path, dest: Path, page: str, result: BuildResult) -> None:
        """Write one rendered page as HTML, PDF or both and record it in ``result``."""
        try:
            if fmt == 'pdf':
                pdf = self._export_pdf_from_html(page, self.pdf_path(dest), src)
                result.pages.append((src, pdf.path))
                result.pdfs.append(pdf)
                return
            self._write(dest, page)
            logger.info(f'Wrote {dest}')
            result.pages.append((src, dest))
            if fmt == 'both':
                result.pdfs.append(self._export_pdf_from_file(dest, src))
        except MdHtmlError:
            raise
        except Exception as exc:
            raise BuildError(f'{src}: could not write output: {exc}') from exc

    def build(
        self, inputs: list[str | Path] | str | Path, output: str | Path | None = None
    ) -> BuildResult:
        """Convert ``inputs`` (files and/or directories) and write HTML, PDF or both.

        What is written depends on ``build.format`` (see :meth:`resolve_format`):

        * ``html`` - one HTML page per source;
        * ``both`` - the HTML pages plus a PDF of each, next to it;
        * ``pdf``  - only PDFs. Each page is rendered to a temporary HTML file in its output
          folder, printed and removed; CSS is inlined, images are read from their original
          location (``copy`` behaves like ``rewrite``) and Markdown cross-links point at the
          other PDFs.

        :param inputs: a path or list of paths.
        :param output: output file/directory (defaults to ``config.build.output``).
        :returns: a :class:`BuildResult`.
        :raises MdHtmlError: on any input, config, theme, PDF or write error.
        """
        try:
            if isinstance(inputs, str | Path):
                inputs = [inputs]
            output = output if output is not None else self.config.build.output
            fmt = self.resolve_format(output)
            pdf_only = fmt == 'pdf'
            if fmt != 'html':
                # Fail fast with an actionable message before any file is written.
                logger.debug(f'PDF backend: {self.pdf_exporter.backend.name}')
            sources, root = self.collector.collect(list(inputs))
            mapping, out_root = self.plan_outputs(sources, root, output, fmt)
            parsed = self._parse_sorted(sources)
            if fmt != 'html':
                self.warn_wide_tables(parsed)
            nav = self._navigation(parsed, mapping, pdf_only)
            links = self._link_processor(out_root, pdf_only)
            result = BuildResult(output_root=out_root, format=fmt)
            if not self.config.build.inline_css and not pdf_only:
                result.stylesheet = out_root / SHARED_STYLESHEET
                self._write(result.stylesheet, self.converter.stylesheet)
            build_date = date.today()
            total = len(parsed)
            for index, (src, document) in enumerate(parsed):
                dest = mapping[src.path]
                href = None
                if result.stylesheet is not None:
                    href = Path(os.path.relpath(result.stylesheet, dest.parent)).as_posix()
                context = PageContext(
                    page=index + 1,
                    total_pages=total,
                    source=src.path,
                    output=dest,
                    nav=nav if total > 1 else [],
                    prev=nav[index - 1] if index > 0 else None,
                    next=nav[index + 1] if index + 1 < total else None,
                    stylesheet_href=href,
                    build_date=build_date,
                )
                page = self.converter.render_document(document, context, links)
                self._emit_page(fmt, src.path, dest, page, result)
            return result
        except MdHtmlError:
            raise
        except Exception as exc:
            raise BuildError(f'Build failed: {exc}') from exc
