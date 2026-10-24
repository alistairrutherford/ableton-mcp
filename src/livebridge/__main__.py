"""Command line: send one bridge command, or wait for Live to come up.

    python -m livebridge get_state
    python -m livebridge set_tempo '{"bpm": 120}'
    python -m livebridge wait --timeout 120
"""

import argparse
import json
import sys
import time

from .client import BRIDGE_NAME, BridgeError, LiveClient


def main(argv=None):
    ap = argparse.ArgumentParser(prog="livebridge")
    ap.add_argument("cmd", help="bridge command, or 'wait'")
    ap.add_argument("args", nargs="?", default="{}", help="JSON object of arguments")
    ap.add_argument("--timeout", type=float, default=60.0, help="for 'wait': seconds")
    opts = ap.parse_args(argv)

    if opts.cmd == "wait":
        deadline = time.monotonic() + opts.timeout
        while True:
            try:
                with LiveClient(timeout=2.0) as c:
                    print(json.dumps(c.call("ping")))
                    return 0
            except OSError:
                if time.monotonic() > deadline:
                    print("%s not answering after %.0f s" % (BRIDGE_NAME, opts.timeout), file=sys.stderr)
                    return 1
                time.sleep(1.0)

    try:
        with LiveClient() as c:
            result = c.call(opts.cmd, **json.loads(opts.args))
    except BridgeError as e:
        print(e, file=sys.stderr)
        return 1
    except OSError as e:
        print("cannot reach %s: %s" % (BRIDGE_NAME, e), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
