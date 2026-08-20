from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence


@dataclass(slots=True)
class ManagedTask:
    name: str
    thread: threading.Thread
    cancel_event: threading.Event
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    error: str = ""

    @property
    def running(self) -> bool:
        return self.thread.is_alive()


class TaskSupervisor:
    """Own background thread/process lifetime for one application instance."""

    def __init__(self, *, logger: Any = None) -> None:
        self.logger = logger
        self._lock = threading.RLock()
        self._tasks: dict[str, ManagedTask] = {}
        self._processes: dict[str, subprocess.Popen[Any]] = {}
        self._closed = False

    def _log(self, level: str, message: str, *args: object) -> None:
        callback = getattr(self.logger, level, None)
        if callable(callback):
            try:
                callback(message, *args)
            except (OSError, RuntimeError, ValueError):
                return

    def start(
        self,
        name: str,
        target: Callable[..., Any],
        *,
        args: Iterable[Any] = (),
        pass_cancel_event: bool = False,
        replace: bool = False,
        daemon: bool = True,
    ) -> ManagedTask:
        task_name = str(name).strip()
        if not task_name:
            raise ValueError("task name is required")
        with self._lock:
            if self._closed:
                raise RuntimeError("task supervisor is closed")
            existing = self._tasks.get(task_name)
            if existing is not None and existing.running:
                if not replace:
                    return existing
                existing.cancel_event.set()
            cancel_event = threading.Event()
            arguments = tuple(args)
            holder: dict[str, ManagedTask] = {}

            def runner() -> None:
                task = holder["task"]
                try:
                    if pass_cancel_event:
                        target(cancel_event, *arguments)
                    else:
                        target(*arguments)
                except Exception as exc:  # noqa: BLE001 - worker boundary records every failure.
                    task.error = str(exc)
                    self._log("exception", "FAIL step=task_supervisor name=%s", task_name)
                finally:
                    task.finished_at = time.time()

            thread = threading.Thread(target=runner, name=task_name, daemon=daemon)
            task = ManagedTask(task_name, thread, cancel_event)
            holder["task"] = task
            self._tasks[task_name] = task
            thread.start()
            return task

    def cancel(self, name: str) -> bool:
        with self._lock:
            task = self._tasks.get(str(name))
            process = self._processes.get(str(name))
            if task is None and process is None:
                return False
            if task is not None:
                task.cancel_event.set()
            if process is not None and process.poll() is None:
                process.terminate()
            return True

    def run_process(
        self,
        name: str,
        command: Sequence[str],
        *,
        cancel_event: threading.Event | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> tuple[int, bytes, bytes]:
        """Run a cancellable child process without an unbounded communicate()."""

        started = time.monotonic()
        process = subprocess.Popen(list(command), **kwargs)
        with self._lock:
            if self._closed:
                process.terminate()
                raise RuntimeError("task supervisor is closed")
            self._processes[str(name)] = process
        try:
            while process.poll() is None:
                if cancel_event is not None and cancel_event.wait(0.1):
                    process.terminate()
                    break
                if timeout is not None and time.monotonic() - started >= float(timeout):
                    process.terminate()
                    raise subprocess.TimeoutExpired(list(command), timeout)
                if cancel_event is None:
                    time.sleep(0.1)
            try:
                stdout, stderr = process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
            return int(process.returncode or 0), stdout or b"", stderr or b""
        finally:
            with self._lock:
                self._processes.pop(str(name), None)

    def status(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "name": task.name,
                    "running": task.running,
                    "cancel_requested": task.cancel_event.is_set(),
                    "started_at": task.started_at,
                    "finished_at": task.finished_at,
                    "error": task.error,
                }
                for task in self._tasks.values()
            ]

    def shutdown(self, *, timeout: float = 5.0) -> None:
        with self._lock:
            self._closed = True
            tasks = list(self._tasks.values())
            processes = list(self._processes.values())
            for task in tasks:
                task.cancel_event.set()
            for process in processes:
                if process.poll() is None:
                    process.terminate()
        deadline = time.monotonic() + max(0.0, float(timeout))
        for task in tasks:
            remaining = max(0.0, deadline - time.monotonic())
            if task.thread is not threading.current_thread():
                task.thread.join(remaining)
        for process in processes:
            if process.poll() is not None:
                continue
            try:
                process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                process.kill()
