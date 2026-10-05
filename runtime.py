"""Lifecycle primitives for named background tasks and process metrics."""

import asyncio
import logging
import time
from collections import Counter


logger = logging.getLogger("MusicBot.Runtime")
started_at = time.time()
metrics = Counter()


class TaskRegistry:
    def __init__(self, label):
        self.label = label
        self.tasks = set()
        self.closing = False

    def create(self, coroutine, name):
        if self.closing:
            coroutine.close()
            return None
        task = asyncio.create_task(coroutine, name=f"{self.label}:{name}")
        self.tasks.add(task)
        metrics["tasks_started_total"] += 1

        def finished(done):
            self.tasks.discard(done)
            if done.cancelled():
                metrics["tasks_cancelled_total"] += 1
                return
            try:
                error = done.exception()
            except asyncio.CancelledError:
                return
            if error:
                metrics["tasks_failed_total"] += 1
                logger.error("Background task %s failed", done.get_name(),
                             exc_info=(type(error), error, error.__traceback__))

        task.add_done_callback(finished)
        return task

    async def cancel_all(self, timeout=15):
        self.closing = True
        tasks = [task for task in self.tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=timeout)
            for task in pending:
                logger.warning("Task did not stop in time: %s", task.get_name())
            metrics["tasks_shutdown_total"] += len(done)
        self.tasks.clear()

    @property
    def active(self):
        return sum(not task.done() for task in self.tasks)


def increment(name, amount=1):
    metrics[name] += amount


def prometheus(extra=None):
    values = dict(metrics)
    values["process_uptime_seconds"] = max(0, time.time() - started_at)
    values.update(extra or {})
    lines = []
    for name, value in sorted(values.items()):
        safe = "".join(ch if ch.isalnum() or ch in "_:" else "_" for ch in name)
        lines.append(f"musicwatch_{safe} {float(value):.3f}")
    return "\n".join(lines) + "\n"
