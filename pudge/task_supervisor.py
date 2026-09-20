from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Literal, Sequence


@dataclass(slots=True)
class ManagedTask:
    name: str
    thread: threading.Thread
    cancel_event: threading.Event
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    error: str = ""
    cooperative_cancel: bool = False

    @property
    def running(self) -> bool:
        return self.thread.is_alive()


@dataclass(frozen=True, slots=True)
class TaskStartResult:
    admission: Literal["started", "already_running"]
    task: ManagedTask

    @property
    def started(self) -> bool:
        return self.admission == "started"


class TaskSupervisor:
    """Own background thread/process lifetime for one application instance."""

    def __init__(self, *, logger: Any = None) -> None:
        self.logger = logger
        self._lock = threading.RLock()
        self._tasks: dict[str, ManagedTask] = {}
        # Cooperative replacement is not instantaneous: the old worker may still
        # be finishing one bounded unit after the new task becomes active. Keep
        # those instances owned until their thread actually exits.
        self._retiring_tasks: dict[int, ManagedTask] = {}
        self._processes: dict[str, subprocess.Popen[Any]] = {}
        self._closed = False
        self._suspended = False

    def _log(self, level: str, message: str, *args: object) -> None:
        callback = getattr(self.logger, level, None)
        if callable(callback):
            try:
                callback(message, *args)
            except (OSError, RuntimeError, ValueError):
                return

    def start_with_admission(
        self,
        name: str,
        target: Callable[..., Any],
        *,
        args: Iterable[Any] = (),
        pass_cancel_event: bool = False,
        replace: bool = False,
        daemon: bool = True,
    ) -> TaskStartResult:
        """Start one owned task and report whether a new worker was admitted."""
        task_name = str(name).strip()
        if not task_name:
            raise ValueError("task name is required")
        with self._lock:
            if self._closed:
                raise RuntimeError("task supervisor is closed")
            if self._suspended:
                raise RuntimeError("task supervisor is suspended")
            existing = self._tasks.get(task_name)
            if existing is not None and existing.running:
                if not replace:
                    return TaskStartResult("already_running", existing)
                existing.cancel_event.set()
                if not existing.cooperative_cancel:
                    self._log(
                        "warning",
                        "SKIP step=task_supervisor.replace name=%s reason=non_cooperative",
                        task_name,
                    )
                    return TaskStartResult("already_running", existing)
                self._retiring_tasks[id(existing)] = existing
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
                    with self._lock:
                        if self._tasks.get(task_name) is not task:
                            self._retiring_tasks.pop(id(task), None)

            thread = threading.Thread(target=runner, name=task_name, daemon=daemon)
            task = ManagedTask(task_name, thread, cancel_event, cooperative_cancel=pass_cancel_event)
            holder["task"] = task
            self._tasks[task_name] = task
            thread.start()
            return TaskStartResult("started", task)

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
        return self.start_with_admission(
            name,
            target,
            args=args,
            pass_cancel_event=pass_cancel_event,
            replace=replace,
            daemon=daemon,
        ).task

    def cancel(self, name: str) -> bool:
        task_name = str(name)
        with self._lock:
            tasks = [
                task
                for task in (
                    [self._tasks.get(task_name)]
                    + [item for item in self._retiring_tasks.values() if item.name == task_name]
                )
                if task is not None
            ]
            process = self._processes.get(task_name)
            if not tasks and process is None:
                return False
            for task in tasks:
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
    ) -> tuple[int, Any, Any]:
        """Run a cancellable child while continuously draining stdout/stderr.

        Repeated ``communicate(timeout=...)`` calls keep pipe reader threads active,
        so a verbose child cannot deadlock on a full PIPE before it exits.
        """

        started = time.monotonic()
        process_name = str(name)
        with self._lock:
            if self._closed:
                raise RuntimeError("task supervisor is closed")
            if self._suspended:
                raise RuntimeError("task supervisor is suspended")
            existing = self._processes.get(process_name)
            if existing is not None and existing.poll() is None:
                raise RuntimeError(f"process already running: {process_name}")
            # Spawn while ownership is locked so shutdown cannot pass between
            # Popen() and registration and lose the child process.
            process = subprocess.Popen(list(command), **kwargs)
            self._processes[process_name] = process
        try:
            while True:
                try:
                    stdout, stderr = process.communicate(timeout=0.10)
                    return int(process.returncode or 0), stdout or b"", stderr or b""
                except subprocess.TimeoutExpired:
                    pass

                if cancel_event is not None and cancel_event.is_set():
                    process.terminate()
                    try:
                        stdout, stderr = process.communicate(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        stdout, stderr = process.communicate()
                    return int(process.returncode or 0), stdout or b"", stderr or b""

                if timeout is not None and time.monotonic() - started >= float(timeout):
                    process.terminate()
                    try:
                        process.communicate(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate()
                    raise subprocess.TimeoutExpired(list(command), timeout)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            with self._lock:
                current = self._processes.get(process_name)
                if current is process:
                    self._processes.pop(process_name, None)

    def status(self) -> list[dict[str, Any]]:
        with self._lock:
            active = list(self._tasks.values())
            retiring = list(self._retiring_tasks.values())
            return [
                {
                    "name": task.name,
                    "running": task.running,
                    "retiring": id(task) in self._retiring_tasks,
                    "cancel_requested": task.cancel_event.is_set(),
                    "started_at": task.started_at,
                    "finished_at": task.finished_at,
                    "error": task.error,
                }
                for task in active + retiring
            ]

    def suspend(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("task supervisor is closed")
            self._suspended = True

    def resume(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("task supervisor is closed")
            self._suspended = False

    def _quiesce_owned(self, *, timeout: float) -> list[str]:
        with self._lock:
            tasks = [
                task
                for task in list(self._tasks.values()) + list(self._retiring_tasks.values())
                if task.running
            ]
            processes = [
                (name, process) for name, process in self._processes.items() if process.poll() is None
            ]
            for task in tasks:
                task.cancel_event.set()
            for _name, process in processes:
                try:
                    process.terminate()
                except OSError:
                    pass

        deadline = time.monotonic() + max(0.0, float(timeout))
        for task in tasks:
            if task.thread is threading.current_thread():
                continue
            task.thread.join(max(0.0, deadline - time.monotonic()))

        for _name, process in processes:
            if process.poll() is not None:
                continue
            remaining = max(0.0, deadline - time.monotonic())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                    process.wait(timeout=1.0)
                except (OSError, subprocess.TimeoutExpired):
                    pass

        lingering: list[str] = []
        with self._lock:
            for task in tasks:
                if task.running and task.name not in lingering:
                    lingering.append(task.name)
            for name, process in processes:
                process_name = f"process:{name}"
                if process.poll() is None and process_name not in lingering:
                    lingering.append(process_name)
        return lingering

    def quiesce(self, *, timeout: float = 5.0) -> list[str]:
        self.suspend()
        return self._quiesce_owned(timeout=timeout)

    def shutdown(self, *, timeout: float = 5.0) -> list[str]:
        with self._lock:
            self._closed = True
            self._suspended = True
        return self._quiesce_owned(timeout=timeout)
