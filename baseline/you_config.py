"""Shared, fail-closed runtime configuration."""
import json
import os
import re
import stat
from pathlib import Path


def absolute_path(name, default=None):
    value = os.environ.get(name, default)
    if value is None or not Path(value).is_absolute():
        raise RuntimeError(f"{name} must be an absolute path")
    return Path(value)


def credential(name):
    value = os.environ.get(name)
    path = os.environ.get(name + "_FILE")
    if path is not None:
        if value is not None or not Path(path).is_absolute():
            raise RuntimeError(f"Invalid credential source for {name}")
        parts = Path(path).parts
        if len(parts) < 3 or ".." in parts:
            raise RuntimeError(f"Invalid credential source for {name}")
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
        fd = os.open("/", flags)
        try:
            for index, part in enumerate(parts[1:-1], 1):
                child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
                info = os.fstat(fd)
                if info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022:
                    raise RuntimeError(f"Unsafe credential directory for {name}")
                if index == len(parts) - 2 and (
                    info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700
                ):
                    raise RuntimeError(f"Unsafe private directory for {name}")
            file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                              dir_fd=fd)
            with os.fdopen(file_fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                        or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 4096):
                    raise RuntimeError(f"Unsafe credential file for {name}")
                value = stream.read(4097).decode("utf-8").rstrip("\r\n")
        except (OSError, UnicodeError):
            raise RuntimeError(f"Cannot read credential for {name}") from None
        finally:
            os.close(fd)
    if not value or not value.strip() or len(value) > 4096 or not value.isascii() or any(
        ord(char) < 33 or ord(char) > 126 for char in value
    ):
        raise RuntimeError(f"Missing or invalid credential for {name}")
    return value


def sessions():
    keys = json.loads(os.environ.get("YOU_SESSIONS", "[]"))
    if not isinstance(keys, list) or any(
        not isinstance(key, str) or not re.fullmatch(r"(?:dm|grp)-[1-9][0-9]{0,19}", key)
        for key in keys
    ):
        raise RuntimeError("Invalid YOU_SESSIONS")
    routes = {
        key: ("private" if key.startswith("dm-") else "group", int(key.split("-")[1]))
        for key in keys
    }
    return routes


def binding(name, prefix, routes):
    key = os.environ.get(name)
    if key is not None and (key not in routes or not key.startswith(prefix)):
        raise RuntimeError(f"Invalid session binding for {name}")
    return key


def open_trusted_directory(path, owners):
    """Open an absolute directory without following links or writable ancestors."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise RuntimeError("Unsafe state directory path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/", flags)
    try:
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            if info.st_uid not in owners or info.st_mode & 0o022:
                raise RuntimeError("Unsafe state directory")
        return fd
    except BaseException:
        os.close(fd)
        raise
