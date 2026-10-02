"""Cron: import path of the restart-safe external worker.

The worker is spawned as ``sys.executable -m cron.scheduler``. Its entry module is
``cron.scheduler``, not ``hermes_cli.main``, so nothing bootstraps the gateway's checkout
onto its ``sys.path``; historically it imported ``cron`` only through the implicit ``-m``
cwd entry. That entry is gone under ``PYTHONSAFEPATH`` and useless when the venv's
editable install maps a moved/deleted checkout -- the worker then dies with
"No module named 'cron'" before its ownership ack (#112729, hypothesised cause).

The shared subprocess sanitizer strips Hermes-owned PYTHONPATH entries because user
children must not see our tree. This child IS Hermes, so the pin is applied *after* the
env is built, on the sanitized env. On a self-managed (shell-installer / PM) install the
sanitizer's drop of the runtime site-packages cannot stand this time: the worker inherits
this process's interpreter, which is PM's store Python and owns no third-party
dependencies, so the committed generation's site-packages is restored here too or the
child dies at its first dependency import (``No module named 'ruamel'``, #122222) before
it can publish its ownership acknowledgement.
"""

from __future__ import annotations

import logging
import os
import sysconfig
from pathlib import Path

logger = logging.getLogger(__name__)


def _installed_purelib() -> Path | None:
    try:
        return Path(sysconfig.get_paths()["purelib"]).resolve()
    except (KeyError, OSError):
        return None


def _committed_dependency_site_packages(project_root: Path) -> Path | None:
    """The dependency ``site-packages`` PM committed for this install, or ``None``.

    Asked of PM's own committed selection -- the record ``activate_dependencies`` resolves
    at process boot -- rather than re-derived from this process's ``sys.path``: the record
    belongs to this install (``install_state_dir``, shared by every profile it serves). A
    runner that owns its dependencies (wheel / pipx / developer venv / Nix: no committed
    generation) has nothing to restore, so ``None`` means "pin the tree only" and nothing
    is invented.
    """
    try:
        from pm.environments import committed_venv, site_packages

        environment = committed_venv(project_root)
    except Exception as exc:
        # An unreadable record: pin the tree only. The cron worker's own boot re-reads it and
        # fails the dispatch with PM's error.
        logger.warning(
            "cron worker: could not read the committed dependency environment: %s", exc
        )
        return None
    if environment is None:
        return None
    selected = site_packages(environment)
    return selected if selected.is_dir() else None


def pin_hermes_tree_on_pythonpath(worker_env: dict, repo_root: Path) -> dict:
    """Prepend ``repo_root`` -- and, when this runner has one, the committed dependency
    generation's ``site-packages`` -- to the worker env's own PYTHONPATH (never
    ``os.environ``'s).

    Skipped when ``repo_root`` is the interpreter's ``purelib``: under a wheel / pipx /
    uv-tool install ``cron/`` lives in site-packages itself, which is already importable,
    and pinning it would move site-packages ahead of the stdlib on ``sys.path``.

    Order (checkout, generation, sanitizer-kept) mirrors ``activate_dependencies``' own
    ``sys.path``. For the cron worker, its boot (``cron/worker_bootstrap.py``) then re-selects
    and leases the committed generation before any third-party import, and exits the worker
    if it cannot.
    """
    root = str(repo_root)
    if _installed_purelib() == Path(root).resolve():
        return worker_env
    existing = [e for e in worker_env.get("PYTHONPATH", "").split(os.pathsep) if e]
    dependency = _committed_dependency_site_packages(Path(root))
    pinned = [root, *([str(dependency)] if dependency is not None else [])]
    worker_env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys([*pinned, *existing]))
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
