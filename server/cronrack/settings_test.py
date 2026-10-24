"""Settings for the test suite (pytest). Development defaults, no .env needed."""

import os

os.environ.setdefault("DEBUG", "1")

import tempfile
from pathlib import Path

from cronrack.settings import *

# A real file instead of SQLite's default in-memory test database, so tests run with
# the same WAL journal and locking as production. In-memory databases shared between
# threads fail with "table is locked" instead of waiting for the lock, which would
# make concurrency tests (two setups at once) meaningless.
# A new dict, not an edit of the imported one: pytest also imports this file as a test
# module (it matches *_test.py), and editing Django's live settings dict a second time
# would break the database setup.
DATABASES = {
    "default": {
        **DATABASES["default"],
        "TEST": {
            "NAME": str(Path(tempfile.gettempdir()) / f"cronrack-test-{os.getpid()}.sqlite3"),
        },
    }
}

# Plain static storage: tests never need collectstatic or its manifest. The manifest
# storage itself is tested in tests/test_static_manifest.py.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
