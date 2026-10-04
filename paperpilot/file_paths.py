"""Windows extended-length I/O paths, without changing logical ownership paths.

Containment and permission checks use logical_path(). io_path() only adapts the
already-authorized path for filesystem calls; it never broadens access.
"""
import os
from pathlib import Path


def logical_path(path):
    value = os.fspath(path)
    if os.name == "nt":
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
    return Path(value)


def io_path(path):
    value = Path(path)
    if os.name != "nt" or str(value).startswith("\\\\?\\"):
        return value
    absolute = str(value.absolute())
    return Path("\\\\?\\UNC\\" + absolute[2:] if absolute.startswith("\\\\") else "\\\\?\\" + absolute)
