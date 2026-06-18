"""Shared pytest setup.

Redirect the structured logger to a temp dir *before* any engine module is
imported, so test runs never write into the project's ``logs/`` directory.
"""

import os
import pathlib
import tempfile

os.environ.setdefault(
    "SHARPE_LOG_DIR", str(pathlib.Path(tempfile.gettempdir()) / "sharpe_test_logs")
)
