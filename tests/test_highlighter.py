"""Code highlighting with Pygments."""

from __future__ import annotations

import pytest

from md_html_builder.errors import ConfigError, InputError
from md_html_builder.highlighter import CodeHighlighter


@pytest.fixture
def highlighter() -> CodeHighlighter:
    return CodeHighlighter()


class TestParseInfo:
    @pytest.mark.parametrize(
        ('info', 'expected'),
        [
            ('python', 'python'),
            ('  Python  ', 'python'),
            ('js {linenos=true}', 'js'),
            ('{.ruby}', 'ruby'),
            ('c++', 'c++'),
            ('', ''),
        ],
    )
    def test_language_extraction(self, info, expected):
        assert CodeHighlighter.parse_info(info) == expected


class TestHighlight:
    @pytest.mark.parametrize(
        ('lang', 'code', 'token_class'),
        [
            ('python', 'import os\n', 'kn'),
            ('javascript', 'const x = 1;\n', 'kd'),
            ('bash', 'echo "hi"\n', 'nb'),
            ('json', '{"a": 1}\n', 'nt'),
            ('rust', 'fn main() {}\n', 'k'),
            ('sql', 'SELECT 1;\n', 'k'),
            ('yaml', 'key: value\n', 'nt'),
            ('html', '<p>x</p>\n', 'nt'),
        ],
    )
    def test_languages_are_coloured(self, highlighter, lang, code, token_class):
        html = highlighter.highlight(code, lang)
        assert f'class="{token_class}"' in html
        assert 'mdh-code-plain' not in html

    @pytest.mark.parametrize('alias', ['py', 'js', 'sh', 'yml', 'ts'])
    def test_aliases(self, highlighter, alias):
        assert 'mdh-code-plain' not in highlighter.highlight('x = 1\n', alias)

    def test_unknown_language_warns_and_escapes(self, highlighter, caplog):
        html = highlighter.highlight('<tag> & stuff\n', 'notalanguage')
        assert '&lt;tag&gt; &amp; stuff' in html
        assert 'mdh-code-plain' in html
        assert 'data-lang="notalanguage"' in html
        assert 'notalanguage' in caplog.text

    def test_unknown_language_strict_raises(self):
        with pytest.raises(InputError, match='Unknown code language'):
            CodeHighlighter(strict=True).highlight('x\n', 'notalanguage')

    def test_no_language_is_plain_without_label(self, highlighter):
        html = highlighter.highlight('plain\n', '')
        assert 'mdh-code-plain' in html
        assert 'data-lang' not in html
        assert 'mdh-code-lang' not in html

    def test_guess_language(self):
        html = CodeHighlighter(guess_language=True).highlight('#!/usr/bin/env python\nimport os\n')
        assert 'mdh-code-plain' not in html

    def test_line_numbers(self):
        html = CodeHighlighter(line_numbers=True).highlight('a = 1\nb = 2\n', 'python')
        assert 'linenos' in html

    def test_language_label_escaped(self, highlighter):
        html = highlighter.highlight('x\n', '"><script>')
        assert '<script>' not in html

    def test_blank_lines_preserved(self, highlighter):
        html = highlighter.highlight('a = 1\n\n\nb = 2\n', 'python')
        assert '\n\n\n' in html


class TestStyles:
    def test_unknown_style_raises_config_error(self):
        with pytest.raises(ConfigError, match='Unknown code style'):
            CodeHighlighter(style='does-not-exist')

    def test_unknown_mode_raises(self):
        with pytest.raises(ConfigError):
            CodeHighlighter(mode='sepia')

    def test_available_styles(self):
        styles = CodeHighlighter.available_styles()
        assert 'monokai' in styles
        assert 'default' in styles

    def test_light_stylesheet_is_unscoped(self):
        css = CodeHighlighter(mode='light', style='friendly').stylesheet()
        assert '.highlight' in css
        assert 'data-theme' not in css

    def test_auto_stylesheet_has_dark_rules(self):
        css = CodeHighlighter(mode='auto').stylesheet()
        assert ':root[data-theme="dark"] .highlight' in css
        assert '@media screen and (prefers-color-scheme: dark)' in css
        assert ':root[data-theme="auto"] .highlight' in css

    def test_dark_stylesheet_uses_dark_style(self):
        css = CodeHighlighter(mode='dark', dark_style='monokai').stylesheet()
        assert '#272822' in css
