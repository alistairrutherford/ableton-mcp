"""Non-blocking localhost JSON server, polled from Live's update tick.

No Live imports here, so the protocol can be tested outside Live.

Protocol: one JSON object per line (UTF-8, "\\n" terminated) in each direction.
    request:  {"id": 1, "cmd": "get_state", "args": {}}
    response: {"id": 1, "ok": true, "result": {...}}
              {"id": 1, "ok": false, "error": "...", "traceback": "..."}
"""

import errno
import json
import socket
import traceback

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9873   # Chordism's bridge uses 9871 and NoteCap's 9872; all three can run in one Live

MAX_LINE = 1 << 20             # drop a client that sends a line longer than this
MAX_COMMANDS_PER_TICK = 64     # keep one tick from stalling Live's UI


class _Connection:
    def __init__(self, sock, addr):
        self.sock = sock
        self.addr = addr
        self.inbuf = b""
        self.outbuf = b""
        self.closed = False


class BridgeServer:
    """Accepts clients and runs their commands, all from poll().

    `dispatch(cmd, args)` returns a JSON-serialisable result or raises. It is
    called from inside poll(), so in Live it runs on the main thread.
    """

    def __init__(self, dispatch, host=DEFAULT_HOST, port=DEFAULT_PORT, log=None):
        self._dispatch = dispatch
        self._log = log or (lambda msg: None)
        self._clients = []
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((host, port))
        self._listener.listen(4)
        self._listener.setblocking(False)
        self.address = self._listener.getsockname()

    def close(self):
        for c in self._clients:
            self._close(c)
        self._clients = []
        try:
            self._listener.close()
        except OSError:
            pass

    def poll(self):
        """Accept, read, run up to MAX_COMMANDS_PER_TICK commands, write. Never blocks."""
        self._accept()
        budget = MAX_COMMANDS_PER_TICK
        for c in self._clients:
            self._read(c)
            while not c.closed and budget > 0 and b"\n" in c.inbuf:
                line, c.inbuf = c.inbuf.split(b"\n", 1)
                line = line.strip()
                if line:
                    budget -= 1
                    c.outbuf += self._handle(line)
            self._write(c)
        self._clients = [c for c in self._clients if not c.closed]

    # -- internals ---------------------------------------------------------

    def _accept(self):
        while True:
            try:
                sock, addr = self._listener.accept()
            except (BlockingIOError, InterruptedError):
                return
            except OSError as e:
                self._log("accept failed: %s" % e)
                return
            sock.setblocking(False)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._clients.append(_Connection(sock, addr))
            self._log("client connected %s:%d" % addr)

    def _read(self, c):
        while not c.closed:
            try:
                data = c.sock.recv(65536)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                self._close(c)
                return
            if not data:
                # Peer finished sending; still answer what is buffered.
                if b"\n" not in c.inbuf:
                    self._close(c)
                return
            c.inbuf += data
            if len(c.inbuf) > MAX_LINE and b"\n" not in c.inbuf:
                self._log("client %s:%d sent an over-long line; dropping it" % c.addr)
                self._close(c)

    def _write(self, c):
        while not c.closed and c.outbuf:
            try:
                n = c.sock.send(c.outbuf)
            except (BlockingIOError, InterruptedError):
                return
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                    self._close(c)
                return
            c.outbuf = c.outbuf[n:]

    def _handle(self, line):
        req_id = None
        try:
            req = json.loads(line.decode("utf-8"))
            if not isinstance(req, dict) or "cmd" not in req:
                raise ValueError('request must be an object with a "cmd" field')
            req_id = req.get("id")
            args = req.get("args") or {}
            if not isinstance(args, dict):
                raise ValueError('"args" must be an object')
            result = self._dispatch(req["cmd"], args)
            resp = {"id": req_id, "ok": True, "result": result}
        except Exception as e:  # report every failure to the client, never to Live
            resp = {"id": req_id, "ok": False, "error": "%s: %s" % (type(e).__name__, e),
                    "traceback": traceback.format_exc()}
        try:
            text = json.dumps(resp, default=str)
        except Exception as e:
            text = json.dumps({"id": req_id, "ok": False, "error": "unserialisable result: %s" % e})
        return text.encode("utf-8") + b"\n"

    def _close(self, c):
        if not c.closed:
            c.closed = True
            try:
                c.sock.close()
            except OSError:
                pass
