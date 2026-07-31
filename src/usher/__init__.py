"""Usher — a self-hosted media catalog backend."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("usher")
except PackageNotFoundError:  # pragma: no cover - only when run from an uninstalled tree
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
