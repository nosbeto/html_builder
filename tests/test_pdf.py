"""PDF export: backends, backend selection, builder integration and the CLI options.

Most tests use :class:`FakeBackend`, which writes a minimal PDF and records the HTML it was
given, so they run without WeasyPrint or Chrome. :class:`TestRealBackends` renders real PDFs
and is skipped for each backend that is not installed.
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path
from typing import ClassVar

import pytest
import yaml

from md_html_builder import Config, SiteBuilder
from md_html_builder import builder as builder_module
from md_html_builder.cli import CommandLine
from md_html_builder.config import PdfConfig
from md_html_builder.errors import (
    ConfigError,
    InputError,
    PdfBackendUnavailableError,
    PdfError,
)
from md_html_builder.pdf import (
    ChromeBackend,
    PdfBackend,
    PdfExporter,
    PdfInspector,
    WeasyPrintBackend,
)

MINIMAL_PDF = b'%PDF-1.7\n1 0 obj << /Type /Page >> endobj\n2 0 obj << /Type /Page >> endobj\n%%EOF'


class FakeBackend(PdfBackend, register=False):
    """Writes :data:`MINIMAL_PDF` (two pages) and records each exported HTML file."""

    name: ClassVar[str] = 'fake'

    def __init__(
        self, reason: str | None = None, output: bytes = MINIMAL_PDF, timeout: float = 5
    ) -> None:
        super().__init__(timeout=timeout)
        self.reason = reason
        self.output = output
        self.calls: list[tuple[Path, str]] = []

    def unavailable_reason(self) -> str | None:
        return self.reason

    def _export(self, html_path: Path, pdf_path: Path) -> int | None:
        self.calls.append((html_path, html_path.read_text(encoding='utf-8')))
        pdf_path.write_bytes(self.output)
        return None


def fake_exporter(backend: FakeBackend | None = None, **config) -> PdfExporter:
    """A PdfExporter whose ``weasyprint`` backend is a fake and ``chrome`` is unavailable."""
    return PdfExporter(
        PdfConfig.from_dict(config),
        backends={
            'weasyprint': backend or FakeBackend(),
            'chrome': FakeBackend(reason='no chrome here'),
        },
    )


def build(inputs, output=None, config: dict | None = None, backend: FakeBackend | None = None):
    exporter = fake_exporter(backend)
    builder = SiteBuilder(Config.from_dict(config or {}), pdf_exporter=exporter)
    return builder.build(inputs, output), exporter.backends['weasyprint']


def files_under(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file())


# ---------------------------------------------------------------------- inspector / base


class TestPdfInspector:
    def test_is_pdf(self, tmp_path):
        good = tmp_path / 'a.pdf'
        good.write_bytes(MINIMAL_PDF)
        bad = tmp_path / 'b.pdf'
        bad.write_bytes(b'<html>')
        assert PdfInspector.is_pdf(good)
        assert not PdfInspector.is_pdf(bad)
        assert not PdfInspector.is_pdf(tmp_path / 'missing.pdf')

    def test_page_count(self, tmp_path):
        path = tmp_path / 'a.pdf'
        path.write_bytes(MINIMAL_PDF + b'\n<< /Type /Pages >>')
        assert PdfInspector.page_count(path) == 2

    def test_page_count_unknown_when_compressed(self, tmp_path):
        path = tmp_path / 'a.pdf'
        path.write_bytes(b'%PDF-1.7\n/ObjStm\n%%EOF')
        assert PdfInspector.page_count(path) is None

    def test_verify_rejects_non_pdf(self, tmp_path):
        path = tmp_path / 'a.pdf'
        path.write_text('nope')
        with pytest.raises(PdfError, match='did not produce a valid PDF'):
            PdfInspector.verify(path, 'fake')


class TestPdfBackendBase:
    def test_timeout_must_be_positive(self):
        with pytest.raises(PdfError, match='between 0 and'):
            ChromeBackend(timeout=0)

    def test_export_returns_result(self, tmp_path):
        html = tmp_path / 'a.html'
        html.write_text('<p>hi</p>')
        result = FakeBackend().export(html, tmp_path / 'out' / 'a.pdf')
        assert result.path == (tmp_path / 'out' / 'a.pdf').resolve()
        assert result.backend == 'fake'
        assert result.pages == 2

    def test_export_missing_html(self, tmp_path):
        with pytest.raises(PdfError, match='not found'):
            FakeBackend().export(tmp_path / 'missing.html', tmp_path / 'a.pdf')

    def test_export_unavailable(self, tmp_path):
        html = tmp_path / 'a.html'
        html.write_text('x')
        with pytest.raises(PdfBackendUnavailableError, match='install me'):
            FakeBackend(reason='install me').export(html, tmp_path / 'a.pdf')

    def test_export_invalid_output(self, tmp_path):
        html = tmp_path / 'a.html'
        html.write_text('x')
        with pytest.raises(PdfError, match='valid PDF'):
            FakeBackend(output=b'garbage').export(html, tmp_path / 'a.pdf')


# ---------------------------------------------------------------------- exporter


class TestPdfExporter:
    def test_explicit_backend(self):
        chrome = FakeBackend()
        exporter = PdfExporter(
            PdfConfig(backend='chrome'), backends={'weasyprint': FakeBackend(), 'chrome': chrome}
        )
        assert exporter.backend is chrome

    def test_explicit_backend_unavailable(self):
        exporter = PdfExporter(
            PdfConfig(backend='chrome'),
            backends={'weasyprint': FakeBackend(), 'chrome': FakeBackend(reason='get chrome')},
        )
        with pytest.raises(PdfBackendUnavailableError, match='get chrome'):
            _ = exporter.backend

    def test_auto_prefers_weasyprint(self):
        weasy = FakeBackend()
        exporter = PdfExporter(backends={'weasyprint': weasy, 'chrome': FakeBackend()})
        assert exporter.backend is weasy

    def test_auto_falls_back_to_chrome(self):
        chrome = FakeBackend()
        exporter = PdfExporter(
            backends={'weasyprint': FakeBackend(reason='no pango'), 'chrome': chrome}
        )
        assert exporter.backend is chrome

    def test_auto_none_available_lists_every_reason(self):
        exporter = PdfExporter(
            backends={
                'weasyprint': FakeBackend(reason='no pango'),
                'chrome': FakeBackend(reason='no chrome'),
            }
        )
        with pytest.raises(PdfBackendUnavailableError) as info:
            _ = exporter.backend
        assert 'weasyprint: no pango' in str(info.value)
        assert 'chrome: no chrome' in str(info.value)

    def test_rejects_wrong_config_type(self):
        with pytest.raises(PdfError, match='needs a PdfConfig'):
            PdfExporter(Config())  # type: ignore[arg-type]

    def test_export_html_stages_in_base_dir_and_cleans_up(self, tmp_path):
        backend = FakeBackend()
        exporter = fake_exporter(backend)
        result = exporter.export_html('<p>hello</p>', tmp_path / 'out.pdf', tmp_path)
        assert result.path == (tmp_path / 'out.pdf').resolve()
        staged, content = backend.calls[0]
        assert staged.parent == tmp_path.resolve()
        assert content == '<p>hello</p>'
        assert files_under(tmp_path) == ['out.pdf']

    def test_export_html_missing_base_dir(self, tmp_path):
        with pytest.raises(PdfError, match='does not exist'):
            fake_exporter().export_html('x', tmp_path / 'a.pdf', tmp_path / 'nope')

    def test_export_file(self, tmp_path):
        html = tmp_path / 'a.html'
        html.write_text('<p>x</p>')
        assert fake_exporter().export_file(html, tmp_path / 'a.pdf').pages == 2


# ---------------------------------------------------------------------- concrete backends


class TestChromeBackend:
    def test_found_on_path(self, tmp_path):
        exe = tmp_path / 'chromium'
        exe.write_text('')
        backend = ChromeBackend(
            which=lambda name: str(exe) if name == 'chromium' else None, candidates=[]
        )
        assert backend.find_executable() == exe
        assert backend.unavailable_reason() is None

    def test_found_in_known_location(self, tmp_path):
        exe = tmp_path / 'Google Chrome'
        exe.write_text('')
        backend = ChromeBackend(which=lambda name: None, candidates=[tmp_path / 'x', exe])
        assert backend.find_executable() == exe

    def test_not_found_message(self):
        backend = ChromeBackend(which=lambda name: None, candidates=[])
        reason = backend.unavailable_reason()
        assert reason is not None
        assert '--chrome-path' in reason
        assert 'weasyprint' in reason.lower()

    def test_configured_path_missing(self, tmp_path):
        backend = ChromeBackend(str(tmp_path / 'nope'), which=lambda name: None, candidates=[])
        assert 'pdf.chrome_path' in (backend.unavailable_reason() or '')

    def test_command_line(self, tmp_path):
        html = tmp_path / 'a b.html'
        command = ChromeBackend().command(
            Path('/bin/chrome'), html, tmp_path / 'a.pdf', tmp_path / 'p'
        )
        assert command[0] == '/bin/chrome'
        assert '--headless' in command
        assert f'--print-to-pdf={tmp_path / "a.pdf"}' in command
        assert '--no-pdf-header-footer' in command
        assert command[-1] == html.as_uri()


class TestWeasyPrintBackend:
    def test_missing_package_gives_install_hint(self, monkeypatch):
        def missing():
            raise ImportError('No module named weasyprint')

        monkeypatch.setattr(WeasyPrintBackend, '_probe_cache', {})
        monkeypatch.setattr(WeasyPrintBackend, '_import', staticmethod(missing))
        reason = WeasyPrintBackend().unavailable_reason()
        assert reason is not None
        assert 'md-html-builder[pdf]' in reason
        assert '--pdf-backend chrome' in reason

    def test_missing_system_libraries(self, monkeypatch):
        def broken():
            raise OSError('cannot load library libgobject-2.0-0')

        monkeypatch.setattr(WeasyPrintBackend, '_probe_cache', {})
        monkeypatch.setattr(WeasyPrintBackend, '_import', staticmethod(broken))
        reason = WeasyPrintBackend(library_dirs=[]).unavailable_reason()
        assert reason is not None
        assert 'Pango' in reason


# ---------------------------------------------------------------------- builder


class TestBuilderFormats:
    def test_html_is_default(self, write, tmp_path):
        src = write('src/doc.md', '# Hi')
        result, backend = build(src, tmp_path / 'site')
        assert result.format == 'html'
        assert result.pdfs == []
        assert backend.calls == []
        assert files_under(tmp_path / 'site') == ['doc.html']

    def test_pdf_only(self, write, tmp_path):
        write('src/a.md', '# A\n\nSee [b](b.md#part).')
        write('src/b.md', '# B')
        result, backend = build(tmp_path / 'src', tmp_path / 'out', {'build': {'format': 'pdf'}})
        assert result.format == 'pdf'
        assert files_under(tmp_path / 'out') == ['a.pdf', 'b.pdf']
        assert result.outputs == [tmp_path / 'out' / 'a.pdf', tmp_path / 'out' / 'b.pdf']
        assert result.pdf_outputs == result.outputs
        html_a = backend.calls[0][1]
        assert 'href="b.pdf#part"' in html_a

    def test_pdf_only_inlines_css_and_reads_images_in_place(self, write, png, tmp_path):
        png('src/img/logo.png')
        write('src/doc.md', '![logo](img/logo.png)')
        config = {'build': {'format': 'pdf', 'inline_css': False, 'assets': 'copy'}}
        _, backend = build(tmp_path / 'src' / 'doc.md', tmp_path / 'out', config)
        html = backend.calls[0][1]
        assert '<style' in html
        assert 'src="../src/img/logo.png"' in html
        assert 'loading="lazy"' not in html
        assert files_under(tmp_path / 'out') == ['doc.pdf']

    def test_both(self, write, tmp_path):
        write('src/a.md', '# A\n\nSee [b](b.md).')
        write('src/b.md', '# B')
        result, backend = build(tmp_path / 'src', tmp_path / 'out', {'build': {'format': 'both'}})
        assert files_under(tmp_path / 'out') == ['a.html', 'a.pdf', 'b.html', 'b.pdf']
        assert result.outputs == [tmp_path / 'out' / 'a.html', tmp_path / 'out' / 'b.html']
        assert [p.name for p in result.pdf_outputs] == ['a.pdf', 'b.pdf']
        assert backend.calls[0][0] == (tmp_path / 'out' / 'a.html').resolve()
        assert 'href="b.html"' in backend.calls[0][1]

    def test_pdf_output_file_implies_pdf(self, write, tmp_path):
        src = write('doc.md', '# Hi')
        result, _ = build(src, tmp_path / 'deep' / 'report.pdf')
        assert result.format == 'pdf'
        assert result.outputs == [tmp_path / 'deep' / 'report.pdf']
        assert files_under(tmp_path / 'deep') == ['report.pdf']

    def test_both_with_pdf_output_file(self, write, tmp_path):
        src = write('doc.md', '# Hi')
        build(src, tmp_path / 'out' / 'report.pdf', {'build': {'format': 'both'}})
        assert files_under(tmp_path / 'out') == ['report.html', 'report.pdf']

    def test_pdf_format_with_html_output_file_is_rejected(self, write, tmp_path):
        src = write('doc.md', '# Hi')
        with pytest.raises(InputError, match='--format both'):
            build(src, tmp_path / 'out.html', {'build': {'format': 'pdf'}})

    def test_pdf_file_output_needs_single_source(self, write, tmp_path):
        write('src/a.md', '# A')
        write('src/b.md', '# B')
        with pytest.raises(InputError, match='use an output directory'):
            build(tmp_path / 'src', tmp_path / 'out.pdf')

    def test_unavailable_backend_fails_before_writing(self, write, tmp_path):
        src = write('src/doc.md', '# Hi')
        builder = SiteBuilder(
            Config.from_dict({'build': {'format': 'both'}}),
            pdf_exporter=PdfExporter(
                backends={
                    'weasyprint': FakeBackend(reason='no pango'),
                    'chrome': FakeBackend(reason='no chrome'),
                }
            ),
        )
        with pytest.raises(PdfBackendUnavailableError):
            builder.build(src, tmp_path / 'out')
        assert not (tmp_path / 'out').exists()

    def test_exporter_built_from_config(self):
        builder = SiteBuilder(Config.from_dict({'pdf': {'backend': 'chrome', 'timeout': 9}}))
        assert builder.pdf_exporter.config.backend == 'chrome'
        assert builder.pdf_exporter.backends['chrome'].timeout == 9

    def test_invalid_format_rejected(self):
        with pytest.raises(ConfigError):
            Config.from_dict({'build': {'format': 'docx'}})


# ---------------------------------------------------------------------- CLI


@pytest.fixture
def cli_backend(monkeypatch) -> FakeBackend:
    """Make the CLI's SiteBuilder use a fake WeasyPrint backend."""
    backend = FakeBackend()
    real = builder_module.PdfExporter

    def factory(config=None, backends=None, **kwargs):
        return real(
            config, backends={'weasyprint': backend, 'chrome': FakeBackend('none')}, **kwargs
        )

    monkeypatch.setattr(builder_module, 'PdfExporter', factory)
    return backend


def run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    return CommandLine(stdout=out).run(list(argv)), out.getvalue()


class TestCli:
    def test_format_pdf(self, write, tmp_path, cli_backend, capsys):
        src = write('doc.md', '# Hi')
        assert run(str(src), '--format', 'pdf')[0] == 0
        assert src.with_suffix('.pdf').is_file()
        assert not src.with_suffix('.html').exists()
        err = capsys.readouterr().err
        assert 'doc.pdf (2 pages, fake)' in err
        assert 'Built 1 page as PDF' in err

    def test_pdf_output_path(self, write, tmp_path, cli_backend):
        src = write('doc.md', '# Hi')
        assert run(str(src), '-o', str(tmp_path / 'r.pdf'))[0] == 0
        assert PdfInspector.is_pdf(tmp_path / 'r.pdf')

    def test_format_both(self, write, tmp_path, cli_backend, capsys):
        src = write('doc.md', '# Hi')
        assert run(str(src), '--format', 'both')[0] == 0
        assert src.with_suffix('.html').is_file()
        assert src.with_suffix('.pdf').is_file()
        assert 'as HTML and PDF' in capsys.readouterr().err

    def test_pdf_options_reach_config(self):
        code, out = run(
            '--print-config',
            '--format',
            'both',
            '--pdf-backend',
            'chrome',
            '--chrome-path',
            '/opt/chrome',
            '--pdf-timeout',
            '30',
        )
        assert code == 0
        data = yaml.safe_load(out)
        assert data['build']['format'] == 'both'
        assert data['pdf'] == {'backend': 'chrome', 'chrome_path': '/opt/chrome', 'timeout': 30.0}

    def test_invalid_timeout(self, capsys):
        assert run('--print-config', '--pdf-timeout', '0')[0] == 1
        assert 'timeout' in capsys.readouterr().err

    def test_unavailable_backend_is_reported(self, write, monkeypatch, capsys):
        real = builder_module.PdfExporter

        def factory(config=None, backends=None, **kwargs):
            return real(
                config,
                **kwargs,
                backends={
                    'weasyprint': FakeBackend('pip install weasyprint'),
                    'chrome': FakeBackend('install chrome'),
                },
            )

        monkeypatch.setattr(builder_module, 'PdfExporter', factory)
        src = write('doc.md', '# Hi')
        assert run(str(src), '--format', 'pdf')[0] == 1
        err = capsys.readouterr().err
        assert 'No PDF backend is available' in err
        assert 'pip install weasyprint' in err


# ---------------------------------------------------------------------- real backends


def _backend_or_skip(name: str) -> PdfBackend:
    backend = WeasyPrintBackend() if name == 'weasyprint' else ChromeBackend()
    reason = backend.unavailable_reason()
    if reason is not None:
        pytest.skip(f'{name} not available: {reason[:80]}')
    return backend


@pytest.mark.parametrize('backend_name', ['weasyprint', 'chrome'])
class TestRealBackends:
    """End-to-end exports with the installed renderers (skipped when missing)."""

    def test_multi_page_document(self, backend_name, write, png, tmp_path):
        backend = _backend_or_skip(backend_name)
        png('docs/pic.png')
        sections = '\n\n'.join(f'## Section {i}\n\n' + 'Lorem ipsum dolor. ' * 80 for i in range(8))
        src = write('docs/report.md', f'# Report\n\n![pic](pic.png)\n\n{sections}')
        config = Config.from_dict(
            {
                'build': {'format': 'pdf'},
                'header': {'enabled': True, 'template': '{title}'},
                'page_numbers': {'enabled': True},
                'page': {'size': 'A5'},
            }
        )
        exporter = PdfExporter(
            backends={backend_name: backend}, config=PdfConfig(backend=backend_name)
        )
        result = SiteBuilder(config, pdf_exporter=exporter).build(src, tmp_path / 'out')
        pdf = result.pdfs[0]
        assert pdf.backend == backend_name
        assert PdfInspector.is_pdf(pdf.path)
        if pdf.pages is not None:
            assert pdf.pages > 1
        assert files_under(tmp_path / 'out') == ['report.pdf']


# ---------------------------------------------------------------------- review regressions


class RaisingBackend(FakeBackend, register=False):
    """Fails every export after writing a partial file."""

    def _export(self, html_path: Path, pdf_path: Path) -> int | None:
        self.calls.append((html_path, html_path.read_text(encoding='utf-8')))
        pdf_path.write_bytes(b'%PDF-partial')
        raise PdfError(f'boom while printing {html_path}')


class TestAtomicExport:
    def test_failed_export_keeps_existing_pdf(self, tmp_path):
        html = tmp_path / 'a.html'
        html.write_text('x')
        existing = tmp_path / 'a.pdf'
        existing.write_bytes(b'%PDF-old')
        with pytest.raises(PdfError):
            FakeBackend(output=b'garbage').export(html, existing)
        assert existing.read_bytes() == b'%PDF-old'
        assert files_under(tmp_path) == ['a.html', 'a.pdf']

    def test_raising_backend_leaves_no_partial_files(self, tmp_path):
        html = tmp_path / 'a.html'
        html.write_text('x')
        with pytest.raises(PdfError, match='boom'):
            RaisingBackend().export(html, tmp_path / 'a.pdf')
        assert files_under(tmp_path) == ['a.html']

    def test_successful_export_replaces_existing_pdf(self, tmp_path):
        html = tmp_path / 'a.html'
        html.write_text('x')
        existing = tmp_path / 'a.pdf'
        existing.write_bytes(b'%PDF-old')
        FakeBackend().export(html, existing)
        assert existing.read_bytes() == MINIMAL_PDF
        assert files_under(tmp_path) == ['a.html', 'a.pdf']

    def test_export_html_cleans_up_and_hides_staged_name_on_failure(self, tmp_path):
        exporter = PdfExporter(backends={'fake': RaisingBackend()})
        with pytest.raises(PdfError) as info:
            exporter.export_html('<p>x</p>', tmp_path / 'out.pdf', tmp_path)
        assert '.md2html-' not in str(info.value)
        assert 'out.pdf' in str(info.value)
        assert files_under(tmp_path) == []


class TestRegistry:
    def test_builtin_backends_registered_in_order(self):
        assert list(PdfBackend.registry)[:2] == ['weasyprint', 'chrome']
        assert PdfConfig.backend_names()[:3] == ['auto', 'weasyprint', 'chrome']

    def test_test_doubles_are_not_registered(self):
        assert 'fake' not in PdfBackend.registry

    def test_custom_backend_is_selectable(self, monkeypatch, tmp_path):
        monkeypatch.setattr(PdfBackend, 'registry', dict(PdfBackend.registry))

        class MineBackend(FakeBackend):
            name: ClassVar[str] = 'mine'

        assert PdfBackend.registry['mine'] is MineBackend
        config = PdfConfig.from_dict({'backend': 'mine'})
        assert isinstance(PdfExporter(config).backend, MineBackend)
        code, out = run('--print-config', '--pdf-backend', 'mine')
        assert code == 0
        assert yaml.safe_load(out)['pdf']['backend'] == 'mine'

    def test_auto_tries_injected_backends_in_order(self):
        mine = FakeBackend()
        exporter = PdfExporter(backends={'mine': mine})
        assert exporter.backend is mine

    def test_unknown_backend_lists_available(self):
        exporter = PdfExporter(PdfConfig(backend='chrome'), backends={'mine': FakeBackend()})
        with pytest.raises(PdfError, match='available: auto, mine'):
            _ = exporter.backend

    def test_unknown_backend_rejected_by_config(self):
        with pytest.raises(ConfigError, match='not one of auto, weasyprint, chrome'):
            PdfConfig.from_dict({'backend': 'nope'})

    def test_reserved_name(self):
        with pytest.raises(PdfError, match='reserved'):

            class AutoBackend(FakeBackend):  # noqa: F841
                name: ClassVar[str] = 'auto'


class JsBackend(FakeBackend, register=False):
    name: ClassVar[str] = 'js'
    runs_javascript: ClassVar[bool] = True


class TestJavaScript:
    def test_auto_prefers_javascript_backend_when_needed(self):
        js = JsBackend()
        exporter = PdfExporter(
            backends={'weasyprint': FakeBackend(), 'chrome': js}, needs_javascript=True
        )
        assert exporter.backend is js

    def test_auto_order_unchanged_without_javascript(self):
        weasy = FakeBackend()
        exporter = PdfExporter(backends={'weasyprint': weasy, 'chrome': JsBackend()})
        assert exporter.backend is weasy

    def test_warns_when_backend_cannot_run_javascript(self, caplog):
        exporter = PdfExporter(
            PdfConfig(backend='weasyprint'),
            backends={'weasyprint': FakeBackend(), 'chrome': JsBackend()},
            needs_javascript=True,
        )
        with caplog.at_level('WARNING', logger='md_html_builder'):
            _ = exporter.backend
        assert 'TeX source' in caplog.text
        assert '--pdf-backend chrome' in caplog.text

    def test_builder_requests_javascript_for_mathjax(self):
        config = Config.from_dict({'markdown': {'math_renderer': 'mathjax'}})
        assert SiteBuilder(config).pdf_exporter.needs_javascript is True
        assert SiteBuilder(Config()).pdf_exporter.needs_javascript is False


class FakeCompleted:
    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestChromeExportFailures:
    @pytest.fixture
    def chrome(self, tmp_path):
        exe = tmp_path / 'chrome'
        exe.write_text('')
        html = tmp_path / 'a.html'
        html.write_text('<p>x</p>')
        return ChromeBackend(str(exe), timeout=5), html

    def test_non_zero_exit_reports_stderr(self, chrome, tmp_path, monkeypatch):
        backend, html = chrome
        monkeypatch.setattr(
            'md_html_builder.pdf.subprocess.run',
            lambda *a, **k: FakeCompleted(1, stderr='first\nGPU exploded'),
        )
        with pytest.raises(PdfError, match='GPU exploded'):
            backend.export(html, tmp_path / 'a.pdf')

    def test_timeout(self, chrome, tmp_path, monkeypatch):
        backend, html = chrome

        def slow(*args, **kwargs):
            raise subprocess.TimeoutExpired('chrome', 5)

        monkeypatch.setattr('md_html_builder.pdf.subprocess.run', slow)
        with pytest.raises(PdfError, match='timed out after 5s'):
            backend.export(html, tmp_path / 'a.pdf')
        assert not (tmp_path / 'a.pdf').exists()

    def test_success_without_pdf(self, chrome, tmp_path, monkeypatch):
        backend, html = chrome
        monkeypatch.setattr('md_html_builder.pdf.subprocess.run', lambda *a, **k: FakeCompleted())
        with pytest.raises(PdfError, match='failed to print'):
            backend.export(html, tmp_path / 'a.pdf')


class TestWeasyPrintChild:
    def test_child_failure_reports_last_line(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            'md_html_builder.pdf.subprocess.run',
            lambda *a, **k: FakeCompleted(1, stderr='Traceback\nValueError: bad css'),
        )
        with pytest.raises(PdfError, match='ValueError: bad css'):
            WeasyPrintBackend()._export_in_child(tmp_path / 'a.html', tmp_path / 'a.pdf', '/lib')

    def test_child_timeout(self, tmp_path, monkeypatch):
        def slow(*args, **kwargs):
            raise subprocess.TimeoutExpired('python', 3)

        monkeypatch.setattr('md_html_builder.pdf.subprocess.run', slow)
        with pytest.raises(PdfError, match='timed out after 3s'):
            WeasyPrintBackend(timeout=3)._export_in_child(
                tmp_path / 'a.html', tmp_path / 'a.pdf', '/lib'
            )

    def test_child_without_page_count(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            'md_html_builder.pdf.subprocess.run', lambda *a, **k: FakeCompleted(0, 'not json')
        )
        backend = WeasyPrintBackend()
        assert backend._export_in_child(tmp_path / 'a.html', tmp_path / 'a.pdf', '/lib') is None

    def test_child_env_keeps_existing_path(self, monkeypatch):
        monkeypatch.setenv('DYLD_FALLBACK_LIBRARY_PATH', '/existing')
        env = WeasyPrintBackend()._child_env('/opt/homebrew/lib')
        assert env['DYLD_FALLBACK_LIBRARY_PATH'] == '/opt/homebrew/lib:/existing'

    def _broken_import(self, monkeypatch):
        def broken():
            raise OSError('cannot load library libgobject-2.0-0')

        monkeypatch.setattr(WeasyPrintBackend, '_probe_cache', {})
        monkeypatch.setattr(WeasyPrintBackend, '_import', staticmethod(broken))
        monkeypatch.setattr(WeasyPrintBackend, '_library_dir', lambda self: '/opt/homebrew/lib')

    def test_probe_uses_child_process_when_it_can_import(self, monkeypatch):
        self._broken_import(monkeypatch)
        monkeypatch.setattr(WeasyPrintBackend, '_child_import_error', lambda self, d: None)
        backend = WeasyPrintBackend()
        assert backend._probe() == ('subprocess', '/opt/homebrew/lib')
        assert backend.unavailable_reason() is None

    def test_probe_reports_child_error(self, monkeypatch):
        self._broken_import(monkeypatch)
        monkeypatch.setattr(
            WeasyPrintBackend, '_child_import_error', lambda self, d: 'timed out after 60s'
        )
        reason = WeasyPrintBackend().unavailable_reason()
        assert reason is not None
        assert 'timed out after 60s' in reason
        assert 'libgobject' in reason

    def test_child_import_error_details(self, monkeypatch):
        monkeypatch.setattr(
            'md_html_builder.pdf.subprocess.run',
            lambda *a, **k: FakeCompleted(1, stderr='OSError: no pango'),
        )
        assert WeasyPrintBackend()._child_import_error('/lib') == 'OSError: no pango'

        def slow(*args, **kwargs):
            raise subprocess.TimeoutExpired('python', 60)

        monkeypatch.setattr('md_html_builder.pdf.subprocess.run', slow)
        assert 'timed out' in (WeasyPrintBackend()._child_import_error('/lib') or '')


class TestBuilderPdfErrors:
    def test_pdf_error_names_the_source(self, write, tmp_path):
        src = write('src/doc.md', '# Hi')
        builder = SiteBuilder(
            Config.from_dict({'build': {'format': 'pdf'}}),
            pdf_exporter=PdfExporter(backends={'fake': RaisingBackend()}),
        )
        with pytest.raises(PdfError) as info:
            builder.build(src, tmp_path / 'out')
        message = str(info.value)
        assert str(src) in message
        assert '.md2html-' not in message
        assert files_under(tmp_path / 'out') == []

    def test_wide_table_warning_for_pdf(self, write, tmp_path, caplog):
        cells = ' | '.join(['x'] * 14)
        src = write('doc.md', f'| {cells} |\n|' + '---|' * 14 + f'\n| {cells} |\n')
        with caplog.at_level('WARNING', logger='md_html_builder'):
            build(src, tmp_path / 'out.pdf')
        assert 'a table has 14 columns' in caplog.text
        assert '--orientation landscape' in caplog.text

    def test_no_wide_table_warning_for_html_or_landscape(self, write, tmp_path, caplog):
        cells = ' | '.join(['x'] * 14)
        src = write('doc.md', f'| {cells} |\n|' + '---|' * 14 + f'\n| {cells} |\n')
        with caplog.at_level('WARNING', logger='md_html_builder'):
            build(src, tmp_path / 'out.html')
            build(src, tmp_path / 'land.pdf', {'page': {'orientation': 'landscape'}})
        assert 'columns' not in caplog.text


class TestRoundTwo:
    def test_subclass_inheriting_a_name_does_not_replace_it(self, monkeypatch):
        monkeypatch.setattr(PdfBackend, 'registry', dict(PdfBackend.registry))

        class QuietChrome(ChromeBackend):
            pass

        assert PdfBackend.registry['chrome'] is ChromeBackend
        assert QuietChrome not in PdfBackend.registry.values()

    def test_duplicate_name_rejected_unless_replace(self, monkeypatch):
        monkeypatch.setattr(PdfBackend, 'registry', dict(PdfBackend.registry))
        with pytest.raises(PdfError, match='already used by'):

            class OtherChrome(FakeBackend):  # noqa: F841
                name: ClassVar[str] = 'chrome'

        class MyChrome(ChromeBackend, replace=True):
            name: ClassVar[str] = 'chrome'

        assert PdfBackend.registry['chrome'] is MyChrome

    @pytest.mark.parametrize('timeout', [float('inf'), 1e300, 0, -1, float('nan')])
    def test_unreasonable_timeouts_rejected(self, timeout):
        with pytest.raises(ConfigError, match='between 0 and 86400'):
            PdfConfig.from_dict({'timeout': timeout})
        with pytest.raises(PdfError, match='between 0 and'):
            ChromeBackend(timeout=timeout)

    def test_cli_rejects_infinite_timeout(self, capsys):
        assert run('--print-config', '--pdf-timeout', 'inf')[0] == 1
        assert 'between 0 and 86400' in capsys.readouterr().err

    def test_replacing_a_pdf_keeps_its_permissions(self, tmp_path):
        html = tmp_path / 'a.html'
        html.write_text('x')
        pdf = tmp_path / 'a.pdf'
        FakeBackend().export(html, pdf)
        pdf.chmod(0o600)
        FakeBackend().export(html, pdf)
        assert pdf.stat().st_mode & 0o777 == 0o600

    def test_pdf_only_build_may_read_an_html_named_source(self, write, tmp_path):
        src = write('note.html', '# Hi')
        result, _ = build(src, tmp_path / 'note.pdf')
        assert result.outputs == [tmp_path / 'note.pdf']
        assert src.read_text() == '# Hi'
        _, _ = build(src, None, {'build': {'format': 'pdf'}})
        assert src.with_suffix('.pdf').is_file()
        assert src.read_text() == '# Hi'

    def test_both_build_still_protects_an_html_named_source(self, write):
        src = write('note.html', '# Hi')
        with pytest.raises(InputError, match='would overwrite'):
            build(src, None, {'build': {'format': 'both'}})
        assert src.read_text() == '# Hi'
