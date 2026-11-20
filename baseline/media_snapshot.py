"""Descriptor-anchored snapshots for approved OneBot export directories."""
import contextlib
import os
import stat
import tempfile
import threading
import time
from pathlib import Path

_slots = threading.BoundedSemaphore(4)


@contextlib.contextmanager
def snapshot(root_fd, relative_path, private_dir, *, audio=False):
    path = Path(relative_path)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError("Invalid media export path")
    if not _slots.acquire(blocking=False):
        raise RuntimeError("Media capacity exhausted")
    try:
        with contextlib.ExitStack() as descriptors:
            parent = root_fd
            for part in path.parts[:-1]:
                parent = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                 dir_fd=parent)
                descriptors.callback(os.close, parent)
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=parent)
            descriptors.callback(os.close, fd)
            info = os.fstat(fd)
            limit = (20 if audio else 12) * 1024 * 1024
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ValueError("Invalid media export file")
            deadline = time.monotonic() + 5
            with tempfile.NamedTemporaryFile(dir=private_dir) as target:
                remaining = limit
                while True:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Media snapshot deadline")
                    chunk = os.read(fd, min(65536, remaining + 1))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    if remaining < 0:
                        raise ValueError("Media export exceeded limit")
                    target.write(chunk)
                target.flush()
                yield Path(target.name)
    finally:
        _slots.release()
