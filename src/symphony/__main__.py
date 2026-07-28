"""``python -m symphony`` entry point (SPEC 17.7).

Equivalent to the ``symphony`` console script declared in ``pyproject.toml``.
Both delegate to :func:`symphony.cli.main`, which returns the exit code rather
than raising :class:`SystemExit`, so the process-exit decision lives here and
nowhere else.
"""

from __future__ import annotations

import sys

from symphony.cli import main

if __name__ == "__main__":
    sys.exit(main())
