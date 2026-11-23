"""Shared pytest fixtures for md_html_builder tests."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from md_html_builder import Config, MarkdownConverter

# 1x1 transparent PNG
PNG_BYTES = bytes.fromhex(
    '89504e470d0a1a0a0000000d4948445200000001000000010806000000'
    '1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082'
)


@pytest.fixture
def make_config() -> Callable[..., Config]:
    """Build a validated Config from a nested mapping."""

    def factory(data: dict[str, Any] | None = None) -> Config:
        return Config.from_dict(data or {})

    return factory


@pytest.fixture
def fragment(make_config: Callable[..., Config]) -> Callable[..., str]:
    """Convert Markdown to an HTML body fragment with an optional config mapping."""

    def convert(text: str, config: dict[str, Any] | None = None) -> str:
        return MarkdownConverter(make_config(config)).convert_fragment(text)

    return convert


@pytest.fixture
def page(make_config: Callable[..., Config]) -> Callable[..., str]:
    """Convert Markdown to a full HTML page with an optional config mapping."""

    def convert(text: str, config: dict[str, Any] | None = None) -> str:
        return MarkdownConverter(make_config(config)).convert_text(text)

    return convert


@pytest.fixture
def write(tmp_path: Path) -> Callable[..., Path]:
    """Write a text file (creating parent directories) under tmp_path."""

    def writer(relative: str, content: str = '', encoding: str = 'utf-8') -> Path:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding=encoding)
        return path

    return writer


@pytest.fixture
def png(tmp_path: Path) -> Callable[[str], Path]:
    """Write a tiny PNG at a path relative to tmp_path."""

    def writer(relative: str) -> Path:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(PNG_BYTES)
        return path

    return writer


@pytest.fixture(autouse=True)
def reset_package_logger():
    """Undo CLI logging configuration so it cannot leak between tests."""
    import logging

    package_logger = logging.getLogger('md_html_builder')
    saved = (package_logger.handlers[:], package_logger.level, package_logger.propagate)
    yield
    package_logger.handlers[:], package_logger.level, package_logger.propagate = (
        saved[0],
        saved[1],
        saved[2],
    )
