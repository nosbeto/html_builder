"""Theme loading and stylesheet assembly."""

from __future__ import annotations

import pytest

from md_html_builder import MarkdownConverter
from md_html_builder.config import Config
from md_html_builder.errors import ConfigError, ThemeError
from md_html_builder.highlighter import CodeHighlighter
from md_html_builder.themes import CORE_COLOR_KEYS, StylesheetBuilder, ThemeRegistry


@pytest.fixture
def registry() -> ThemeRegistry:
    return ThemeRegistry()


def stylesheet(config: Config) -> str:
    theme = ThemeRegistry().load(config.style.theme)
    return StylesheetBuilder(config, theme, CodeHighlighter(mode=config.style.mode)).build()


class TestRegistry:
    def test_default_theme_available(self, registry):
        assert 'default' in registry.available()

    def test_builtin_themes_define_core_variables(self, registry):
        for name in registry.available():
            defined = registry.load(name).defined_variables()
            missing = [k for k in CORE_COLOR_KEYS if f'--mdh-color-{k}' not in defined]
            assert not missing, f'{name} is missing {missing}'
            for var in ('--mdh-font-body', '--mdh-font-code', '--mdh-content-max-width'):
                assert var in defined, f'{name} is missing {var}'

    def test_builtin_themes_support_dark_mode(self, registry):
        for name in registry.available():
            css = registry.load(name).css
            assert ':root[data-theme="dark"]' in css
            assert 'prefers-color-scheme: dark' in css

    def test_describe(self, registry):
        assert 'default' in registry.describe()

    def test_unknown_theme_lists_available(self, registry):
        with pytest.raises(ThemeError, match='Available themes: .*default'):
            registry.load('no-such-theme')

    @pytest.mark.parametrize('name', ['', '   ', '../etc', 'bad name'])
    def test_invalid_names(self, registry, name):
        with pytest.raises(ThemeError):
            registry.load(name)

    def test_load_css_file_theme(self, registry, write):
        path = write('mine.css', ':root { --mdh-color-bg: pink; }')
        theme = registry.load(str(path))
        assert theme.name == 'mine'
        assert 'pink' in theme.css

    def test_load_directory_theme(self, registry, write, tmp_path):
        write('themes/fancy/theme.css', ':root { --mdh-color-bg: navy; }')
        write(
            'themes/fancy/theme.yaml',
            'name: Fancy\ndescription: A test\ncode_style: friendly\n',
        )
        theme = registry.load(str(tmp_path / 'themes' / 'fancy'))
        assert theme.name == 'Fancy'
        assert theme.description == 'A test'
        assert theme.code_style == 'friendly'
        assert theme.template is None

    def test_custom_builtin_dir(self, write, tmp_path):
        write('lib/alpha/theme.css', 'a{}')
        write('lib/beta.css', 'b{}')
        registry = ThemeRegistry(tmp_path / 'lib')
        assert registry.available() == ['alpha', 'beta']
        assert registry.load('beta').css == 'b{}'

    def test_directory_without_css(self, registry, tmp_path):
        (tmp_path / 'empty').mkdir()
        with pytest.raises(ThemeError, match='no theme.css'):
            registry.load(str(tmp_path / 'empty'))

    def test_missing_path(self, registry, tmp_path):
        with pytest.raises(ThemeError, match='not found'):
            registry.load(str(tmp_path / 'gone.css'))

    def test_bad_metadata(self, registry, write, tmp_path):
        write('t/theme.css', '')
        write('t/theme.yaml', 'colour: red\n')
        with pytest.raises(ThemeError, match='unknown keys'):
            registry.load(str(tmp_path / 't'))

    def test_malformed_metadata(self, registry, write, tmp_path):
        write('t/theme.css', '')
        write('t/theme.yaml', 'name: [oops')
        with pytest.raises(ThemeError, match='malformed'):
            registry.load(str(tmp_path / 't'))

    def test_theme_code_style_used_by_converter(self, write, tmp_path):
        write('t/theme.css', ':root{}')
        write('t/theme.yaml', 'code_style: friendly\ncode_style_dark: dracula\n')
        converter = MarkdownConverter(Config.from_dict({'style': {'theme': str(tmp_path / 't')}}))
        assert converter.highlighter.style == 'friendly'
        assert converter.highlighter.dark_style == 'dracula'

    def test_config_code_style_beats_theme(self, write, tmp_path):
        write('t/theme.css', ':root{}')
        write('t/theme.yaml', 'code_style: friendly\n')
        config = Config.from_dict({'style': {'theme': str(tmp_path / 't'), 'code_style': 'vim'}})
        assert MarkdownConverter(config).highlighter.style == 'vim'


class TestStylesheet:
    def test_order_theme_code_overrides_custom(self, write):
        css_file = write('extra.css', '.custom-marker { color: red; }')
        config = Config.from_dict({'style': {'custom_css': [str(css_file)]}})
        css = stylesheet(config)
        positions = [
            css.index('theme: default'),
            css.index('code highlighting'),
            css.index('configuration overrides'),
            css.index('.custom-marker'),
        ]
        assert positions == sorted(positions)

    def test_variable_overrides(self):
        config = Config.from_dict(
            {
                'layout': {'max_width': '70rem', 'sidebar_width': '12rem'},
                'style': {
                    'font_body': 'Georgia, serif',
                    'font_heading': 'Inter',
                    'font_code': 'Fira Code',
                    'font_size': '18px',
                    'line_height': '1.8',
                    'colors': {'accent': '#ff0000', 'code_bg': '#000'},
                    'variables': {'my-gap': '3px', '--other': '1em'},
                },
            }
        )
        css = stylesheet(config)
        for decl in (
            '--mdh-content-max-width: 70rem;',
            '--mdh-sidebar-width: 12rem;',
            '--mdh-font-body: Georgia, serif;',
            '--mdh-font-heading: Inter;',
            '--mdh-font-code: Fira Code;',
            '--mdh-font-size: 18px;',
            '--mdh-line-height: 1.8;',
            '--mdh-color-accent: #ff0000;',
            '--mdh-color-code-bg: #000;',
            '--my-gap: 3px;',
            '--other: 1em;',
        ):
            assert decl in css

    def test_dark_colour_overrides(self):
        config = Config.from_dict({'style': {'colors_dark': {'bg': '#111'}}})
        css = stylesheet(config)
        overrides = css[css.index('configuration overrides') :]
        assert ':root[data-theme="dark"]' in overrides
        assert ':root[data-theme="auto"]' in overrides
        assert '--mdh-color-bg: #111;' in overrides

    def test_unknown_colour_key_warns(self, caplog):
        stylesheet(Config.from_dict({'style': {'colors': {'unicorn': 'pink'}}}))
        assert 'unicorn' in caplog.text

    def test_missing_custom_css(self, tmp_path):
        config = Config.from_dict({'style': {'custom_css': [str(tmp_path / 'missing.css')]}})
        with pytest.raises(ConfigError, match='Custom CSS file not found'):
            stylesheet(config)

    def test_print_page_counters(self):
        config = Config.from_dict(
            {'page_numbers': {'enabled': True, 'format': '{page} / {total_pages}'}}
        )
        css = stylesheet(config)
        assert '@page' in css
        assert 'counter(page) " / " counter(pages)' in css

    def test_print_counters_disabled(self):
        config = Config.from_dict({'page_numbers': {'enabled': True, 'print': False}})
        assert 'counter(page)' not in stylesheet(config)

    def test_no_print_counters_without_page_numbers(self):
        assert 'counter(page)' not in stylesheet(Config())

    def test_style_close_tag_in_css_is_neutralised(self, write, page):
        css_file = write('evil.css', '/* </style><script>alert(1)</script> */')
        html = page('# x', {'style': {'custom_css': [str(css_file)]}})
        assert '</style><script>' not in html
