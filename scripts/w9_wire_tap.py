"""A transparent stdio proxy that records every JSON-RPC line crossing it.

Week 9's "look at the raw messages once": an MCP host launches its server as a
subprocess and talks to it over stdin/stdout, one JSON message per line. This
script *is* the subprocess the host launches; it starts the real server, copies
bytes in both directions unchanged, and appends each line to the file named by
``WIRE_TAP_LOG`` as ``{"t": <epoch>, "dir": "->server"|"<-server", "line": ...}``.

    WIRE_TAP_LOG=wire.jsonl python scripts/w9_wire_tap.py python -m app.mcp_server --ingest x.pdf

It changes nothing about the traffic; it exists so the wire can be read.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from typing import BinaryIO

_lock = threading.Lock()


def _log(path: str, direction: str, line: bytes) -> None:
    record = {"t": time.time(), "dir": direction, "line": line.decode("utf-8", "replace").rstrip("\r\n")}
    with _lock, open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def _pump(source: BinaryIO, sink: BinaryIO, path: str, direction: str) -> None:
    for line in iter(source.readline, b""):
        _log(path, direction, line)
        sink.write(line)
        sink.flush()
    try:
        sink.close()
    except OSError:
        pass


def main() -> int:
    log_path = os.environ.get("WIRE_TAP_LOG")
    if not log_path or len(sys.argv) < 2:
        print("usage: WIRE_TAP_LOG=<file> w9_wire_tap.py <server command...>", file=sys.stderr)
        return 2
    process = subprocess.Popen(sys.argv[1:], stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
    assert process.stdin is not None and process.stdout is not None
    to_server = threading.Thread(target=_pump, args=(sys.stdin.buffer, process.stdin, log_path, "->server"), daemon=True)
    from_server = threading.Thread(target=_pump, args=(process.stdout, sys.stdout.buffer, log_path, "<-server"), daemon=True)
    to_server.start()
    from_server.start()
    code = process.wait()
    from_server.join(timeout=5)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
