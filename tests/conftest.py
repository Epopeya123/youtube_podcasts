"""pytest bootstrap.

``harness`` MUST be imported before anything pulls in ``kivy`` -- it sets the
environment Kivy reads at import time.  conftest.py is the first thing pytest
imports, so this is the right place for it.
"""

import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import harness  # noqa: E402

harness.setup_kivy_env()
harness.silence_kivy_logging()
