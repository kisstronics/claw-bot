"""
Very simple interval scheduler (OpenClaw-style heartbeats).

Jobs belong to a Telegram user. Each user's jobs are stored in
data/users/<id>/schedules.json.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

from config import SCHEDULES_PATH as DEFAULT_SCHEDULES_PATH

load_dotenv()

InvokeFn = Callable[[str], str]
HandlerFn = Callable[[], str]


def load_schedules(
    path: Path | str = DEFAULT_SCHEDULES_PATH,
    *,
    include_disabled: bool = False,
) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    jobs = data.get("jobs", data if isinstance(data, list) else [])
    if include_disabled:
        return list(jobs)
    return [j for j in jobs if j.get("enabled", True)]


def save_schedules(jobs: list[dict[str, Any]], path: Path | str = DEFAULT_SCHEDULES_PATH) -> None:
    Path(path).write_text(
        json.dumps({"jobs": jobs}, indent=2) + "\n",
        encoding="utf-8",
    )


def _job_key(job: dict[str, Any]) -> str:
    return f"{job.get('user_id')}:{job.get('name')}"


def _load_user_jobs() -> list[dict[str, Any]]:
    from accounts import list_user_ids, load_user_schedules

    jobs: list[dict[str, Any]] = []
    for user_id in list_user_ids():
        for job in load_user_schedules(user_id):
            item = dict(job)
            item["user_id"] = str(user_id)
            jobs.append(item)
    return jobs


class SimpleScheduler:
    """Background daemon that runs prompts or named handlers on intervals."""

    def __init__(
        self,
        invoke: InvokeFn,
        jobs: list[dict[str, Any]] | None = None,
        schedules_path: Path | str = DEFAULT_SCHEDULES_PATH,
        handlers: dict[str, HandlerFn] | None = None,
    ) -> None:
        self._invoke = invoke
        self._handlers = handlers or {}
        self._path = Path(schedules_path)
        self._jobs = jobs if jobs is not None else _load_user_jobs()
        self._stop = threading.Event()
        self._started = False
        self._cancelled: set[str] = set()
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._last_runs: dict[str, dict[str, Any]] = {}

    @property
    def jobs(self) -> list[dict[str, Any]]:
        return list(self._jobs)

    def jobs_for(self, user_id: str) -> list[dict[str, Any]]:
        user_id = str(user_id)
        return [dict(job) for job in self._jobs if str(job.get("user_id")) == user_id]

    @property
    def last_runs(self) -> dict[str, dict[str, Any]]:
        return dict(self._last_runs)

    def last_runs_for(self, user_id: str) -> dict[str, dict[str, Any]]:
        prefix = f"{user_id}:"
        return {
            key[len(prefix) :]: value
            for key, value in self._last_runs.items()
            if key.startswith(prefix)
        }

    def start(self) -> None:
        self._started = True
        for job in self._jobs:
            if job.get("enabled", True):
                self._start_job(job)

    def load_user(self, user_id: str) -> None:
        from accounts import load_user_schedules

        user_id = str(user_id)
        with self._lock:
            if any(str(job.get("user_id")) == user_id for job in self._jobs):
                return
            fresh: list[dict[str, Any]] = []
            for job in load_user_schedules(user_id):
                item = dict(job)
                item["user_id"] = user_id
                self._jobs.append(item)
                fresh.append(item)
        for job in fresh:
            if job.get("enabled", True):
                self._start_job(job)

    def _persist_user(self, user_id: str) -> None:
        from accounts import save_user_schedules

        user_id = str(user_id)
        stored = []
        for job in self._jobs:
            if str(job.get("user_id")) != user_id:
                continue
            stored.append({key: value for key, value in job.items() if key != "user_id"})
        save_user_schedules(user_id, stored)

    def upsert_job(self, job: dict[str, Any]) -> dict[str, Any]:
        name = str(job.get("name") or "").strip()
        user_id = str(job.get("user_id") or "").strip()
        if not name:
            raise ValueError("Job name is required.")
        if not user_id:
            raise ValueError("Job user_id is required.")
        job = dict(job)
        job["name"] = name
        job["user_id"] = user_id
        job["every_seconds"] = max(5, int(job.get("every_seconds") or 300))
        job["enabled"] = bool(job.get("enabled", True))
        key = _job_key(job)
        with self._lock:
            current = next(
                (
                    item
                    for item in self._jobs
                    if item.get("name") == name and str(item.get("user_id")) == user_id
                ),
                None,
            )
            if current is None:
                self._jobs.append(job)
                current = job
            else:
                current.clear()
                current.update(job)
            self._persist_user(user_id)
        if current.get("enabled", True):
            self._cancelled.discard(key)
            self._start_job(current)
        else:
            self._cancelled.add(key)
        return dict(current)

    def delete_job(self, name: str, user_id: str) -> None:
        user_id = str(user_id)
        with self._lock:
            before = len(self._jobs)
            self._jobs = [
                job
                for job in self._jobs
                if not (job.get("name") == name and str(job.get("user_id")) == user_id)
            ]
            if len(self._jobs) == before:
                raise KeyError(f"Unknown job: {name}")
            self._persist_user(user_id)
        self._cancelled.add(f"{user_id}:{name}")

    def stop(self) -> None:
        self._stop.set()

    def run_job_once(self, name: str, user_id: str) -> str:
        user_id = str(user_id)
        job = next(
            (
                item
                for item in self._jobs
                if item.get("name") == name and str(item.get("user_id")) == user_id
            ),
            None,
        )
        if job is None:
            raise KeyError(f"Unknown job: {name}")
        return self._fire(job)

    def _start_job(self, job: dict[str, Any]) -> None:
        if not self._started:
            return
        key = _job_key(job)
        thread_name = f"schedule-{key}"
        if any(thread.name == thread_name and thread.is_alive() for thread in self._threads):
            return
        thread = threading.Thread(
            target=self._run_job_loop,
            args=(job,),
            name=thread_name,
            daemon=True,
        )
        thread.start()
        self._threads.append(thread)
        kind = job.get("handler") or "prompt"
        print(
            f"[scheduler] started `{job.get('name')}` for user {job.get('user_id')} "
            f"({kind}) every {job.get('every_seconds')}s"
        )

    def _run_job_loop(self, job: dict[str, Any]) -> None:
        key = _job_key(job)
        delay = int(job.get("initial_delay_seconds", 5))
        if delay > 0 and self._stop.wait(delay):
            return
        while not self._stop.is_set() and key not in self._cancelled:
            if not job.get("enabled", True):
                return
            try:
                self._fire(job)
            except Exception as exc:  # noqa: BLE001
                print(f"[scheduler] job `{job.get('name')}` failed: {exc}")
                self._last_runs[key] = {
                    "ok": False,
                    "error": str(exc),
                    "ts": time.time(),
                }
            interval = max(5, int(job.get("every_seconds", 300)))
            if self._stop.wait(interval):
                return

    def _fire(self, job: dict[str, Any]) -> str:
        from user_scope import reset_user, use_user

        name = job.get("name", "job")
        key = _job_key(job)
        handler_name = (job.get("handler") or "").strip()
        print(f"[scheduler] firing `{name}` for user {job.get('user_id')}")
        token = use_user(str(job.get("user_id") or "") or None)
        try:
            if handler_name:
                handler = self._handlers.get(handler_name)
                if handler is None:
                    raise KeyError(f"No handler registered for '{handler_name}'")
                reply = handler()
            else:
                prompt = job.get("prompt") or (
                    "You are running on a schedule. Use your tools if needed, "
                    "then briefly report what you found and any actions taken."
                )
                reply = self._invoke(prompt)
        finally:
            reset_user(token)

        self._last_runs[key] = {
            "ok": True,
            "reply": reply[:1000],
            "ts": time.time(),
        }
        print(f"[scheduler] `{name}` done: {_preview(reply)}")
        return reply


def _preview(text: str, limit: int = 200) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"
