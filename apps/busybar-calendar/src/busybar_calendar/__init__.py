"""CalDAV and ICS calendar reminders for the BUSY Bar."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("busybar-calendar")
except PackageNotFoundError:  # run from a checkout without installing, as app.py does
    __version__ = "unknown"
