"""Restricted ``{variable}`` templates for headers, footers and page numbers.

Templates are trusted HTML written by the user (e.g. ``'<b>{title}</b> - {date}'``).
Variable *values* are HTML-escaped before substitution. Only simple names are allowed
(no ``{a.b}``/``{a[0]}`` access), and an unknown name raises
:class:`~md_html_builder.errors.TemplateError` listing the available variables.
"""

from __future__ import annotations

import html
import logging
import string
from collections.abc import Mapping
from typing import Any

from .errors import MdHtmlError, TemplateError

__all__ = ['VariableTemplate']

logger = logging.getLogger(__name__)


class _EscapingFormatter(string.Formatter):
    """``string.Formatter`` that forbids attribute/index access and escapes values."""

    def get_field(self, field_name: str, args: Any, kwargs: Any) -> Any:
        """Look up a plain variable name only."""
        try:
            if not field_name.isidentifier():
                raise TemplateError(f'invalid template field {{{field_name}}}')
            if field_name not in kwargs:
                available = ', '.join(sorted(kwargs))
                raise TemplateError(
                    f'unknown template variable {{{field_name}}}; available: {available}'
                )
            return kwargs[field_name], field_name
        except MdHtmlError:
            raise
        except Exception as exc:
            raise TemplateError(f'Unexpected error in _EscapingFormatter.get_field: {exc}') from exc

    def format_field(self, value: Any, format_spec: str) -> str:
        """Escape the value for HTML; format specs are not supported."""
        try:
            if format_spec:
                raise TemplateError('format specs are not allowed in templates')
            return html.escape('' if value is None else str(value), quote=True)
        except MdHtmlError:
            raise
        except Exception as exc:
            raise TemplateError(
                f'Unexpected error in _EscapingFormatter.format_field: {exc}'
            ) from exc

    def convert_field(self, value: Any, conversion: str | None) -> Any:
        """Reject ``!r``/``!s`` conversions."""
        try:
            if conversion:
                raise TemplateError('conversions such as !r are not allowed in templates')
            return value
        except MdHtmlError:
            raise
        except Exception as exc:
            raise TemplateError(
                f'Unexpected error in _EscapingFormatter.convert_field: {exc}'
            ) from exc


class VariableTemplate:
    """A header/footer template.

    :param template: template text using ``{name}`` placeholders (``{{``/``}}`` for braces).
    :param name: label used in error messages (e.g. ``'footer.template'``).
    """

    _formatter = _EscapingFormatter()

    def __init__(self, template: str, name: str = 'template') -> None:
        try:
            if not isinstance(template, str):
                raise TemplateError(f'{name}: template must be a string')
            self.template = template
            self.name = name
        except MdHtmlError:
            raise
        except Exception as exc:
            raise TemplateError(f'Unexpected error in VariableTemplate.__init__: {exc}') from exc

    def render(self, variables: Mapping[str, Any]) -> str:
        """Substitute ``variables`` (escaped) into the template.

        :raises TemplateError: on unknown variables or malformed templates.
        """
        try:
            return self._formatter.vformat(self.template, (), dict(variables))
        except TemplateError as exc:
            raise TemplateError(f'{self.name}: {exc}') from exc
        except (ValueError, IndexError, KeyError) as exc:
            raise TemplateError(
                f'{self.name}: malformed template {self.template!r} ({exc})'
            ) from exc
