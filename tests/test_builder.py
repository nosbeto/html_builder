"""Single-file and multi-file builds, output paths, ordering, navigation and assets."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from md_html_builder import Config, SiteBuilder
from md_html_builder.assets import AssetResolver, LinkProcessor
from md_html_builder.builder import SourceCollector
from md_html_builder.errors import BuildError, InputError


def build(inputs, output=None, config: dict | None = None):
    return SiteBuilder(Config.from_dict(config or {})).build(inputs, output)


def body(path: Path) -> str:
    return path.read_text(encoding='utf-8').split('</head>', 1)[1]


class TestSingleFile:
    def test_default_output_next_to_source(self, write):
        src = write('doc.md', '# Hi')
        result = build(src)
        assert result.outputs == [src.with_suffix('.html')]
        assert '<h1 id="hi">Hi</h1>' in src.with_suffix('.html').read_text()

    def test_explicit_html_file(self, write, tmp_path):
        src = write('doc.md', '# Hi')
        out = tmp_path / 'deep' / 'nested' / 'page.html'
        assert build(src, out).outputs == [out]
        assert out.is_file()

    def test_output_directory(self, write, tmp_path):
        src = write('src/doc.md', '# Hi')
        result = build(src, tmp_path / 'site')
        assert result.outputs == [tmp_path / 'site' / 'doc.html']

    def test_output_from_config(self, write, tmp_path):
        src = write('doc.md', 'x')
        result = build(src, config={'build': {'output': str(tmp_path / 'cfg-out')}})
        assert result.outputs == [tmp_path / 'cfg-out' / 'doc.html']

    def test_single_page_is_page_one_of_one(self, write):
        src = write('doc.md', 'x')
        build(src, config={'page_numbers': {'enabled': True}})
        assert 'Page 1 of 1' in body(src.with_suffix('.html'))

    def test_no_navigation_for_single_page(self, write):
        src = write('doc.md', 'x')
        build(src)
        html = body(src.with_suffix('.html'))
        assert 'mdh-site-nav' not in html
        assert 'mdh-pager' not in html

    def test_non_markdown_extension_warns(self, write, caplog):
        src = write('notes.txt', '# Notes')
        build(src)
        assert src.with_suffix('.html').is_file()
        assert 'Markdown extension' in caplog.text


class TestMultiFile:
    @pytest.fixture
    def docs(self, write, tmp_path):
        write('docs/index.md', '# Home')
        write('docs/b.md', '# Bravo')
        write('docs/a.md', '# Alpha')
        write('docs/guide/intro.md', '# Intro\n\n[home](../index.md)')
        return tmp_path / 'docs'

    def test_mirrors_tree(self, docs, tmp_path):
        result = build(docs, tmp_path / 'site')
        names = [p.relative_to(tmp_path / 'site').as_posix() for p in result.outputs]
        assert names == ['index.html', 'a.html', 'b.html', 'guide/intro.html']

    def test_page_numbers_and_navigation(self, docs, tmp_path):
        build(docs, tmp_path / 'site', {'page_numbers': {'enabled': True}})
        a = body(tmp_path / 'site' / 'a.html')
        assert 'Page 2 of 4' in a
        assert 'class="mdh-site-nav"' in a
        assert 'rel="prev" href="index.html"' in a
        assert 'rel="next" href="b.html"' in a
        intro = body(tmp_path / 'site' / 'guide' / 'intro.html')
        assert 'Page 4 of 4' in intro
        assert 'href="../index.html"' in intro
        assert 'mdh-pager-next' not in intro

    def test_total_pages_in_footer_template(self, docs, tmp_path):
        config = {'footer': {'enabled': True, 'template': '{page}/{total_pages}'}}
        build(docs, tmp_path / 'site', config)
        assert '>1/4<' in body(tmp_path / 'site' / 'index.html')

    def test_md_cross_links_rewritten(self, docs, tmp_path):
        build(docs, tmp_path / 'site')
        assert 'href="../index.html">home' in body(tmp_path / 'site' / 'guide' / 'intro.html')

    def test_front_matter_order(self, write, tmp_path):
        write('d/z.md', '---\norder: 1\n---\n# Z')
        write('d/a.md', '# A')
        write('d/m.md', '---\norder: 2.5\n---\n# M')
        result = build(tmp_path / 'd', tmp_path / 'out')
        assert [p.stem for p in result.outputs] == ['z', 'm', 'a']

    def test_non_numeric_order(self, write, tmp_path):
        write('d/a.md', '---\norder: first\n---\n')
        write('d/b.md', '')
        with pytest.raises(InputError, match='order'):
            build(tmp_path / 'd', tmp_path / 'out')

    def test_multiple_file_inputs(self, write, tmp_path):
        a = write('x/a.md', '# A')
        b = write('y/b.md', '# B')
        result = build([a, b], tmp_path / 'out')
        assert sorted(p.relative_to(tmp_path / 'out').as_posix() for p in result.outputs) == [
            'x/a.html',
            'y/b.html',
        ]

    def test_default_output_alongside_sources(self, docs):
        result = build(docs)
        assert (docs / 'guide' / 'intro.html') in result.outputs

    def test_html_file_output_with_many_inputs(self, docs, tmp_path):
        with pytest.raises(InputError, match='use an output directory'):
            build(docs, tmp_path / 'one.html')

    def test_output_is_existing_file(self, docs, write):
        existing = write('taken', 'x')
        with pytest.raises(InputError, match='not a directory'):
            build(docs, existing)

    def test_output_clash(self, write, tmp_path):
        write('d/page.md', 'a')
        write('d/page.markdown', 'b')
        with pytest.raises(InputError, match='both be written'):
            build(tmp_path / 'd', tmp_path / 'out')

    def test_shared_stylesheet(self, docs, tmp_path):
        result = build(docs, tmp_path / 'site', {'build': {'inline_css': False}})
        assert result.stylesheet == tmp_path / 'site' / 'assets' / 'md-html-builder.css'
        assert '--mdh-color-bg' in result.stylesheet.read_text()
        intro = (tmp_path / 'site' / 'guide' / 'intro.html').read_text()
        assert 'href="../assets/md-html-builder.css"' in intro
        assert '<style' not in intro


class TestCollector:
    def test_skips_hidden_and_excluded(self, write, tmp_path):
        write('d/keep.md')
        write('d/.hidden/secret.md')
        write('d/node_modules/pkg/readme.md')
        write('d/drafts/wip.md')
        write('d/image.png')
        sources, root = SourceCollector(exclude=['node_modules/*', 'drafts/*']).collect(
            [tmp_path / 'd']
        )
        assert [s.relative.as_posix() for s in sources] == ['keep.md']
        assert root == (tmp_path / 'd').resolve()

    def test_non_recursive(self, write, tmp_path):
        write('d/top.md')
        write('d/sub/deep.md')
        sources, _ = SourceCollector(recursive=False).collect([tmp_path / 'd'])
        assert [s.relative.as_posix() for s in sources] == ['top.md']

    def test_duplicates_removed(self, write, tmp_path):
        a = write('d/a.md')
        sources, _ = SourceCollector().collect([a, tmp_path / 'd'])
        assert len(sources) == 1

    def test_missing_input(self, tmp_path):
        with pytest.raises(InputError, match='Input not found'):
            build(tmp_path / 'missing.md')

    def test_empty_directory(self, tmp_path):
        (tmp_path / 'empty').mkdir()
        with pytest.raises(InputError, match='No Markdown files found'):
            build(tmp_path / 'empty')

    def test_no_inputs(self):
        with pytest.raises(InputError, match='No input'):
            build([])


class TestAssets:
    def test_copy_images_to_output(self, write, png, tmp_path):
        png('src/img/logo.png')
        src = write('src/doc.md', '![logo](img/logo.png)')
        build(src, tmp_path / 'site')
        assert (tmp_path / 'site' / 'img' / 'logo.png').is_file()
        assert 'src="img/logo.png"' in body(tmp_path / 'site' / 'doc.html')

    def test_copy_noop_when_output_beside_source(self, write, png, tmp_path):
        png('img/a.png')
        src = write('doc.md', '![a](img/a.png)')
        build(src)
        assert 'src="img/a.png"' in body(src.with_suffix('.html'))

    def test_copy_falls_back_to_rewrite_outside_root(self, write, png, tmp_path):
        png('shared/pic.png')
        src = write('src/doc.md', '![p](../shared/pic.png)')
        build(src, tmp_path / 'out' / 'site')
        assert 'src="../../shared/pic.png"' in body(tmp_path / 'out' / 'site' / 'doc.html')

    def test_rewrite_mode(self, write, png, tmp_path):
        png('src/img/a b.png')
        src = write('src/doc.md', '![a](<img/a b.png>)')
        build(src, tmp_path / 'site' / 'deep', {'build': {'assets': 'rewrite'}})
        assert 'src="../../src/img/a%20b.png"' in body(tmp_path / 'site' / 'deep' / 'doc.html')
        assert not (tmp_path / 'site' / 'deep' / 'img').exists()

    def test_embed_mode(self, write, png, tmp_path):
        image = png('src/a.png')
        src = write('src/doc.md', '![a](a.png)')
        build(src, tmp_path / 'site', {'build': {'assets': 'embed'}})
        expected = base64.b64encode(image.read_bytes()).decode()
        assert f'src="data:image/png;base64,{expected}"' in body(tmp_path / 'site' / 'doc.html')

    def test_keep_mode(self, write, png, tmp_path):
        png('src/a.png')
        src = write('src/doc.md', '![a](a.png)')
        build(src, tmp_path / 'site', {'build': {'assets': 'keep'}})
        assert not (tmp_path / 'site' / 'a.png').exists()
        assert 'src="a.png"' in body(tmp_path / 'site' / 'doc.html')

    def test_remote_and_absolute_untouched(self, write, tmp_path):
        src = write(
            'src/doc.md',
            '![r](https://example.com/x.png) ![a](/abs/x.png) ![d](data:image/png;base64,AAAA)',
        )
        build(src, tmp_path / 'site', {'build': {'assets': 'rewrite'}})
        html = body(tmp_path / 'site' / 'doc.html')
        assert 'src="https://example.com/x.png"' in html
        assert 'src="/abs/x.png"' in html

    def test_missing_image_warns(self, write, tmp_path, caplog):
        src = write('src/doc.md', '![gone](gone.png)')
        build(src, tmp_path / 'site')
        assert 'src="gone.png"' in body(tmp_path / 'site' / 'doc.html')
        assert 'referenced file not found' in caplog.text

    def test_missing_image_strict(self, write, tmp_path):
        src = write('src/doc.md', '![gone](gone.png)')
        with pytest.raises(InputError, match='gone.png'):
            build(src, tmp_path / 'site', {'build': {'strict': True}})

    def test_linked_local_file_copied(self, write, tmp_path):
        write('src/files/report.pdf', 'PDF')
        src = write('src/doc.md', '[report](files/report.pdf#page=2)')
        build(src, tmp_path / 'site')
        assert (tmp_path / 'site' / 'files' / 'report.pdf').read_text() == 'PDF'
        assert 'href="files/report.pdf#page=2"' in body(tmp_path / 'site' / 'doc.html')

    def test_raw_html_img_resolved(self, write, png, tmp_path):
        png('src/pics/x.png')
        src = write(
            'src/doc.md', '<p><img src="pics/x.png" alt="x"></p>\n\ninline <img src="pics/x.png">'
        )
        build(src, tmp_path / 'site', {'build': {'assets': 'rewrite'}})
        html = body(tmp_path / 'site' / 'doc.html')
        assert html.count('src="../src/pics/x.png"') == 2

    def test_query_and_fragment_preserved(self, write, png, tmp_path):
        png('src/a.png')
        src = write('src/doc.md', '![a](a.png?v=2#frag)')
        build(src, tmp_path / 'site', {'build': {'assets': 'rewrite'}})
        assert 'src="../src/a.png?v=2#frag"' in body(tmp_path / 'site' / 'doc.html')

    def test_resolver_rejects_unknown_mode(self, tmp_path):
        with pytest.raises(BuildError):
            AssetResolver('zip', tmp_path)

    @pytest.mark.parametrize(
        ('url', 'local'),
        [
            ('img.png', True),
            ('../img.png', True),
            ('https://x.org/a.png', False),
            ('mailto:a@b.c', False),
            ('//cdn/x.png', False),
            ('#anchor', False),
            ('/root.png', False),
            ('', False),
        ],
    )
    def test_is_local(self, url, local):
        assert AssetResolver.is_local(url) is local

    @pytest.mark.parametrize(
        ('url', 'expected'),
        [
            ('a.md', 'a.html'),
            ('A.MD#x', 'A.html#x'),
            ('dir/b.markdown?q=1', 'dir/b.html?q=1'),
            ('https://x.org/a.md', 'https://x.org/a.md'),
            ('notes.mdx', 'notes.mdx'),
        ],
    )
    def test_md_to_html(self, url, expected):
        assert LinkProcessor.md_to_html(url) == expected


class TestInputErrors:
    def test_non_utf8_input(self, tmp_path):
        src = tmp_path / 'latin.md'
        src.write_bytes('caf\xe9'.encode('latin-1'))
        with pytest.raises(InputError, match='not valid UTF-8'):
            build(src)

    def test_malformed_front_matter(self, write):
        src = write('bad.md', '---\ntitle: [unclosed\n---\n# x')
        with pytest.raises(InputError, match='malformed YAML front matter'):
            build(src)

    def test_front_matter_not_mapping(self, write):
        src = write('list.md', '---\n- a\n- b\n---\n# x')
        with pytest.raises(InputError, match='must be a mapping'):
            build(src)

    def test_unwritable_output(self, write, tmp_path):
        src = write('doc.md', 'x')
        blocker = write('blocker', 'file')
        with pytest.raises(BuildError, match='Could not write'):
            build(src, blocker / 'sub' / 'out.html')


class TestReviewRegressions:
    def test_html_named_input_is_not_overwritten(self, write):
        src = write('note.html', '# Hi\n\ntext\n')
        with pytest.raises(InputError, match='would overwrite the input file'):
            build(src)
        assert src.read_text() == '# Hi\n\ntext\n'

    def test_explicit_output_equal_to_input_rejected(self, write):
        src = write('doc.htm', '# Hi')
        with pytest.raises(InputError, match='would overwrite'):
            build(src, src)
        assert src.read_text() == '# Hi'

    def test_nav_links_are_url_encoded(self, write, tmp_path):
        write('docs/index.md', '# Home')
        write('docs/C# notes 100%.md', '# Notes')
        build(tmp_path / 'docs', tmp_path / 'site')
        html = body(tmp_path / 'site' / 'index.html')
        assert 'href="C%23%20notes%20100%25.html"' in html
        assert 'href="C# notes' not in html


def _case_insensitive_fs(directory: Path) -> bool:
    probe = directory / 'CaseProbe.tmp'
    probe.write_text('x')
    try:
        return (directory / 'caseprobe.tmp').exists()
    finally:
        probe.unlink()


class TestCaseInsensitivePaths:
    def test_outputs_differing_only_in_case_clash(self, write, tmp_path):
        write('d/guide.md', '# Lower')
        write('d/GUIDE.markdown', '# Upper')
        with pytest.raises(InputError, match='differ only in case'):
            build(tmp_path / 'd', tmp_path / 'out')
        assert not (tmp_path / 'out').exists()

    def test_input_with_other_case_is_not_overwritten(self, write, tmp_path):
        if not _case_insensitive_fs(tmp_path):
            pytest.skip('file system is case-sensitive')
        src = write('Report.HTML', '# Original')
        with pytest.raises(InputError, match='would overwrite the input file'):
            build(src)
        with pytest.raises(InputError, match='would overwrite the input file'):
            build(src, tmp_path / 'REPORT.html')
        assert src.read_text() == '# Original'
