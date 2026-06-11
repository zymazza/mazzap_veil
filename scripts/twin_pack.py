#!/usr/bin/env python3
"""Regional pack loader.

A pack supplies the place-specific knowledge the generic engine deliberately
lacks: vegetation species/community config (Phase 3), layer styles and
enrichment (Phase 4), and its own source-acquisition scripts. The engine core
has no hardcoded region — it loads the active pack's hooks when one is present
and degrades gracefully when none is.

The active pack is chosen by, in order:
  * the TWIN_PACK environment variable (a name under packs/, or a path),
  * data/pack.txt (a committed one-line marker), then
  * nothing — the generic engine.

A pack is a directory packs/<name>/ with an optional pack.json and optional
Python hook modules (e.g. vegetation.py exposing load(context)).
"""

import importlib.util
import os

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
PACKS_DIR = os.path.join(PROJECT, "packs")


def active_pack_dir(data_dir=None):
    """Absolute path to the active pack directory, or None for generic. The
    pack is chosen by TWIN_PACK, else the twin's own <data_dir>/pack.txt
    (so each twin selects its own pack — a scratch twin with no marker uses
    the generic engine even when the default ./data pins one)."""
    name = os.environ.get("TWIN_PACK")
    if not name:
        base = data_dir or os.environ.get("TWIN_DATA_DIR") or os.path.join(PROJECT, "data")
        marker = os.path.join(base, "pack.txt")
        if os.path.exists(marker):
            with open(marker) as fh:
                name = fh.read().strip()
    if not name:
        return None
    path = name if os.path.isabs(name) else os.path.join(PACKS_DIR, name)
    if not os.path.isdir(path):
        raise SystemExit(f"pack not found: {name} (looked in {path})")
    return path


def active_pack_name(data_dir=None):
    d = active_pack_dir(data_dir)
    return os.path.basename(d) if d else None


def _load_module(pack_dir, module_name):
    path = os.path.join(pack_dir, module_name + ".py")
    if not os.path.exists(path):
        return None
    spec = importlib.util.spec_from_file_location(
        f"twin_pack_{os.path.basename(pack_dir)}_{module_name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_hook(module_name, context=None):
    """Import packs/<active>/<module_name>.py and call its load(context),
    returning whatever it provides (or None when there is no pack / module).
    The pack is selected from the context's data_dir, so each twin gets its
    own pack."""
    ctx = dict(context or {})
    ctx.setdefault("project", PROJECT)
    ctx.setdefault("data_dir", os.environ.get("TWIN_DATA_DIR")
                   or os.path.join(PROJECT, "data"))
    pack_dir = active_pack_dir(ctx["data_dir"])
    if not pack_dir:
        return None
    mod = _load_module(pack_dir, module_name)
    if mod is None or not hasattr(mod, "load"):
        return None
    ctx.setdefault("pack_dir", pack_dir)
    return mod.load(ctx)


def load_vegetation(context=None):
    """The vegetation knowledge hook (species/community/type), or None."""
    return load_hook("vegetation", context)


def load_layers(context=None):
    """The atlas-layer styling/enrichment hook, or None."""
    return load_hook("layers", context)
