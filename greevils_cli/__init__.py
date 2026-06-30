"""greevils-cli -- participant CLI for the Greevils competition."""
from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("greevils-cli")
except PackageNotFoundError:  # running from a source tree that isn't installed
    __version__ = "1.1.0"
