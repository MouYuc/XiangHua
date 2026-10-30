"""Shared test configuration.

The synthesis core writes signed provenance sidecars next to every output by
default. That is the production behaviour, but unit tests must not create a
server key inside the real ``.guangpu_webui`` config dir, so the sidecar is
disabled for the whole test run here. ``test_provenance.py`` exercises the
provenance module itself against temporary directories and is unaffected.
"""

from __future__ import annotations

import os

os.environ.setdefault("XIANGHUA_PROVENANCE", "0")
