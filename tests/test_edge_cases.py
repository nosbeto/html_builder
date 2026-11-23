"""Edge cases: empty input, unicode, line endings, escaping, odd structures."""

from __future__ import annotations

import pytest

from md_html_builder import Config, MarkdownConverter, SiteBuilder, convert
from md_html_builder.errors import InputError
from md_html_builder.parser import MarkdownParser


def body(html: str) -> str:
    return html.split('</head>', 1)[1]


class TestEmptyAndWhitespace:
    @pytest.mark.parametrize('text', ['', '   ', '\n\n\n', '\t'])
    def test_empty_documents_render(self, text):
        html = convert(text)
        assert '<article class="mdh-article markdown-body">' in html
        assert '<title>Untitled</title>' in html

    def test_empty_file_build(self, write):
        src = write('empty.md', '')
        SiteBuilder().build(src)
        assert '<title>empty</title>' in src.with_suffix('.html').read_text()

    def test_front_matter_only(self, page):
        html = page('---\ntitle: Only FM\n---\n')
        assert '<title>Only FM</title>' in html

    def test_empty_front_matter(self, page):
        assert '<title>Untitled</title>' in page('---\n---\n')


class TestUnicode:
    def test_unicode_content_and_headings(self, page):
        html = page('# Café ünïcødé 中文 🚀\n\n## Ελληνικά\n\nΤεστ — “quotes”')
        assert '<title>Café ünïcødé 中文 🚀</title>' in html
        assert 'Τεστ — “quotes”' in html
        assert 'href="#ελληνικά"' in html

    def test_unicode_file_name_and_bom(self, tmp_path):
        src = tmp_path / 'résumé.md'
        src.write_text('﻿# Résumé', encoding='utf-8')
        SiteBuilder().build(src)
        html = (tmp_path / 'résumé.html').read_text(encoding='utf-8')
        assert '<h1 id="résumé">Résumé</h1>' in html
        assert '﻿' not in html

    def test_utf8_output_declared(self, page):
        assert '<meta charset="utf-8">' in page('ü')

    def test_rtl_text(self, fragment):
        assert 'שלום' in fragment('שלום עולם')


class TestLineEndingsAndStructure:
    def test_crlf_line_endings(self, fragment):
        html = fragment('# Title\r\n\r\n- a\r\n- b\r\n\r\n```py\r\nx = 1\r\n```\r\n')
        assert '<h1 id="title">Title</h1>' in html
        assert '<li>a</li>' in html
        assert 'data-lang="py"' in html

    def test_deeply_nested_blockquotes(self, fragment):
        html = fragment('> ' * 10 + 'deep')
        assert html.count('<blockquote>') == 10

    def test_deeply_nested_lists(self, fragment):
        lines = [f'{"  " * i}- level {i}' for i in range(8)]
        assert fragment('\n'.join(lines)).count('<ul>') == 8

    def test_unclosed_fence_runs_to_end(self, fragment):
        html = fragment('```python\nx = 1\n')
        assert 'data-lang="python"' in html

    def test_large_document(self, fragment):
        text = '\n\n'.join(f'## Section {i}\n\nParagraph {i} with **bold**.' for i in range(500))
        html = fragment(text)
        assert html.count('<h2') == 500

    def test_table_without_body(self, fragment):
        assert '<thead>' in fragment('| a | b |\n|---|---|')

    def test_table_ragged_rows(self, fragment):
        html = fragment('| a | b |\n|---|---|\n| 1 |\n| 1 | 2 | 3 |')
        assert '<td>1</td>' in html
        assert '3' not in html.split('<tbody>')[1].split('</tbody>')[0].replace('<td></td>', '')

    def test_horizontal_rule_not_front_matter_midway(self, fragment):
        html = fragment('Intro\n\n---\n\ntitle: x\n\n---\n')
        assert 'Intro' in html
        assert '<hr />' in html


class TestEscaping:
    def test_html_entities_preserved(self, fragment):
        html = fragment('&copy; &amp; &#169; &nbsp;')
        assert '©' in html or '&copy;' in html

    def test_link_url_with_quotes_escaped(self, fragment):
        html = fragment('[x](https://e.com/?a="b"&c=<d>)')
        assert '"b"' not in html

    def test_heading_with_html_in_toc_is_plain(self, page):
        html = body(page('## A <em>b</em> c\n\n## D'))
        toc = html.split('<aside', 1)[1].split('</aside>', 1)[0]
        assert '<em>' not in toc

    def test_title_with_markup_in_front_matter(self, page):
        html = page('---\ntitle: "<script>alert(1)</script>"\n---\n')
        assert '<script>alert(1)</script>' not in html

    def test_non_string_input_rejected(self):
        parser = MarkdownConverter(Config()).parser
        with pytest.raises(InputError):
            parser.parse(b'bytes')  # type: ignore[arg-type]


class TestFrontMatterValues:
    def test_dates_normalised(self):
        doc = MarkdownConverter().parse('---\ndate: 2024-01-02\nlist: [2024-01-03]\n---\n')
        assert doc.meta == {'date': '2024-01-02', 'list': ['2024-01-03']}

    def test_numeric_title(self, page):
        assert '<title>2024</title>' in page('---\ntitle: 2024\n---\n')

    def test_nested_values_ignored_in_templates(self, page):
        config = {'footer': {'enabled': True, 'template': '{title}'}}
        html = page('---\ntitle: T\nextra: {a: 1}\n---\n', config)
        assert '<div class="mdh-footer-text">T</div>' in html

    def test_heading_extraction_static(self):
        parser = MarkdownConverter().parser
        doc = parser.parse('# A\n\n## `B` c')
        assert [(h.level, h.text) for h in doc.headings] == [(1, 'A'), (2, 'B c')]
        assert doc.first_h1 == 'A'
        assert isinstance(parser, MarkdownParser)
