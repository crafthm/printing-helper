"""Offline stream/transport detection.

Detection ladder (least paper first):
1. Reuse a local CUPS queue whose device URI matches the printer; read its PPD
   to learn the driver/filter chain (e.g. foo2zjs -> ZJS).
2. Match the model against a small knowledge base of common host-based models.
3. Fall back to a short calibration ladder (zjs -> pcl -> ps -> pdf).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import discover

PPD_DIR = Path("/etc/cups/ppd")

MODEL_RULES: list[dict[str, Any]] = [
    {
        "pattern": r"(laserjet\s*(?:1000|1005|1010|1018|1020|1022|p1005|p1006|p1505|m1005)|psc\s*(?:1000|1010|1510))",
        "stream": "zjs", "driver": "foo2zjs", "confidence": 0.95,
        "reason": "HP host-based LaserJet 1000/1018/1020/1022 系列使用 ZJS 原生流",
    },
    {
        "pattern": r"(canon\s*(?:lbp|mf)|capt\b)",
        "stream": "capt", "driver": "capt", "confidence": 0.7,
        "reason": "Canon LBP/MF 常见 CAPT/UFR II 主机流",
    },
    {
        "pattern": r"(samsung\s*ml|spl\b)",
        "stream": "spl", "driver": "splix", "confidence": 0.7,
        "reason": "Samsung ML 常见 SPL 主机流",
    },
    {
        "pattern": r"brother\s*hl",
        "stream": "pcl", "driver": "brlaser/pcl", "confidence": 0.6,
        "reason": "Brother HL 常见 PCL/PS;部分型号需要 brlaser",
    },
    {
        "pattern": r"(ipp\s*everywhere|mopria|airprint|urf|pwg)",
        "stream": "pdf", "driver": "ipp-everywhere", "confidence": 0.6,
        "reason": "IPP Everywhere 通常支持 PDF/URF/PWG",
    },
]

PPD_KEYS = ("NickName", "ShortNickName", "ModelName",
            "FoomaticRIPCommandLine", "DefaultPageSize", "DefaultResolution",
            "DefaultColorSpace", "DefaultQuality")


def read_ppd(queue: str) -> dict | None:
    path = PPD_DIR / f"{queue}.ppd"
    try:
        text = path.read_text("utf-8", errors="replace")
    except OSError:
        return None
    info: dict[str, Any] = {"path": str(path)}
    for key in PPD_KEYS:
        match = re.search(rf"^\*{re.escape(key)}:\s*(.+)$", text, re.MULTILINE)
        if match:
            info[key] = match.group(1).strip().strip('"')
    info["filters"] = re.findall(r'^\*cupsFilter:\s*"([^"]+)"', text, re.MULTILINE)
    info["filters2"] = re.findall(r'^\*cupsFilter2:\s*"([^"]+)"', text, re.MULTILINE)
    return info


def _stream_from_ppd(ppd: dict) -> tuple[str, str | None]:
    blob = " ".join([
        ppd.get("NickName", ""), ppd.get("ShortNickName", ""),
        ppd.get("FoomaticRIPCommandLine", ""), " ".join(ppd.get("filters", [])),
    ]).lower()
    if "foo2zjs" in blob:
        return "zjs", "foo2zjs-wrapper"
    if "foo2hbpl" in blob or "foo2hp" in blob:
        return "hbpl", "foo2hbpl2-wrapper"
    if "capt" in blob:
        return "capt", "captfilter"
    if "splix" in blob or "spl" in blob:
        return "spl", "splix"
    if "brlaser" in blob:
        return "pcl", "brlaser"
    if "hpcups" in blob or "hplip" in blob:
        return "cups", "hpcups"
    if "postscript" in blob or "ps2write" in blob:
        return "ps", "cups"
    if "pcl" in blob:
        return "pcl", "cups"
    return "cups", None


def _candidate(transport: str, stream: str, confidence: float, reason: str,
               queue: str | None = None, generator: str | None = None,
               **extra: Any) -> dict:
    return {"transport": transport, "stream": stream, "confidence": round(confidence, 2),
            "reason": reason, "queue": queue, "generator": generator, **extra}


def _match_queue(printer: dict, queues: list[dict]) -> dict | None:
    if printer.get("queue"):
        for item in queues:
            if item["queue"] == printer["queue"]:
                return item
    host = printer.get("host")
    port = printer.get("port") or 9100
    if printer.get("transport") == "socket" and host:
        wanted = f"socket://{host}:{port}".lower().rstrip("/")
        for item in queues:
            if item["uri"].lower().rstrip("/") == wanted:
                return item
    model = (printer.get("model") or "").lower()
    if model:
        matches = [item for item in queues
                   if model in jsonish_queue_text(item).lower()]
        if len(matches) == 1:
            return matches[0]
    return None


def jsonish_queue_text(item: dict) -> str:
    return " ".join(str(value) for value in item.values() if value)


def _model_candidates(model: str) -> list[dict]:
    text = (model or "").lower()
    found: list[dict] = []
    for rule in MODEL_RULES:
        if re.search(rule["pattern"], text):
            found.append(_candidate(
                "socket", rule["stream"], rule["confidence"], rule["reason"],
                generator=rule["driver"]))
    return found


def stream_from_ppd(ppd: dict) -> tuple[str, str | None]:
    """Public wrapper used by ``printer add`` to pick a native stream."""
    return _stream_from_ppd(ppd)


def model_candidates(model: str) -> list[dict]:
    return _model_candidates(model)


def _dedupe(candidates: list[dict]) -> list[dict]:
    seen: set[tuple[str, str, str | None]] = set()
    result: list[dict] = []
    for item in sorted(candidates, key=lambda c: c["confidence"], reverse=True):
        key = (item["transport"], item["stream"], item.get("queue"))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def detect(slot: str, printer: dict) -> dict:
    queues = discover.local_cups()
    matched = _match_queue(printer, queues)
    ppd = read_ppd(matched["queue"]) if matched else None

    model = (printer.get("model")
             or (ppd or {}).get("NickName")
             or (matched or {}).get("uri") or "")
    candidates: list[dict] = []
    evidence: list[str] = []

    if matched:
        evidence.append(f"匹配到 CUPS 队列 {matched['queue']} ({matched['uri']})")
    if ppd:
        evidence.append(f"PPD: {ppd.get('NickName') or ppd.get('ModelName') or ppd['path']}")
        if ppd.get("FoomaticRIPCommandLine"):
            evidence.append(f"驱动命令: {ppd['FoomaticRIPCommandLine']}")
        stream, generator = _stream_from_ppd(ppd)
        candidates.append(_candidate(
            "cups", stream, 0.99,
            "复用现有 CUPS 队列及其 PPD/驱动链(最稳,免猜测)",
            queue=matched["queue"], generator="cups-filter-chain",
            ppd=ppd["path"], ppd_stream=stream,
            default_page=ppd.get("DefaultPageSize"),
            default_resolution=ppd.get("DefaultResolution"),
        ))
        if stream == "zjs" and printer.get("transport") == "socket":
            candidates.append(_candidate(
                "socket", "zjs", 0.92,
                "同一 PPD 的 foo2zjs 可直接生成 ZJS 发往 9100",
                generator="foo2zjs-wrapper", ppd=ppd["path"],
                default_page=ppd.get("DefaultPageSize"),
                default_resolution=ppd.get("DefaultResolution"),
            ))

    for item in _model_candidates(model):
        candidates.append(item)
        evidence.append(item["reason"])

    if not candidates:
        for stream, confidence in (("zjs", 0.35), ("pcl", 0.3),
                                   ("ps", 0.25), ("pdf", 0.2)):
            candidates.append(_candidate(
                "socket", stream, confidence,
                "无离线证据,按常见程度排序的校准候选"))
        evidence.append("未找到 CUPS 队列/PPD/型号匹配,需要 1 页校准")

    candidates = _dedupe(candidates)
    recommendation = candidates[0] if candidates else None
    streams = printer.get("streams") or {}
    failed = {name: rec for name, rec in streams.items()
              if rec.get("status") == "failed"}
    verified = {name: rec for name, rec in streams.items()
                if rec.get("status") == "verified"}

    if verified:
        # A previously verified stream outranks untested suggestions.
        for name, rec in verified.items():
            for item in candidates:
                if item["stream"] == name:
                    item["confidence"] = max(item["confidence"], 0.99)
                    item["reason"] += " · 历史已验证"
        candidates = _dedupe(candidates)
        recommendation = candidates[0]

    return {
        "ok": True,
        "printer": slot,
        "model": model,
        "matched_queue": matched,
        "ppd": ppd,
        "recommendation": recommendation,
        "candidates": candidates,
        "verified_streams": verified,
        "failed_streams": failed,
        "evidence": evidence,
        "calibration_order": ["zjs", "pcl", "ps", "pdf"],
    }