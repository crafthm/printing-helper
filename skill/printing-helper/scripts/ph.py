#!/usr/bin/env python3
"""printing-helper CLI.

Deterministic work lives here; the LLM only decides *what* the user wants.
All commands support ``--json`` for stable machine-readable output.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import compose  # noqa: E402
import detect  # noqa: E402
import discover  # noqa: E402
import jobs  # noqa: E402
import registry  # noqa: E402
import transport  # noqa: E402
from converters import MM_TO_PT, parse_margins, parse_paper  # noqa: E402
from errors import (InputError, NeedsConfirmation, PhError, UsageError)  # noqa: E402

JSON_MODE = False
VERSION = "1.0.0"


# ---------------------------------------------------------------- formatting

def _describe_target(printer: dict) -> str:
    transport_name = printer.get("transport")
    if transport_name == "socket":
        return f"{printer.get('host')}:{printer.get('port') or 9100}"
    if transport_name in {"cups", "cups-remote"}:
        prefix = "cups"
        if transport_name == "cups-remote":
            prefix = f"cups@{printer.get('host')}:{printer.get('port') or 631}"
        return f"{prefix}:{printer.get('queue')}"
    return printer.get("uri") or transport_name or "?"


def _human_printer_list(payload: dict) -> str:
    rows = []
    for item in payload.get("printers", []):
        mark = "*" if item["slot"] == payload.get("default") else " "
        aliases = " / ".join(item.get("aliases", [])[:4])
        rows.append(f"{mark} {item['slot']:<24} {item.get('transport',''):<12} "
                    f"{_describe_target(item):<28} {item.get('paper','')}  {aliases}")
    if not rows:
        return "尚未登记打印机。用 `ph.py printer discover` 或 `printer add` 添加。"
    header = "  SLOT                     TRANSPORT    TARGET                       PAPER  ALIASES"
    return header + "\n" + "\n".join(rows)


def _human_resolve(payload: dict) -> str:
    if payload.get("chosen"):
        item = payload["matches"][0]
        return (f"选定: {item['slot']} ({item.get('transport')} "
                f"{_describe_target(item)}) — {payload.get('reason')}")
    if payload.get("matches"):
        names = ", ".join(item["slot"] for item in payload["matches"])
        return f"不唯一: {names} — 请向用户确认具体打印机"
    return "未找到匹配的打印机"


def _human_spec(spec: dict, *, show_next: bool = True) -> str:
    printer = spec["printer"]
    margin = spec["margin"]
    pt = margin["pt"]
    units = [round(value / MM_TO_PT, 1) for value in
             (pt["top"], pt["right"], pt["bottom"], pt["left"])]
    margin_text = (f"{margin['mode']}({units[0]}/{units[1]}/{units[2]}/{units[3]}mm)"
                   if margin["mode"] == "custom" else
                   f"{margin['mode']}({units[0]}mm)")
    lines = [
        f"打印机: {printer.get('slot')}  [{printer.get('transport')} "
        f"{_describe_target(printer)}]",
        f"纸张: {spec['paper']['name']} · 边距: {margin_text} · "
        f"方向: {spec['orientation']} · 份数: {spec['copies']}",
        "内容:",
    ]
    for item in spec["outputs"]:
        if item["kind"] == "image":
            name = item["input"][0] if item["count"] == 1 else f"{item['count']} 张图片"
            lines.append(f"  - {name}: 图片 {item['layout']} {item.get('grid','')}, "
                         f"{item['pages']} 页")
        else:
            lines.append(f"  - {Path(item['input']).name}: {item['kind']}, "
                         f"{item['pages']} 页 ({item['method']})")
    lines.append(f"合计: {spec['estimated_pages']} 页")
    if spec.get("fidelity") == "reduced":
        lines.append("保真度: docx 经 Mammoth 渲染,版式可能简化")
    for warning in spec.get("warnings", []):
        lines.append(f"注意: {warning}")
    lines.append(f"PDF: {spec['pdf_path']}")
    lines.append(f"预览: {spec['preview_png']}")
    if show_next:
        lines.append(f"确认后发送: ph.py send --job {spec['job_id']} --yes")
    return "\n".join(lines)


def _human_send(result: dict) -> str:
    if result.get("dry_run"):
        return (f"[dry-run] 未发送。目标 {result.get('target') or result.get('queue')} "
                f"· 格式 {result.get('format', 'pdf')} · {result.get('bytes', '?')} 字节")
    lines = [f"已发送: {result.get('job_status', 'submitted')}"]
    if result.get("job_id"):
        lines.append(f"作业号: {result['job_id']}")
    if result.get("target"):
        lines.append(f"目标: {result['target']} · 格式 {result.get('format')} · "
                     f"{result.get('bytes')} 字节 × {result.get('copies', 1)} 份")
    if result.get("note"):
        lines.append(result["note"])
    for warning in result.get("warnings", []):
        lines.append(f"注意: {warning}")
    return "\n".join(lines)


def _human_discover(payload: dict) -> str:
    lines = []
    local = payload.get("local") or []
    if local:
        lines.append("本机 CUPS 队列:")
        for item in local:
            lines.append(f"  - {item['queue']}: {item['uri']} ({item.get('state')})")
    network = payload.get("network") or []
    if network:
        lines.append("网络候选(需确认后添加):")
        for item in network:
            lines.append(f"  - {item.get('name') or ''} {item['uri']}".rstrip())
    if not local and not network:
        lines.append("未发现打印机。可用 `printer add --uri socket://host:9100` 手动添加。")
    for warning in payload.get("warnings", []):
        lines.append(f"注意: {warning}")
    return "\n".join(lines)


# --------------------------------------------------------------- resolution

def _load_context():
    return registry.load_printers(), registry.load_prefs()


def _resolve_printer(reference: str | None):
    data, prefs = _load_context()
    slot, printer = registry.get_printer(data, reference)
    return data, prefs, slot, printer


def _resolve_options(args, printer: dict, prefs: dict) -> dict:
    paper_name = args.paper or printer.get("paper") or prefs.get("paper") or "A4"
    paper = parse_paper(paper_name)
    margin_value = args.margin or prefs.get("margin") or "normal"
    margins, margin_mode = parse_margins(margin_value)
    images = args.images or prefs.get("image_layout") or "one-per-page"
    copies = args.copies if args.copies is not None else int(prefs.get("copies", 1))
    if copies < 1:
        raise UsageError("--copies 必须 >= 1")
    raw_format = args.raw_format or printer.get("raw_format") or "pdf"
    pjl = args.pjl if getattr(args, "pjl", None) is not None else printer.get("pjl", False)
    return {
        "paper_name": paper_name,
        "paper": paper,
        "margins": margins,
        "margin_mode": margin_mode,
        "orient": args.orient or "auto",
        "images": images,
        "grid": getattr(args, "grid", "auto"),
        "gap_mm": getattr(args, "gap", 5.0),
        "fill": bool(getattr(args, "fill", False)),
        "caption": bool(getattr(args, "caption", False)),
        "copies": int(copies),
        "duplex": bool(getattr(args, "duplex", False) or prefs.get("duplex", False)),
        "quality": getattr(args, "quality", "normal") or "normal",
        "page_numbers": bool(getattr(args, "page_numbers", False)),
        "bookmarks": not bool(getattr(args, "no_bookmarks", False)),
        "title": getattr(args, "title", None),
        "raw_format": raw_format,
        "pjl": bool(pjl),
    }


def _build_spec(args) -> tuple[dict, str, dict, dict, dict]:
    data, prefs, slot, printer = _resolve_printer(args.printer)
    options = _resolve_options(args, printer, prefs)
    options["printer_slot"] = slot
    spec = compose.build_job(args.inputs, options=options, printer=printer)
    if options["images"] == "one-per-page" and args.grid not in (None, "auto"):
        spec["warnings"].append("--images one-per-page 时 --grid 不生效")
    return data, slot, printer, options, spec


def _confirmation_payload(spec: dict) -> dict:
    return {
        "job_id": spec["job_id"],
        "summary": {
            "printer": spec["printer"]["slot"],
            "paper": spec["paper"]["name"],
            "margin": spec["margin"]["mode"],
            "pages": spec["estimated_pages"],
            "copies": spec["copies"],
            "fidelity": spec.get("fidelity"),
            "warnings": spec.get("warnings", []),
        },
        "pdf_path": spec["pdf_path"],
        "preview_png": spec.get("preview_png"),
        "next": f"ph.py send --job {spec['job_id']} --yes",
    }


def _audit_failure(exc: PhError, *, action: str, job_id: str | None, slot: str,
                   printer: dict, pages, copies: int, inputs=None) -> None:
    """Record failed sends so the audit log reflects reality, not just successes."""
    jobs.audit({
        "action": action, "job_id": job_id, "printer": slot,
        "transport": printer.get("transport"), "target": _describe_target(printer),
        "inputs": inputs, "pages": pages, "copies": copies,
        "result": f"error: {exc.message}", "warnings": [],
    })


def _stream_name(printer: dict, result: dict | None = None,
                 raw_format: str | None = None) -> str:
    if result and result.get("format"):
        return str(result["format"])
    if printer.get("transport") in {"cups", "cups-remote"}:
        return printer.get("preferred_stream") or "cups"
    return (raw_format or printer.get("preferred_stream")
            or printer.get("raw_format") or "pdf")


# ----------------------------------------------------------------- commands

def cmd_printer_list(args) -> tuple[dict, str]:
    data = registry.load_printers()
    items = []
    for slot, printer in data["printers"].items():
        item = dict(printer)
        item["slot"] = slot
        item["default"] = slot == data.get("default")
        items.append(item)
    payload = {"ok": True, "default": data.get("default"), "printers": items,
               "data_dir": str(registry.data_dir())}
    return payload, _human_printer_list(payload)


def cmd_printer_resolve(args) -> tuple[dict, str]:
    data = registry.load_printers()
    payload = registry.resolve(data, args.query)
    payload["ok"] = True
    return payload, _human_resolve(payload)


def cmd_printer_add(args) -> tuple[dict, str]:
    data = registry.load_printers()
    aliases = list(args.alias or [])
    uri = args.uri
    parsed = registry.parse_uri(uri) if uri else {}

    transport_name = args.transport or parsed.get("transport")
    queue = args.queue or parsed.get("queue")
    host = args.host or parsed.get("host")
    port = args.port or parsed.get("port")

    if not transport_name:
        transport_name = "cups" if queue else ("socket" if host else None)
    if not transport_name:
        raise UsageError("需要提供 --queue(本机 CUPS)或 --uri/--host(网络打印机)")

    if transport_name == "socket":
        if not host:
            raise UsageError("socket 打印机需要 --host 或 socket:// URI")
        port = int(port or 9100)
        caps = {"media": [args.paper or "A4"], "duplex": False, "color": None}
        probe = discover.probe_socket(host, port)
    elif transport_name in {"cups", "cups-remote"}:
        if not queue:
            raise UsageError("CUPS 打印机需要 --queue")
        if transport_name == "cups-remote" and not host:
            raise UsageError("远端 CUPS 需要 host")
        probe = discover.probe_queue(queue) if transport_name == "cups" else {"ok": True}
        caps = {
            "media": probe.get("media", [args.paper or "A4"]),
            "duplex": bool(probe.get("duplex")),
            "color": probe.get("color"),
            "resolution": probe.get("resolution", []),
            "options": probe.get("options", {}),
        }
    elif transport_name == "ipp":
        raise UsageError("直连 IPP 暂未实现。请用 CUPS 注册: "
                         f"lpadmin -p <name> -E -v {uri} -m everywhere")
    elif transport_name == "lpd":
        raise UsageError("LPD 直发暂未实现。请用 CUPS 注册后 --queue 添加。")
    else:
        raise UsageError(f"未知 transport: {transport_name}")

    raw_format_value = args.raw_format
    if not raw_format_value:
        if transport_name in {"cups", "cups-remote"}:
            ppd = detect.read_ppd(queue) if transport_name == "cups" else None
            raw_format_value = (detect.stream_from_ppd(ppd)[0]
                                if ppd else "cups")
        else:
            matches = detect.model_candidates(args.model or "")
            raw_format_value = matches[0]["stream"] if matches else "pdf"

    slot, printer = registry.add_printer(
        data, aliases=aliases, transport=transport_name, host=host, port=port,
        queue=queue, uri=uri, slot=args.slot, model=args.model,
        location=args.location, paper=args.paper,
        raw_format=raw_format_value, pjl=args.pjl,
        capabilities=caps, notes=args.notes, make_default=args.default)
    payload = {"ok": True, "slot": slot, "printer": printer, "probe": probe}
    human = (f"已{'添加' if bool(probe.get('ok')) else '登记'}: {slot} "
             f"[{transport_name} {_describe_target(printer)}]\n"
             f"别名: {' / '.join(printer['aliases'])}")
    if not probe.get("ok"):
        human += f"\n注意: 连通性探测未通过: {probe.get('error') or probe.get('state')}"
    if args.default:
        human += "\n已设为默认打印机。"
    return payload, human


def cmd_printer_default(args) -> tuple[dict, str]:
    data = registry.load_printers()
    slot, printer = registry.set_default(data, args.reference)
    return {"ok": True, "default": slot, "printer": printer}, f"默认打印机: {slot}"


def cmd_printer_remove(args) -> tuple[dict, str]:
    data = registry.load_printers()
    slot = registry.remove_printer(data, args.reference)
    return {"ok": True, "removed": slot}, f"已移除: {slot}"


def cmd_printer_probe(args) -> tuple[dict, str]:
    data, _, slot, printer = _resolve_printer(args.reference)
    if printer.get("transport") == "socket":
        result = transport.probe(printer)
    else:
        result = discover.probe_queue(printer.get("queue"))
        if result.get("ok"):
            printer["capabilities"] = {
                "media": result.get("media", []),
                "duplex": bool(result.get("duplex")),
                "color": result.get("color"),
                "resolution": result.get("resolution", []),
                "options": result.get("options", {}),
            }
            registry.save_printers(data)
    payload = {"ok": bool(result.get("ok")), "slot": slot, "probe": result}
    return payload, (f"{slot}: {'可达' if result.get('ok') else '不可达'} "
                     f"{result.get('error') or result.get('state') or ''}").strip()


def cmd_printer_discover(args) -> tuple[dict, str]:
    payload = discover.discover()
    payload["ok"] = True
    return payload, _human_discover(payload)


def cmd_printer_detect(args) -> tuple[dict, str]:
    _, _, slot, printer = _resolve_printer(args.reference)
    payload = detect.detect(slot, printer)
    rec = payload.get("recommendation")
    lines: list[str] = []
    if rec:
        if rec["transport"] in {"cups", "cups-remote"}:
            target = f"CUPS 队列 {rec.get('queue')}"
        else:
            target = f"raw {printer.get('host')}:{printer.get('port') or 9100}"
        lines.append(f"推荐: {rec['transport']} · {target} · stream={rec['stream']} "
                     f"· generator={rec.get('generator')} "
                     f"· 置信度 {rec['confidence']:.0%}")
        lines.append(f"依据: {rec['reason']}")
    else:
        lines.append("未找到离线证据;需要按校准顺序各试 1 页。")
    if payload.get("model"):
        lines.append(f"型号: {payload['model']}")
    if payload.get("evidence"):
        lines.append("证据:")
        for item in payload["evidence"]:
            lines.append(f"  - {item}")
    verified = payload.get("verified_streams") or {}
    failed = payload.get("failed_streams") or {}
    if verified:
        lines.append("已验证流: " + ", ".join(verified))
    if failed:
        lines.append("已失败流: " + ", ".join(
            f"{name}({record.get('error', 'failed')})"
            for name, record in failed.items()))
    lines.append("候选:")
    for item in payload.get("candidates", [])[:5]:
        lines.append(f"  - {item['transport']:<6} stream={item['stream']:<4} "
                     f"{item['confidence']:.0%}  {item['reason']}")
    if rec:
        lines.append("下一步:")
        if rec["transport"] in {"cups", "cups-remote"}:
            lines.append(f"  ph.py printer add --alias {slot} "
                         f"--queue {rec['queue']} --paper A4")
        else:
            lines.append(f"  ph.py run <文件> --printer {slot} "
                         f"--raw-format {rec['stream']} --dry-run")
    return payload, "\n".join(lines)


def cmd_printer_show(args) -> tuple[dict, str]:
    _, _, slot, printer = _resolve_printer(args.reference)
    lines = [f"slot: {slot}",
             f"别名: {' / '.join(printer.get('aliases', []))}",
             f"传输: {printer.get('transport')} {_describe_target(printer)}"]
    identity = printer.get("identity") or {}
    if any(identity.values()):
        lines.append("身份: " + ", ".join(
            f"{key}={value}" for key, value in identity.items() if value))
    lines.append(f"首选流: {printer.get('preferred_stream') or printer.get('raw_format')}")
    for name, record in (printer.get("streams") or {}).items():
        detail = str(record.get("status"))
        if record.get("error"):
            detail += f" ({record['error']})"
        lines.append(f"  - {name}: {detail}")
    capabilities = printer.get("capabilities") or {}
    lines.append(f"能力: media={capabilities.get('media')} "
                 f"duplex={capabilities.get('duplex')} "
                 f"color={capabilities.get('color')} "
                 f"resolution={capabilities.get('resolution')}")
    lines.append(f"默认: {printer.get('defaults')}")
    lines.append(f"历史: {printer.get('history')}")
    return {"ok": True, "slot": slot, "printer": printer}, "\n".join(lines)


def cmd_printer_stream(args) -> tuple[dict, str]:
    data, _, slot, printer = _resolve_printer(args.reference)
    if args.status == "clear":
        printer.get("streams", {}).pop(args.stream, None)
        registry.save_printers(data)
        record = None
    else:
        record = registry.set_stream_status(data, slot, args.stream, args.status,
                                            args.error)
    return {"ok": True, "slot": slot, "stream": args.stream,
            "status": args.status, "record": record}, f"{slot} · {args.stream}: {args.status}"


def cmd_plan(args) -> tuple[dict, str]:
    _, _, _, _, spec = _build_spec(args)
    return {"ok": True, "spec": spec}, _human_spec(spec)


def cmd_preview(args) -> tuple[dict, str]:
    _, _, _, _, spec = _build_spec(args)
    payload = {"ok": True, "job_id": spec["job_id"],
               "preview_png": spec["preview_png"], "pdf_path": spec["pdf_path"],
               "pages": spec["estimated_pages"]}
    return payload, (f"预览: {spec['preview_png']}\nPDF: {spec['pdf_path']}\n"
                     f"共 {spec['estimated_pages']} 页")


def _send_from_job(args) -> tuple[dict, dict, dict, str | None, int, bool, str | None]:
    spec = jobs.load_spec(args.job)
    if not Path(spec["pdf_path"]).exists():
        raise InputError(f"作业 PDF 不存在: {spec['pdf_path']}")
    data = registry.load_printers()
    reference = args.printer or spec["printer"].get("slot")
    slot, printer = registry.get_printer(data, reference)
    copies = args.copies if args.copies is not None else int(spec.get("copies", 1))
    duplex = bool(args.duplex or spec.get("duplex", False))
    raw_format = args.raw_format or spec.get("raw_format")
    return spec, printer, data, slot, copies, duplex, raw_format


def cmd_send(args) -> tuple[dict, str]:
    if bool(args.job) == bool(args.pdf):
        raise UsageError("请提供 --job <id> 或一个 PDF 文件(二选一)")

    if args.job:
        spec, printer, data, slot, copies, duplex, raw_format = _send_from_job(args)
        pdf_path = spec["pdf_path"]
        title = spec.get("title")
        pjl = args.pjl if args.pjl is not None else spec.get("pjl")
        quality = args.quality or "normal"
        job_id = spec["job_id"]
        pages = spec.get("estimated_pages")
        inputs = [item.get("input") for item in spec.get("outputs", [])]
        paper_name = spec["paper"]["name"]
    else:
        pdf_path = args.pdf
        if not args.printer:
            raise UsageError("直接发送 PDF 时需要 --printer")
        data, _, slot, printer = _resolve_printer(args.printer)
        copies = args.copies if args.copies is not None else 1
        duplex = bool(args.duplex)
        raw_format = args.raw_format or printer.get("raw_format")
        title = args.title or Path(pdf_path).stem
        pjl = args.pjl
        quality = args.quality or "normal"
        job_id = None
        pages = None
        inputs = [str(pdf_path)]
        paper_name = printer.get("paper") or "A4"

    if not args.dry_run and not args.yes:
        payload = {
            "job_id": job_id,
            "printer": slot,
            "target": _describe_target(printer),
            "pages": pages,
            "copies": copies,
            "pdf_path": pdf_path,
            "next": (f"ph.py send --job {job_id} --yes" if job_id
                     else f"ph.py send {pdf_path} --printer {slot} --yes"),
        }
        raise NeedsConfirmation("需要用户确认后才能发送打印任务", payload=payload)

    try:
        result = transport.send_pdf(
            pdf_path, printer, copies=copies, duplex=duplex, quality=quality,
            dry_run=args.dry_run, no_wait=args.no_wait, title=title,
            raw_format=raw_format, pjl=pjl, paper_name=paper_name)
    except PhError as exc:
        _audit_failure(exc, action="print", job_id=job_id, slot=slot,
                       printer=printer, pages=pages, copies=copies, inputs=inputs)
        registry.record_stream_result(
            data, slot, _stream_name(printer, raw_format=raw_format),
            ok=False, error=exc.message)
        raise
    jobs.audit({
        "action": "print", "job_id": job_id, "printer": slot,
        "transport": printer.get("transport"), "target": _describe_target(printer),
        "inputs": inputs, "pages": pages, "copies": copies,
        "result": result.get("job_status") or ("dry-run" if result.get("dry_run") else "sent"),
        "warnings": result.get("warnings", []),
    })
    if result.get("sent"):
        registry.touch(data, slot)
    payload = {"ok": True, "job_id": job_id, "printer": slot, "result": result}
    return payload, _human_send(result)


def cmd_run(args) -> tuple[dict, str]:
    data, slot, printer, options, spec = _build_spec(args)
    if args.dry_run:
        try:
            result = transport.send_pdf(
                spec["pdf_path"], printer, copies=options["copies"],
                duplex=options["duplex"], quality=options["quality"],
                dry_run=True, no_wait=args.no_wait, title=spec.get("title"),
                raw_format=options["raw_format"], pjl=options["pjl"],
                paper_name=options["paper_name"])
        except PhError as exc:
            _audit_failure(exc, action="dry-run", job_id=spec["job_id"], slot=slot,
                           printer=printer, pages=spec["estimated_pages"],
                           copies=options["copies"])
            raise
        jobs.audit({"action": "dry-run", "job_id": spec["job_id"], "printer": slot,
                    "transport": printer.get("transport"), "target": _describe_target(printer),
                    "pages": spec["estimated_pages"], "copies": options["copies"],
                    "result": "dry-run", "warnings": spec.get("warnings", [])})
        payload = {"ok": True, "spec": spec, "send": result}
        return payload, _human_spec(spec) + "\n" + _human_send(result)

    if not args.yes:
        raise NeedsConfirmation("需要用户确认后才能发送打印任务",
                                payload=_confirmation_payload(spec))

    try:
        result = transport.send_pdf(
            spec["pdf_path"], printer, copies=options["copies"],
            duplex=options["duplex"], quality=options["quality"],
            dry_run=False, no_wait=args.no_wait, title=spec.get("title"),
            raw_format=options["raw_format"], pjl=options["pjl"],
            paper_name=options["paper_name"])
    except PhError as exc:
        _audit_failure(exc, action="print", job_id=spec["job_id"], slot=slot,
                       printer=printer, pages=spec["estimated_pages"],
                       copies=options["copies"],
                       inputs=[item.get("input") for item in spec.get("outputs", [])])
        registry.record_stream_result(
            data, slot, _stream_name(printer, raw_format=options["raw_format"]),
            ok=False, error=exc.message)
        raise
    jobs.audit({"action": "print", "job_id": spec["job_id"], "printer": slot,
                "transport": printer.get("transport"), "target": _describe_target(printer),
                "inputs": [item.get("input") for item in spec.get("outputs", [])],
                "pages": spec["estimated_pages"], "copies": options["copies"],
                "result": result.get("job_status") or "sent",
                "warnings": result.get("warnings", [])})
    if result.get("sent"):
        registry.touch(data, slot)
    payload = {"ok": True, "spec": spec, "send": result}
    return payload, _human_spec(spec, show_next=False) + "\n" + _human_send(result)


def cmd_jobs(args) -> tuple[dict, str]:
    records = jobs.list_jobs(args.limit)
    lines = []
    for item in records:
        lines.append(f"{item.get('ts','')}  {item.get('action',''):<8} "
                     f"{item.get('job_id') or '-':<22} {item.get('target') or '-':<26} "
                     f"{item.get('pages') or '-'} 页 x{item.get('copies', 1)}  "
                     f"{item.get('result','')}")
    return {"ok": True, "jobs": records}, "\n".join(lines) or "暂无记录"


def cmd_prefs_get(args) -> tuple[dict, str]:
    prefs = registry.load_prefs()
    return {"ok": True, "preferences": prefs}, json.dumps(prefs, ensure_ascii=False, indent=2)


def cmd_prefs_set(args) -> tuple[dict, str]:
    value = registry.set_pref(args.key, args.value)
    return {"ok": True, "key": args.key, "value": value}, f"{args.key} = {value}"


def cmd_calibrate(args) -> tuple[dict, str]:
    data, _, slot, printer = _resolve_printer(args.printer)
    options = {
        "paper_name": "A4", "paper": parse_paper("A4"),
        "margins": parse_margins("normal")[0], "margin_mode": "normal",
        "orient": "portrait", "images": "one-per-page", "grid": "auto",
        "gap_mm": 5.0, "fill": False, "caption": False, "copies": 1,
        "duplex": False, "quality": "normal", "page_numbers": False,
        "bookmarks": False, "title": "printing-helper calibration",
        "raw_format": args.raw_format or printer.get("raw_format") or "pdf",
        "pjl": False, "printer_slot": slot,
    }
    body = (
        "打印校准页 / PRINT CALIBRATION\n"
        "================================\n\n"
        f"打印机: {slot}  {_describe_target(printer)}\n"
        f"格式: {options['raw_format']}   纸张: A4   边距: normal\n\n"
        "如果本页文字清晰可读,说明当前 raw 格式正确。\n"
        "如果整页空白、乱码或打印机无反应:\n"
        f"  1) 用 --raw-format pcl 重试: ph.py calibrate --printer {slot} "
        f"--raw-format pcl --yes\n"
        "  2) 若仍失败,该 9100 设备可能只接受专用驱动,请改用 CUPS。\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                     encoding="utf-8") as handle:
        handle.write(body)
        temp_path = handle.name
    try:
        spec = compose.build_job([temp_path], options=options, printer=printer)
    finally:
        Path(temp_path).unlink(missing_ok=True)
    if not args.yes and not args.dry_run:
        raise NeedsConfirmation("需要确认后发送校准页(会实际出纸)",
                                payload=_confirmation_payload(spec))
    try:
        result = transport.send_pdf(
            spec["pdf_path"], printer, copies=1, dry_run=args.dry_run,
            raw_format=options["raw_format"], pjl=False, title="calibration",
            paper_name="A4")
    except PhError as exc:
        _audit_failure(exc, action="calibrate", job_id=spec["job_id"], slot=slot,
                       printer=printer, pages=spec["estimated_pages"], copies=1,
                       inputs=["calibration"])
        registry.record_stream_result(
            data, slot, _stream_name(printer, raw_format=options["raw_format"]),
            ok=False, error=exc.message)
        raise
    jobs.audit({
        "action": "calibrate", "job_id": spec["job_id"], "printer": slot,
        "transport": printer.get("transport"), "target": _describe_target(printer),
        "inputs": ["calibration"], "pages": spec["estimated_pages"], "copies": 1,
        "result": result.get("job_status") or (
            "dry-run" if result.get("dry_run") else "sent"),
        "warnings": result.get("warnings", []),
    })
    if result.get("sent"):
        registry.touch(data, slot)
    return {"ok": True, "spec": spec, "send": result}, \
        _human_spec(spec, show_next=not result.get("sent")) + "\n" + _human_send(result)


def cmd_version(args) -> tuple[dict, str]:
    return {"ok": True, "version": VERSION, "data_dir": str(registry.data_dir())}, \
        f"printing-helper {VERSION}\ndata: {registry.data_dir()}"


# ------------------------------------------------------------------- parser

def _add_compose_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("inputs", nargs="+", help="要打印的文件(可混合)")
    _add_layout_options(parser)
    parser.add_argument("--json", action="store_true", help="输出 JSON")


def _add_layout_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--printer", help="打印机别名/slot;省略则用默认")
    parser.add_argument("--paper", help="纸张,如 A4 / Letter / 210x297mm")
    parser.add_argument("--margin", help="normal / narrow / none / T,R,B,L(mm)")
    parser.add_argument("--orient", choices=["auto", "portrait", "landscape"],
                        default="auto")
    parser.add_argument("--images", choices=["one-per-page", "nup"], default=None,
                        help="图片:每张一页 或 多张拼一页")
    parser.add_argument("--grid", default="auto", help="N-up 网格,如 2x2;默认 auto")
    parser.add_argument("--gap", type=float, default=5.0, help="N-up 间距(mm)")
    parser.add_argument("--fill", action="store_true", help="N-up 时裁剪填满格子")
    parser.add_argument("--caption", action="store_true", help="N-up 时显示文件名")
    parser.add_argument("--copies", type=int, default=None)
    parser.add_argument("--duplex", action="store_true")
    parser.add_argument("--quality", choices=["draft", "normal", "high"],
                        default="normal")
    parser.add_argument("--page-numbers", action="store_true")
    parser.add_argument("--no-bookmarks", action="store_true")
    parser.add_argument("--title")
    parser.add_argument("--raw-format", choices=sorted(transport.RAW_FORMATS),
                        help="socket 直发格式;默认取打印机配置")
    parser.add_argument("--pjl", action="store_true", default=None,
                        help="socket 直发时包裹 PJL(部分打印机需要)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ph.py", description="printing-helper: 排版、记忆打印机、发送打印",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  ph.py printer add --alias 网络打印机 --uri socket://192.168.1.42:9100 --default
  ph.py plan 合同.docx --printer 网络打印机 --margin narrow
  ph.py send --job 20261122-120000-ab12 --yes
  ph.py run a.jpg b.jpg c.jpg d.jpg --images nup --grid 2x2 --printer 网络打印机 --dry-run
""")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("printer", help="打印机管理")
    psub = p.add_subparsers(dest="printer_command", required=True)

    q = psub.add_parser("list", help="列出已登记打印机")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_printer_list)

    q = psub.add_parser("add", help="添加/更新打印机")
    q.add_argument("--alias", action="append", help="可重复")
    q.add_argument("--slot", help="自定义 slot id")
    q.add_argument("--queue", help="本机 CUPS 队列名")
    q.add_argument("--uri", help="socket://host:port 或 ipp://host/printers/queue")
    q.add_argument("--host")
    q.add_argument("--port", type=int)
    q.add_argument("--transport", choices=["socket", "cups", "cups-remote"])
    q.add_argument("--model")
    q.add_argument("--location")
    q.add_argument("--notes")
    q.add_argument("--paper")
    q.add_argument("--raw-format", choices=sorted(transport.RAW_FORMATS))
    q.add_argument("--pjl", action="store_true", default=None)
    q.add_argument("--default", action="store_true")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_printer_add)

    q = psub.add_parser("resolve", help="解析别名")
    q.add_argument("query")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_printer_resolve)

    q = psub.add_parser("default", help="设置默认打印机")
    q.add_argument("reference")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_printer_default)

    q = psub.add_parser("remove", help="移除打印机")
    q.add_argument("reference")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_printer_remove)

    q = psub.add_parser("probe", help="探测可达性并刷新能力")
    q.add_argument("reference")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_printer_probe)

    q = psub.add_parser("discover", help="发现本机/网络打印机")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_printer_discover)

    q = psub.add_parser("detect", help="离线检测推荐的流与传输(不打印)")
    q.add_argument("reference")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_printer_detect)

    q = psub.add_parser("show", help="显示完整打印机档案")
    q.add_argument("reference")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_printer_show)

    q = psub.add_parser("stream", help="记录/清除流验证结果")
    q.add_argument("reference")
    q.add_argument("stream")
    q.add_argument("status", choices=["verified", "failed", "unknown", "clear"])
    q.add_argument("--error")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_printer_stream)

    p = sub.add_parser("plan", help="排版并生成预览(不打印)")
    _add_compose_options(p)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("compose", help="plan 的别名")
    _add_compose_options(p)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("preview", help="生成并返回第一页预览")
    _add_compose_options(p)
    p.set_defaults(func=cmd_preview)

    p = sub.add_parser("send", help="发送已生成的作业或 PDF")
    p.add_argument("pdf", nargs="?")
    p.add_argument("--job")
    p.add_argument("--printer")
    p.add_argument("--copies", type=int)
    p.add_argument("--duplex", action="store_true")
    p.add_argument("--quality", choices=["draft", "normal", "high"], default="normal")
    p.add_argument("--raw-format", choices=sorted(transport.RAW_FORMATS))
    p.add_argument("--pjl", action="store_true", default=None)
    p.add_argument("--title")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--no-wait", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("run", help="排版 + 发送(--yes 才真正打印)")
    _add_compose_options(p)
    p.add_argument("--yes", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-wait", action="store_true")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("calibrate", help="生成/发送校准页(验证 raw 格式)")
    p.add_argument("--printer")
    p.add_argument("--raw-format", choices=sorted(transport.RAW_FORMATS))
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_calibrate)

    p = sub.add_parser("jobs", help="最近作业记录")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_jobs)

    p = sub.add_parser("prefs", help="用户偏好")
    psub = p.add_subparsers(dest="prefs_command", required=True)
    q = psub.add_parser("get")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_prefs_get)
    q = psub.add_parser("set")
    q.add_argument("key")
    q.add_argument("value")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_prefs_set)

    p = sub.add_parser("version", help="版本与数据目录")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_version)

    return parser


def main(argv: list[str] | None = None) -> int:
    global JSON_MODE
    parser = build_parser()
    args = parser.parse_args(argv)
    JSON_MODE = bool(getattr(args, "json", False))

    try:
        result, human = args.func(args)
    except PhError as exc:
        if JSON_MODE:
            print(json.dumps(exc.to_dict(), ensure_ascii=False, indent=2))
        else:
            print(f"错误: {exc.message}", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        print("已取消", file=sys.stderr)
        return 130
    except Exception as exc:  # unexpected: keep a stable exit code
        if JSON_MODE:
            print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}",
                              "code": 1}, ensure_ascii=False, indent=2))
        else:
            print(f"内部错误: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if JSON_MODE:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif human:
        print(human)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())