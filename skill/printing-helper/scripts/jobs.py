"""Job directories, specs and audit logging."""

from __future__ import annotations

import json
import re
import secrets
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from errors import InputError
from registry import data_dir

_JOB_ID_RE = re.compile(r"^[0-9A-Za-z._-]+$")


def new_job_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(2)}"


def jobs_root() -> Path:
    root = data_dir() / "jobs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def validate_job_id(job_id: str) -> str:
    if not _JOB_ID_RE.fullmatch(job_id or ""):
        raise InputError(f"非法 job id: {job_id}")
    return job_id


def job_dir(job_id: str) -> Path:
    path = jobs_root() / validate_job_id(job_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def spec_path(job_id: str) -> Path:
    return job_dir(job_id) / "spec.json"


def save_spec(spec: dict) -> Path:
    path = spec_path(spec["job_id"])
    path.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return path


def load_spec(job_id: str) -> dict:
    path = spec_path(job_id)
    if not path.exists():
        raise InputError(f"找不到作业: {job_id}")
    return json.loads(path.read_text("utf-8"))


def audit(record: dict) -> None:
    record = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"), **record}
    path = data_dir() / "jobs.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def list_jobs(limit: int = 20) -> list[dict]:
    path = data_dir() / "jobs.jsonl"
    if not path.exists():
        return []
    lines = path.read_text("utf-8").splitlines()[-limit:]
    jobs = []
    for line in lines:
        try:
            jobs.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return jobs


def cleanup(days: int = 7) -> int:
    """Remove job directories older than N days; audit log is kept."""
    cutoff = datetime.now() - timedelta(days=days)
    removed = 0
    for child in jobs_root().iterdir():
        if not child.is_dir():
            continue
        try:
            mtime = datetime.fromtimestamp(child.stat().st_mtime)
        except OSError:
            continue
        if mtime < cutoff:
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
    return removed