"""Design features: paged view, page setup, print CSS, themes, accessibility and figures."""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from md_html_builder import MarkdownConverter, SiteBuilder
from md_html_builder.cli import CommandLine
from md_html_builder.config import Config, PageConfig
from md_html_builder.errors import ConfigError, ThemeError
from md_html_builder.highlighter import CodeHighlighter
from md_html_builder.printing import PrintLayout
from md_html_builder.renderer import PageContext
from md_html_builder.themes import BASE_CSS_PATH, StylesheetBuilder, ThemeRegistry

BUILTIN_THEMES = ('default', 'academic', 'midnight', 'minimal')


def render(text: str, config: dict | None = None, context: PageContext | None = None) -> str:
    return MarkdownConverter(Config.from_dict(config or {})).convert_text(text, context)


def body(html: str) -> str:
    """The part of a page after </head> (so inline CSS does not match)."""
    return html.split('</head>', 1)[1]


def stylesheet(config: dict | None = None) -> str:
    cfg = Config.from_dict(config or {})
    theme = ThemeRegistry().load(cfg.style.theme)
    return StylesheetBuilder(cfg, theme, CodeHighlighter(mode=cfg.style.mode)).build()


# ---------------------------------------------------------------------------- WCAG helpers


def _luminance(hex_colour: str) -> float:
    value = hex_colour.lstrip('#')
    channels = [int(value[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(a: str, b: str) -> float:
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def palettes(css: str) -> dict[str, dict[str, str]]:
    """Map each palette selector in a theme to its ``{colour key: #hex}`` values."""
    found: dict[str, dict[str, str]] = {}
    for selector, block in re.findall(r'(:root[^{,]*)\{([^}]*)\}', css):
        colours = dict(re.findall(r'--mdh-color-([a-z-]+):\s*(#[0-9a-fA-F]{6})\s*;', block))
        if colours:
            found[selector.strip()] = colours
    return found


# (foreground, background, minimum ratio) - 4.5:1 for text, 3:1 for UI components.
CONTRAST_PAIRS = [
    ('text', 'bg', 4.5),
    ('text', 'surface', 4.5),
    ('text', 'table-stripe', 4.5),
    ('muted', 'bg', 4.5),
    ('muted', 'surface', 4.5),
    ('heading', 'bg', 4.5),
    ('heading', 'table-header-bg', 4.5),
    ('link', 'bg', 4.5),
    ('link', 'surface', 4.5),
    ('code-text', 'code-bg', 4.5),
    ('header-text', 'header-bg', 4.5),
    ('footer-text', 'footer-bg', 4.5),
    ('mark-text', 'mark', 4.5),
    ('note', 'surface', 4.5),
    ('tip', 'surface', 4.5),
    ('important', 'surface', 4.5),
    ('warning', 'surface', 4.5),
    ('caution', 'surface', 4.5),
    ('focus', 'bg', 3.0),
    ('accent', 'bg', 3.0),
]


class TestBuiltinThemes:
    def test_all_themes_available(self):
        available = ThemeRegistry().available()
        for name in BUILTIN_THEMES:
            assert name in available
        assert '_shared' not in available

    @pytest.mark.parametrize('name', BUILTIN_THEMES)
    def test_metadata_and_code_styles(self, name):
        theme = ThemeRegistry().load(name)
        assert theme.description
        assert theme.base is True
        styles = CodeHighlighter.available_styles()
        assert theme.code_style in styles
        assert theme.code_style_dark in styles

    @pytest.mark.parametrize('name', BUILTIN_THEMES)
    def test_palettes_meet_wcag_aa(self, name):
        found = palettes(ThemeRegistry().load(name).css)
        assert {':root', ':root[data-theme="dark"]', ':root[data-theme="auto"]'} <= set(found)
        for selector, colours in found.items():
            for fg, bg, minimum in CONTRAST_PAIRS:
                ratio = contrast(colours[fg], colours[bg])
                assert ratio >= minimum, f'{name} {selector}: {fg} on {bg} is {ratio:.2f}'

    @pytest.mark.parametrize('name', BUILTIN_THEMES)
    def test_auto_dark_palette_is_screen_only(self, name):
        css = ThemeRegistry().load(name).css
        assert '@media screen and (prefers-color-scheme: dark)' in css
        assert '@media (prefers-color-scheme: dark)' not in css

    @pytest.mark.parametrize('name', BUILTIN_THEMES)
    def test_print_engine_friendly_css(self, name):
        """WeasyPrint has no color-mix() and only accepts weights in steps of 100 via var()."""
        css = ThemeRegistry().load(name).css + BASE_CSS_PATH.read_text(encoding='utf-8')
        assert 'color-mix(' not in css
        for weight in re.findall(r'--mdh-heading-weight:\s*(\d+)', css):
            assert int(weight) % 100 == 0

    def test_midnight_is_dark_in_every_mode(self):
        found = palettes(ThemeRegistry().load('midnight').css)
        assert found[':root']['bg'] == found[':root[data-theme="dark"]']['bg']
        assert _luminance(found[':root']['bg']) < 0.05


class TestBaseStylesheet:
    def test_base_included_before_theme(self):
        css = stylesheet()
        assert css.index('---- base ----') < css.index('---- theme: default ----')
        assert '.mdh-sheet' in css

    def test_theme_can_opt_out_of_base(self, write, tmp_path):
        write('t/theme.css', ':root { --mdh-color-bg: #fff; }')
        write('t/theme.yaml', 'base: false\n')
        css = stylesheet({'style': {'theme': str(tmp_path / 't')}})
        assert '---- base ----' not in css
        assert '.mdh-sheet' not in css

    def test_base_flag_must_be_boolean(self, write, tmp_path):
        write('t/theme.css', '')
        write('t/theme.yaml', 'base: maybe\n')
        with pytest.raises(ThemeError, match='base must be true or false'):
            ThemeRegistry().load(str(tmp_path / 't'))

    def test_single_file_theme_gets_base(self, write):
        path = write('mine.css', ':root { --mdh-color-bg: pink; }')
        assert ThemeRegistry().load(str(path)).base is True

    def test_base_variables_do_not_warn(self, caplog):
        stylesheet({'style': {'colors': {'desk': '#ccc', 'note': '#00f'}}})
        assert 'desk' not in caplog.text
        assert 'note' not in caplog.text

    def test_print_rules_hide_screen_chrome(self):
        css = BASE_CSS_PATH.read_text(encoding='utf-8')
        print_css = css[css.index('@media print') :]
        for selector in ('.mdh-site-nav', '.mdh-toc.mdh-toc-left', '.mdh-pager', '.mdh-header'):
            assert selector in print_css
        assert 'position: running(mdh-running-header)' in print_css
        assert 'display: table-header-group' in print_css
        assert 'break-inside: avoid' in print_css
        assert 'break-after: avoid' in print_css

    def test_focus_styles_present(self):
        css = BASE_CSS_PATH.read_text(encoding='utf-8')
        assert ':focus-visible' in css
        assert '.mdh-skip-link:focus' in css
        assert 'prefers-reduced-motion' in css


class TestPageConfig:
    def test_defaults(self):
        page = Config().page
        assert (page.size, page.orientation, page.margin) == ('A4', 'portrait', '20mm 18mm')
        assert page.dimensions_mm() == (210, 297)

    def test_landscape_swaps_dimensions(self):
        page = PageConfig.from_dict({'size': 'Letter', 'orientation': 'landscape'})
        assert page.dimensions_mm() == (279.4, 215.9)

    @pytest.mark.parametrize(
        ('margin', 'expected'),
        [
            ('10mm', ('10mm', '10mm', '10mm', '10mm')),
            ('1in 2cm', ('1in', '2cm', '1in', '2cm')),
            ('1mm 2mm 3mm', ('1mm', '2mm', '3mm', '2mm')),
            ('1mm 2mm 3mm 4mm', ('1mm', '2mm', '3mm', '4mm')),
        ],
    )
    def test_margin_shorthand(self, margin, expected):
        assert PageConfig.from_dict({'margin': margin}).margins() == expected

    @pytest.mark.parametrize('margin', ['1 2 3 4 5', '10mm; color: red', '{x}'])
    def test_invalid_margin(self, margin):
        with pytest.raises(ConfigError, match='margin'):
            PageConfig.from_dict({'margin': margin})

    def test_invalid_size(self):
        with pytest.raises(ConfigError, match='A4'):
            Config.from_dict({'page': {'size': 'A0'}})

    def test_invalid_view(self):
        with pytest.raises(ConfigError, match='paged'):
            Config.from_dict({'layout': {'view': 'slides'}})

    def test_page_number_position_choices(self):
        with pytest.raises(ConfigError, match='right'):
            Config.from_dict({'page_numbers': {'position': 'top'}})


class TestPrintLayout:
    def layout(self, data: dict | None = None) -> PrintLayout:
        return PrintLayout(Config.from_dict(data or {}))

    def test_requires_config(self):
        with pytest.raises(ConfigError):
            PrintLayout({})  # type: ignore[arg-type]

    def test_page_rule(self):
        css = self.layout({'page': {'size': 'A5', 'margin': '12mm'}}).stylesheet()
        assert 'size: 148mm 210mm;' in css
        assert 'margin: 12mm;' in css
        assert 'print-color-adjust: exact' in css

    def test_page_number_box_follows_position(self):
        data = {'page_numbers': {'enabled': True, 'position': 'left', 'format': 'p. {page}'}}
        layout = self.layout(data)
        assert '@bottom-left { content: "p. " counter(page); }' in layout.stylesheet()
        assert layout.footer_box() == 'bottom-right'
        assert self.layout({'page_numbers': {'enabled': True}}).footer_box() == 'bottom-left'

    def test_link_urls_option(self):
        assert 'attr(href)' not in self.layout().stylesheet()
        assert 'attr(href)' in self.layout({'page': {'print_link_urls': True}}).stylesheet()

    def test_page_variables(self):
        variables = self.layout(
            {'page': {'size': 'A4', 'orientation': 'landscape', 'margin': '1mm 2mm 3mm 4mm'}}
        ).page_variables()
        assert variables['--mdh-page-width'] == '297mm'
        assert variables['--mdh-page-height'] == '210mm'
        assert variables['--mdh-page-margin-left'] == '4mm'

    def test_plain_text_and_css_string(self):
        assert PrintLayout.plain_text('<b>A</b> &amp; <i>B</i>\n C') == 'A & B C'
        assert PrintLayout.css_string('say "hi" </style>') == '"say \\"hi\\" \\3c /style\\3e "'

    def test_region_css_has_string_then_element(self):
        css = self.layout().region_css('<b>Head</b>', 'Foot')
        assert '@top-left { content: "Head"; content: element(mdh-running-header);' in css
        assert '@bottom-left { content: "Foot"; content: element(mdh-running-footer); }' in css
        assert self.layout().region_css(None, None) == ''

    def test_regions_html(self):
        html = self.layout().regions_html('<b>Head</b>', None)
        assert 'aria-hidden="true"' in html
        assert '<div class="mdh-running-header"><div class="mdh-header-inner"><b>Head</b>' in html
        assert 'mdh-running-footer' not in html
        assert self.layout().regions_html(None, None) == ''


class TestPagedView:
    def test_paged_is_default(self):
        html = body(render('# Hi'))
        assert 'mdh-view-paged' in html
        assert '<div class="mdh-sheet">' in html

    def test_web_view_template(self):
        html = body(render('# Hi', {'layout': {'view': 'web'}}))
        assert 'mdh-view-web' in html
        assert 'mdh-sheet' not in html

    def test_header_and_footer_inside_sheet(self):
        config = {'header': {'enabled': True}, 'footer': {'enabled': True}}
        html = body(render('# Hi', config))
        sheet = html[html.index('<div class="mdh-sheet">') :]
        assert sheet.index('<header class="mdh-header">') < sheet.index('<main')
        assert sheet.index('<main') < sheet.index('<footer class="mdh-footer">')

    def test_page_size_reaches_stylesheet(self):
        css = stylesheet({'page': {'size': 'Legal', 'orientation': 'landscape'}})
        assert '--mdh-page-width: 355.6mm;' in css
        assert 'size: 355.6mm 215.9mm;' in css

    def test_print_regions_for_header_and_footer(self):
        config = {
            'title': 'Site',
            'header': {'enabled': True, 'template': '<b>{site_title}</b> | {title}'},
            'footer': {'enabled': True, 'template': '{title}'},
        }
        html = render('---\ntitle: Doc "1"\n---\ntext', config)
        head, page_body = html.split('</head>', 1)
        assert '<style id="mdh-print-regions" media="print">' in head
        assert 'content: "Site | Doc \\"1\\""; content: element(mdh-running-header)' in head
        regions = page_body[: page_body.index('<div class="mdh-layout')]
        assert '<div class="mdh-print-regions" aria-hidden="true">' in regions
        assert 'mdh-running-footer' in regions

    def test_no_print_regions_without_header_or_footer(self):
        html = render('x')
        assert 'mdh-print-regions' not in body(html)
        assert 'id="mdh-print-regions"' not in html

    def test_front_matter_disables_running_header(self):
        html = render('---\nheader: false\n---\nx', {'header': {'enabled': True}})
        assert 'mdh-running-header' not in body(html)

    def test_title_cannot_break_out_of_region_style(self):
        config = {'header': {'enabled': True, 'template': '{title}'}}
        html = render('---\ntitle: </style><script>alert(1)</script>\n---\n', config)
        head = html.split('</head>', 1)[0]
        assert '<script>' not in head

    def test_page_number_position_class(self):
        config = {'page_numbers': {'enabled': True, 'position': 'center'}}
        assert 'mdh-pagenum-center' in body(render('x', config))
        assert 'mdh-pagenum-' not in body(render('x'))

    def test_site_nav_has_inner_wrapper(self):
        from md_html_builder.renderer import NavEntry

        pages = [NavEntry('A', Path('/o/a.html')), NavEntry('B', Path('/o/b.html'))]
        context = PageContext(page=1, total_pages=2, output=Path('/o/a.html'), nav=pages)
        html = body(render('# A', context=context))
        assert '<div class="mdh-site-nav-inner">' in html

    def test_custom_template_may_use_print_regions(self, write, tmp_path):
        write('t/theme.css', ':root{}')
        write('t/template.html', '<body>${print_regions}${content}</body>')
        config = {'style': {'theme': str(tmp_path / 't')}, 'footer': {'enabled': True}}
        assert 'mdh-running-footer' in render('x', config)


class TestFonts:
    def test_google_fonts_links(self):
        url = 'https://fonts.googleapis.com/css2?family=Inter:wght@400;700&display=swap'
        html = render('x', {'style': {'font_url': [url], 'font_body': 'Inter, sans-serif'}})
        head = html.split('</head>', 1)[0]
        assert '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>' in head
        assert f'<link rel="stylesheet" href="{url.replace("&", "&amp;")}">' in head
        assert head.index('fonts.googleapis.com/css2') < head.index('<style id="mdh-styles">')

    def test_other_font_host_has_no_preconnect(self):
        html = render('x', {'style': {'font_url': 'https://example.com/fonts.css'}})
        assert 'preconnect' not in html
        assert '<link rel="stylesheet" href="https://example.com/fonts.css">' in html

    @pytest.mark.parametrize(
        'url', ['http://fonts.example.com/a.css', 'https://x.com/a.css" onload="x', 'javascript:x']
    )
    def test_unsafe_font_url_rejected(self, url):
        with pytest.raises(ConfigError, match='font_url'):
            Config.from_dict({'style': {'font_url': [url]}})


class TestFiguresAndTasks:
    def test_titled_standalone_image_becomes_figure(self, fragment):
        html = fragment('![Alt](pic.png "A <caption>")')
        assert html.startswith('<figure class="mdh-figure"><img src="pic.png" alt="Alt"')
        assert '<figcaption>A &lt;caption&gt;</figcaption></figure>' in html
        assert '<p>' not in html

    def test_inline_or_untitled_images_stay_inline(self, fragment):
        assert '<figure' not in fragment('text ![a](p.png "T")')
        assert '<figure' not in fragment('![a](p.png)')
        assert '<figure' not in fragment('![a](p.png "T") ![b](q.png "U")')

    def test_figures_can_be_disabled(self, fragment):
        assert '<figure' not in fragment('![a](p.png "T")', {'markdown': {'figures': False}})

    def test_figure_image_paths_are_still_resolved(self, write, png, tmp_path):
        png('src/img/x.png')
        src = write('src/doc.md', '![x](img/x.png "Cap")')
        out = tmp_path / 'site' / 'doc.html'
        SiteBuilder(Config()).build([src], out)
        html = body(out.read_text(encoding='utf-8'))
        assert '<figure class="mdh-figure"><img src="img/x.png"' in html

    def test_checked_task_items_are_marked(self, fragment):
        html = fragment('- [x] done\n- [ ] todo')
        assert '<li class="task-list-item is-checked">' in html
        assert '<li class="task-list-item"><input' in html


class TestDesignCli:
    def run(self, *argv: str) -> dict:
        out = io.StringIO()
        assert CommandLine(stdout=out).run([*argv, '--print-config']) == 0
        return yaml.safe_load(out.getvalue())

    def test_page_and_view_flags(self):
        config = self.run(
            '--view',
            'web',
            '--page-size',
            'Letter',
            '--orientation',
            'landscape',
            '--margin',
            '1in',
            '--font-url',
            'https://fonts.googleapis.com/css2?family=Inter',
        )
        assert config['layout']['view'] == 'web'
        assert config['page'] == {
            'size': 'Letter',
            'orientation': 'landscape',
            'margin': '1in',
            'print_link_urls': False,
        }
        assert config['style']['font_url'] == ['https://fonts.googleapis.com/css2?family=Inter']

    def test_set_page_options(self):
        config = self.run(
            '--set', 'page.print_link_urls=true', '--set', 'page_numbers.position=left'
        )
        assert config['page']['print_link_urls'] is True
        assert config['page_numbers']['position'] == 'left'


def _weasyprint_available() -> bool:
    """WeasyPrint is usable through the package's backend (in or out of process)."""
    from md_html_builder.pdf import WeasyPrintBackend

    return WeasyPrintBackend().unavailable_reason() is None


@pytest.mark.skipif(
    shutil.which('pdftotext') is None or not _weasyprint_available(),
    reason='needs WeasyPrint (with pango) and pdftotext',
)
class TestPrintOutput:
    """End-to-end: the print CSS produces running headers, footers and page numbers."""

    @staticmethod
    def pdf_pages(html: str, tmp_path: Path) -> list[str]:
        from md_html_builder.pdf import PdfExporter, WeasyPrintBackend

        exporter = PdfExporter(backends={'weasyprint': WeasyPrintBackend()})
        pdf = exporter.export_html(html, tmp_path / 'doc.pdf', tmp_path).path
        result = subprocess.run(
            ['pdftotext', '-layout', str(pdf), '-'],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ},
        )
        return [page for page in result.stdout.split('\f') if page.strip()]

    def test_running_header_footer_and_numbers_on_every_page(self, tmp_path):
        config = {
            'header': {'enabled': True, 'template': 'HEAD {title}'},
            'footer': {'enabled': True, 'template': 'FOOT'},
            'page_numbers': {'enabled': True, 'format': 'Page {page} of {total_pages}'},
            'page': {'size': 'A5'},
        }
        text = '# Doc\n\n' + '\n\n'.join(f'Paragraph {i} ' + 'text ' * 60 for i in range(30))
        pages = self.pdf_pages(render(text, config), tmp_path)
        assert len(pages) > 1
        for number, page in enumerate(pages, start=1):
            assert 'HEAD Doc' in page
            assert 'FOOT' in page
            assert f'Page {number} of {len(pages)}' in page

    def test_wide_table_fits_the_page(self, tmp_path):
        months = [f'M{i:02d}' for i in range(1, 13)]
        header = '| Region | ' + ' | '.join(months) + ' | Sum |'
        rule = '|---|' + '--:|' * 13
        row = '| North | ' + ' | '.join(['1,234,567'] * 12) + ' | ZZ |'
        pages = self.pdf_pages(render(f'{header}\n{rule}\n{row}\n'), tmp_path)
        text = '\n'.join(pages)
        assert 'M12' in text
        assert 'ZZ' in text
