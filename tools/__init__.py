"""Project helpers exposed alongside GPT-SoVITS' own tools package.

The GPT-SoVITS environment adds its ``tools`` directory to ``sys.path``.
Extending this package path keeps both tool families importable while making
the project's helper modules resolve first.
"""

from pkgutil import extend_path

__path__ = extend_path(__path__, __name__)
