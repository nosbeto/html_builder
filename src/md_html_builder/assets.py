"""Resolution of local images/files and Markdown cross-links.

When the output HTML is written somewhere other than next to its source, relative paths
such as ``![logo](img/logo.png)`` would break. :class:`AssetResolver` fixes them according
to ``build.assets``:

* ``copy``    - copy the file to the same relative location under the output directory
  (falls back to ``rewrite`` if that location would escape the output root);
* ``rewrite`` - point the URL at the original file, relative to the output page;
* ``embed``   - inline images as ``data:`` URIs (other linked files are copied);
* ``keep``    - leave URLs untouched.

:class:`LinkProcessor` walks a parsed token stream and applies the resolver to images, links
and ``<img src>`` in raw HTML, and rewrites links to ``*.md`` files to ``*.html``.
"""

from __future__ import annotations

import base64
import html
import logging
import mimetypes
import os
import re
import shutil
from pathlib import Path
from typing import ClassVar
from urllib.parse import quote, unquote, urlsplit

from markdown_it.token import Token

from .errors import BuildError, InputError, MdHtmlError
from .parser import ParsedDocument

__all__ = ['MARKDOWN_SUFFIXES', 'AssetResolver', 'LinkProcessor']

logger = logging.getLogger(__name__)

MARKDOWN_SUFFIXES: tuple[str, ...] = ('.md', '.markdown', '.mdown', '.mkd', '.mkdn')
_SCHEME = re.compile(r'^[A-Za-z][A-Za-z0-9+.-]*:')
_RAW_IMG = re.compile(r'(<img\b[^>]*?\bsrc\s*=\s*)(["\'])(.*?)\2', re.IGNORECASE | re.DOTALL)


class AssetResolver:
    """Resolves local file references for one build.

    :param mode: ``copy``, ``rewrite``, ``embed`` or ``keep``.
    :param output_root: root output directory (copies never escape it).
    :param strict: raise :class:`InputError` for missing files instead of warning.
    """

    MODES: ClassVar[tuple[str, ...]] = ('copy', 'rewrite', 'embed', 'keep')

    def __init__(self, mode: str, output_root: Path, strict: bool = False) -> None:
        try:
            if mode not in self.MODES:
                raise BuildError(f'Unknown asset mode {mode!r}; expected one of {self.MODES}')
            self.mode = mode
            self.output_root = output_root.resolve()
            self.strict = strict
            self.copied: set[Path] = set()
        except MdHtmlError:
            raise
        except Exception as exc:
            raise BuildError(f'Unexpected error in AssetResolver.__init__: {exc}') from exc

    @staticmethod
    def is_local(url: str) -> bool:
        """True for relative filesystem references (not URLs, anchors or absolute paths)."""
        try:
            return bool(url) and not (
                _SCHEME.match(url) or url.startswith(('//', '#', '/', '\\', '?'))
            )
        except MdHtmlError:
            raise
        except Exception as exc:
            raise BuildError(f'Unexpected error in AssetResolver.is_local: {exc}') from exc

    @staticmethod
    def split(url: str) -> tuple[str, str]:
        """Split ``url`` into (decoded path, ``?query#fragment`` suffix)."""
        try:
            parts = urlsplit(url)
            suffix = (f'?{parts.query}' if parts.query else '') + (
                f'#{parts.fragment}' if parts.fragment else ''
            )
            return unquote(parts.path), suffix
        except MdHtmlError:
            raise
        except Exception as exc:
            raise BuildError(f'Unexpected error in AssetResolver.split: {exc}') from exc

    def _missing(self, url: str, source: Path) -> str:
        """Handle a reference to a file that does not exist."""
        try:
            message = f'{source}: referenced file not found: {url}'
            if self.strict:
                raise InputError(message)
            logger.warning(message)
            return url
        except MdHtmlError:
            raise
        except Exception as exc:
            raise BuildError(f'Unexpected error in AssetResolver._missing: {exc}') from exc

    def resolve(self, url: str, source: Path, output: Path, image: bool = True) -> str:
        """Return the URL to use in ``output`` for a reference found in ``source``.

        :param url: URL as written (already percent-encoded by the parser).
        :param source: Markdown file containing the reference.
        :param output: HTML file being written.
        :param image: whether the reference is an image (only images are embedded).
        """
        try:
            if self.mode == 'keep' or not self.is_local(url):
                return url
            path_part, suffix = self.split(url)
            if not path_part:
                return url
            target = (source.parent / path_part).resolve()
            if not target.is_file():
                if target.is_dir():
                    return url
                return self._missing(url, source)
            mode = self.mode
            if mode == 'embed':
                if image:
                    return self._embed(target)
                mode = 'copy'
            if mode == 'copy':
                return self._copy(url, path_part, suffix, target, source, output)
            return self._relative(target, output, suffix)
        except (InputError, BuildError):
            raise
        except Exception as exc:
            raise BuildError(f'{source}: could not resolve {url!r}: {exc}') from exc

    @staticmethod
    def _relative(target: Path, output: Path, suffix: str) -> str:
        """URL of ``target`` relative to the directory of ``output``."""
        try:
            rel = os.path.relpath(target, output.resolve().parent)
            return quote(Path(rel).as_posix(), safe='/') + suffix
        except MdHtmlError:
            raise
        except Exception as exc:
            raise BuildError(f'Unexpected error in AssetResolver._relative: {exc}') from exc

    def _copy(
        self, url: str, path_part: str, suffix: str, target: Path, source: Path, output: Path
    ) -> str:
        """Copy ``target`` beside ``output`` keeping its relative path."""
        try:
            out_dir = output.resolve().parent
            if out_dir == source.resolve().parent:
                return url
            destination = (out_dir / path_part).resolve()
            if not destination.is_relative_to(self.output_root):
                logger.info(
                    f'{url} would be copied outside {self.output_root}; linking to original'
                )
                return self._relative(target, output, suffix)
            if destination not in self.copied:
                destination.parent.mkdir(parents=True, exist_ok=True)
                stale = (
                    not destination.exists()
                    or destination.stat().st_size != target.stat().st_size
                    or destination.stat().st_mtime < target.stat().st_mtime
                )
                if stale:
                    shutil.copy2(target, destination)
                    logger.debug(f'Copied {target} -> {destination}')
                self.copied.add(destination)
            return url
        except OSError as exc:
            raise BuildError(f'Could not copy {target}: {exc}') from exc

    @staticmethod
    def _embed(target: Path) -> str:
        """Return a base64 ``data:`` URI for ``target``."""
        try:
            mime = mimetypes.guess_type(target.name)[0] or 'application/octet-stream'
            data = base64.b64encode(target.read_bytes()).decode('ascii')
            return f'data:{mime};base64,{data}'
        except OSError as exc:
            raise BuildError(f'Could not embed {target}: {exc}') from exc


class LinkProcessor:
    """Rewrites image/link URLs in a parsed document before rendering.

    :param resolver: asset resolver (``None`` disables asset handling).
    :param rewrite_md_links: change relative ``*.md`` link targets to ``*.html``.
    :param lazy_images: add ``loading="lazy"`` to images.
    :param link_suffix: extension that ``*.md`` links are rewritten to (``.html``, or
        ``.pdf`` when only PDF files are generated).
    """

    def __init__(
        self,
        resolver: AssetResolver | None,
        rewrite_md_links: bool = True,
        lazy_images: bool = True,
        link_suffix: str = '.html',
    ) -> None:
        try:
            if not link_suffix.startswith('.'):
                raise BuildError(f'link_suffix must start with a dot, got {link_suffix!r}')
            self.resolver = resolver
            self.rewrite_md_links = rewrite_md_links
            self.lazy_images = lazy_images
            self.link_suffix = link_suffix
        except MdHtmlError:
            raise
        except Exception as exc:
            raise BuildError(f'Unexpected error in LinkProcessor.__init__: {exc}') from exc

    @staticmethod
    def md_to_html(url: str, suffix: str = '.html') -> str:
        """Rewrite ``guide.md#intro`` to ``guide.html#intro`` (or ``suffix``) for relative links."""
        try:
            if not AssetResolver.is_local(url):
                return url
            parts = urlsplit(url)
            lower = parts.path.lower()
            for ext in MARKDOWN_SUFFIXES:
                if lower.endswith(ext):
                    new_path = parts.path[: -len(ext)] + suffix
                    rest = (f'?{parts.query}' if parts.query else '') + (
                        f'#{parts.fragment}' if parts.fragment else ''
                    )
                    return new_path + rest
            return url
        except Exception as exc:
            raise BuildError(f'Could not rewrite link {url!r}: {exc}') from exc

    def resolve_image(self, url: str, source: Path | None, output: Path | None) -> str:
        """Resolve one image URL written relative to ``source`` (unchanged without assets)."""
        try:
            if self.resolver is None or source is None or output is None or not url:
                return url
            return self.resolver.resolve(url, source, output, image=True)
        except MdHtmlError:
            raise
        except Exception as exc:
            raise BuildError(f'{source}: could not resolve image {url!r}: {exc}') from exc

    def process(self, document: ParsedDocument, source: Path | None, output: Path | None) -> None:
        """Rewrite URLs in ``document`` in place."""
        try:
            for token in document.tokens:
                if token.type == 'html_block':
                    token.content = self._rewrite_raw_html(token.content, source, output)
                for child in token.children or []:
                    self._process_inline(child, source, output)
        except (InputError, BuildError):
            raise
        except Exception as exc:
            raise BuildError(f'{document.source}: could not process links: {exc}') from exc

    def _process_inline(self, token: Token, source: Path | None, output: Path | None) -> None:
        """Handle one inline child token."""
        try:
            can_resolve = self.resolver is not None and source is not None and output is not None
            if token.type == 'image':
                src = str(token.attrGet('src') or '')
                if can_resolve and src:
                    token.attrSet('src', self.resolver.resolve(src, source, output, image=True))  # type: ignore[union-attr,arg-type]
                if self.lazy_images:
                    token.attrSet('loading', 'lazy')
            elif token.type == 'link_open':
                href = str(token.attrGet('href') or '')
                if not href:
                    return
                rewritten = (
                    self.md_to_html(href, self.link_suffix) if self.rewrite_md_links else href
                )
                if rewritten != href:
                    token.attrSet('href', rewritten)
                elif can_resolve:
                    token.attrSet('href', self.resolver.resolve(href, source, output, image=False))  # type: ignore[union-attr,arg-type]
            elif token.type == 'html_inline':
                token.content = self._rewrite_raw_html(token.content, source, output)
        except MdHtmlError:
            raise
        except Exception as exc:
            raise BuildError(f'Unexpected error in LinkProcessor._process_inline: {exc}') from exc

    def _rewrite_raw_html(self, content: str, source: Path | None, output: Path | None) -> str:
        """Resolve ``<img src="...">`` references inside raw HTML."""
        try:
            if (
                self.resolver is None
                or source is None
                or output is None
                or '<img' not in content.lower()
            ):
                return content
            resolver = self.resolver

            def replace(match: re.Match[str]) -> str:
                try:
                    src = html.unescape(match.group(3))
                    quoted = quote(src, safe="/:?#=&%~+@!$,;'()*")
                    new = resolver.resolve(quoted, source, output)
                    if new == quoted:
                        return match.group(0)
                    prefix, quote_char = match.group(1), match.group(2)
                    escaped = html.escape(new, quote=True)
                    return f'{prefix}{quote_char}{escaped}{quote_char}'
                except MdHtmlError:
                    raise
                except Exception as exc:
                    raise BuildError(
                        f'Unexpected error in LinkProcessor._rewrite_raw_html.replace: {exc}'
                    ) from exc

            return _RAW_IMG.sub(replace, content)
        except MdHtmlError:
            raise
        except Exception as exc:
            raise BuildError(f'Unexpected error in LinkProcessor._rewrite_raw_html: {exc}') from exc
