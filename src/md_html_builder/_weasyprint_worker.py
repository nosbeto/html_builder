"""Child-process worker for :class:`~md_html_builder.pdf.WeasyPrintBackend`.

Run as ``python -m md_html_builder._weasyprint_worker <input.html> <output.pdf>`` with the
environment prepared by the backend (``DYLD_FALLBACK_LIBRARY_PATH`` on macOS). Prints
``{"pages": N}`` on success; errors go to stderr with a non-zero exit status.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

logger = logging.getLogger('md_html_builder.pdf.worker')


class WeasyPrintWorker:
    """Renders one HTML file to PDF in this process."""

    def run(self, argv: list[str]) -> int:
        """Render ``argv[0]`` to ``argv[1]``; return the exit status."""
        try:
            if len(argv) != 2:
                raise ValueError('usage: _weasyprint_worker <input.html> <output.pdf>')
            import weasyprint

            from md_html_builder.pdf import WeasyPrintBackend

            pages = WeasyPrintBackend.render(weasyprint, Path(argv[0]), Path(argv[1]))
            print(json.dumps({'pages': pages}))
            return 0
        except Exception as exc:
            logger.debug('WeasyPrint worker failed', exc_info=True)
            print(f'error: {exc}', file=sys.stderr)
            return 1


if __name__ == '__main__':
    sys.exit(WeasyPrintWorker().run(sys.argv[1:]))
