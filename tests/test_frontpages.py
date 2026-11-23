"""Cover page and printed contents page."""

from __future__ import annotations

import io
import logging
from datetime import date

import pytest
import yaml

from md_html_builder import Config, MarkdownConverter, SiteBuilder
from md_html_builder.cli import CommandLine
from md_html_builder.config import ConfigLoader
from md_html_builder.errors import ConfigError, InputError
from md_html_builder.pdf import ChromeBackend, PdfExporter, WeasyPrintBackend
from md_html_builder.printing import PrintLayout
from md_html_builder.renderer import PageContext

DOC = '# Report\n\nIntro.\n\n## First\n\nText.\n\n### Detail\n\nMore.\n\n## Second\n\nEnd.\n'


def render(text: str, config: dict | None = None, context: PageContext | None = None) -> str:
    converter = MarkdownConverter(Config.from_dict(config or {}))
    context = context or PageContext(build_date=date(2024, 5, 1))
    return converter.render_document(converter.parse(text), context)


def body(html: str) -> str:
    return html.split('</head>', 1)[1]


def run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = CommandLine(stdout=out).run(list(argv))
    return code, out.getvalue()


# ---------------------------------------------------------------------- configuration


class TestConfig:
    def test_off_by_default(self):
        config = Config()
        assert config.cover.enabled is False
        assert config.contents_page.enabled is False
        assert config.contents_page.depth == 2
        assert {'cover', 'contents_page'} <= set(config.to_dict())

    @pytest.mark.parametrize(
        ('data', 'message'),
        [
            ({'cover': {'title': '  '}}, 'cover.title must not be empty'),
            ({'cover': {'authors': ['Ada', '']}}, r'cover.authors\[1\] must not be empty'),
            ({'cover': {'logo': ''}}, 'cover.logo must not be empty'),
            ({'cover': {'subtitel': 'x'}}, "did you mean 'subtitle'"),
            ({'contents_page': {'depth': 0}}, 'depth must be between 1 and 6'),
            ({'contents_page': {'title': ''}}, 'contents_page.title must not be empty'),
        ],
    )
    def test_invalid_values(self, data, message):
        with pytest.raises(ConfigError, match=message):
            Config.from_dict(data)

    def test_single_author_string_becomes_list(self):
        assert Config.from_dict({'cover': {'authors': 'Ada'}}).cover.authors == ['Ada']

    def test_empty_date_is_allowed(self):
        assert Config.from_dict({'cover': {'date': ''}}).cover.date == ''

    def test_config_file_logo_resolved_against_file(self, write, tmp_path):
        path = write('conf/md2html.yaml', 'cover: {enabled: true, logo: img/logo.png}')
        config = ConfigLoader().load(path)
        assert config.cover.logo == str((tmp_path / 'conf' / 'img' / 'logo.png').resolve())

    @pytest.mark.parametrize('logo', ['https://example.com/logo.png', 'data:image/png;base64,AA'])
    def test_config_file_logo_url_untouched(self, write, logo):
        path = write('md2html.yaml', yaml.safe_dump({'cover': {'logo': logo}}))
        assert ConfigLoader().load(path).cover.logo == logo


# ---------------------------------------------------------------------- cover page


class TestCover:
    def test_absent_by_default(self):
        html = body(render(DOC))
        assert 'mdh-cover' not in html
        assert 'mdh-has-cover' not in html

    def test_defaults_from_page(self):
        html = body(render(DOC, {'cover': {'enabled': True}, 'author': 'Ada'}))
        assert '<p class="mdh-cover-title">Report</p>' in html
        assert '<ul class="mdh-cover-authors"><li>Ada</li></ul>' in html
        assert '<p class="mdh-cover-date">2024-05-01</p>' in html
        assert 'mdh-cover-subtitle' not in html
        assert 'mdh-cover-details' not in html
        assert 'mdh-has-cover' in html

    def test_all_fields(self):
        cover = {
            'enabled': True,
            'title': 'Paper',
            'subtitle': 'Sub',
            'authors': ['Ada', 'Alan'],
            'details': ['INTA 6450', 'Fall 2026'],
            'date': 'Spring',
        }
        html = body(render(DOC, {'cover': cover}))
        assert '<p class="mdh-cover-title">Paper</p>' in html
        assert '<p class="mdh-cover-subtitle">Sub</p>' in html
        assert '<li>Ada</li><li>Alan</li>' in html
        assert '<ul class="mdh-cover-details"><li>INTA 6450</li><li>Fall 2026</li></ul>' in html
        assert '<p class="mdh-cover-date">Spring</p>' in html

    def test_empty_date_hides_date(self):
        html = body(render(DOC, {'cover': {'enabled': True, 'date': ''}}))
        assert 'mdh-cover-date' not in html

    def test_text_is_escaped(self):
        cover = {'enabled': True, 'title': '<script>x</script>', 'details': ['a & b']}
        html = body(render(DOC, {'cover': cover}))
        assert '&lt;script&gt;x&lt;/script&gt;' in html
        assert '<li>a &amp; b</li>' in html
        assert '<script>x' not in html

    def test_cover_comes_before_the_content(self):
        html = body(render(DOC, {'cover': {'enabled': True}}))
        assert html.index('class="mdh-cover"') < html.index('<main')

    def test_cover_comes_after_the_screen_header(self):
        html = body(render(DOC, {'cover': {'enabled': True}, 'header': {'enabled': True}}))
        assert html.index('<header class="mdh-header">') < html.index('class="mdh-cover"')

    def test_cover_before_layout_in_web_view(self):
        html = body(render(DOC, {'cover': {'enabled': True}, 'layout': {'view': 'web'}}))
        assert html.index('class="mdh-cover"') < html.index('class="mdh-layout')

    def test_front_matter_mapping_enables_and_overrides(self):
        text = '---\ncover:\n  subtitle: From front matter\n  date: 2024-06-02\n---\n' + DOC
        html = body(render(text, {'cover': {'subtitle': 'From config'}}))
        assert '<p class="mdh-cover-subtitle">From front matter</p>' in html
        assert '<p class="mdh-cover-date">2024-06-02</p>' in html

    def test_front_matter_false_disables(self):
        html = body(render('---\ncover: false\n---\n' + DOC, {'cover': {'enabled': True}}))
        assert 'class="mdh-cover"' not in html

    def test_front_matter_true_enables(self):
        assert 'class="mdh-cover"' in body(render('---\ncover: true\n---\n' + DOC))

    def test_front_matter_mapping_can_disable(self):
        text = '---\ncover: {enabled: false, subtitle: x}\n---\n' + DOC
        assert 'class="mdh-cover"' not in body(render(text, {'cover': {'enabled': True}}))

    def test_front_matter_authors_fall_back_to_author_list(self):
        text = '---\nauthor: [Ada, Alan]\ncover: true\n---\n' + DOC
        assert '<li>Ada</li><li>Alan</li>' in body(render(text, {'author': 'Config'}))

    @pytest.mark.parametrize('value', ['yes please', '[1, 2]', '3'])
    def test_front_matter_wrong_type(self, value):
        with pytest.raises(InputError, match='"cover" must be true, false or a mapping'):
            render(f'---\ncover: {value}\n---\n' + DOC)

    def test_front_matter_invalid_mapping(self):
        message = "invalid front matter: Unknown configuration key 'cover.colour'"
        with pytest.raises(InputError, match=message):
            render('---\ncover: {colour: red}\n---\n' + DOC)


# ---------------------------------------------------------------------- contents page


class TestContentsPage:
    def test_absent_by_default(self):
        assert 'mdh-contents-page' not in body(render(DOC))

    def test_lists_headings_to_depth(self):
        html = body(render(DOC, {'contents_page': {'enabled': True}}))
        assert '<p class="mdh-contents-title">Contents</p>' in html
        assert '<a href="#first">First</a>' in html
        assert '<a href="#second">Second</a>' in html
        assert '#detail' not in html.split('</nav>', 1)[0].split('mdh-contents-page', 1)[1]
        assert '<a href="#report">' not in html  # the single leading H1 is the title
        assert 'mdh-contents-numbered' in html
        assert 'mdh-has-contents-page' in html

    def test_depth_and_indentation(self):
        html = body(render(DOC, {'contents_page': {'enabled': True, 'depth': 3}}))
        assert 'class="mdh-contents-h2 mdh-contents-indent-0"><a href="#first">' in html
        assert 'class="mdh-contents-h3 mdh-contents-indent-1"><a href="#detail">' in html

    def test_title_and_numbers_options(self):
        settings = {'enabled': True, 'title': 'Index', 'page_numbers': False}
        html = body(render(DOC, {'contents_page': settings}))
        assert '<p class="mdh-contents-title">Index</p>' in html
        assert 'aria-label="Index"' in html
        assert 'mdh-contents-numbered' not in html

    def test_after_cover(self):
        config = {'cover': {'enabled': True}, 'contents_page': {'enabled': True}}
        html = body(render(DOC, config))
        assert html.index('class="mdh-cover"') < html.index('class="mdh-contents-page')

    def test_front_matter_switch(self):
        text = '---\ncontents_page: false\n---\n' + DOC
        assert 'mdh-contents-page' not in body(render(text, {'contents_page': {'enabled': True}}))
        assert 'mdh-contents-page' in body(render('---\ncontents_page: true\n---\n' + DOC))

    def test_front_matter_wrong_type(self):
        with pytest.raises(InputError, match='"contents_page" must be true or false'):
            render('---\ncontents_page: maybe\n---\n' + DOC)

    def test_no_headings_skips_page(self, caplog):
        with caplog.at_level(logging.WARNING, logger='md_html_builder'):
            html = body(render('Just text.', {'contents_page': {'enabled': True}}))
        assert 'mdh-contents-page' not in html
        assert 'no headings to list' in caplog.text

    def test_independent_of_sidebar_toc(self):
        config = {'contents_page': {'enabled': True}, 'layout': {'toc': 'none'}}
        html = body(render(DOC, config))
        assert 'mdh-contents-page' in html
        assert 'class="mdh-toc ' not in html


# ---------------------------------------------------------------------- templates and CSS


class TestTemplateAndCss:
    def test_template_without_placeholder_gets_front_pages_in_content(self, write, tmp_path):
        write('t/theme.css', ':root{}')
        write('t/template.html', '<body>${content}</body>')
        config = {'style': {'theme': str(tmp_path / 't')}, 'cover': {'enabled': True}}
        html = render(DOC, config)
        assert html.startswith('<body><section class="mdh-cover"')

    def test_template_with_placeholder(self, write, tmp_path):
        write('t/theme.css', ':root{}')
        write('t/template.html', '<body>${front_pages}|${content}</body>')
        config = {'style': {'theme': str(tmp_path / 't')}, 'cover': {'enabled': True}}
        front, content = render(DOC, config)[len('<body>') :].split('|', 1)
        assert front.startswith('<section class="mdh-cover"')
        assert 'mdh-cover' not in content

    def test_cover_page_css_empties_every_margin_box(self):
        css = PrintLayout(Config()).cover_page_css()
        assert css.startswith('@page mdh-cover {')
        for box in PrintLayout.MARGIN_BOXES:
            assert f'@{box} {{ content: none; }}' in css

    def test_cover_reset_comes_after_the_boxes_it_overrides(self):
        layout = PrintLayout(Config.from_dict({'page_numbers': {'enabled': True}}))
        sheet = layout.stylesheet()
        assert sheet.index('counter(page)') < sheet.index('@page mdh-cover')
        regions = layout.region_css('Head', 'Foot')
        assert regions.index('element(mdh-running-footer)') < regions.index('@page mdh-cover')

    def test_stylesheet_has_front_page_rules(self):
        css = MarkdownConverter(Config()).stylesheet
        assert 'page: mdh-cover' in css
        assert 'target-counter(attr(href url), page)' in css
        assert '.mdh-has-cover .mdh-print-regions { page: mdh-cover; }' in css


# ---------------------------------------------------------------------- logo


class TestLogo:
    def test_front_matter_logo_is_copied(self, write, png, tmp_path):
        png('docs/img/logo.png')
        src = write('docs/report.md', '---\ncover: {logo: img/logo.png}\n---\n' + DOC)
        SiteBuilder(Config()).build(src, tmp_path / 'out')
        html = (tmp_path / 'out' / 'report.html').read_text(encoding='utf-8')
        assert '<img class="mdh-cover-logo" src="img/logo.png" alt="">' in html
        assert (tmp_path / 'out' / 'img' / 'logo.png').is_file()

    def test_logo_is_embedded(self, write, png, tmp_path):
        png('docs/logo.png')
        src = write('docs/report.md', DOC)
        config = Config.from_dict({'cover': {'logo': 'logo.png'}, 'build': {'assets': 'embed'}})
        config = config.with_overrides({'cover': {'enabled': True}})
        SiteBuilder(config).build(src, tmp_path / 'out')
        html = (tmp_path / 'out' / 'report.html').read_text(encoding='utf-8')
        assert 'class="mdh-cover-logo" src="data:image/png;base64,' in html

    def test_absolute_config_logo_is_made_relative(self, write, png, tmp_path):
        logo = png('brand/logo.png')
        src = write('docs/report.md', DOC)
        config = Config.from_dict({'cover': {'enabled': True, 'logo': str(logo)}})
        SiteBuilder(config.with_overrides({'build': {'assets': 'rewrite'}})).build(
            src, tmp_path / 'out'
        )
        html = (tmp_path / 'out' / 'report.html').read_text(encoding='utf-8')
        assert 'src="../brand/logo.png"' in html

    def test_url_logo_untouched(self, write, tmp_path):
        src = write('report.md', DOC)
        config = Config.from_dict({'cover': {'enabled': True, 'logo': 'https://x.org/l.png'}})
        SiteBuilder(config).build(src, tmp_path / 'out')
        html = (tmp_path / 'out' / 'report.html').read_text(encoding='utf-8')
        assert 'src="https://x.org/l.png"' in html

    def test_missing_logo_fails(self, write, tmp_path):
        src = write('report.md', '---\ncover: {logo: nope.png}\n---\n' + DOC)
        with pytest.raises(InputError, match='cover logo not found'):
            SiteBuilder(Config()).build(src, tmp_path / 'out')

    def test_no_logo_without_cover(self, write, png, tmp_path):
        png('logo.png')
        src = write('report.md', DOC)
        config = Config.from_dict({'cover': {'enabled': False, 'logo': 'logo.png'}})
        SiteBuilder(config).build(src, tmp_path / 'out')
        assert 'mdh-cover-logo' not in body((tmp_path / 'out' / 'report.html').read_text('utf-8'))


# ---------------------------------------------------------------------- command line


class TestCommandLine:
    def config(self, *argv: str) -> dict:
        code, out = run('--print-config', *argv)
        assert code == 0
        return yaml.safe_load(out)

    def test_cover_options_imply_cover(self):
        cover = self.config(
            '--cover-subtitle',
            'Sub',
            '--cover-author',
            'Ada',
            '--cover-author',
            'Alan',
            '--cover-detail',
            'INTA 6450',
            '--cover-date',
            '',
        )['cover']
        assert cover['enabled'] is True
        assert cover['subtitle'] == 'Sub'
        assert cover['authors'] == ['Ada', 'Alan']
        assert cover['details'] == ['INTA 6450']
        assert cover['date'] == ''

    def test_cover_switch(self):
        assert self.config('--cover')['cover']['enabled'] is True
        assert self.config('--no-cover')['cover']['enabled'] is False

    def test_cover_logo_resolved_from_working_directory(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cover = self.config('--cover-logo', 'logo.png')['cover']
        assert cover['logo'] == str((tmp_path / 'logo.png').resolve())

    def test_contents_options(self):
        contents = self.config('--contents-depth', '3')['contents_page']
        assert contents == {**contents, 'enabled': True, 'depth': 3}
        assert self.config('--contents-page')['contents_page']['enabled'] is True

    @pytest.mark.parametrize(
        'argv',
        [('--no-cover', '--cover-subtitle', 'x'), ('--no-contents-page', '--contents-depth', '2')],
    )
    def test_conflicting_options(self, argv, capsys):
        code, _ = run('--print-config', *argv)
        assert code == 1
        assert 'cannot be combined' in capsys.readouterr().err

    def test_build_with_cover(self, write):
        src = write('doc.md', DOC)
        assert run(str(src), '--cover-detail', 'Course', '--contents-page')[0] == 0
        html = src.with_suffix('.html').read_text(encoding='utf-8')
        assert '<ul class="mdh-cover-details"><li>Course</li></ul>' in html
        assert 'mdh-contents-page' in html


# ---------------------------------------------------------------------- real PDF output


@pytest.mark.parametrize('backend_name', ['weasyprint', 'chrome'])
def test_front_pages_add_exactly_two_pdf_pages(backend_name, write, tmp_path):
    """The cover and the contents page each take one page, with no blank page added."""
    backend = WeasyPrintBackend() if backend_name == 'weasyprint' else ChromeBackend()
    reason = backend.unavailable_reason()
    if reason is not None:
        pytest.skip(f'{backend_name} not available: {reason[:80]}')
    src = write('report.md', DOC)
    base = {
        'build': {'format': 'pdf'},
        'pdf': {'backend': backend_name},
        'header': {'enabled': True},
        'footer': {'enabled': True},
        'page_numbers': {'enabled': True},
    }
    pages = []
    for extra in ({}, {'cover': {'enabled': True}, 'contents_page': {'enabled': True}}):
        config = Config.from_dict({**base, **extra})
        exporter = PdfExporter(config=config.pdf, backends={backend_name: backend})
        out = tmp_path / f'out{len(pages)}'
        result = SiteBuilder(config, pdf_exporter=exporter).build(src, out)
        pages.append(result.pdfs[0].pages)
    if None in pages:
        pytest.skip(f'{backend_name} did not report page counts')
    assert pages[1] == pages[0] + 2
