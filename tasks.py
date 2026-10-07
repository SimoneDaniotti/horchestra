"""Team tasks: what the orchestrator assigned to whom, and how it ended.

Stored per project in `.orchestra/tasks.json` (next to the role profiles),
not in team.toml: tasks change often and are written by members too, and
rewriting team.toml would drop the human's comments. Every change happens
under an exclusive lock, so two members finishing at once cannot lose an
update.

A task is {id, role, text, state, created, updated, summary, nudged}; state
is "open", "done", "blocked" or "cancelled". Blocked tasks stay listed until
the orchestrator reassigns or cancels them.
"""

import contextlib
import fcntl
import json
import os
import time

FOLDER = ".orchestra"
FILE = "tasks.json"
STATES = ("open", "done", "blocked", "cancelled")
ACTIVE = ("open", "blocked")


class TaskError(Exception):
    pass


def path_for(root):
    return os.path.join(root, FOLDER, FILE)


def _empty():
    return {"next_id": 1, "tasks": []}


def _read(path):
    try:
        with open(path) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return _empty()
    except (OSError, ValueError) as exc:
        raise TaskError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        raise TaskError(f"{path} is not a Horchestra task file")
    data["tasks"] = [t for t in data["tasks"] if isinstance(t, dict) and isinstance(t.get("id"), int)]
    top = max([t["id"] for t in data["tasks"]] + [0])
    data["next_id"] = max(top + 1, data.get("next_id") if isinstance(data.get("next_id"), int) else 1)
    return data


def load(root):
    """The task file's contents (read-only; no lock)."""
    return _read(path_for(root))


@contextlib.contextmanager
def editing(root):
    """Lock, read, yield the data for changes, then write it back atomically."""
    path = path_for(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data = _read(path)
            yield data
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "w") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.replace(tmp, path)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def stamp(now=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if now is None else now))


def add(root, role, text, now=None):
    text = " ".join(str(text).split())
    if not text:
        raise TaskError("a task needs a description")
    with editing(root) as data:
        task = {"id": data["next_id"], "role": role, "text": text, "state": "open",
                "created": stamp(now), "updated": stamp(now)}
        data["next_id"] += 1
        data["tasks"].append(task)
    return task


def find(data, task_id):
    return next((t for t in data["tasks"] if t["id"] == task_id), None)


def open_for(data, role, states=("open",)):
    return [t for t in data["tasks"] if t.get("role", "").lower() == role.lower() and t.get("state") in states]


def close(root, task_id, state, summary="", now=None):
    """Set a task's final state; returns the updated task."""
    if state not in STATES or state == "open":
        raise TaskError(f"cannot set a task to {state!r}")
    with editing(root) as data:
        task = find(data, task_id)
        if task is None:
            raise TaskError(f"no task #{task_id}")
        task["state"] = state
        task["summary"] = " ".join(str(summary).split())
        task["updated"] = stamp(now)
        return dict(task)


def mark_nudged(root, task_ids, now=None):
    with editing(root) as data:
        for task in data["tasks"]:
            if task["id"] in task_ids:
                task["nudged"] = int(time.time() if now is None else now)


def line(task, width=60):
    text = task.get("text", "")
    text = text if len(text) <= width else text[: width - 1] + "…"
    return f"#{task['id']} {task.get('role', '?')}: {text}"
