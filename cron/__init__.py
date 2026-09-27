"""Cron job scheduling for Hermes Agent: scheduled tasks (cron expressions, intervals, one-shot),
self-scheduled reminders, isolated sessions. The gateway daemon (``hermes gateway [install]``) ticks
the scheduler every 60 seconds; a file lock prevents duplicate execution across processes.
"""

# The restart-safe external worker (`python -m cron.scheduler`) boots with a sanitized
# env and no activation prologue, so re-expose the runtime dependency environment before
# this package's imports run -- they reach third-party modules at module level
# (cron.jobs -> ... -> utils -> hermes_yaml -> ruamel). No-op for booted children.
try:
    from cron.scheduler_worker_env import ensure_runtime_dependencies_on_syspath

    ensure_runtime_dependencies_on_syspath()
except Exception:
    pass

from cron.jobs import (
    create_job,
    get_job,
    list_jobs,
    remove_job,
    update_job,
    pause_job,
    resume_job,
    trigger_job,
    rearm_oneshot,
    JOBS_FILE,
)
from cron.scheduler import tick

__all__ = [
    "create_job",
    "get_job",
    "list_jobs",
    "remove_job",
    "update_job",
    "pause_job",
    "resume_job",
    "trigger_job",
    "rearm_oneshot",
    "tick",
    "JOBS_FILE",
]
