"""Full-page rendering: head, title, header, footer, page numbers, TOC, page templates."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from md_html_builder import MarkdownConverter
from md_html_builder.config import Config
from md_html_builder.errors import TemplateError, ThemeError
from md_html_builder.parser import Heading
from md_html_builder.renderer import NavEntry, PageContext, TocBuilder
from md_html_builder.templating import VariableTemplate


def render(text: str, config: dict | None = None, context: PageContext | None = None) -> str:
    return MarkdownConverter(Config.from_dict(config or {})).convert_text(text, context)


def body(html: str) -> str:
    """Return the part of a page after </head> (so inline CSS does not match)."""
    return html.split('</head>', 1)[1]


class TestDocumentShell:
    def test_basic_structure(self):
        html = render('# Hello')
        assert html.startswith('<!DOCTYPE html>')
        assert '<html lang="en" data-theme="auto"' in html
        assert '<meta charset="utf-8">' in html
        assert 'name="viewport"' in html
        assert '<style id="mdh-styles">' in html
        assert '<main id="mdh-content"' in html
        assert html.rstrip().endswith('</html>')

    @pytest.mark.parametrize('mode', ['light', 'dark', 'auto'])
    def test_mode(self, mode):
        assert f'data-theme="{mode}"' in render('x', {'style': {'mode': mode}})

    def test_lang_from_config_and_front_matter(self):
        assert '<html lang="fr"' in render('x', {'lang': 'fr'})
        assert '<html lang="de"' in render('---\nlang: de\n---\nx', {'lang': 'fr'})

    def test_meta_tags_escaped(self):
        html = render('---\ndescription: A "quoted" <desc>\nauthor: Ann\nkeywords: [a, b]\n---\nx')
        assert '<meta name="description" content="A &quot;quoted&quot; &lt;desc&gt;">' in html
        assert '<meta name="author" content="Ann">' in html
        assert '<meta name="keywords" content="a, b">' in html

    def test_head_html_injected(self):
        html = render('x', {'head_html': '<link rel="icon" href="f.ico">'})
        assert '<link rel="icon" href="f.ico">' in html

    def test_layout_classes(self):
        html = render('## A\n\n## B', {'layout': {'toc': 'left', 'align': 'left'}})
        assert 'mdh-toc-left' in html
        assert 'mdh-align-left' in html

    def test_stylesheet_href_instead_of_inline(self):
        html = render('x', context=PageContext(stylesheet_href='assets/s.css'))
        assert '<link rel="stylesheet" href="assets/s.css">' in html
        assert '<style' not in html


class TestTitle:
    def test_front_matter_title_wins(self):
        assert '<title>FM</title>' in render('---\ntitle: FM\n---\n# H1', {'title': 'Site'})

    def test_first_h1(self):
        assert '<title>First *x*</title>' not in render('# First *x*')
        assert '<title>First x</title>' in render('# First *x*\n\n# Second')

    def test_site_title_fallback(self):
        assert '<title>Site</title>' in render('no heading', {'title': 'Site'})

    def test_filename_fallback(self):
        html = render('no heading', context=PageContext(source=Path('/tmp/my-notes.md')))
        assert '<title>my-notes</title>' in html

    def test_untitled(self):
        assert '<title>Untitled</title>' in render('')

    def test_title_escaped(self):
        assert '<title>a &lt;b&gt; &amp; c</title>' in render('---\ntitle: a <b> & c\n---\n')


class TestHeaderFooter:
    def test_disabled_by_default(self):
        html = render('# x')
        assert '<header class="mdh-header">' not in html
        assert '<footer class="mdh-footer">' not in html

    def test_header_and_footer_templates(self):
        config = {
            'title': 'Site',
            'header': {'enabled': True, 'template': '<b>{site_title}</b> | {title}'},
            'footer': {'enabled': True, 'template': '{author} - {date} - {year} - {filename}'},
            'author': 'Ann',
        }
        context = PageContext(source=Path('/x/doc.md'), build_date=date(2024, 3, 5))
        html = render('# Doc', config, context)
        assert '<b>Site</b> | Doc' in html
        assert 'Ann - 2024-03-05 - 2024 - doc.md' in html

    def test_front_matter_variables_and_date(self):
        config = {
            'footer': {'enabled': True, 'template': '{version} on {date}'},
            'build': {'date_format': '%d/%m/%Y'},
        }
        html = render('---\nversion: 1.2\ndate: 2023-12-25\n---\n', config)
        assert '1.2 on 25/12/2023' in html

    def test_values_are_escaped_template_is_not(self):
        config = {'header': {'enabled': True, 'template': '<em>{title}</em>'}}
        html = render('---\ntitle: <script>x</script>\n---\n', config)
        assert '<em>&lt;script&gt;x&lt;/script&gt;</em>' in html

    def test_unknown_variable_raises(self):
        with pytest.raises(TemplateError, match='unknown template variable \\{nope\\}'):
            render('x', {'footer': {'enabled': True, 'template': '{nope}'}})

    def test_literal_braces(self):
        html = render('x', {'footer': {'enabled': True, 'template': '{{literal}}'}})
        assert '{literal}' in html

    def test_front_matter_can_disable_header_and_footer(self):
        config = {'header': {'enabled': True}, 'footer': {'enabled': True}}
        html = render('---\nheader: false\nfooter: false\n---\n', config)
        assert '<header class="mdh-header">' not in html
        assert '<footer class="mdh-footer">' not in html


class TestPageNumbers:
    def test_page_number_in_footer(self):
        config = {'page_numbers': {'enabled': True}}
        html = render('x', config, PageContext(page=2, total_pages=5))
        assert '<div class="mdh-page-number">Page 2 of 5</div>' in html
        assert '<footer class="mdh-footer">' in html
        assert 'mdh-footer-text' not in body(html)

    def test_custom_format(self):
        config = {'page_numbers': {'enabled': True, 'format': '{page}/{total_pages}'}}
        assert '>1/1<' in render('x', config)


class TestToc:
    def test_toc_rendered_with_links(self):
        html = render('# T\n\n## Alpha\n\n### Beta\n\n## Gamma')
        assert '<aside class="mdh-toc mdh-toc-right">' in html
        assert '<a href="#alpha">Alpha</a>' in html
        assert '<a href="#beta">Beta</a>' in html
        assert 'href="#t"' not in html

    def test_toc_depth(self):
        html = render('## A\n\n### B\n\n#### C', {'layout': {'toc_depth': 2}})
        assert 'href="#a"' in html
        assert 'href="#b"' not in html

    def test_toc_none(self):
        html = body(render('## A\n\n## B', {'layout': {'toc': 'none'}}))
        assert '<aside' not in html
        assert 'mdh-toc-none' in html

    def test_no_headings_no_toc(self):
        html = render('just text')
        assert '<aside' not in html
        assert 'mdh-toc-none' in html

    def test_front_matter_disables_toc(self):
        assert '<aside' not in render('---\ntoc: false\n---\n## A\n\n## B')

    def test_toc_title(self):
        assert 'Sommaire' in render('## A\n\n## B', {'layout': {'toc_title': 'Sommaire'}})

    def test_toc_requires_anchors(self):
        assert '<aside' not in render('## A\n\n## B', {'markdown': {'heading_anchors': False}})

    def test_toc_text_strips_formatting(self):
        assert '>Use code here</a>' in render('## Use `code` *here*\n\n## B')


class TestTocBuilder:
    @staticmethod
    def h(level: int, name: str) -> Heading:
        return Heading(level, name, name.lower())

    def test_nesting_is_balanced(self):
        headings = [self.h(2, 'A'), self.h(3, 'B'), self.h(4, 'C'), self.h(2, 'D'), self.h(4, 'E')]
        html = TocBuilder(6).render(headings)
        assert html.count('<ul') == html.count('</ul>')
        assert html.count('<li') == html.count('</li>')

    def test_starting_deeper_than_later(self):
        html = TocBuilder(6).render([self.h(3, 'A'), self.h(2, 'B')])
        assert html.count('<ul') == html.count('</ul>') == 1

    def test_invalid_depth(self):
        with pytest.raises(TemplateError):
            TocBuilder(0)

    def test_empty(self):
        assert TocBuilder().render([]) == ''


class TestNavigation:
    def test_site_nav_and_pager(self, tmp_path):
        pages = [NavEntry('One', tmp_path / 'a.html'), NavEntry('Two', tmp_path / 'sub' / 'b.html')]
        context = PageContext(
            page=2, total_pages=2, output=tmp_path / 'sub' / 'b.html', nav=pages, prev=pages[0]
        )
        html = body(render('x', context=context))
        assert '<nav class="mdh-site-nav"' in html
        assert 'href="../a.html"' in html
        assert 'aria-current="page"' in html
        assert 'class="mdh-pager-prev"' in html
        assert 'mdh-pager-next' not in html
        assert 'mdh-has-nav' in html

    def test_navigation_disabled(self, tmp_path):
        pages = [NavEntry('One', tmp_path / 'a.html'), NavEntry('Two', tmp_path / 'b.html')]
        context = PageContext(
            page=1, total_pages=2, output=pages[0].output, nav=pages, next=pages[1]
        )
        html = body(render('x', {'layout': {'navigation': False}}, context))
        assert 'mdh-site-nav' not in html
        assert 'mdh-pager' not in html


class TestThemeTemplate:
    def test_custom_template(self, write, tmp_path):
        write('t/theme.css', ':root{}')
        write('t/template.html', '<html><body data-x="$$">${title}|${content}</body></html>')
        html = render('# Hi', {'style': {'theme': str(tmp_path / 't')}})
        assert html == '<html><body data-x="$">Hi|<h1 id="hi">Hi</h1></body></html>'

    def test_unknown_placeholder(self, write, tmp_path):
        write('t/theme.css', ':root{}')
        write('t/template.html', '${content} ${sidebar}')
        with pytest.raises(ThemeError, match='sidebar'):
            render('x', {'style': {'theme': str(tmp_path / 't')}})

    def test_missing_content_placeholder(self, write, tmp_path):
        write('t/theme.css', ':root{}')
        write('t/template.html', '${title}')
        with pytest.raises(ThemeError, match='content'):
            render('x', {'style': {'theme': str(tmp_path / 't')}})

    def test_invalid_dollar(self, write, tmp_path):
        write('t/theme.css', ':root{}')
        write('t/template.html', '${content} costs $5')
        with pytest.raises(ThemeError, match='invalid'):
            render('x', {'style': {'theme': str(tmp_path / 't')}})


class TestVariableTemplate:
    def test_render(self):
        assert VariableTemplate('{a} & {b}').render({'a': '<1>', 'b': 2}) == '&lt;1&gt; & 2'

    def test_none_renders_empty(self):
        assert VariableTemplate('[{a}]').render({'a': None}) == '[]'

    @pytest.mark.parametrize('template', ['{a.b}', '{a[0]}', '{a!r}', '{a:>10}', '{0}', '{'])
    def test_rejects_unsafe_or_malformed(self, template):
        with pytest.raises(TemplateError):
            VariableTemplate(template).render({'a': 'x'})

    def test_non_string(self):
        with pytest.raises(TemplateError):
            VariableTemplate(123)  # type: ignore[arg-type]
