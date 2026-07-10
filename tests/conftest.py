"""Root test conftest.

Ensures the ``tests/`` directory is on ``sys.path`` so both the smoke and
upgrade suites can ``import owui`` (the shared helper module) regardless of the
directory pytest is invoked from.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
