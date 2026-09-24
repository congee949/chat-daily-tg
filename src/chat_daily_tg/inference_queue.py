"""Cross-process priority admission for the single-device Qwen runtime.

The local runtime serializes MLX inference internally, but its HTTP server has
no notion of priority.  This small SQLite-backed admission queue makes all
ChatDaily clients agree on which request may enter the runtime next.  A running
request is never pre-empted; waiting online queries sort ahead of offline
embedding/backfill batches.

Queue files live in a private per-user temporary directory and contain only
opaque job ids, priorities, deadlines and process ids--never prompts or model
outputs.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator


class InferenceQueueError(RuntimeError):
    pass


class InferenceQueueFull(InferenceQueueError):
    pass


class InferenceQueueTimeout(InferenceQueueError):
    pass


class InferencePriorityYield(InferenceQueueError):
    """The caller should requeue a local offline job behind a new online job."""


_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;
CREATE TABLE IF NOT EXISTS queue_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS queue_jobs (
    job_id TEXT PRIMARY KEY,
    pid INTEGER NOT NULL,
    priority INTEGER NOT NULL CHECK(priority IN (0,1)),
    enqueued_ns INTEGER NOT NULL,
    deadline_epoch REAL NOT NULL,
    lease_until_epoch REAL NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('waiting','running'))
);
CREATE INDEX IF NOT EXISTS queue_order
ON queue_jobs(state, priority, enqueued_ns, job_id);
"""


def _private_queue_dir() -> Path:
    root = Path(tempfile.gettempdir()) / f"chatdaily-qwen-queue-{os.getuid()}"
    try:
        root.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or root.is_symlink() or info.st_uid != os.getuid():
        raise InferenceQueueError(f"unsafe inference queue directory: {root}")
    if stat.S_IMODE(info.st_mode) != 0o700:
        root.chmod(0o700)
    return root


def _queue_path(endpoint: str) -> Path:
    identity = hashlib.sha256(endpoint.rstrip("/").encode("utf-8")).hexdigest()[:24]
    return _private_queue_dir() / f"{identity}.sqlite"


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class CrossProcessInferenceQueue:
    """Bounded, crash-cleaning priority admission queue for one endpoint."""

    def __init__(self, endpoint: str, *, capacity: int = 64, path: Path | None = None):
        if not 1 <= capacity <= 1024:
            raise ValueError("inference queue capacity must be 1..1024")
        self.endpoint = endpoint.rstrip("/")
        self.capacity = int(capacity)
        self.path = path or _queue_path(self.endpoint)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=1.0, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=1000")
        return connection

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and (self.path.is_symlink() or not self.path.is_file()):
            raise InferenceQueueError(f"unsafe inference queue database: {self.path}")
        connection = self._connect()
        try:
            connection.executescript(_SCHEMA)
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT value FROM queue_meta WHERE key='capacity'"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO queue_meta(key,value) VALUES('capacity',?)",
                    (str(self.capacity),),
                )
            elif int(row[0]) != self.capacity:
                raise InferenceQueueError(
                    "inference queue capacity conflicts with the shared endpoint queue"
                )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
        self.path.chmod(0o600)

    @staticmethod
    def _clean_stale(connection: sqlite3.Connection, now: float) -> None:
        rows = connection.execute(
            "SELECT job_id,pid,state,deadline_epoch,lease_until_epoch FROM queue_jobs"
        ).fetchall()
        stale = []
        for job_id, pid, state, deadline_epoch, lease_until_epoch in rows:
            if not _process_alive(int(pid)):
                stale.append(str(job_id))
            elif state == "waiting" and float(deadline_epoch) <= now:
                stale.append(str(job_id))
            elif state == "running" and float(lease_until_epoch) <= now:
                stale.append(str(job_id))
        if stale:
            connection.executemany(
                "DELETE FROM queue_jobs WHERE job_id=?", ((job_id,) for job_id in stale)
            )

    def _remove(self, job_id: str) -> None:
        connection = self._connect()
        try:
            connection.execute("DELETE FROM queue_jobs WHERE job_id=?", (job_id,))
        finally:
            connection.close()

    @contextmanager
    def acquire(
        self,
        *,
        online: bool,
        deadline: float,
        yield_to_online: Callable[[], bool] | None = None,
    ) -> Iterator[None]:
        """Wait until this request is the highest-priority runnable job.

        ``deadline`` is an absolute ``time.monotonic()`` value.  Online callers
        fail immediately when the bounded queue is full; offline callers wait
        for capacity until their deadline.
        """
        if deadline <= time.monotonic():
            raise InferenceQueueTimeout("inference deadline exhausted before queueing")
        priority = 0 if online else 1
        job_id = uuid.uuid4().hex
        inserted = False
        granted = False
        try:
            while True:
                monotonic_now = time.monotonic()
                if monotonic_now >= deadline:
                    raise InferenceQueueTimeout("inference deadline exhausted in queue")
                now = time.time()
                deadline_epoch = now + max(0.0, deadline - monotonic_now)
                connection = self._connect()
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    self._clean_stale(connection, now)
                    if not inserted:
                        count = int(
                            connection.execute("SELECT count(*) FROM queue_jobs").fetchone()[0]
                        )
                        if count >= self.capacity:
                            connection.rollback()
                            if online:
                                raise InferenceQueueFull("online inference queue is full")
                        else:
                            connection.execute(
                                "INSERT INTO queue_jobs("
                                "job_id,pid,priority,enqueued_ns,deadline_epoch,"
                                "lease_until_epoch,state) VALUES(?,?,?,?,?,?,?)",
                                (
                                    job_id,
                                    os.getpid(),
                                    priority,
                                    time.time_ns(),
                                    deadline_epoch,
                                    deadline_epoch + 30.0,
                                    "waiting",
                                ),
                            )
                            inserted = True
                    if inserted:
                        running = connection.execute(
                            "SELECT job_id FROM queue_jobs WHERE state='running' LIMIT 1"
                        ).fetchone()
                        first = connection.execute(
                            "SELECT job_id FROM queue_jobs WHERE state='waiting' "
                            "ORDER BY priority,enqueued_ns,job_id LIMIT 1"
                        ).fetchone()
                        if running is None and first is not None and first[0] == job_id:
                            connection.execute(
                                "UPDATE queue_jobs SET state='running',lease_until_epoch=? "
                                "WHERE job_id=?",
                                (deadline_epoch + 30.0, job_id),
                            )
                            granted = True
                    connection.commit()
                except BaseException:
                    if connection.in_transaction:
                        connection.rollback()
                    raise
                finally:
                    connection.close()
                if granted:
                    break
                if not online and inserted and yield_to_online and yield_to_online():
                    raise InferencePriorityYield("offline job yielded to a local online job")
                time.sleep(min(0.01, max(0.001, deadline - time.monotonic())))
            yield
        finally:
            if inserted:
                self._remove(job_id)
