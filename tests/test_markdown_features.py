"""Every Markdown construct renders to the expected HTML."""

from __future__ import annotations

import re

import pytest


class TestBlocks:
    @pytest.mark.parametrize('level', range(1, 7))
    def test_atx_headings(self, fragment, level):
        html = fragment(f'{"#" * level} Heading {level}')
        assert f'<h{level} id="heading-{level}">Heading {level}</h{level}>' in html

    def test_setext_headings(self, fragment):
        html = fragment('Title\n=====\n\nSub\n---')
        assert '<h1 id="title">Title</h1>' in html
        assert '<h2 id="sub">Sub</h2>' in html

    def test_duplicate_headings_get_unique_ids(self, fragment):
        html = fragment('## Same\n\n## Same')
        assert 'id="same"' in html
        assert 'id="same-1"' in html

    def test_heading_anchors_can_be_disabled(self, fragment):
        html = fragment('## Plain', {'markdown': {'heading_anchors': False}})
        assert '<h2>Plain</h2>' in html

    def test_heading_permalinks(self, fragment):
        html = fragment('## Linked', {'markdown': {'heading_permalinks': True}})
        assert 'class="header-anchor"' in html
        assert 'href="#linked"' in html

    def test_paragraphs_and_line_breaks(self, fragment):
        html = fragment('one\ntwo\n\nthree  \nfour')
        assert '<p>one\ntwo</p>' in html
        assert '<br />' in html

    def test_breaks_option(self, fragment):
        html = fragment('one\ntwo', {'markdown': {'breaks': True}})
        assert 'one<br />' in html

    def test_blockquote_nested(self, fragment):
        html = fragment('> outer\n>\n> > inner')
        assert html.count('<blockquote>') == 2
        assert 'inner' in html

    def test_horizontal_rules(self, fragment):
        html = fragment('a\n\n---\n\nb\n\n***\n\nc\n\n___')
        assert html.count('<hr />') == 3

    def test_unordered_and_ordered_lists(self, fragment):
        html = fragment('- a\n- b\n\n1. one\n2. two')
        assert '<ul>\n<li>a</li>' in html
        assert '<ol>\n<li>one</li>' in html

    def test_ordered_list_start(self, fragment):
        assert '<ol start="3">' in fragment('3. three\n4. four')

    def test_nested_lists(self, fragment):
        html = fragment('- a\n  - b\n    1. c\n- d')
        assert html.count('<ul>') == 2
        assert '<ol>' in html

    def test_task_lists(self, fragment):
        html = fragment('- [ ] todo\n- [x] done')
        assert 'class="contains-task-list"' in html
        assert html.count('type="checkbox"') == 2
        assert 'checked' in html
        assert 'disabled' in html

    def test_definition_list(self, fragment):
        html = fragment('Term\n: Definition')
        assert '<dl>' in html
        assert '<dt>Term</dt>' in html
        assert '<dd>Definition</dd>' in html

    def test_github_alert(self, fragment):
        html = fragment('> [!WARNING]\n> Careful')
        assert 'markdown-alert-warning' in html
        assert 'Careful' in html


class TestInline:
    def test_emphasis(self, fragment):
        html = fragment('*em* _em_ **strong** __strong__ ***both***')
        assert html.count('<em>') == 3
        assert html.count('<strong>') == 3

    @pytest.mark.parametrize('source', ['~~gone~~', '~gone~'])
    def test_strikethrough(self, fragment, source):
        assert '<s>gone</s>' in fragment(source)

    def test_inline_code_is_escaped(self, fragment):
        assert '<code>&lt;div&gt; &amp;</code>' in fragment('`<div> &`')

    def test_backslash_escapes(self, fragment):
        html = fragment(r'\*not em\* \# \[x\]')
        assert '<em>' not in html
        assert '*not em*' in html

    def test_special_characters_escaped(self, fragment):
        html = fragment('5 < 6 & 7 > 3 "quoted"', {'markdown': {'raw_html': False}})
        assert '5 &lt; 6 &amp; 7 &gt; 3 &quot;quoted&quot;' in html

    def test_typographer(self, fragment):
        html = fragment('"quote" -- wait...', {'markdown': {'typographer': True}})
        assert '“quote”' in html
        assert '–' in html
        assert '…' in html


class TestLinks:
    def test_inline_link_with_title(self, fragment):
        html = fragment('[site](https://example.com "Example")')
        assert '<a href="https://example.com" title="Example">site</a>' in html

    def test_reference_links(self, fragment):
        html = fragment('[one][ref] and [ref]\n\n[ref]: https://example.org')
        assert html.count('href="https://example.org"') == 2

    def test_angle_autolink(self, fragment):
        assert '<a href="https://example.com">https://example.com</a>' in fragment(
            '<https://example.com>'
        )

    def test_bare_url_and_email_autolinks(self, fragment):
        html = fragment('see www.example.com or https://x.org and me@example.com')
        assert 'href="http://www.example.com"' in html
        assert 'href="https://x.org"' in html
        assert 'href="mailto:me@example.com"' in html

    def test_autolinks_can_be_disabled(self, fragment):
        html = fragment('see https://x.org', {'markdown': {'autolinks': False}})
        assert '<a ' not in html

    def test_javascript_links_are_not_rendered(self, fragment):
        html = fragment('[x](javascript:alert(1))')
        assert 'href="javascript' not in html

    def test_md_links_rewritten_to_html(self, fragment):
        html = fragment('[a](guide.md) [b](sub/Page.markdown#intro) [c](https://x.org/r.md)')
        assert 'href="guide.html"' in html
        assert 'href="sub/Page.html#intro"' in html
        assert 'href="https://x.org/r.md"' in html

    def test_md_link_rewrite_can_be_disabled(self, fragment):
        html = fragment('[a](guide.md)', {'build': {'rewrite_md_links': False}})
        assert 'href="guide.md"' in html


class TestImages:
    def test_image_alt_and_title(self, fragment):
        html = fragment('![A *cat*](cat.png "Kitty")')
        assert 'src="cat.png"' in html
        assert 'alt="A cat"' in html
        assert 'title="Kitty"' in html
        assert 'loading="lazy"' in html

    def test_image_with_spaces_in_angle_brackets(self, fragment):
        assert 'src="my%20cat.png"' in fragment('![c](<my cat.png>)')

    def test_reference_image(self, fragment):
        html = fragment('![logo][l]\n\n[l]: https://example.com/l.png')
        assert 'src="https://example.com/l.png"' in html


class TestTables:
    def test_table_with_alignment(self, fragment):
        html = fragment('| L | R | C | N |\n|:--|--:|:-:|---|\n| 1 | 2 | 3 | 4 |')
        assert '<div class="mdh-table-wrap" data-columns="4">' in html
        assert '<thead>' in html
        assert '<tbody>' in html
        assert '<th style="text-align:left">L</th>' in html
        assert '<td style="text-align:right">2</td>' in html
        assert '<td style="text-align:center">3</td>' in html
        assert '<td>4</td>' in html

    def test_table_inline_formatting_and_escaped_pipe(self, fragment):
        html = fragment('| a | b |\n|---|---|\n| `x \\| y` | **bold** |')
        assert '<code>x | y</code>' in html
        assert '<strong>bold</strong>' in html

    def test_table_wrapper_is_closed(self, fragment):
        html = fragment('| a |\n|---|\n| 1 |')
        assert re.search(r'</table>\s*</div>', html)


class TestCode:
    def test_fenced_code_highlighted(self, fragment):
        html = fragment('```python\ndef f():\n    return 1\n```')
        assert 'class="mdh-code"' in html
        assert 'data-lang="python"' in html
        assert '<span class="k">def</span>' in html

    def test_tilde_fence(self, fragment):
        assert 'data-lang="js"' in fragment('~~~js\nlet x = 1;\n~~~')

    def test_indented_code_block(self, fragment):
        html = fragment('para\n\n    x = <1>\n')
        assert 'mdh-code-plain' in html
        assert 'x = &lt;1&gt;' in html

    def test_unknown_language_falls_back(self, fragment, caplog):
        html = fragment('```klingon\nqapla <b>\n```')
        assert 'mdh-code-plain' in html
        assert 'qapla &lt;b&gt;' in html
        assert 'Unknown code language' in caplog.text

    def test_code_content_is_escaped(self, fragment):
        html = fragment('```html\n<script>alert(1)</script>\n```')
        assert '<script>' not in html


class TestFootnotesMathFrontMatterHtml:
    def test_footnotes(self, fragment):
        html = fragment('Text[^1] more[^note].\n\n[^1]: First.\n[^note]: Second.')
        assert 'class="footnote-ref"' in html
        assert 'class="footnotes"' in html
        assert 'First.' in html
        assert 'Second.' in html

    def test_inline_footnote(self, fragment):
        assert 'Inline note' in fragment('Text^[Inline note].')

    def test_inline_and_block_math(self, fragment):
        html = fragment('Euler $e^{i\\pi} < 0$ here.\n\n$$\na^2 + b^2\n$$')
        assert '<span class="math inline">e^{i\\pi} &lt; 0</span>' in html
        assert '<div class="math block">' in html

    def test_math_fence(self, fragment):
        assert '<div class="math block">' in fragment('```math\nx^2\n```')

    def test_currency_is_not_math(self, fragment):
        assert 'math' not in fragment('It costs $5 and $10.')

    def test_mathjax_delimiters(self, page):
        html = page('$x$', {'markdown': {'math_renderer': 'mathjax'}})
        assert '\\(x\\)' in html
        assert 'mathjax' in html

    def test_math_can_be_disabled(self, fragment):
        assert 'math' not in fragment('$x$', {'markdown': {'math': False}})

    def test_front_matter_not_rendered(self, fragment):
        html = fragment('---\ntitle: Hidden\n---\n\nBody')
        assert 'Hidden' not in html
        assert '<p>Body</p>' in html

    def test_raw_html_passthrough(self, fragment):
        html = fragment('<div class="note">raw <b>html</b></div>\n\ninline <kbd>Ctrl</kbd>')
        assert '<div class="note">raw <b>html</b></div>' in html
        assert '<kbd>Ctrl</kbd>' in html

    @pytest.mark.parametrize(
        'markdown_config', [{'safe_mode': True}, {'raw_html': False}], ids=['safe', 'no-raw']
    )
    def test_raw_html_escaped_when_disabled(self, fragment, markdown_config):
        html = fragment('<script>alert(1)</script>\n\n<b>x</b>', {'markdown': markdown_config})
        assert '<script>' not in html
        assert '&lt;script&gt;' in html
        assert '&lt;b&gt;' in html
