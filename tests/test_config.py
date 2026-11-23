"""Configuration defaults, loading, overrides and validation."""

from __future__ import annotations

import json

import pytest

from md_html_builder.config import (
    Config,
    ConfigLoader,
    validate_css_value,
    validate_template_syntax,
)
from md_html_builder.errors import ConfigError


class TestDefaults:
    def test_defaults_are_valid(self):
        config = Config()
        assert config.style.theme == 'default'
        assert config.style.mode == 'auto'
        assert config.layout.toc == 'right'
        assert config.header.enabled is False
        assert config.page_numbers.format == 'Page {page} of {total_pages}'
        assert config.markdown.allow_html is True

    def test_round_trip(self):
        config = Config.from_dict({'title': 'T', 'style': {'colors': {'accent': 'red'}}})
        assert Config.from_dict(config.to_dict()) == config

    def test_safe_mode_disables_html(self):
        assert Config.from_dict({'markdown': {'safe_mode': True}}).markdown.allow_html is False


class TestValidation:
    def test_unknown_top_level_key_suggests(self):
        with pytest.raises(ConfigError, match="did you mean 'title'"):
            Config.from_dict({'titel': 'x'})

    def test_unknown_nested_key(self):
        with pytest.raises(ConfigError, match='layout.width'):
            Config.from_dict({'layout': {'width': '10px'}})

    def test_hyphenated_keys_accepted(self):
        assert Config.from_dict({'layout': {'max-width': '50rem'}}).layout.max_width == '50rem'

    @pytest.mark.parametrize(
        ('data', 'message'),
        [
            ({'layout': {'toc': 'middle'}}, 'layout.toc'),
            ({'layout': {'toc_depth': 9}}, 'toc_depth'),
            ({'layout': {'toc_depth': 'three'}}, 'expected an integer'),
            ({'layout': {'navigation': 'yes'}}, 'expected true/false'),
            ({'style': {'mode': 'sepia'}}, 'style.mode'),
            ({'style': {'colors': 'red'}}, 'expected a mapping'),
            ({'style': {'colors': {'Bad Key': 'red'}}}, 'invalid colour key'),
            ({'style': {'colors': {'accent': 'red; } body { x'}}}, 'forbidden characters'),
            ({'style': {'font_body': '</style><script>'}}, 'forbidden characters'),
            ({'style': {'variables': {'1bad': '1px'}}}, 'invalid CSS variable name'),
            ({'style': {'theme': '  '}}, 'theme must not be empty'),
            ({'header': {'template': '{title'}}, 'malformed template'),
            ({'footer': {'template': '{title.__class__}'}}, 'invalid template field'),
            ({'page_numbers': {'format': '{page!r}'}}, 'not allowed'),
            ({'build': {'assets': 'zip'}}, 'build.assets'),
            ({'build': {'date_format': ''}}, 'date_format'),
            ({'lang': 'not a lang!'}, 'valid language tag'),
            ({'layout': 'wide'}, 'expected a mapping'),
            ({'title': True}, 'expected a string'),
        ],
    )
    def test_invalid_values(self, data, message):
        with pytest.raises(ConfigError, match=message):
            Config.from_dict(data)

    def test_numbers_accepted_for_string_fields(self):
        config = Config.from_dict({'style': {'line_height': 1.7}})
        assert config.style.line_height == '1.7'

    def test_single_string_becomes_list(self):
        assert Config.from_dict({'style': {'custom_css': 'a.css'}}).style.custom_css == ['a.css']

    def test_validate_css_value(self):
        assert validate_css_value('"Inter", sans-serif', 'k') == '"Inter", sans-serif'
        with pytest.raises(ConfigError):
            validate_css_value('', 'k')

    def test_validate_template_syntax(self):
        assert validate_template_syntax('{{literal}} {title}', 'k')


class TestOverrides:
    def test_with_overrides_deep_merges(self):
        base = Config.from_dict({'style': {'colors': {'accent': 'red', 'bg': 'white'}}})
        merged = base.with_overrides({'style': {'colors': {'accent': 'blue'}}})
        assert merged.style.colors == {'accent': 'blue', 'bg': 'white'}
        assert base.style.colors['accent'] == 'red'

    def test_with_overrides_validates(self):
        with pytest.raises(ConfigError):
            Config().with_overrides({'layout': {'toc': 'nowhere'}})

    @pytest.mark.parametrize(
        ('assignment', 'expected'),
        [
            ('layout.max_width=60rem', {'layout': {'max_width': '60rem'}}),
            ('layout.toc_depth=2', {'layout': {'toc_depth': 2}}),
            ('header.enabled=true', {'header': {'enabled': True}}),
            ('title=My: Docs', {'title': 'My: Docs'}),
            ('build.exclude=[a, b]', {'build': {'exclude': ['a', 'b']}}),
            ('title=', {'title': ''}),
            ('footer.template={date}', {'footer': {'template': '{date}'}}),
            ('title=2024-01-01', {'title': '2024-01-01'}),
            ('style.colors={accent: red}', {'style': {'colors': {'accent': 'red'}}}),
        ],
    )
    def test_parse_assignment(self, assignment, expected):
        assert ConfigLoader.parse_assignment(assignment) == expected

    @pytest.mark.parametrize('bad', ['novalue', '=x', 'a..b=1', '1abc=2'])
    def test_parse_assignment_errors(self, bad):
        with pytest.raises(ConfigError):
            ConfigLoader.parse_assignment(bad)


class TestLoading:
    def test_load_yaml(self, write):
        path = write(
            'conf/site.yaml',
            'title: Docs\nstyle:\n  mode: dark\n  custom_css: extra.css\nbuild:\n  output: out\n',
        )
        config = ConfigLoader().load(path)
        assert config.title == 'Docs'
        assert config.style.mode == 'dark'
        assert config.style.custom_css == [str(path.parent / 'extra.css')]
        assert config.build.output == str(path.parent / 'out')

    def test_load_json(self, write):
        path = write('site.json', json.dumps({'layout': {'toc': 'left'}}))
        assert ConfigLoader().load(path).layout.toc == 'left'

    def test_load_toml(self, write):
        path = write('site.toml', '[page_numbers]\nenabled = true\n')
        assert ConfigLoader().load(path).page_numbers.enabled is True

    def test_empty_file_gives_defaults(self, write):
        assert ConfigLoader().load(write('empty.yaml', '')) == Config()

    def test_theme_path_resolved_relative_to_config(self, write):
        path = write('cfg/site.yml', 'style:\n  theme: themes/mine.css\n')
        assert ConfigLoader().load(path).style.theme == str(path.parent / 'themes' / 'mine.css')

    def test_missing_file(self, tmp_path):
        with pytest.raises(ConfigError, match='not found'):
            ConfigLoader().load(tmp_path / 'nope.yaml')

    def test_unsupported_suffix(self, write):
        with pytest.raises(ConfigError, match='unsupported config format'):
            ConfigLoader().load(write('conf.ini', 'x=1'))

    @pytest.mark.parametrize(
        ('name', 'content'),
        [
            ('bad.yaml', 'title: [unclosed'),
            ('bad.json', '{"title": '),
            ('bad.toml', 'title = '),
        ],
    )
    def test_malformed_files(self, write, name, content):
        with pytest.raises(ConfigError, match='malformed'):
            ConfigLoader().load(write(name, content))

    def test_non_mapping_top_level(self, write):
        with pytest.raises(ConfigError, match='mapping'):
            ConfigLoader().load(write('list.yaml', '- a\n- b\n'))

    def test_invalid_value_reports_file(self, write):
        path = write('site.yaml', 'layout:\n  toc: diagonal\n')
        with pytest.raises(ConfigError, match='site.yaml'):
            ConfigLoader().load(path)

    def test_non_utf8_file(self, tmp_path):
        path = tmp_path / 'latin.yaml'
        path.write_bytes('title: caf\xe9'.encode('latin-1'))
        with pytest.raises(ConfigError, match='UTF-8'):
            ConfigLoader().load(path)


class TestPathResolution:
    def test_tilde_expanded_in_config_paths(self, write, monkeypatch, tmp_path):
        home = tmp_path / 'home'
        monkeypatch.setenv('HOME', str(home))
        cfg = write(
            'cfg/c.yaml',
            'build: {output: ~/out}\nstyle: {custom_css: [~/x.css], theme: ~/themes/t.css}\n'
            'pdf: {chrome_path: ~/bin/chrome}\n',
        )
        config = ConfigLoader().load(cfg)
        assert config.build.output == str((home / 'out').resolve())
        assert config.style.custom_css == [str((home / 'x.css').resolve())]
        assert config.style.theme == str((home / 'themes' / 't.css').resolve())
        assert config.pdf.chrome_path == str((home / 'bin' / 'chrome').resolve())

    def test_relative_paths_resolved_against_config(self, write, tmp_path):
        cfg = write('cfg/c.yaml', 'build: {output: site}\npdf: {chrome_path: bin/chrome}\n')
        config = ConfigLoader().load(cfg)
        assert config.build.output == str((tmp_path / 'cfg' / 'site').resolve())
        assert config.pdf.chrome_path == str((tmp_path / 'cfg' / 'bin' / 'chrome').resolve())

    def test_bare_chrome_command_left_for_path_lookup(self, write):
        cfg = write('c.yaml', 'pdf: {chrome_path: chromium}\n')
        assert ConfigLoader().load(cfg).pdf.chrome_path == 'chromium'


def test_page_size_literal_matches_dimensions():
    from typing import get_args

    from md_html_builder.config import PageConfig, PageSize

    assert get_args(PageSize) == tuple(PageConfig.SIZES_MM)
