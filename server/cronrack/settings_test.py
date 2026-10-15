"""Settings for the test suite (pytest). Development defaults, no .env needed."""

import os

os.environ.setdefault("DEBUG", "1")

from cronrack.settings import *

# Plain static storage: tests never need collectstatic or its manifest. The manifest
# storage itself is tested in tests/test_static_manifest.py.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
