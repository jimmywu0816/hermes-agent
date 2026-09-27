"""Cron: import path of the restart-safe external worker.

The worker is spawned as ``sys.executable -m cron.scheduler``. Its entry module is
``cron.scheduler``, not ``hermes_cli.main``, so nothing bootstraps the gateway's checkout
onto its ``sys.path``; historically it imported ``cron`` only through the implicit ``-m``
cwd entry. That entry is gone under ``PYTHONSAFEPATH`` and useless when the venv's
editable install maps a moved/deleted checkout -- the worker then dies with
"No module named 'cron'" before its ownership ack (#112729, hypothesised cause).

The shared subprocess sanitizer strips Hermes-owned PYTHONPATH entries because user
children must not see our tree. This child IS Hermes, so the pin is applied *after* the
env is built, on the sanitized env -- the sanitizer's other decisions (dropped runtime
site-packages, dropped venv markers) stand.

The pin cannot put the runtime's *dependencies* back: under the PM layout the worker's
interpreter is the bare store Python, so the worker process itself must re-expose the
selected environment before its imports run (``ensure_runtime_dependencies_on_syspath``,
called from ``cron/__init__`` -- without it ``utils`` -> ``hermes_yaml`` -> ``ruamel``
fails before the ownership ack).
"""

from __future__ import annotations

import os
import sysconfig
from pathlib import Path


def _installed_purelib() -> Path | None:
    try:
        return Path(sysconfig.get_paths()["purelib"]).resolve()
    except (KeyError, OSError):
        return None


def pin_hermes_tree_on_pythonpath(worker_env: dict, repo_root: Path) -> dict:
    """Prepend ``repo_root`` to the worker env's own PYTHONPATH (never ``os.environ``'s).

    Skipped when ``repo_root`` is the interpreter's ``purelib``: under a wheel / pipx /
    uv-tool install ``cron/`` lives in site-packages itself, which is already importable,
    and pinning it would move site-packages ahead of the stdlib on ``sys.path``.
    """
    root = str(repo_root)
    if _installed_purelib() == Path(root).resolve():
        return worker_env
    existing = [e for e in worker_env.get("PYTHONPATH", "").split(os.pathsep) if e]
    worker_env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys([root, *existing]))
    return worker_env


def ensure_runtime_dependencies_on_syspath(repo_root: Path | None = None) -> bool:
    """Re-expose the PM-selected dependency environment to a Hermes child that booted
    without the activation prologue (the restart-safe external worker).

    The worker's sanitized env drops Hermes-owned PYTHONPATH entries and the spawn-side
    pin restores only the repo checkout, so under the PM runtime layout the bare store
    interpreter cannot import the checkout's module-level third-party dependencies
    (``utils`` -> ``hermes_yaml`` -> ``ruamel``) and the worker dies before publishing
    its ownership acknowledgement. Called from ``cron/__init__`` ahead of its imports;
    a no-op when the environment is already importable (activated children, wheel
    installs) or belongs to a different interpreter version.
    """
    import site
    import sys

    try:
        from pm.environments import selected_venv, site_packages, venv_python_version

        root = Path(repo_root).resolve() if repo_root is not None else Path(__file__).resolve().parent.parent
        environment = selected_venv(root)
        version = venv_python_version(environment)
        if version is not None and version != (sys.version_info.major, sys.version_info.minor):
            return False
        site_dir = site_packages(environment)
        if not site_dir.is_dir():
            return False
        entry = str(site_dir)
        if entry in sys.path:
            return True
        # addsitedir over a plain insert: .pth-based members (uv editable installs
        # among them) activate exactly as they do for the gateway process.
        site.addsitedir(entry)
        if entry in sys.path:
            sys.path.remove(entry)
        # Keep the runtime immediately behind this checkout -- the order
        # activate_dependencies establishes for the gateway itself.
        position = 0
        for index, item in enumerate(sys.path):
            try:
                resolved = Path(item).resolve() if item else None
            except OSError:
                continue
            if resolved == root:
                position = index + 1
                break
        sys.path.insert(position, entry)
        return True
    except Exception:
        return False
