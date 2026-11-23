"""PDF export: print generated HTML pages to PDF with a pluggable backend.

The PDF is produced from the very same HTML a browser shows, using its print stylesheet
(``@page`` size and margins, running header/footer, page-number counters; see
:mod:`md_html_builder.printing`). Nothing PDF-specific is added to the page, so a PDF made
here looks like the browser's "Save as PDF".

Backends implement :class:`PdfBackend`. A subclass with a ``name`` registers itself in
:attr:`PdfBackend.registry`, which makes it selectable with ``pdf.backend`` /
``--pdf-backend`` and part of ``auto`` selection (in definition order):

* :class:`WeasyPrintBackend` (default) - pure Python, needs the optional ``weasyprint``
  package (``pip install 'md-html-builder[pdf]'``) and the Pango system libraries. Supports
  running elements, so the header/footer keep their formatting.
* :class:`ChromeBackend` - headless Chrome / Chromium / Edge (``--print-to-pdf``), found
  automatically or given with ``pdf.chrome_path``. Header/footer are printed as plain text.

:class:`PdfExporter` picks the backend from :class:`~md_html_builder.config.PdfConfig`
(``auto`` tries WeasyPrint, then Chrome; when the page needs JavaScript, e.g. MathJax,
backends that run JavaScript are tried first) and raises
:class:`~md_html_builder.errors.PdfBackendUnavailableError` with an actionable message when
none is usable.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from .config import PdfConfig
from .errors import MdHtmlError, PdfBackendUnavailableError, PdfError

__all__ = [
    'ChromeBackend',
    'PdfBackend',
    'PdfExporter',
    'PdfInspector',
    'PdfResult',
    'WeasyPrintBackend',
]

logger = logging.getLogger(__name__)

PDF_MAGIC = b'%PDF-'
#: Longest allowed export timeout in seconds (one day).
MAX_TIMEOUT = 86400.0
INSTALL_HINT = "pip install 'md-html-builder[pdf]'"


@dataclass(frozen=True)
class PdfResult:
    """Outcome of one export.

    :ivar path: the written PDF.
    :ivar backend: name of the backend that produced it.
    :ivar pages: number of physical pages, when known.
    """

    path: Path
    backend: str
    pages: int | None = None


class PdfInspector:
    """Small, dependency-free checks on PDF files (used to verify exporter output)."""

    _PAGE_OBJECT = re.compile(rb'/Type\s*/Page(?![a-zA-Z])')

    @staticmethod
    def is_pdf(path: Path) -> bool:
        """True if ``path`` exists and starts with the ``%PDF-`` signature."""
        try:
            with Path(path).open('rb') as handle:
                return handle.read(len(PDF_MAGIC)) == PDF_MAGIC
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise PdfError(f'Could not read {path}: {exc}') from exc

    @classmethod
    def page_count(cls, path: Path) -> int | None:
        """Best-effort page count by counting uncompressed ``/Type /Page`` objects.

        Returns ``None`` when the page objects are compressed (object streams) and so
        cannot be counted without a PDF library.
        """
        try:
            count = len(cls._PAGE_OBJECT.findall(Path(path).read_bytes()))
            return count or None
        except OSError as exc:
            raise PdfError(f'Could not read {path}: {exc}') from exc

    @classmethod
    def verify(cls, path: Path, backend: str) -> None:
        """Raise :class:`PdfError` unless ``path`` is a non-empty PDF file."""
        try:
            if not cls.is_pdf(path):
                raise PdfError(f'{backend} did not produce a valid PDF at {path}')
        except MdHtmlError:
            raise
        except Exception as exc:
            raise PdfError(f'Unexpected error in PdfInspector.verify: {exc}') from exc


class PdfBackend(ABC):
    """Interface of a PDF backend: availability check plus HTML file -> PDF export.

    Subclasses set :attr:`name` (which registers them in :attr:`registry`), implement
    :meth:`unavailable_reason` and :meth:`_export`, and override :meth:`from_config` if
    they need more than the timeout.

    :param timeout: seconds allowed for one export.
    """

    name: ClassVar[str] = ''
    #: Whether the renderer executes JavaScript (needed for MathJax).
    runs_javascript: ClassVar[bool] = False
    #: Registered backend classes by name, in definition order.
    registry: ClassVar[dict[str, type[PdfBackend]]] = {}

    def __init_subclass__(cls, register: bool = True, replace: bool = False, **kwargs: Any) -> None:
        """Register subclasses that define their own ``name``.

        A subclass that only inherits a name (``class QuietChrome(ChromeBackend)``) is not
        registered, so it never displaces the original. ``register=False`` opts out;
        ``replace=True`` deliberately takes over an existing name, which otherwise raises.
        """
        try:
            super().__init_subclass__(**kwargs)
            name = cls.__dict__.get('name')
            if register and name:
                if name == 'auto':
                    raise PdfError('"auto" is reserved and cannot name a PDF backend')
                existing = PdfBackend.registry.get(name)
                if existing is not None and existing is not cls and not replace:
                    raise PdfError(
                        f'PDF backend name {name!r} is already used by '
                        f'{existing.__module__}.{existing.__qualname__}; choose another name '
                        'or pass replace=True'
                    )
                PdfBackend.registry[name] = cls
        except MdHtmlError:
            raise
        except Exception as exc:
            raise PdfError(f'Could not register PDF backend {cls.__name__}: {exc}') from exc

    @classmethod
    def from_config(cls, config: PdfConfig) -> PdfBackend:
        """Create the backend from the ``pdf`` configuration section."""
        try:
            return cls(timeout=config.timeout)
        except MdHtmlError:
            raise
        except Exception as exc:
            raise PdfError(f'Could not create the {cls.name} PDF backend: {exc}') from exc

    def __init__(self, timeout: float = 120.0) -> None:
        try:
            if not math.isfinite(timeout) or not 0 < timeout <= MAX_TIMEOUT:
                raise PdfError(
                    f'PDF timeout must be between 0 and {MAX_TIMEOUT:g} seconds, got {timeout}'
                )
            self.timeout = timeout
        except MdHtmlError:
            raise
        except Exception as exc:
            raise PdfError(f'Unexpected error in PdfBackend.__init__: {exc}') from exc

    @abstractmethod
    def unavailable_reason(self) -> str | None:
        """``None`` when the backend can be used, otherwise an actionable explanation."""

    def ensure_available(self) -> None:
        """Raise :class:`PdfBackendUnavailableError` (with a fix) if the backend is unusable."""
        try:
            reason = self.unavailable_reason()
            if reason is not None:
                raise PdfBackendUnavailableError(reason)
        except MdHtmlError:
            raise
        except Exception as exc:
            raise PdfError(f'Unexpected error in PdfBackend.ensure_available: {exc}') from exc

    @abstractmethod
    def _export(self, html_path: Path, pdf_path: Path) -> int | None:
        """Write ``pdf_path`` from ``html_path``; return the page count if known."""

    def export(self, html_path: Path, pdf_path: Path) -> PdfResult:
        """Render the HTML file ``html_path`` to ``pdf_path``.

        Relative URLs (images, a linked stylesheet) resolve against the HTML file's folder.
        The PDF is written to a hidden temporary file next to ``pdf_path`` and moved into
        place only once it is valid, so a failed or interrupted export never destroys an
        existing PDF.

        :raises PdfBackendUnavailableError: if the backend is not usable.
        :raises PdfError: if rendering fails or no valid PDF is produced.
        """
        try:
            html_path = Path(html_path).resolve()
            pdf_path = Path(pdf_path).resolve()
            if not html_path.is_file():
                raise PdfError(f'HTML file to export not found: {html_path}')
            self.ensure_available()
            pdf_path.parent.mkdir(parents=True, exist_ok=True)
            handle, name = tempfile.mkstemp(
                prefix=f'.{pdf_path.stem}-', suffix='.part.pdf', dir=pdf_path.parent
            )
            os.close(handle)
            partial = Path(name)
            partial.unlink()
            try:
                pages = self._export(html_path, partial)
                PdfInspector.verify(partial, self.name)
                if pages is None:
                    pages = PdfInspector.page_count(partial)
                if pdf_path.exists():
                    # Keep the permissions of the PDF being replaced.
                    shutil.copymode(pdf_path, partial)
                os.replace(partial, pdf_path)
            finally:
                partial.unlink(missing_ok=True)
            logger.debug(f'{self.name}: {html_path} -> {pdf_path} ({pages or "?"} pages)')
            return PdfResult(pdf_path, self.name, pages)
        except MdHtmlError:
            raise
        except Exception as exc:
            raise PdfError(f'{self.name}: could not export {html_path} to PDF: {exc}') from exc


class WeasyPrintBackend(PdfBackend):
    """Exports with `WeasyPrint <https://weasyprint.org>`_.

    WeasyPrint loads Pango/cairo with ``dlopen``. On macOS with Homebrew those libraries
    live in ``/opt/homebrew/lib`` (or ``/usr/local/lib``), which the dynamic loader only
    searches when ``DYLD_FALLBACK_LIBRARY_PATH`` is set before Python starts; importing
    WeasyPrint then fails with ``OSError``. In that case the backend runs the export in a
    child Python process with the variable set, so no manual setup is needed.

    The timeout applies when rendering runs in a child process; WeasyPrint rendering inside
    this process cannot be interrupted.

    :param timeout: seconds allowed for one export in a child process.
    :param library_dirs: directories searched for Homebrew-style Pango libraries.
    """

    name: ClassVar[str] = 'weasyprint'
    LIBRARY_DIRS: ClassVar[tuple[str, ...]] = ('/opt/homebrew/lib', '/usr/local/lib')
    PROBE_LIBRARY: ClassVar[str] = 'libgobject-2.0.0.dylib'
    WORKER_MODULE: ClassVar[str] = 'md_html_builder._weasyprint_worker'
    # Probe results are shared by all instances: importing WeasyPrint is slow.
    _probe_cache: ClassVar[dict[tuple[str, ...], tuple[str, str | None]]] = {}

    def __init__(self, timeout: float = 120.0, library_dirs: Sequence[str] | None = None) -> None:
        try:
            super().__init__(timeout)
            self.library_dirs = tuple(
                library_dirs if library_dirs is not None else self.LIBRARY_DIRS
            )
        except MdHtmlError:
            raise
        except Exception as exc:
            raise PdfError(f'Unexpected error in WeasyPrintBackend.__init__: {exc}') from exc

    # ------------------------------------------------------------------ probing

    @staticmethod
    def _import() -> Any:
        """Import and return the ``weasyprint`` module (separate for mocking).

        WeasyPrint prints an installation banner to stdout when its libraries are missing;
        that output is captured and logged at debug level instead.
        """
        try:
            captured = io.StringIO()
            try:
                with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
                    import weasyprint
                return weasyprint
            finally:
                if captured.getvalue().strip():
                    logger.debug(f'weasyprint import output: {captured.getvalue().strip()}')
        except Exception as exc:
            logger.debug(f'weasyprint import failed: {exc!r}')
            raise

    def _library_dir(self) -> str | None:
        """A directory holding the Pango stack, if one of :attr:`library_dirs` has it."""
        try:
            if sys.platform != 'darwin':
                return None
            for directory in self.library_dirs:
                if (Path(directory) / self.PROBE_LIBRARY).is_file():
                    return directory
            return None
        except MdHtmlError:
            raise
        except Exception as exc:
            raise PdfError(f'Unexpected error in WeasyPrintBackend._library_dir: {exc}') from exc

    def _child_env(self, library_dir: str) -> dict[str, str]:
        """Environment for a child process that can load the Pango libraries."""
        try:
            env = dict(os.environ)
            current = env.get('DYLD_FALLBACK_LIBRARY_PATH')
            env['DYLD_FALLBACK_LIBRARY_PATH'] = (
                f'{library_dir}:{current}' if current else library_dir
            )
            return env
        except MdHtmlError:
            raise
        except Exception as exc:
            raise PdfError(f'Unexpected error in WeasyPrintBackend._child_env: {exc}') from exc

    def _child_import_error(self, library_dir: str) -> str | None:
        """Try importing WeasyPrint in a child Python with the library path set.

        :returns: ``None`` on success, otherwise why it failed (the child's last error
            line, a timeout or the reason the child could not start).
        """
        try:
            completed = subprocess.run(
                [sys.executable, '-c', 'import weasyprint'],
                env=self._child_env(library_dir),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if completed.returncode == 0:
                return None
            lines = (completed.stderr or completed.stdout or '').strip().splitlines()
            return lines[-1] if lines else f'exit status {completed.returncode}'
        except subprocess.TimeoutExpired:
            logger.warning('weasyprint: importing WeasyPrint in a child process timed out')
            return 'importing WeasyPrint in a child process timed out after 60s'
        except OSError as exc:
            logger.warning(f'weasyprint: could not start a child Python: {exc}')
            return f'could not start a child Python ({exc})'
        except MdHtmlError:
            raise
        except Exception as exc:
            raise PdfError(f'Could not probe WeasyPrint in a child process: {exc}') from exc

    @staticmethod
    def _missing_libraries_reason(error: str) -> str:
        """Actionable message when WeasyPrint is installed but Pango/cairo do not load."""
        try:
            return (
                'WeasyPrint is installed but could not load its system libraries '
                f'(Pango/cairo): {error}. '
                'Install them (macOS: brew install pango; Debian/Ubuntu: '
                'apt install libpango-1.0-0 libpangoft2-1.0-0; see '
                'https://doc.courtbouillon.org/weasyprint/stable/first_steps.html). '
                'On macOS you may also need: '
                'export DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib. '
                'Alternatively use headless Chrome: --pdf-backend chrome'
            )
        except MdHtmlError:
            raise
        except Exception as exc:
            raise PdfError(
                f'Unexpected error in WeasyPrintBackend._missing_libraries_reason: {exc}'
            ) from exc

    def _probe(self) -> tuple[str, str | None]:
        """Return ``(mode, detail)``; mode is ``inprocess``, ``subprocess`` or ``missing``.

        For ``subprocess`` the detail is the library directory; for ``missing`` it is the
        actionable reason.
        """
        try:
            key = self.library_dirs
            if key in self._probe_cache:
                return self._probe_cache[key]
            try:
                self._import()
                result: tuple[str, str | None] = ('inprocess', None)
            except ImportError:
                result = (
                    'missing',
                    'WeasyPrint is not installed. Install it with: '
                    f'{INSTALL_HINT} (or: pip install weasyprint), '
                    'or use headless Chrome instead: --pdf-backend chrome',
                )
            except OSError as exc:
                error = str(exc).splitlines()[0] if str(exc) else repr(exc)
                library_dir = self._library_dir()
                child_error = self._child_import_error(library_dir) if library_dir else None
                if library_dir and child_error is None:
                    logger.debug(
                        f'weasyprint: system libraries found in {library_dir}; '
                        'exporting in a child process with DYLD_FALLBACK_LIBRARY_PATH set'
                    )
                    result = ('subprocess', library_dir)
                else:
                    if child_error:
                        error = f'{error}; with {library_dir} on the library path: {child_error}'
                    result = ('missing', self._missing_libraries_reason(error))
            self._probe_cache[key] = result
            return result
        except MdHtmlError:
            raise
        except Exception as exc:
            raise PdfError(f'Could not check whether WeasyPrint is usable: {exc}') from exc

    def unavailable_reason(self) -> str | None:
        """``None`` when WeasyPrint can be used (in this process or a child process)."""
        try:
            mode, detail = self._probe()
            return detail if mode == 'missing' else None
        except Exception as exc:
            raise PdfError(f'Could not check whether WeasyPrint is usable: {exc}') from exc

    # ------------------------------------------------------------------ export

    def _export(self, html_path: Path, pdf_path: Path) -> int | None:
        """Render in-process, or in a child process when the libraries need it."""
        try:
            mode, detail = self._probe()
            if mode == 'subprocess':
                return self._export_in_child(html_path, pdf_path, str(detail))
            return self.render(self._import(), html_path, pdf_path)
        except MdHtmlError:
            raise
        except Exception as exc:
            raise PdfError(f'Unexpected error in WeasyPrintBackend._export: {exc}') from exc

    @staticmethod
    def render(weasyprint: Any, html_path: Path, pdf_path: Path) -> int:
        """Render ``html_path`` to ``pdf_path`` with the given ``weasyprint`` module.

        Also used by the child-process worker. Returns the number of pages.
        """
        try:
            document = weasyprint.HTML(filename=str(html_path)).render()
            document.write_pdf(str(pdf_path))
            return len(document.pages)
        except Exception as exc:
            raise PdfError(f'WeasyPrint failed to render {html_path}: {exc}') from exc

    def _export_in_child(self, html_path: Path, pdf_path: Path, library_dir: str) -> int | None:
        """Run :data:`WORKER_MODULE` in a child Python with the library path set.

        :returns: the page count reported by the worker, or ``None`` if it reported none
            (the count is then read from the PDF).
        """
        try:
            command = [sys.executable, '-m', self.WORKER_MODULE, str(html_path), str(pdf_path)]
            completed = subprocess.run(
                command,
                env=self._child_env(library_dir),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip().splitlines()
                message = detail[-1] if detail else f'exit status {completed.returncode}'
                raise PdfError(f'WeasyPrint failed to render {html_path}: {message}')
            return self._page_count(completed.stdout)
        except subprocess.TimeoutExpired as exc:
            raise PdfError(
                f'WeasyPrint timed out after {self.timeout:g}s exporting {html_path} '
                '(raise pdf.timeout for very long documents)'
            ) from exc
        except OSError as exc:
            raise PdfError(f'Could not start the WeasyPrint worker: {exc}') from exc
        except MdHtmlError:
            raise
        except Exception as exc:
            raise PdfError(f'WeasyPrint worker failed for {html_path}: {exc}') from exc

    @staticmethod
    def _page_count(stdout: str) -> int | None:
        """Parse the worker's ``{"pages": N}`` line; ``None`` (logged) if it is missing."""
        try:
            return int(json.loads(stdout.strip().splitlines()[-1])['pages'])
        except (ValueError, KeyError, IndexError, TypeError):
            logger.debug(f'weasyprint worker gave no page count: {stdout!r}')
            return None


class ChromeBackend(PdfBackend):
    """Exports with headless Chrome, Chromium or Edge (``--print-to-pdf``).

    Chrome honours the CSS ``@page`` size, margins and margin boxes, so the page numbers
    and the (plain-text) running header/footer of the print stylesheet appear on every page.

    :param executable: path or command name; auto-detected when ``None``.
    :param timeout: seconds allowed for one export.
    :param which: ``shutil.which``-compatible lookup (injectable for tests).
    :param candidates: absolute paths to try during auto-detection (injectable for tests).
    """

    name: ClassVar[str] = 'chrome'
    runs_javascript: ClassVar[bool] = True
    COMMANDS: ClassVar[tuple[str, ...]] = (
        'google-chrome',
        'google-chrome-stable',
        'chromium',
        'chromium-browser',
        'chrome',
        'microsoft-edge',
        'msedge',
    )
    MAC_APPS: ClassVar[tuple[str, ...]] = (
        'Google Chrome.app/Contents/MacOS/Google Chrome',
        'Chromium.app/Contents/MacOS/Chromium',
        'Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
    )
    WINDOWS_PATHS: ClassVar[tuple[str, ...]] = (
        r'Google\Chrome\Application\chrome.exe',
        r'Chromium\Application\chrome.exe',
        r'Microsoft\Edge\Application\msedge.exe',
    )

    def __init__(
        self,
        executable: str | None = None,
        timeout: float = 120.0,
        which: Callable[[str], str | None] | None = None,
        candidates: Sequence[Path] | None = None,
    ) -> None:
        try:
            super().__init__(timeout)
            self.executable = executable
            self._which = which or shutil.which
            self._candidates = list(candidates) if candidates is not None else None
            self._resolved: Path | None = None
        except MdHtmlError:
            raise
        except Exception as exc:
            raise PdfError(f'Unexpected error in ChromeBackend.__init__: {exc}') from exc

    @classmethod
    def from_config(cls, config: PdfConfig) -> PdfBackend:
        """Create the backend with ``pdf.chrome_path`` and ``pdf.timeout``."""
        try:
            return cls(config.chrome_path, timeout=config.timeout)
        except MdHtmlError:
            raise
        except Exception as exc:
            raise PdfError(f'Could not create the chrome PDF backend: {exc}') from exc

    @classmethod
    def default_candidates(cls) -> list[Path]:
        """Well-known install locations for the current platform."""
        try:
            paths: list[Path] = []
            if sys.platform == 'darwin':
                for root in (Path('/Applications'), Path.home() / 'Applications'):
                    paths.extend(root / app for app in cls.MAC_APPS)
            elif os.name == 'nt':
                roots = [
                    os.environ.get(var)
                    for var in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA')
                ]
                for root in filter(None, roots):
                    paths.extend(Path(root) / rel for rel in cls.WINDOWS_PATHS)
            return paths
        except Exception as exc:
            raise PdfError(f'Could not list Chrome install locations: {exc}') from exc

    def find_executable(self) -> Path | None:
        """Locate the browser: the configured executable, then PATH, then known locations."""
        try:
            if self._resolved is not None:
                return self._resolved
            found: str | Path | None = None
            if self.executable:
                configured = Path(self.executable).expanduser()
                found = configured if configured.is_file() else self._which(self.executable)
            else:
                for command in self.COMMANDS:
                    found = self._which(command)
                    if found:
                        break
                if not found:
                    candidates = (
                        self._candidates
                        if self._candidates is not None
                        else self.default_candidates()
                    )
                    found = next((path for path in candidates if Path(path).is_file()), None)
            self._resolved = Path(found) if found else None
            return self._resolved
        except Exception as exc:
            raise PdfError(f'Could not search for a Chrome executable: {exc}') from exc

    def unavailable_reason(self) -> str | None:
        """``None`` when a Chrome/Chromium executable was found."""
        try:
            if self.find_executable() is not None:
                return None
            if self.executable:
                return (
                    f'Chrome executable not found: {self.executable}. Check pdf.chrome_path '
                    '(--chrome-path), or use --pdf-backend weasyprint'
                )
            return (
                'Headless Chrome/Chromium was not found (looked on PATH for '
                f'{", ".join(self.COMMANDS)} and in the standard install locations). Install '
                'Google Chrome or Chromium, set its path with --chrome-path / pdf.chrome_path, '
                f'or use WeasyPrint instead: {INSTALL_HINT}'
            )
        except MdHtmlError:
            raise
        except Exception as exc:
            raise PdfError(f'Unexpected error in ChromeBackend.unavailable_reason: {exc}') from exc

    def command(
        self, executable: Path, html_path: Path, pdf_path: Path, profile: Path
    ) -> list[str]:
        """The Chrome command line that prints ``html_path`` to ``pdf_path``."""
        try:
            return [
                str(executable),
                '--headless',
                '--disable-gpu',
                '--no-first-run',
                '--no-default-browser-check',
                '--disable-extensions',
                '--hide-scrollbars',
                f'--user-data-dir={profile}',
                # Remove Chrome's own date/URL header and footer; the page CSS provides them.
                '--no-pdf-header-footer',
                '--print-to-pdf-no-header',
                '--run-all-compositor-stages-before-draw',
                f'--print-to-pdf={pdf_path}',
                html_path.as_uri(),
            ]
        except MdHtmlError:
            raise
        except Exception as exc:
            raise PdfError(f'Unexpected error in ChromeBackend.command: {exc}') from exc

    def _export(self, html_path: Path, pdf_path: Path) -> int | None:
        """Run headless Chrome with a throw-away profile."""
        try:
            executable = self.find_executable()
            if executable is None:
                raise PdfBackendUnavailableError(self.unavailable_reason() or 'Chrome not found')
            with tempfile.TemporaryDirectory(prefix='md2html-chrome-') as profile:
                command = self.command(executable, html_path, pdf_path, Path(profile))
                try:
                    completed = subprocess.run(
                        command,
                        capture_output=True,
                        text=True,
                        timeout=self.timeout,
                        check=False,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise PdfError(
                        f'Chrome timed out after {self.timeout:g}s exporting {html_path} '
                        '(raise pdf.timeout for very long documents)'
                    ) from exc
                except OSError as exc:
                    raise PdfError(f'Could not run {executable}: {exc}') from exc
            if completed.returncode != 0 or not pdf_path.is_file():
                lines = (completed.stderr or completed.stdout or '').strip().splitlines()
                detail = lines[-1] if lines else f'exit status {completed.returncode}'
                raise PdfError(f'Chrome failed to print {html_path} to PDF: {detail}')
            return None
        except MdHtmlError:
            raise
        except Exception as exc:
            raise PdfError(f'Unexpected error in ChromeBackend._export: {exc}') from exc


class PdfExporter:
    """Exports HTML to PDF with the backend chosen by a :class:`PdfConfig`.

    :param config: PDF options (defaults to ``PdfConfig()``).
    :param backends: backend instances by name, tried in this order by ``auto`` (defaults
        to every class in :attr:`PdfBackend.registry`, built with
        :meth:`PdfBackend.from_config`); injectable for tests and custom backends.
    :param needs_javascript: the pages rely on JavaScript (MathJax). ``auto`` then prefers
        backends that run JavaScript, and a warning is logged if the chosen one cannot.
    """

    def __init__(
        self,
        config: PdfConfig | None = None,
        backends: dict[str, PdfBackend] | None = None,
        needs_javascript: bool = False,
    ) -> None:
        try:
            self.config = config or PdfConfig()
            if not isinstance(self.config, PdfConfig):
                raise PdfError(f'PdfExporter needs a PdfConfig, got {type(config).__name__}')
            if backends is None:
                backends = {
                    name: backend_class.from_config(self.config)
                    for name, backend_class in PdfBackend.registry.items()
                }
            if not backends:
                raise PdfError('No PDF backends are registered')
            self.backends = dict(backends)
            self.needs_javascript = needs_javascript
            self._backend: PdfBackend | None = None
        except MdHtmlError:
            raise
        except Exception as exc:
            raise PdfError(f'Could not initialise the PDF exporter: {exc}') from exc

    @property
    def backend(self) -> PdfBackend:
        """The backend to use (resolved once).

        :raises PdfBackendUnavailableError: when the chosen backend (or, for ``auto``,
            every backend) is unusable; the message explains how to fix it.
        """
        try:
            if self._backend is None:
                self._backend = self._select()
                logger.debug(f'PDF backend: {self._backend.name}')
            return self._backend
        except MdHtmlError:
            raise
        except Exception as exc:
            raise PdfError(f'Could not select a PDF backend: {exc}') from exc

    def auto_order(self) -> list[str]:
        """Backend names in the order ``auto`` tries them."""
        try:
            names = list(self.backends)
            if self.needs_javascript:
                names.sort(key=lambda name: not self.backends[name].runs_javascript)
            return names
        except MdHtmlError:
            raise
        except Exception as exc:
            raise PdfError(f'Could not order the PDF backends: {exc}') from exc

    def _select(self) -> PdfBackend:
        """Pick the configured backend, or the first usable one for ``auto``."""
        try:
            choice = self.config.backend
            if choice != 'auto':
                if choice not in self.backends:
                    known = ', '.join(['auto', *self.backends])
                    raise PdfError(f'Unknown PDF backend {choice!r}; available: {known}')
                backend = self.backends[choice]
                backend.ensure_available()
                return self._checked(backend)
            reasons: list[str] = []
            for name in self.auto_order():
                backend = self.backends[name]
                reason = backend.unavailable_reason()
                if reason is None:
                    return self._checked(backend)
                reasons.append(f'- {name}: {reason}')
            raise PdfBackendUnavailableError(
                'No PDF backend is available. Install one of them:\n' + '\n'.join(reasons)
            )
        except MdHtmlError:
            raise
        except Exception as exc:
            raise PdfError(f'Unexpected error in PdfExporter._select: {exc}') from exc

    def _checked(self, backend: PdfBackend) -> PdfBackend:
        """Warn when the pages need JavaScript that ``backend`` cannot run."""
        try:
            if self.needs_javascript and not backend.runs_javascript:
                capable = [n for n, b in self.backends.items() if b.runs_javascript]
                hint = f' Use --pdf-backend {capable[0]} to typeset it.' if capable else ''
                logger.warning(
                    f'The {backend.name} PDF backend does not run JavaScript, so MathJax '
                    f'math will appear as TeX source in the PDF.{hint}'
                )
            return backend
        except MdHtmlError:
            raise
        except Exception as exc:
            raise PdfError(f'Could not check the PDF backend: {exc}') from exc

    def export_file(self, html_path: str | Path, pdf_path: str | Path) -> PdfResult:
        """Print the HTML file ``html_path`` to ``pdf_path``."""
        try:
            return self.backend.export(Path(html_path), Path(pdf_path))
        except MdHtmlError:
            raise
        except Exception as exc:
            raise PdfError(f'Could not export {html_path} to PDF: {exc}') from exc

    def export_html(self, html: str, pdf_path: str | Path, base_dir: str | Path) -> PdfResult:
        """Print an HTML string to ``pdf_path``.

        The HTML is written to a hidden temporary file inside ``base_dir`` (so relative image
        and stylesheet URLs resolve exactly as they would for a page saved there) and removed
        afterwards.
        """
        try:
            base = Path(base_dir).resolve()
            if not base.is_dir():
                raise PdfError(f'Base directory for PDF export does not exist: {base}')
            backend = self.backend
            handle, name = tempfile.mkstemp(prefix='.md2html-', suffix='.html', dir=base)
            staged = Path(name)
            try:
                with os.fdopen(handle, 'w', encoding='utf-8') as stream:
                    stream.write(html)
                return backend.export(staged, Path(pdf_path))
            except MdHtmlError as exc:
                # The staged file is an implementation detail: name the PDF instead.
                message = str(exc).replace(str(staged), f'the page for {pdf_path}')
                raise type(exc)(message) from exc
            finally:
                staged.unlink(missing_ok=True)
        except MdHtmlError:
            raise
        except Exception as exc:
            raise PdfError(f'Could not export HTML to {pdf_path}: {exc}') from exc
