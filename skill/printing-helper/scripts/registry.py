"""Persistent printer registry and user preferences.

The registry is deliberately plain JSON: it is the single source of truth for
printer aliases, transports and defaults, independent of any LLM conversation
history.  Memory across sessions is just reading this file again.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from errors import PrinterError, UsageError

APP_NAME = "printing-helper"
SCHEMA_VERSION = 2

DEFAULT_PREFS: dict[str, Any] = {
    "paper": "A4",
    "margin": "normal",
    "image_layout": "one-per-page",
    "copies": 1,
    "duplex": False,
    "confirm_before_print": True,
}

DEFAULT_WORDS = {"", "default", "默认", "默认打印机", "本机", "current"}
LAST_WORDS = {"last", "上次", "上次那台", "上回", "上一次", "上一个"}


def data_dir() -> Path:
    """Runtime data directory (overridable with PH_DATA_DIR)."""
    raw = os.environ.get("PH_DATA_DIR")
    if raw:
        path = Path(raw).expanduser()
    else:
        xdg = os.environ.get("XDG_DATA_HOME")
        base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
        path = base / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _printers_path() -> Path:
    return data_dir() / "printers.json"


def _prefs_path() -> Path:
    return data_dir() / "preferences.json"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as exc:
        raise PrinterError(f"配置文件损坏: {path}: {exc}") from exc


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")
    tmp.replace(path)


def load_printers() -> dict:
    data = _read_json(_printers_path(), {"version": 1, "default": None, "printers": {}})
    return _migrate(data)


def _migrate(data: dict) -> dict:
    """Bring older registries up to the profile schema in memory."""
    data.setdefault("version", 1)
    data.setdefault("default", None)
    data.setdefault("printers", {})
    data["schema_version"] = SCHEMA_VERSION
    for printer in data["printers"].values():
        printer.setdefault("identity", {
            "model": printer.get("model"),
            "vendor": None,
            "serial": None,
            "device_id": None,
            "mac": None,
            "location": printer.get("location"),
        })
        printer.setdefault("streams", {})
        printer.setdefault("preferred_stream", printer.get("raw_format") or "pdf")
        printer.setdefault("generator", None)
        printer.setdefault("detection", None)
        printer.setdefault("history", {
            "last_used": printer.get("last_used"),
            "last_success": None,
            "last_error": None,
        })
    return data


def save_printers(data: dict) -> None:
    _write_json(_printers_path(), data)


def load_prefs() -> dict:
    prefs = dict(DEFAULT_PREFS)
    prefs.update(_read_json(_prefs_path(), {}))
    return prefs


def save_prefs(prefs: dict) -> None:
    _write_json(_prefs_path(), prefs)


def set_pref(key: str, value: str) -> Any:
    """Persist a preference, coercing the CLI string to the default's type."""
    prefs = load_prefs()
    if key not in DEFAULT_PREFS:
        raise UsageError(f"未知偏好: {key}(可用: {', '.join(DEFAULT_PREFS)})")
    current = prefs[key]
    if isinstance(current, bool):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "是", "开"}:
            coerced: Any = True
        elif lowered in {"0", "false", "no", "off", "否", "关"}:
            coerced = False
        else:
            raise UsageError(f"{key} 需要布尔值(true/false)")
    elif isinstance(current, int):
        try:
            coerced = int(value)
        except ValueError as exc:
            raise UsageError(f"{key} 需要整数") from exc
    else:
        coerced = value
    prefs[key] = coerced
    save_prefs(prefs)
    return coerced


def slugify(value: str, fallback: str = "printer") -> str:
    slug = re.sub(r"\s+", "-", value.strip().lower())
    slug = re.sub(r"[^0-9a-z一-鿿._-]+", "-", slug).strip("-._")
    return slug or fallback


def make_slot(transport: str, host: str | None, port: int | None, queue: str | None,
              aliases: Iterable[str]) -> str:
    if transport == "socket" and host:
        suffix = "-".join(host.split("."))
        if port and port != 9100:
            suffix += f"-{port}"
        return f"net-{suffix}"
    if transport in {"cups", "cups-remote"} and queue:
        return f"cups-{slugify(queue)}"
    if transport.startswith("ipp") and host:
        return "ipp-" + "-".join(host.split("."))
    first = next((a for a in aliases if a), None)
    return slugify(first or "printer")


def _norm(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def _iter_fields(printer: dict) -> list[str]:
    fields = [printer.get("queue"), printer.get("host"), printer.get("model"),
              printer.get("location"), printer.get("uri")]
    fields.extend(printer.get("aliases", []))
    return [f for f in fields if isinstance(f, str) and f.strip()]


def _summary(slot: str, printer: dict) -> dict:
    return {
        "slot": slot,
        "aliases": printer.get("aliases", []),
        "transport": printer.get("transport"),
        "host": printer.get("host"),
        "port": printer.get("port"),
        "queue": printer.get("queue"),
        "model": printer.get("model"),
        "location": printer.get("location"),
        "paper": printer.get("paper"),
        "raw_format": printer.get("raw_format"),
    }


def resolve(data: dict, query: str | None) -> dict:
    """Resolve a natural-language reference to a printer slot.

    Returns ``{"chosen": slot|None, "matches": [...], "reason": str}``.
    Never guesses when multiple printers match.
    """
    printers = data.get("printers", {})
    q = (query or "").strip()

    if q in DEFAULT_WORDS:
        slot = data.get("default")
        if slot and slot in printers:
            return {"chosen": slot, "matches": [_summary(slot, printers[slot])],
                    "reason": "默认打印机"}
        return {"chosen": None, "matches": [], "reason": "尚未设置默认打印机"}

    if q in LAST_WORDS:
        used = [(p.get("last_used") or "", slot, p) for slot, p in printers.items()]
        used = [item for item in used if item[0]]
        if used:
            _, slot, printer = max(used)
            return {"chosen": slot, "matches": [_summary(slot, printer)],
                    "reason": "最近使用"}
        slot = data.get("default")
        if slot and slot in printers:
            return {"chosen": slot, "matches": [_summary(slot, printers[slot])],
                    "reason": "无最近使用记录,回退到默认打印机"}

    if q in printers:
        return {"chosen": q, "matches": [_summary(q, printers[q])], "reason": "slot 精确匹配"}

    nq = _norm(q)
    exact = [slot for slot, printer in printers.items()
             if any(_norm(field) == nq for field in _iter_fields(printer))]
    if len(exact) == 1:
        slot = exact[0]
        return {"chosen": slot, "matches": [_summary(slot, printers[slot])],
                "reason": "别名精确匹配"}
    if len(exact) > 1:
        return {"chosen": None, "matches": [_summary(s, printers[s]) for s in exact],
                "reason": "多个打印机精确匹配,需要用户选择"}

    partial = [slot for slot, printer in printers.items()
               if any(nq and nq in _norm(field) for field in _iter_fields(printer))]
    if len(partial) == 1:
        slot = partial[0]
        return {"chosen": slot, "matches": [_summary(slot, printers[slot])],
                "reason": "别名包含匹配"}
    return {"chosen": None, "matches": [_summary(s, printers[s]) for s in partial],
            "reason": "多个候选,需要用户选择" if partial else "未找到匹配的打印机"}


def get_printer(data: dict, reference: str | None) -> tuple[str, dict]:
    result = resolve(data, reference)
    chosen = result.get("chosen")
    if chosen:
        return chosen, data["printers"][chosen]
    if result["matches"]:
        names = ", ".join(m["slot"] for m in result["matches"])
        raise PrinterError(f"打印机指代不唯一: {names}", payload={"matches": result["matches"]})
    raise PrinterError(
        "未找到匹配的打印机。可先用 `printer discover` 查看,或用 `printer add` 添加。",
        payload={"query": reference},
    )


def add_printer(data: dict, *, aliases: Iterable[str], transport: str,
                host: str | None = None, port: int | None = None,
                queue: str | None = None, uri: str | None = None,
                slot: str | None = None, model: str | None = None,
                location: str | None = None, paper: str | None = None,
                raw_format: str | None = None, pjl: bool | None = None,
                capabilities: dict | None = None, notes: str | None = None,
                make_default: bool = False) -> tuple[str, dict]:
    alias_list = [a.strip() for a in aliases if a and a.strip()]
    slot = slot or make_slot(transport, host, port, queue, alias_list)
    existing = data["printers"].get(slot, {})
    paper = paper or existing.get("paper") or "A4"
    raw_format = raw_format or existing.get("raw_format") or "pdf"
    pjl = bool(existing.get("pjl", False) if pjl is None else pjl)
    merged_aliases = list(dict.fromkeys(
        [slot] + alias_list + existing.get("aliases", [])
        + [x for x in (queue, host, model) if x]
    ))
    printer = {
        "aliases": merged_aliases,
        "transport": transport,
        "host": host or existing.get("host"),
        "port": port or existing.get("port"),
        "queue": queue or existing.get("queue"),
        "uri": uri or existing.get("uri"),
        "model": model or existing.get("model"),
        "location": location or existing.get("location"),
        "paper": paper,
        "raw_format": raw_format,
        "pjl": pjl,
        "capabilities": capabilities or existing.get("capabilities")
        or {"media": [paper], "duplex": False, "color": None},
        "defaults": existing.get("defaults") or {"margin": "normal", "copies": 1},
        "identity": existing.get("identity") or {
            "model": model or existing.get("model"),
            "vendor": None, "serial": None, "device_id": None, "mac": None,
            "location": location or existing.get("location"),
        },
        "streams": existing.get("streams") or {},
        "preferred_stream": raw_format,
        "generator": existing.get("generator"),
        "detection": existing.get("detection"),
        "history": existing.get("history") or {
            "last_used": existing.get("last_used"),
            "last_success": None, "last_error": None,
        },
        "added_at": existing.get("added_at") or _now(),
        "last_used": existing.get("last_used"),
        "notes": notes or existing.get("notes"),
    }
    data["printers"][slot] = printer
    if make_default or not data.get("default"):
        data["default"] = slot
    save_printers(data)
    return slot, printer


def set_default(data: dict, reference: str) -> tuple[str, dict]:
    slot, printer = get_printer(data, reference)
    data["default"] = slot
    save_printers(data)
    return slot, printer


def remove_printer(data: dict, reference: str) -> str:
    slot, _ = get_printer(data, reference)
    data["printers"].pop(slot, None)
    if data.get("default") == slot:
        data["default"] = next(iter(data["printers"]), None)
    save_printers(data)
    return slot


def touch(data: dict, slot: str) -> None:
    if slot in data.get("printers", {}):
        now = _now()
        printer = data["printers"][slot]
        printer["last_used"] = now
        printer.setdefault("history", {})["last_used"] = now
        save_printers(data)


def record_stream_result(data: dict, slot: str, stream: str, *, ok: bool,
                         error: str | None = None,
                         evidence: str | None = None) -> dict:
    """Persist the outcome of a stream attempt (verified/failed)."""
    printer = data["printers"][slot]
    record = printer.setdefault("streams", {}).setdefault(stream, {})
    record["status"] = "verified" if ok else "failed"
    record["attempted_at"] = _now()
    if ok:
        record["verified_at"] = _now()
        record.pop("error", None)
        printer["preferred_stream"] = stream
    else:
        record["error"] = error or "unknown"
    if evidence:
        record["evidence"] = evidence
    history = printer.setdefault("history", {})
    if ok:
        history["last_success"] = _now()
    else:
        history["last_error"] = error or "unknown"
    save_printers(data)
    return record


def set_stream_status(data: dict, slot: str, stream: str, status: str,
                      error: str | None = None) -> dict:
    printer = data["printers"][slot]
    record = printer.setdefault("streams", {}).setdefault(stream, {})
    record["status"] = status
    record["updated_at"] = _now()
    if error:
        record["error"] = error
    save_printers(data)
    return record


def parse_uri(uri: str) -> dict:
    """Parse a printer URI into transport fields (no network calls)."""
    parts = urlsplit(uri.strip())
    scheme = parts.scheme.lower()
    host = parts.hostname
    try:
        port = parts.port
    except ValueError as exc:
        raise UsageError(f"URI 端口非法: {uri}") from exc
    path = parts.path or ""

    if not scheme or not host:
        raise UsageError(f"无法解析打印机 URI: {uri}")

    if scheme == "socket":
        return {"transport": "socket", "host": host, "port": port or 9100, "uri": uri}
    if scheme in {"ipp", "ipps"}:
        default_port = 631
        if "/printers/" in path:
            queue = path.rsplit("/printers/", 1)[1].strip("/")
            return {"transport": "cups-remote", "host": host,
                    "port": port or default_port, "queue": queue, "uri": uri}
        return {"transport": "ipp", "host": host, "port": port or default_port,
                "queue": None, "uri": uri}
    if scheme == "lpd":
        queue = path.strip("/") or None
        return {"transport": "lpd", "host": host, "port": port or 515,
                "queue": queue, "uri": uri}
    if scheme in {"usb", "hp", "hpfax", "file", "cups-brf"}:
        return {"transport": "cups", "host": None, "port": None,
                "queue": None, "uri": uri}
    raise UsageError(f"不支持的 URI scheme: {scheme}(支持 socket/ipp/ipps/lpd/usb/hp)")