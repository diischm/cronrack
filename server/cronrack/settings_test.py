"""Settings for the test suite (pytest). Development defaults, no .env needed."""

import os

os.environ.setdefault("DEBUG", "1")

from cronrack.settings import *
