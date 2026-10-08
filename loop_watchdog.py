"""Finds what makes Huddle music drop out.

The audio sender paces 20 ms frames on the same asyncio loop as everything
else (the web UI, Huddle polling, the DJ engine). If anything blocks that loop,
frames go out late and the listener hears a dip. This watchdog pings the loop
from a background thread; when the loop has not answered within the threshold
it snapshots the loop thread's stack, and once the loop recovers it logs how
long the stall lasted and where it was stuck. Idle cost: one cross-thread
callback every 50 ms.

It also times Python's garbage collector: a full collection in a large,
long-running process walks every tracked object and freezes the loop while it
does (1.5 million small objects take ~300 ms), so any collection over 30 ms is
logged with its generation and length.

Disable with MUSICBOT_LOOP_WATCHDOG=0.
"""
import asyncio
import gc
import logging
import os
import sys
import threading
import time
import traceback

logger = logging.getLogger("MusicBot.LoopWatchdog")


class LoopWatchdog:
    def __init__(self, loop: asyncio.AbstractEventLoop, threshold: float = 0.06,
                 interval: float = 0.05, cooldown: float = 5.0):
        self.loop = loop
        self.threshold = threshold
        self.interval = interval
        self.cooldown = cooldown
        self.loop_thread_id = threading.get_ident()
        self._stop = threading.Event()
        self._last_report = 0.0
        self._suppressed = 0
        self.stalls = 0
        self.worst = 0.0

    @classmethod
    def maybe_start(cls):
        """Start one for the running loop unless switched off by the environment."""
        if os.getenv("MUSICBOT_LOOP_WATCHDOG", "1") == "0":
            return None
        watchdog = cls(asyncio.get_running_loop())
        watchdog.start()
        return watchdog

    def start(self):
        threading.Thread(target=self._run, name="loop-watchdog", daemon=True).start()
        gc.callbacks.append(self._on_gc)
        logger.info("Event-loop stall watchdog on (threshold %d ms)", self.threshold * 1000)

    _gc_started = 0.0

    def _on_gc(self, phase, info):
        if phase == "start":
            self._gc_started = time.perf_counter()
            return
        took = time.perf_counter() - self._gc_started
        if took >= 0.03:
            logger.warning(
                "Garbage collection (generation %s) took %d ms; collected %s objects.",
                info.get("generation"), took * 1000, info.get("collected"),
            )

    def stop(self):
        self._stop.set()
        if self._on_gc in gc.callbacks:
            gc.callbacks.remove(self._on_gc)

    def _stack(self):
        frame = sys._current_frames().get(self.loop_thread_id)
        if frame is None:
            return "(no stack)"
        lines = traceback.format_stack(frame, limit=14)
        return "".join(lines).rstrip()

    def _run(self):
        while not self._stop.is_set():
            answered = threading.Event()
            started = time.monotonic()
            try:
                self.loop.call_soon_threadsafe(answered.set)
            except RuntimeError:
                return  # the loop is closed
            if not answered.wait(self.threshold):
                stack = self._stack()
                answered.wait(30)
                stalled = time.monotonic() - started
                self.stalls += 1
                self.worst = max(self.worst, stalled)
                now = time.monotonic()
                if now - self._last_report >= self.cooldown:
                    extra = f" ({self._suppressed} more since the last report)" if self._suppressed else ""
                    logger.warning(
                        "Event loop stalled for %d ms%s; audio frames went out late. "
                        "Stuck in:\n%s",
                        stalled * 1000, extra, stack,
                    )
                    self._last_report = now
                    self._suppressed = 0
                else:
                    self._suppressed += 1
            self._stop.wait(self.interval)
