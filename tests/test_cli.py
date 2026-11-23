"""The md2html command-line interface."""

from __future__ import annotations

import io
import subprocess
import sys

import pytest
import yaml

from md_html_builder import __version__
from md_html_builder.cli import CommandLine, main


def run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = CommandLine(stdout=out).run(list(argv))
    return code, out.getvalue()


def body(path) -> str:
    return path.read_text(encoding='utf-8').split('</head>', 1)[1]


class TestBasics:
    def test_convert_single_file(self, write):
        src = write('doc.md', '# Hello')
        code, _ = run(str(src))
        assert code == 0
        assert src.with_suffix('.html').is_file()

    def test_output_option(self, write, tmp_path):
        src = write('doc.md', '# Hello')
        assert run(str(src), '-o', str(tmp_path / 'out.html'))[0] == 0
        assert (tmp_path / 'out.html').is_file()

    def test_directory_build(self, write, tmp_path):
        write('d/a.md', '# A')
        write('d/b.md', '# B')
        assert run(str(tmp_path / 'd'), '-o', str(tmp_path / 'site'), '--page-numbers')[0] == 0
        assert 'Page 2 of 2' in body(tmp_path / 'site' / 'b.html')

    def test_missing_input_returns_1(self, tmp_path, capsys):
        code, _ = run(str(tmp_path / 'nope.md'))
        assert code == 1
        assert 'Input not found' in capsys.readouterr().err

    def test_no_inputs_is_usage_error(self):
        assert main([]) == 2

    def test_version(self, capsys):
        assert main(['--version']) == 0
        assert __version__ in capsys.readouterr().out

    def test_invalid_choice_is_usage_error(self, write):
        assert main([str(write('a.md')), '--toc', 'diagonal']) == 2

    def test_module_entry_point(self, write):
        src = write('doc.md', '# Mod')
        proc = subprocess.run(
            [sys.executable, '-m', 'md_html_builder', str(src), '-q'],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert src.with_suffix('.html').is_file()


class TestInformation:
    def test_list_themes(self):
        code, out = run('--list-themes')
        assert code == 0
        assert 'default' in out

    def test_list_code_styles(self):
        code, out = run('--list-code-styles')
        assert code == 0
        assert 'monokai' in out.split()

    def test_print_config_reflects_flags(self):
        code, out = run('--print-config', '--toc', 'left', '--color', 'accent=#123456')
        assert code == 0
        data = yaml.safe_load(out)
        assert data['layout']['toc'] == 'left'
        assert data['style']['colors'] == {'accent': '#123456'}


class TestOptions:
    def test_style_flags(self, write):
        src = write('doc.md', '## A\n\n## B')
        code, _ = run(
            str(src),
            '--theme', 'default',
            '--mode', 'dark',
            '--font-body', 'Georgia, serif',
            '--font-size', '18px',
            '--max-width', '60rem',
            '--toc', 'left',
            '--code-style', 'friendly',
            '--var', 'gap=2px',
            '--dark-color', 'bg=#000',
        )  # fmt: skip
        assert code == 0
        html = src.with_suffix('.html').read_text()
        assert 'data-theme="dark"' in html
        assert '--mdh-font-body: Georgia, serif;' in html
        assert '--mdh-font-size: 18px;' in html
        assert '--mdh-content-max-width: 60rem;' in html
        assert '--gap: 2px;' in html
        assert 'mdh-toc-left' in html

    def test_header_footer_flags(self, write):
        src = write('doc.md', '# T')
        code, _ = run(
            str(src), '--header-template', '<b>{title}</b>', '--footer', '--title', 'Site'
        )
        assert code == 0
        html = body(src.with_suffix('.html'))
        assert '<b>T</b>' in html
        assert 'class="mdh-footer-text"' in html

    def test_header_without_template_uses_default(self, write):
        src = write('doc.md', '# T')
        assert run(str(src), '--header', '--title', 'My Site')[0] == 0
        assert 'My Site' in body(src.with_suffix('.html'))

    def test_header_flag_does_not_swallow_inputs(self, write, tmp_path, capsys):
        a = write('a.md', '# A')
        b = write('b.md', '# B')
        assert run('--header', str(a), str(b), '--title', 'Site')[0] == 0
        assert a.with_suffix('.html').is_file()
        assert b.with_suffix('.html').is_file()
        assert 'Built 2 pages' in capsys.readouterr().err

    def test_no_header_overrides_config(self, write):
        cfg = write('c.yaml', 'header: {enabled: true}\nfooter: {enabled: true}\n')
        code, out = run('--print-config', '-c', str(cfg), '--no-header', '--no-footer')
        assert code == 0
        data = yaml.safe_load(out)
        assert data['header']['enabled'] is False
        assert data['footer']['enabled'] is False

    def test_conflicting_header_flags(self, write, capsys):
        code, _ = run(str(write('doc.md')), '--header-template', 'x', '--no-header')
        assert code == 1
        assert 'cannot be combined' in capsys.readouterr().err

    def test_config_file_then_flags_then_set(self, write):
        cfg = write('site.yaml', 'layout:\n  toc: left\n  max_width: 10rem\ntitle: FromFile\n')
        code, out = run(
            '--print-config', '-c', str(cfg), '--toc', 'top', '--set', 'layout.toc=none'
        )
        data = yaml.safe_load(out)
        assert code == 0
        assert data['title'] == 'FromFile'
        assert data['layout']['max_width'] == '10rem'
        assert data['layout']['toc'] == 'none'

    def test_css_flag_appends(self, write):
        extra = write('extra.css', '.from-cli { color: red; }')
        src = write('doc.md', 'x')
        assert run(str(src), '--css', str(extra))[0] == 0
        assert '.from-cli' in src.with_suffix('.html').read_text()

    def test_safe_flag(self, write):
        src = write('doc.md', '<script>alert(1)</script>')
        assert run(str(src), '--safe')[0] == 0
        html = body(src.with_suffix('.html'))
        assert '<script>alert' not in html
        assert '&lt;script&gt;alert(1)&lt;/script&gt;' in html

    @pytest.mark.parametrize(
        ('flags', 'key', 'expected'),
        [
            (['--no-raw-html'], 'markdown.raw_html', False),
            (['--safe'], 'markdown.safe_mode', True),
            (['--line-numbers'], 'markdown.code_line_numbers', True),
            (['--no-recursive'], 'build.recursive', False),
            (['--strict'], 'build.strict', True),
            (['--link-css'], 'build.inline_css', False),
            (['--nav'], 'layout.navigation', True),
            (['--no-nav'], 'layout.navigation', False),
            (['--page-numbers'], 'page_numbers.enabled', True),
            (['--no-page-numbers'], 'page_numbers.enabled', False),
            (['--header'], 'header.enabled', True),
            (['--footer-template', '{date}'], 'footer.template', '{date}'),
            (['--view', 'web'], 'layout.view', 'web'),
            (['--page-size', 'Letter'], 'page.size', 'Letter'),
            (['--orientation', 'landscape'], 'page.orientation', 'landscape'),
            (['--margin', '10mm'], 'page.margin', '10mm'),
            (['--toc-depth', '2'], 'layout.toc_depth', 2),
            (['--align', 'left'], 'layout.align', 'left'),
            (['--assets', 'embed'], 'build.assets', 'embed'),
            (['--format', 'both'], 'build.format', 'both'),
            (['--pdf-backend', 'chrome'], 'pdf.backend', 'chrome'),
            (['--lang', 'de'], 'lang', 'de'),
            (['--author', 'Ada'], 'author', 'Ada'),
        ],
    )
    def test_flag_reaches_config(self, flags, key, expected):
        code, out = run('--print-config', *flags)
        assert code == 0
        value = yaml.safe_load(out)
        for part in key.split('.'):
            value = value[part]
        assert value == expected

    def test_keyboard_interrupt_exit_status(self, write, monkeypatch, capsys):
        def interrupted(self, inputs, output=None):
            raise KeyboardInterrupt

        monkeypatch.setattr('md_html_builder.cli.SiteBuilder.build', interrupted)
        assert run(str(write('doc.md', '# x')))[0] == 130
        assert 'Interrupted' in capsys.readouterr().err

    def test_link_css_and_assets(self, write, tmp_path):
        src = write('src/doc.md', 'x')
        code, _ = run(str(src), '-o', str(tmp_path / 'site'), '--link-css', '--assets', 'rewrite')
        assert code == 0
        assert (tmp_path / 'site' / 'assets' / 'md-html-builder.css').is_file()

    def test_strict_flag(self, write, capsys):
        src = write('doc.md', '```nolang\nx\n```')
        assert run(str(src), '--strict')[0] == 1
        assert 'Unknown code language' in capsys.readouterr().err

    @pytest.mark.parametrize(
        'args',
        [
            ['--color', 'novalue'],
            ['--set', 'layout.toc'],
            ['--set', 'layout.toc=diagonal'],
            ['--theme', 'nonexistent'],
            ['--code-style', 'nonexistent'],
            ['--color', 'accent=red;}'],
        ],
    )
    def test_bad_options_return_1(self, write, args, capsys):
        code, _ = run(str(write('doc.md', 'x')), *args)
        assert code == 1
        assert 'error:' in capsys.readouterr().err

    def test_missing_config_file(self, write, tmp_path, capsys):
        code, _ = run(str(write('doc.md')), '-c', str(tmp_path / 'none.yaml'))
        assert code == 1
        assert 'not found' in capsys.readouterr().err

    def test_malformed_config_file(self, write, capsys):
        cfg = write('bad.yaml', 'style: [')
        code, _ = run(str(write('doc.md')), '-c', str(cfg))
        assert code == 1
        assert 'malformed' in capsys.readouterr().err

    def test_verbose_and_quiet(self, write, capsys):
        src = write('doc.md', 'x')
        run(str(src), '-q')
        assert 'Wrote' not in capsys.readouterr().err
        run(str(src))
        assert 'Wrote' in capsys.readouterr().err
