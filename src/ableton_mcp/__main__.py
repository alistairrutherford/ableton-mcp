"""Command line.

    python -m ableton_mcp                                  the MCP server (stdio)
    python -m ableton_mcp install-remote-script [--link]   put the Remote Script in Live's User Library
    python -m ableton_mcp status                           Remote Script, Live, and where things go
"""

import argparse
import json
import sys


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        from .server import main as serve
        serve()
        return 0
    ap = argparse.ArgumentParser(prog="ableton-mcp")
    sub = ap.add_subparsers(dest="cmd", required=True)
    inst = sub.add_parser("install-remote-script", help="copy (or --link) the Remote Script into Live's User Library")
    inst.add_argument("--link", action="store_true", help="symlink instead of copying (for a git checkout)")
    sub.add_parser("status", help="show the Remote Script, Live and the projects folder")
    opts = ap.parse_args(argv)

    from . import projects, remote_script
    if opts.cmd == "install-remote-script":
        result = remote_script.install(link=opts.link)
    else:
        from .server import live_status
        result = {"live": live_status(), "projects_dir": projects.PROJECTS_ROOT}
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
