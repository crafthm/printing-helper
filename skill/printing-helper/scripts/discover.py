"""Printer discovery and capability probing (read-only).

Nothing here sends print data.  Network discovery only performs TCP connects,
``ippfind`` queries and mDNS browsing, which do not produce paper.
"""

from __future__ import annotations

import re
import socket
import subprocess
from typing import Any

from errors import PrinterError


def _run(cmd: list[str], timeout: float = 8.0) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, check=False)
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        return 127, "", f"{cmd[0]} 未安装"
    except subprocess.TimeoutExpired:
        return 124, "", f"{cmd[0]} 超时"


def local_cups() -> list[dict]:
    """List CUPS queues with their device URIs and states."""
    code, out, err = _run(["lpstat", "-v"])
    if code != 0:
        raise PrinterError(f"无法读取 CUPS 队列: {err.strip() or out.strip()}")

    devices: dict[str, str] = {}
    for line in out.splitlines():
        match = re.match(r"^device for (.+?):\s*(\S+)\s*$", line.strip())
        if match:
            devices[match.group(1)] = match.group(2)

    states: dict[str, str] = {}
    _, pout, _ = _run(["lpstat", "-p"])
    for line in pout.splitlines():
        match = re.match(r"^printer (\S+) is (.+)$", line.strip())
        if match:
            states[match.group(1)] = match.group(2).strip()

    return [
        {"queue": queue, "uri": uri, "state": states.get(queue, "unknown"),
         "transport": "cups"}
        for queue, uri in devices.items()
    ]


def probe_queue(queue: str) -> dict:
    """Read driver capabilities for a CUPS queue via lpoptions."""
    code, out, err = _run(["lpoptions", "-p", queue, "-l"])
    if code != 0:
        return {"ok": False, "error": (err or out).strip()}
    options: dict[str, list[str]] = {}
    for line in out.splitlines():
        if ":" not in line:
            continue
        left, right = line.split(":", 1)
        name = left.split("/")[0].strip()
        values = []
        for token in right.split():
            values.append(token.lstrip("*"))
        options[name] = values

    media = options.get("PageSize") or options.get("media") or []
    sides = options.get("Duplex") or options.get("sides") or []
    color_values = options.get("ColorModel") or options.get("print-color-mode") or []
    resolution = options.get("Resolution") or []
    duplex = any("two-sided" in value.lower() or value.lower() == "duplex"
                 for value in sides)
    if color_values:
        color = any(value.lower() not in {"gray", "monochrome", "black"}
                    for value in color_values)
    else:
        color = None
    return {
        "ok": True,
        "media": media,
        "duplex": duplex,
        "color": color,
        "resolution": resolution,
        "options": options,
    }


def probe_socket(host: str, port: int, timeout: float = 3.0) -> dict:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return {"ok": True, "reachable": True, "host": host, "port": port}
    except OSError as exc:
        return {"ok": False, "reachable": False, "host": host, "port": port,
                "error": f"{type(exc).__name__}: {exc}"}
    finally:
        sock.close()


def discover_network(timeout: float = 5.0) -> list[dict]:
    """Best-effort mDNS / ippfind discovery; returns candidate URIs only."""
    found: dict[str, dict] = {}

    code, out, _ = _run(["ippfind", "-T", str(int(timeout))], timeout=timeout + 3)
    if code == 0:
        for line in out.splitlines():
            uri = line.strip()
            if uri.startswith(("ipp://", "ipps://")):
                found.setdefault(uri, {"uri": uri, "name": None, "source": "ippfind"})

    code, out, _ = _run(["avahi-browse", "-rt", "_ipp._tcp"], timeout=timeout + 3)
    if code == 0:
        for line in out.splitlines():
            if not line.startswith("="):
                continue
            fields = line.split(";")
            if len(fields) < 9:
                continue
            try:
                name, host, address, port = fields[3], fields[6], fields[7], fields[8]
            except IndexError:
                continue
            if not re.match(r"^\d+\.\d+\.\d+\.\d+$", address):
                continue
            uri = f"ipp://{address}:{port or 631}/ipp/print"
            found.setdefault(uri, {"uri": uri, "name": name, "source": "avahi",
                                   "host": host})

    return list(found.values())


def discover() -> dict[str, Any]:
    result: dict[str, Any] = {"local": [], "network": [], "warnings": []}
    try:
        result["local"] = local_cups()
    except PrinterError as exc:
        result["warnings"].append(str(exc))
    try:
        result["network"] = discover_network()
    except Exception as exc:  # discovery must never break the CLI
        result["warnings"].append(f"网络发现失败: {exc}")
    return result