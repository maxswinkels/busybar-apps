"""Claude Code's usage limits on a BUSY Bar: bars, and a countdown to the reset."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("busybar-cclimits")
except PackageNotFoundError:  # run from a checkout without installing, as app.py does
    __version__ = "unknown"
