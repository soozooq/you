"""Unix peer-authorized publisher and durable JSONL queue."""
import json
import os
import socket
import stat
import struct
import time

from you_config import absolute_path

LINE_LIMIT = 65536


def _exchange(record):
    line = json.dumps(record, ensure_ascii=False, allow_nan=False).encode() + b"\n"
    if len(line) > LINE_LIMIT:
        raise ValueError("Queue record exceeds byte limit")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(3)
        client.connect(str(absolute_path("YOU_QUEUE_SOCKET")))
        client.sendall(line)
        client.shutdown(socket.SHUT_WR)
        with client.makefile("rb") as response:
            return response.read(3)


def publish(record):
    if _exchange(record) != b"OK":
        raise RuntimeError("Queue did not acknowledge durable handoff")


def handed_off(key, tx):
    if not os.environ.get("YOU_QUEUE_SOCKET"):
        return False
    result = _exchange({"key": key, "lookup_tx": tx})
    if result not in (b"OK", b"NO"):
        raise RuntimeError("Queue could not reconcile transaction")
    return result == b"OK"


def _index_transactions(index, row):
    transactions = row.get("tx")
    if isinstance(transactions, str):
        transactions = [transactions]
    index.update((row["key"], tx) for tx in (transactions or []))


def main():
    from sender import queue_record

    os.umask(0o077)
    configured = json.loads(os.environ["YOU_QUEUE_PUBLISHERS"])
    if not isinstance(configured, dict) or not configured or any(
        not uid.isdecimal() or not isinstance(keys, list) or not keys
        or any(not isinstance(key, str) for key in keys)
        for uid, keys in configured.items()
    ):
        raise RuntimeError("Invalid publisher authorization")
    authorized = {int(uid): frozenset(keys) for uid, keys in configured.items()}
    path = absolute_path("YOU_QUEUE_FILE")
    socket_path = absolute_path("YOU_QUEUE_SOCKET")
    flags = os.O_APPEND | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(path, flags), "a+b", buffering=0) as ledger:
        import fcntl

        fcntl.flock(ledger, fcntl.LOCK_EX | fcntl.LOCK_NB)
        info = os.fstat(ledger.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o027:
            raise RuntimeError("Unsafe queue ledger")
        records = {}
        transactions = set()
        ledger.seek(0)
        while line := ledger.readline(LINE_LIMIT + 1):
            row = queue_record(line)
            if len(line) > LINE_LIMIT or not line.endswith(b"\n") or row is None:
                raise RuntimeError("Queue ledger needs reconciliation")
            if row["record_id"] in records:
                raise RuntimeError("Duplicate queue record requires reconciliation")
            records[row["record_id"]] = row
            _index_transactions(transactions, row)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(socket_path))
            try:
                os.chmod(socket_path, 0o660)
                server.listen(16)
                while True:
                    conn, _ = server.accept()
                    with conn:
                        conn.settimeout(2)
                        _, uid, _ = struct.unpack("3i", conn.getsockopt(
                            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
                        if uid not in authorized:
                            continue
                        try:
                            deadline = time.monotonic() + 2
                            line = bytearray()
                            while len(line) <= LINE_LIMIT and not line.endswith(b"\n"):
                                remaining = deadline - time.monotonic()
                                if remaining <= 0:
                                    raise TimeoutError
                                conn.settimeout(remaining)
                                chunk = conn.recv(min(8192, LINE_LIMIT + 1 - len(line)))
                                if not chunk:
                                    break
                                line.extend(chunk)
                            if len(line) > LINE_LIMIT or not line.endswith(b"\n"):
                                continue
                            request = json.loads(line.decode("utf-8"))
                            if isinstance(request, dict) and "lookup_tx" in request:
                                key, tx = request.get("key"), request["lookup_tx"]
                                if (isinstance(key, str) and key in authorized[uid]
                                        and isinstance(tx, str) and len(tx) <= 128):
                                    conn.sendall(b"OK" if (key, tx) in transactions else b"NO")
                                continue
                            row = queue_record(line)
                        except (OSError, ValueError, RecursionError):
                            continue
                        if row is None or row["key"] not in authorized[uid]:
                            continue
                        rid = row["record_id"]
                        if rid in records:
                            if records[rid] != row:
                                continue
                        else:
                            start = ledger.tell()
                            try:
                                if ledger.write(line) != len(line):
                                    raise OSError("Short queue write")
                                os.fsync(ledger.fileno())
                            except OSError:
                                ledger.truncate(start)
                                os.fsync(ledger.fileno())
                                raise
                            records[rid] = row
                            _index_transactions(transactions, row)
                        try:
                            conn.sendall(b"OK")
                        except OSError:
                            pass
            finally:
                socket_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
