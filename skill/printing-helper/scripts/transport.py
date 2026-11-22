"""Print transports: raw socket 9100 and CUPS/IPP via ``lp``.

Safety: ``dry_run`` never opens a network connection.  Real sends are only
reachable through the CLI after explicit ``--yes``.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from converters import run_cmd
from errors import SendError
import discover

RAW_FORMATS = {"pdf", "pcl", "pcl-color", "ps", "zjs"}
GS_DEVICES = {"pcl": "ljet4", "pcl-color": "pxlcolor", "ps": "ps2write"}
PJL_LANGUAGE = {"pdf": "PDF", "pcl": "PCL", "pcl-color": "PCLXL",
                "ps": "POSTSCRIPT"}
# foo2zjs codes for the -z1 family (HP LJ 1018/1020/1022/...).
ZJS_PAPER_CODES = {"Letter": 1, "Legal": 5, "Executive": 7, "A4": 9, "A5": 11,
                   "B5": 13, "A6": 262}
ZJS_SOURCE_CODES = {"auto": 7, "upper": 1, "lower": 2, "manual": 4}


def _safe_title(title: str | None, fallback: str) -> str:
    text = (title or fallback or "printing-helper").replace("\n", " ").strip()
    return text[:120] or "printing-helper"


def _job_state(printer: dict, job_id: str, timeout: float) -> tuple[str, list[str]]:
    warnings: list[str] = []
    queue = printer.get("queue")
    if not queue:
        return "unknown", ["CUPS 打印机缺少 queue,无法查询状态"]
    base = ["lpstat"]
    if printer.get("transport") == "cups-remote":
        base += ["-h", f"{printer.get('host')}:{printer.get('port') or 631}"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        proc = subprocess.run(base + ["-o", queue], capture_output=True,
                              text=True, check=False)
        if job_id in proc.stdout:
            time.sleep(2)
            continue
        state = subprocess.run(base + ["-p", queue], capture_output=True,
                               text=True, check=False).stdout.casefold()
        if "disabled" in state:
            return "queue-disabled", warnings
        if "now printing" in state or "processing" in state:
            time.sleep(2)
            continue
        return "completed", warnings
    warnings.append("等待超时:作业已提交,但未确认完成")
    return "unknown", warnings


def send_cups(pdf_path: Path, printer: dict, *, copies: int, duplex: bool,
              quality: str, dry_run: bool, no_wait: bool, title: str | None,
              wait_timeout: float = 120.0) -> dict:
    queue = printer.get("queue")
    if not queue:
        raise SendError("CUPS 打印机缺少 queue 名称")
    caps = printer.get("capabilities") or {}
    warnings: list[str] = []

    cmd = ["lp"]
    if printer.get("transport") == "cups-remote":
        host = printer.get("host")
        port = printer.get("port") or 631
        if not host:
            raise SendError("远端 CUPS 打印机缺少 host")
        cmd += ["-h", f"{host}:{port}"]
    cmd += ["-d", queue, "-t", _safe_title(title, pdf_path.stem),
            "-n", str(max(1, int(copies)))]
    paper = printer.get("paper") or "A4"
    cmd += ["-o", f"media={paper}", "-o", "NupPages=1up"]
    if duplex:
        if caps.get("duplex"):
            cmd += ["-o", "sides=two-sided-long-edge"]
        else:
            warnings.append("打印机未报告双面能力,已忽略 --duplex")
    if quality in {"draft", "normal"}:
        cmd += ["-o", f"Quality={quality}"]
    elif quality and quality != "high":
        warnings.append(f"忽略未知质量选项: {quality}")
    cmd.append(str(pdf_path))

    if dry_run:
        return {"sent": False, "dry_run": True, "transport": "cups",
                "queue": queue, "command": cmd, "warnings": warnings}

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                          check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise SendError(f"lp 发送失败: {detail[:500]}", payload={"command": cmd})
    output = (proc.stdout or "") + (proc.stderr or "")
    match = re.search(r"request id is (\S+)", output)
    job_id = match.group(1) if match else None
    if not job_id:
        warnings.append("未从 lp 输出解析到作业号")
    if no_wait or not job_id:
        state = "submitted"
    else:
        state, more = _job_state(printer, job_id, wait_timeout)
        warnings.extend(more)
    return {"sent": True, "dry_run": False, "transport": "cups", "queue": queue,
            "job_id": job_id, "job_status": state, "warnings": warnings}


def _pdf_to_zjs(pdf_path: Path, printer: dict, *, paper_name: str,
                duplex: bool) -> bytes:
    """PDF -> PS -> ZJS via foo2zjs-wrapper (same filter CUPS uses)."""
    wrapper = shutil.which("foo2zjs-wrapper")
    if not wrapper:
        raise SendError(
            "未安装 foo2zjs-wrapper;ZJS 机型需要 printer-driver-foo2zjs。"
            "Ubuntu: sudo apt install printer-driver-foo2zjs")
    paper_code = ZJS_PAPER_CODES.get(paper_name)
    if paper_code is None:
        raise SendError(
            f"ZJS 暂不支持纸张 {paper_name};可用: {', '.join(ZJS_PAPER_CODES)}")
    model = str(printer.get("zjs_model") or "z1").lower().lstrip("z")
    resolution = str(printer.get("zjs_resolution") or "1200x600")
    source = ZJS_SOURCE_CODES.get(str(printer.get("zjs_source") or "auto"), 7)
    density = int(printer.get("zjs_density") or 3)
    duplex_code = 2 if duplex else 1

    with tempfile.TemporaryDirectory(prefix="ph-zjs-") as tempdir:
        ps_path = Path(tempdir) / "in.ps"
        run_cmd([
            "gs", "-q", "-dBATCH", "-dNOPAUSE", "-dSAFER",
            "-sDEVICE=ps2write", f"-sOutputFile={ps_path}", str(pdf_path),
        ], timeout=180, what="PDF→PostScript")
        proc = subprocess.run([
            wrapper, f"-z{model}", "-P", "-L0",
            f"-p{paper_code}", f"-r{resolution}", "-m1",
            f"-s{source}", f"-d{duplex_code}", f"-T{density}", str(ps_path),
        ], capture_output=True, timeout=180, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip()
        raise SendError(f"foo2zjs 转换失败 (exit {proc.returncode}): {detail[:500]}")
    payload = proc.stdout
    if not payload or not (payload.startswith(b"\x1b%-12345X")
                           or payload.startswith(b"JZJZ")):
        raise SendError("foo2zjs 输出不是有效的 ZJS 流")
    return payload


def _pdf_to_raw(pdf_path: Path, raw_format: str, *, printer: dict | None = None,
                paper_name: str = "A4", duplex: bool = False) -> bytes:
    if raw_format == "pdf":
        return pdf_path.read_bytes()
    if raw_format == "zjs":
        return _pdf_to_zjs(pdf_path, printer or {}, paper_name=paper_name,
                           duplex=duplex)
    device = GS_DEVICES.get(raw_format)
    if not device:
        raise SendError(f"不支持的 raw 格式: {raw_format}")
    suffix = ".ps" if raw_format == "ps" else ".pcl"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        temp_path = Path(handle.name)
    try:
        run_cmd([
            "gs", "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER",
            f"-sDEVICE={device}", "-r600",
            f"-sOutputFile={temp_path}", str(pdf_path),
        ], timeout=120, what="Ghostscript 转换")
        return temp_path.read_bytes()
    finally:
        temp_path.unlink(missing_ok=True)


def _wrap_pjl(payload: bytes, language: str, paper: str) -> bytes:
    paper = re.sub(r"[^A-Za-z0-9]", "", paper or "A4") or "A4"
    header = (
        b'\x1b%-12345X@PJL JOB NAME="printing-helper"\r\n'
        + f"@PJL SET PAPER={paper}\r\n".encode("ascii")
        + f"@PJL ENTER LANGUAGE={language}\r\n".encode("ascii")
    )
    tail = b"\r\n\x1b%-12345X@PJL EOJ\r\n"
    return header + payload + tail


def send_socket(pdf_path: Path, printer: dict, *, copies: int, dry_run: bool,
                title: str | None, raw_format: str | None = None,
                pjl: bool | None = None, paper_name: str | None = None,
                duplex: bool = False, timeout: float = 30.0) -> dict:
    host = printer.get("host")
    port = int(printer.get("port") or 9100)
    if not host:
        raise SendError("socket 打印机缺少 host")
    fmt = (raw_format or printer.get("raw_format") or "pdf").lower()
    if fmt not in RAW_FORMATS:
        raise SendError(f"不支持的 raw 格式: {fmt}(可用: {', '.join(sorted(RAW_FORMATS))})")
    use_pjl = printer.get("pjl", False) if pjl is None else bool(pjl)
    if fmt == "zjs":
        use_pjl = False  # foo2zjs-wrapper already emits the PJL wrapper
    paper = paper_name or printer.get("paper") or "A4"

    payload = _pdf_to_raw(pdf_path, fmt, printer=printer, paper_name=paper,
                          duplex=duplex)
    if use_pjl:
        payload = _wrap_pjl(payload, PJL_LANGUAGE[fmt], paper)

    info = {"transport": "socket", "target": f"{host}:{port}", "format": fmt,
            "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
            "magic": payload[:4].hex(), "pjl": use_pjl}
    if dry_run:
        return {"sent": False, "dry_run": True, **info,
                "copies": max(1, int(copies))}

    total = max(1, int(copies))
    for number in range(total):
        try:
            with socket.create_connection((host, port), timeout=10) as sock:
                sock.settimeout(timeout)
                sock.sendall(payload)
                try:
                    sock.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
        except OSError as exc:
            raise SendError(
                f"发送到 {host}:{port} 失败(第 {number + 1}/{total} 份): {exc}",
                payload={**info, "sent_copies": number}) from exc
        if number + 1 < total:
            time.sleep(0.3)
    return {"sent": True, "dry_run": False, **info, "copies": total,
            "job_status": "unknown",
            "note": "raw socket 无作业状态接口;请检查打印机面板/出纸"}


def probe(printer: dict) -> dict:
    transport = printer.get("transport")
    if transport == "socket":
        return discover.probe_socket(printer.get("host"), int(printer.get("port") or 9100))
    if transport in {"cups", "cups-remote"}:
        queue = printer.get("queue")
        base = ["lpstat"]
        if transport == "cups-remote":
            base += ["-h", f"{printer.get('host')}:{printer.get('port') or 631}"]
        proc = subprocess.run(base + ["-p", queue], capture_output=True,
                              text=True, check=False)
        return {"ok": proc.returncode == 0, "queue": queue,
                "state": (proc.stdout or proc.stderr).strip()}
    return {"ok": False, "error": f"暂不支持探测 transport={transport}"}


def send_pdf(pdf_path: str | Path, printer: dict, *, copies: int = 1,
             duplex: bool = False, quality: str = "normal",
             dry_run: bool = False, no_wait: bool = False,
             title: str | None = None, raw_format: str | None = None,
             pjl: bool | None = None, paper_name: str | None = None,
             wait_timeout: float = 120.0) -> dict:
    path = Path(pdf_path)
    if not path.exists():
        raise SendError(f"PDF 不存在: {path}")
    if path.stat().st_size == 0:
        raise SendError(f"PDF 为空: {path}")

    transport = printer.get("transport")
    if transport == "socket":
        return send_socket(path, printer, copies=copies, dry_run=dry_run,
                           title=title, raw_format=raw_format, pjl=pjl,
                           paper_name=paper_name, duplex=duplex)
    if transport in {"cups", "cups-remote"}:
        return send_cups(path, printer, copies=copies, duplex=duplex,
                         quality=quality, dry_run=dry_run, no_wait=no_wait,
                         title=title, wait_timeout=wait_timeout)
    if transport == "ipp":
        raise SendError(
            "直连 IPP 打印尚未实现。请先注册到 CUPS: "
            f"lpadmin -p <name> -E -v {printer.get('uri')} -m everywhere")
    if transport == "lpd":
        raise SendError(
            "LPD 直发尚未实现。请先注册到 CUPS: "
            f"lpadmin -p <name> -E -v {printer.get('uri')} -m everywhere")
    raise SendError(f"未知 transport: {transport}")