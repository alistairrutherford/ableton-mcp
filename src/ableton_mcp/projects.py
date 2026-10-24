"""Live projects on disk: templates, making a new project, and finding existing ones.

The LOM can't create, open or save a set, so a project is made here, as files, and then opened in
Live with `open -a`. docs/spikes.md §1-2 found what Live needs, in Live 12.4.7:

- an `Ableton Project Info/` directory in `<Name> Project/` *before* the set is opened, or Live
  refuses to save the set in place ("located outside of a Project folder");
- a template already on the running Live's schema (`MinorVersion`), or Live refuses to save over
  it ("created with a version of Live that is either older or has fewer features");
- tempo and time signature set in the parameter's Manual value *and* its automation envelope.

Then Cmd-S saves the set in place with no dialog. Nothing here talks to Live; server.py opens the
set and shapes its tracks through the bridge.
"""

import glob
import os
import re
import shutil
import time

from livebridge import als

# Empty values count as unset: a plugin passes an option the user left blank as "".
PROJECTS_ROOT = os.path.expanduser(os.environ.get("ABLETON_MCP_PROJECTS") or "~/Music/Ableton/Projects")
USER_LIBRARY = os.path.expanduser(os.environ.get("LIVE_USER_LIBRARY") or "~/Music/Ableton/User Library")
MARKER = "Ableton Project Info"
SUFFIX = " Project"

# The Core Library template new projects start from when neither the call nor the user's own
# templates name one. Its tracks are replaced; it is the smallest of Live 12.4's templates.
BASE_TEMPLATE = "Vocal Recording"


class ProjectError(ValueError):
    pass


# -- Live and its templates ------------------------------------------------------------

def live_app():
    """The Live 12 application: $LIVE_APP, else the newest /Applications/Ableton Live 12*.app."""
    app = os.environ.get("LIVE_APP")
    if app:
        return app
    apps = sorted(glob.glob("/Applications/Ableton Live 12*.app"))
    if not apps:
        raise ProjectError("Live 12 not found in /Applications (set LIVE_APP)")
    return apps[-1]


def core_templates(app=None):
    return sorted(glob.glob(os.path.join(app or live_app(), "Contents", "App-Resources", "Core Library",
                                         "Templates", "*.als")))


def user_templates():
    return sorted(glob.glob(os.path.join(USER_LIBRARY, "Templates", "*.als")))


def schema_of(path):
    return als.header(als.read(path)).get("MinorVersion", "")


def current_schema(app=None):
    """The schema the running Live writes, as far as files can tell: the newest one among the Core
    Library templates that ship with it (12.0_12402 for Live 12.4.7, which also writes 12402)."""
    schemas = [schema_of(p) for p in core_templates(app)]
    return max(schemas, key=als.schema_key) if schemas else None


def _stem(path):
    return os.path.splitext(os.path.basename(path))[0]


def find_template(template=None, app=None):
    """(path, is_base): a template by path or name (the user's templates first, then the Core
    Library's); with none given, the user's template named "Default..." if there is one, else the
    Core Library base (is_base: its tracks are to be replaced)."""
    if template:
        if os.path.sep in template or template.endswith(".als"):
            path = os.path.expanduser(template)
            if not os.path.isfile(path):
                raise ProjectError("no template at %s" % path)
            return path, False
        want = template.strip().lower()
        candidates = user_templates() + core_templates(app)
        exact = [p for p in candidates if _stem(p).lower() == want]
        partial = [p for p in candidates if want in _stem(p).lower()]
        if exact or len(partial) == 1:
            return (exact or partial)[0], False
        names = [_stem(p) for p in (partial or candidates)]
        raise ProjectError("%s template %r; templates: %s" % (
            "ambiguous" if partial else "no", template, names))
    defaults = [p for p in user_templates() if _stem(p).lower().startswith("default")]
    if defaults:
        return defaults[0], False
    base = [p for p in core_templates(app) if _stem(p) == BASE_TEMPLATE]
    if not base:
        raise ProjectError("the Core Library has no %r template; pass `template`" % BASE_TEMPLATE)
    return base[0], True


def list_templates(app=None):
    current = current_schema(app)
    out = []
    for where, paths in (("user", user_templates()), ("core", core_templates(app))):
        for p in paths:
            schema = schema_of(p)
            out.append({"name": _stem(p), "where": where, "path": p, "schema": schema,
                        "saves_in_place": current is None or als.schema_key(schema) >= als.schema_key(current)})
    return out


# -- projects --------------------------------------------------------------------------

def check_name(name):
    name = (name or "").strip()
    if not name or name.startswith(".") or re.search(r'[/\\:\x00-\x1f]', name):
        raise ProjectError("a project name can't be empty, start with '.', or contain / \\ or :")
    if name.endswith(SUFFIX):
        name = name[:-len(SUFFIX)].rstrip()   # "Night Drive Project" -> "Night Drive"
    return name


def project_dir(name, location=None):
    return os.path.join(os.path.expanduser(location or PROJECTS_ROOT), check_name(name) + SUFFIX)


def create(name, location=None, template=None, tempo=None, signature=None, app=None):
    """Make `<location>/<name> Project/` with its marker and `<name>.als` from a template, with tempo
    and signature ([numerator, denominator]) patched in. Refuses to touch an existing folder."""
    name = check_name(name)
    folder = project_dir(name, location)
    if os.path.exists(folder):
        raise ProjectError("%s already exists; pick another name, or open it with open_project" % folder)
    path, is_base = find_template(template, app)
    text = als.read(path)
    schema = als.header(text).get("MinorVersion", "")
    if tempo is not None:
        text = als.set_tempo(text, tempo)
    if signature is not None:
        if len(signature) != 2:
            raise ProjectError("signature is [numerator, denominator], e.g. [3, 4]")
        text = als.set_time_signature(text, *signature)
    set_path = os.path.join(folder, name + ".als")
    os.makedirs(os.path.join(folder, MARKER))     # before Live ever opens the set
    try:
        als.write(set_path, text)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    current = current_schema(app)
    out = {"name": name, "project_dir": folder, "set_path": set_path, "template": _stem(path),
           "template_path": path, "template_is_base": is_base, "schema": schema}
    if current and als.schema_key(schema) < als.schema_key(current):
        out["warning"] = ("the template is on schema %s, older than this Live's %s: Live will open the "
                          "set but ask to save it as a new file" % (schema, current))
    return out


def project_of(set_path):
    """The project folder a set is in (the nearest folder above it with the marker), or None."""
    d = os.path.dirname(os.path.realpath(set_path)) if set_path else None
    while d and d != os.path.dirname(d):
        if os.path.isdir(os.path.join(d, MARKER)):
            return d
        d = os.path.dirname(d)
    return None


def list_projects(location=None, open_set=None):
    """Projects in `location`: folders with the marker (or named "... Project"), with their sets,
    newest first."""
    root = os.path.expanduser(location or PROJECTS_ROOT)
    if not os.path.isdir(root):
        return {"location": root, "exists": False, "projects": []}
    open_real = os.path.realpath(open_set) if open_set else None
    projects = []
    for entry in sorted(os.listdir(root)):
        folder = os.path.join(root, entry)
        marked = os.path.isdir(os.path.join(folder, MARKER))
        if not os.path.isdir(folder) or not (marked or entry.endswith(SUFFIX)):
            continue
        sets = []
        for p in sorted(glob.glob(os.path.join(folder, "*.als"))):
            sets.append({"name": _stem(p), "path": p, "modified": time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(p))),
                "is_open": open_real is not None and os.path.realpath(p) == open_real})
        if not sets:
            continue
        sets.sort(key=lambda s: s["modified"], reverse=True)
        projects.append({"name": entry[:-len(SUFFIX)] if entry.endswith(SUFFIX) else entry,
                         "project_dir": folder, "has_marker": marked, "sets": sets,
                         "modified": sets[0]["modified"]})
    projects.sort(key=lambda p: p["modified"], reverse=True)
    return {"location": root, "exists": True, "projects": projects}


def resolve_set(project, location=None):
    """The .als to open for `project`: a path to a set, a project folder, or a project name in
    `location` (its newest set)."""
    p = os.path.expanduser(project)
    if p.endswith(".als"):
        if not os.path.isfile(p):
            raise ProjectError("no set at %s" % p)
        return p
    if not os.path.isdir(p):
        p = project_dir(project, location)
        if not os.path.isdir(p):
            known = [x["name"] for x in list_projects(location)["projects"]]
            raise ProjectError("no project %r in %s; projects: %s" % (
                project, os.path.expanduser(location or PROJECTS_ROOT), known))
    sets = sorted(glob.glob(os.path.join(p, "*.als")), key=os.path.getmtime, reverse=True)
    if not sets:
        raise ProjectError("%s has no .als set" % p)
    return sets[0]
