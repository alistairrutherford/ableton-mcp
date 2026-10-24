"""Installing the ClaudeLiveBridge Remote Script into Live's User Library, and checking it.

Live loads Remote Scripts from `<User Library>/Remote Scripts/<name>/` at startup. An installed
plugin lives in a versioned folder that moves on every update, so by default the script is
copied, and `status()` says when the copy no longer matches the one this server ships. A git
checkout can link instead (`--link`), so edits reach Live on its next start.
"""

import filecmp
import os
import shutil

from livebridge.client import BRIDGE_NAME

HERE = os.path.dirname(os.path.abspath(__file__))
# src/ableton_mcp -> the repository (or plugin) root
ROOT = os.path.dirname(os.path.dirname(HERE))
BUNDLED = os.path.join(ROOT, "RemoteScript", BRIDGE_NAME)


def remote_scripts_dir():
    library = os.environ.get("LIVE_USER_LIBRARY") or os.path.expanduser("~/Music/Ableton/User Library")
    return os.path.join(library, "Remote Scripts")


def installed_dir():
    return os.path.join(remote_scripts_dir(), BRIDGE_NAME)


def _sources(folder):
    return sorted(f for f in os.listdir(folder) if f.endswith(".py"))


def status():
    """{installed, kind ("link"/"copy"), up_to_date, path, bundled}."""
    dest = installed_dir()
    out = {"path": dest, "bundled": BUNDLED, "installed": os.path.isdir(dest)}
    if not out["installed"]:
        out.update(kind=None, up_to_date=False)
        return out
    if os.path.islink(dest):
        out["kind"] = "link"
        out["links_to"] = os.path.realpath(dest)
        out["up_to_date"] = os.path.realpath(dest) == os.path.realpath(BUNDLED)
        return out
    out["kind"] = "copy"
    names = _sources(BUNDLED)
    out["up_to_date"] = names == _sources(dest) and all(
        filecmp.cmp(os.path.join(BUNDLED, n), os.path.join(dest, n), shallow=False) for n in names)
    return out


def install(link=False):
    """Copy (or, with `link`, symlink) the bundled Remote Script into the User Library, replacing an
    earlier copy or link of it. Live picks it up at its next start."""
    if not os.path.isfile(os.path.join(BUNDLED, "__init__.py")):
        raise RuntimeError("no Remote Script at %s" % BUNDLED)
    dest = installed_dir()
    os.makedirs(remote_scripts_dir(), exist_ok=True)
    if os.path.islink(dest):
        os.remove(dest)
    elif os.path.isdir(dest):
        if not os.path.isfile(os.path.join(dest, "__init__.py")):
            raise RuntimeError("%s exists and doesn't look like a Remote Script; leaving it alone" % dest)
        shutil.rmtree(dest)
    if link:
        os.symlink(BUNDLED, dest)
    else:
        os.mkdir(dest)
        for name in _sources(BUNDLED):
            shutil.copy2(os.path.join(BUNDLED, name), os.path.join(dest, name))
    out = status()
    out["next"] = ("Restart Live (it loads Remote Scripts only at startup), then in Settings > Link, "
                   "Tempo & MIDI pick %s in an empty Control Surface row, with Input and Output None. "
                   "Live remembers that choice." % BRIDGE_NAME)
    return out
