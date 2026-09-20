from __future__ import annotations

import importlib
import logging
import multiprocessing
import os
import signal
import threading
import time
from collections import deque
from dataclasses import dataclass
from multiprocessing.connection import Connection

from src.agents.agent_manager import load_external_tools
from src.core.bound_document import bound_document_scope, document_id_of
from src.core.planned_manifests import apply_operation, ask_the_executor_instead
from src.core.user_token import user_token_scope
from src.core.utils import (
    ANONYMOUS_OUTPUTS_DIRECTORY,
    caller_directory_scope,
    preload_geo_stack,
)

logger = logging.getLogger(__name__)

MEMORY_LIMIT_ENV = "GEOLANG_TOOL_MEMORY_LIMIT_MB"
TIMEOUT_ENV = "GEOLANG_TOOL_TIMEOUT_SECONDS"
MAX_CONCURRENT_ENV = "GEOLANG_TOOL_MAX_CONCURRENT"

DEFAULT_MEMORY_LIMIT_MB = 3072
# under the API client's EXECUTOR_TIMEOUT_SECONDS, so the caller is told why
DEFAULT_TIMEOUT_SECONDS = 840.0
DEFAULT_MAX_CONCURRENT = 2

# how long a call waits for a free worker before it is told the executor is busy
FREE_WORKER_WAIT_SECONDS = 1.0
# a fresh worker imports the geo stack before it can take a run
WORKER_START_SECONDS = 120.0
WORKER_EXIT_SECONDS = 2.0
MESSAGE_POLL_SECONDS = 0.05
KILOBYTES_PER_MEBIBYTE = 1024
RESIDENT_MEMORY_FIELD = "VmRSS:"

READY = "ready"

FINISHED = "finished"
OVER_MEMORY = "over the memory limit"
OVER_TIME = "over the time limit"
STOPPED = "stopped without answering"
NEVER_STARTED = "never started"

RUN_FAILED_MESSAGES = {
    OVER_MEMORY: (
        "{name} exceeded the {memory_limit} MiB memory limit, ask for a smaller area"
    ),
    OVER_TIME: (
        "{name} exceeded the {timeout:g} second time limit, ask for a smaller area"
    ),
    STOPPED: "{name} stopped without answering, it may have run out of memory",
    NEVER_STARTED: "{name} could not be started by the tool executor",
}
BUSY_MESSAGE = (
    "the tool executor is at its limit of {limit} tool runs, "
    "try {name} again in a moment"
)


def _positive_setting(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if value <= 0:
        logger.warning(f"{name}={raw} is not a positive number, using {default}")
        return default
    return value


def memory_limit_mb() -> int:
    return int(_positive_setting(MEMORY_LIMIT_ENV, DEFAULT_MEMORY_LIMIT_MB))


def tool_timeout_seconds() -> float:
    return _positive_setting(TIMEOUT_ENV, DEFAULT_TIMEOUT_SECONDS)


def max_concurrent() -> int:
    return int(_positive_setting(MAX_CONCURRENT_ENV, DEFAULT_MAX_CONCURRENT))


@dataclass
class ToolRun:
    name: str
    module: str
    qualified_name: str
    args: dict
    token: str | None
    outputs_directory: str | None
    document_id: str | None


@dataclass
class RecordAsk:
    operation: str
    manifest_toml: str


@dataclass
class Worker:
    process: multiprocessing.process.BaseProcess
    connection: Connection


def resident_kilobytes(pid: int | None) -> int:
    if pid is None:
        return 0
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as status:
            for line in status:
                if line.startswith(RESIDENT_MEMORY_FIELD):
                    return int(line.split()[1])
    except OSError:
        return 0
    return 0


def _tool_function(module: str, qualified_name: str):
    target = importlib.import_module(module)
    for attribute in qualified_name.split("."):
        target = getattr(target, attribute)
    return target


def _execute(tool_run: ToolRun) -> dict:
    try:
        func = _tool_function(tool_run.module, tool_run.qualified_name)
        with (
            user_token_scope(tool_run.token),
            caller_directory_scope(tool_run.outputs_directory),
            bound_document_scope(document_id_of(tool_run.document_id)),
        ):
            return {"result": str(func(**tool_run.args))}
    except Exception as e:
        logger.exception(f"Tool {tool_run.name} failed")
        return {"error": str(e)}


def _asking_the_executor(connection: Connection):
    def ask(operation: str, manifest_toml: str) -> bool:
        connection.send(RecordAsk(operation, manifest_toml))
        return connection.recv()

    return ask


def _worker_main(connection: Connection) -> None:
    logging.basicConfig(level=logging.INFO)
    # its own process group, so killing the worker kills what the tool started
    os.setsid()
    ask_the_executor_instead(_asking_the_executor(connection))
    preload_geo_stack()
    load_external_tools()
    connection.send(READY)
    tool_run = connection.recv()
    connection.send(_execute(tool_run))
    connection.close()


def _kill(pid: int | None) -> None:
    if pid is None:
        return
    try:
        # the group only when the worker leads it, or this kills the executor
        if os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def _stop(worker: Worker) -> None:
    worker.connection.close()
    if worker.process.is_alive():
        _kill(worker.process.pid)
    worker.process.join(WORKER_EXIT_SECONDS)


def _next_message(
    worker: Worker, deadline: float, limit_mb: int, caller: str
) -> tuple[object, str | None]:
    limit_kilobytes = limit_mb * KILOBYTES_PER_MEBIBYTE
    while True:
        if worker.connection.poll(MESSAGE_POLL_SECONDS):
            try:
                message = worker.connection.recv()
            except EOFError:
                return None, STOPPED
            if isinstance(message, RecordAsk):
                # the run's caller, never one the worker names: it runs tool code
                worker.connection.send(
                    apply_operation(message.operation, caller, message.manifest_toml)
                )
                continue
            return message, None
        # an exited worker can still have its reply waiting in the pipe
        if not worker.process.is_alive() and not worker.connection.poll():
            return None, STOPPED
        if resident_kilobytes(worker.process.pid) > limit_kilobytes:
            return None, OVER_MEMORY
        if time.monotonic() >= deadline:
            return None, OVER_TIME


def _failed(reason: str, tool_run: ToolRun, limit_mb: int, timeout: float) -> dict:
    return {
        "error": RUN_FAILED_MESSAGES[reason].format(
            name=tool_run.name, memory_limit=limit_mb, timeout=timeout
        )
    }


class ToolWorkerPool:
    def __init__(self) -> None:
        # spawn, not fork: the executor answers from uvicorn's threadpool, and a
        # child forked from a threaded process inherits locks nobody will release
        self._context = multiprocessing.get_context("spawn")
        self._idle: deque[Worker] = deque()
        self._idle_lock = threading.Lock()
        self._refill_lock = threading.Lock()
        self._slots = threading.Condition()
        self._running = 0

    def start(self) -> None:
        self._refill_in_background()

    def shutdown(self) -> None:
        # the refill lock, or a worker started before this call lands after it
        with self._refill_lock:
            with self._idle_lock:
                waiting = list(self._idle)
                self._idle.clear()
        for worker in waiting:
            _stop(worker)

    def run(self, tool_run: ToolRun) -> dict:
        limit = max_concurrent()
        if not self._take_slot(limit):
            logger.warning(
                f"tool {tool_run.name} refused: {limit} runs already in flight"
            )
            return {"error": BUSY_MESSAGE.format(limit=limit, name=tool_run.name)}
        try:
            return self._run_in_worker(self._take_worker(), tool_run)
        finally:
            self._release_slot()

    def _take_slot(self, limit: int) -> bool:
        deadline = time.monotonic() + FREE_WORKER_WAIT_SECONDS
        with self._slots:
            while self._running >= limit:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._slots.wait(remaining)
            self._running += 1
            return True

    def _release_slot(self) -> None:
        with self._slots:
            self._running -= 1
            self._slots.notify()

    def _start_worker(self) -> Worker:
        ours, theirs = self._context.Pipe()
        process = self._context.Process(
            target=_worker_main, args=(theirs,), daemon=True
        )
        process.start()
        theirs.close()
        return Worker(process=process, connection=ours)

    def _take_worker(self) -> Worker:
        with self._idle_lock:
            worker = self._idle.popleft() if self._idle else None
        if worker is None:
            worker = self._start_worker()
        self._refill_in_background()
        return worker

    def _refill_in_background(self) -> None:
        threading.Thread(target=self._refill, daemon=True).start()

    def _refill(self) -> None:
        with self._refill_lock:
            while True:
                with self._idle_lock:
                    if len(self._idle) >= max_concurrent():
                        return
                worker = self._start_worker()
                with self._idle_lock:
                    self._idle.append(worker)

    def _run_in_worker(self, worker: Worker, tool_run: ToolRun) -> dict:
        limit_mb = memory_limit_mb()
        timeout = tool_timeout_seconds()
        caller = tool_run.outputs_directory or ANONYMOUS_OUTPUTS_DIRECTORY
        logger.info(f"tool {tool_run.name} started in worker {worker.process.pid}")

        _, failure = _next_message(
            worker, time.monotonic() + WORKER_START_SECONDS, limit_mb, caller
        )
        if failure is not None:
            self._end_run(worker, tool_run.name, NEVER_STARTED)
            return _failed(NEVER_STARTED, tool_run, limit_mb, timeout)

        try:
            worker.connection.send(tool_run)
        except OSError:
            self._end_run(worker, tool_run.name, STOPPED)
            return _failed(STOPPED, tool_run, limit_mb, timeout)

        reply, failure = _next_message(
            worker, time.monotonic() + timeout, limit_mb, caller
        )
        if failure is not None:
            self._end_run(worker, tool_run.name, failure)
            return _failed(failure, tool_run, limit_mb, timeout)

        self._end_run(worker, tool_run.name, FINISHED)
        return reply

    def _end_run(self, worker: Worker, name: str, reason: str) -> None:
        if reason == FINISHED:
            logger.info(f"tool {name} finished")
        else:
            logger.warning(f"tool {name} killed: {reason}")
        _stop(worker)


tool_workers = ToolWorkerPool()
