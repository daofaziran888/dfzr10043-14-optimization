"""抽检样本导出证据：清单签名、断点续传快照、样本行核验与资格判定。

抽检样本的导出件不是普通 CSV，而是一份带自校验信息的「清单」：

* 清单抬头写明本次抽检采用的**复核结论**和**原样本范围**；
* 抬头里的**汇总**完全由清单自己的样本行推导，接收方可独立复算；
* 每一行都带**行指纹**，整份清单带**清单摘要**与**签名**；
* 证据按证据编号留存原文快照，下载中断后可以按字节区间续传同一份快照，
  不会被后续复核轮次混写。

证据按「关联批次」维护当前版本指针：同一批次再次导出会取代旧证据的签收资格，
但旧证据仍可按编号回查当时的样本范围和复核结论。其他批次互不影响。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

MANIFEST_FORMAT = "sample-check-manifest/v1"
ROW_FINGERPRINT_FIELD = "行指纹"

# 签名密钥：生产环境通过 SAMPLE_EXPORT_SECRET 注入；本地默认值只用于防误改。
_SIGNING_SECRET = os.environ.get(
    "SAMPLE_EXPORT_SECRET", "zhuliu-workshop-sample-export-v1"
).encode("utf-8")

_ROW_FIELDS = [
    "id", "样本编号", "关联批次", "抽样比例", "抽样行数",
    "字段通过率", "缺陷描述", "抽检结论", "抽检时间", "status",
]
_META_FIELDS = [
    "格式", "证据编号", "版本", "生成时间", "复核结论", "原样本范围", "汇总",
    "清单摘要", "签名",
]
_REQUIRED_META = ["格式", "证据编号", "版本", "生成时间", "复核结论", "原样本范围",
                  "汇总", "清单摘要", "签名"]


def now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def _canonical(value: Any) -> str:
    """稳定序列化：键排序、无多余空白，保证两端复算结果一致。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_digest(content: bytes | str) -> str:
    if isinstance(content, str):
        content = content.encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def row_core(row: dict[str, Any]) -> dict[str, str]:
    """清单行的可复算内容：固定字段、全部转字符串、去掉行指纹。"""
    return {field: str(row.get(field, "")) for field in _ROW_FIELDS}


def rows_core(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [row_core(row) for row in rows]


def rows_digest(rows: list[dict[str, Any]]) -> str:
    """样本行集合的规范化摘要：用于比对两份清单的样本行是否完全一致。"""
    return _sha256_hex(_canonical(rows_core(rows)))


def row_digest(row: dict[str, Any]) -> str:
    return _sha256_hex(_canonical(row_core(row)))[:16]


def sign_digest(digest: str) -> str:
    return hmac.new(_SIGNING_SECRET, digest.encode("utf-8"), hashlib.sha256).hexdigest()


def effective_conclusion(row: dict[str, Any]) -> str:
    """样本行的有效复核结论：优先取抽检结论，缺失时回落到样本状态。"""
    conclusion = str(row.get("抽检结论", ""))
    if conclusion in ("通过", "驳回"):
        return conclusion
    status = str(row.get("status", ""))
    if status == "已通过":
        return "通过"
    if status == "已驳回":
        return "驳回"
    return "待定"


def aggregate_conclusion(rows: list[dict[str, Any]]) -> str:
    """复核结论：任一行驳回即整份清单驳回；否则有未定即待定；全通过才是通过。"""
    if not rows:
        return "无样本"
    conclusions = [effective_conclusion(row) for row in rows]
    if "驳回" in conclusions:
        return "驳回"
    if any(item not in ("通过", "驳回") for item in conclusions):
        return "待定"
    return "通过"


def recompute_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """从样本行复算汇总；清单抬头里的汇总必须与它一致。"""
    summary: dict[str, Any] = {"总数": len(rows), "通过": 0, "驳回": 0, "待定": 0, "按批次": {}}
    for row in rows:
        key = effective_conclusion(row)
        summary[key] += 1
        batch = str(row.get("关联批次", ""))
        bucket = summary["按批次"].setdefault(
            batch, {"总数": 0, "通过": 0, "驳回": 0, "待定": 0}
        )
        bucket["总数"] += 1
        bucket[key] += 1
    summary["按批次"] = {key: summary["按批次"][key] for key in sorted(summary["按批次"])}
    return summary


def manifest_digest(meta: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    payload = {
        "格式": meta.get("格式", MANIFEST_FORMAT),
        "证据编号": str(meta.get("证据编号", "")),
        "版本": str(meta.get("版本", "")),
        "生成时间": str(meta.get("生成时间", "")),
        "复核结论": meta.get("复核结论", ""),
        "原样本范围": meta.get("原样本范围", {}),
        "汇总": meta.get("汇总", {}),
        "样本行": rows_core(rows),
    }
    return _sha256_hex(_canonical(payload))


def build_manifest_document(
    *,
    evidence_id: str,
    version: int,
    generated_at: str,
    review_conclusion: str,
    scope: dict[str, Any],
    summary: dict[str, Any],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """生成完整清单文档（含每行指纹、清单摘要和签名）。"""
    fingerprinted = []
    for row in rows:
        core = row_core(row)
        core[ROW_FINGERPRINT_FIELD] = row_digest(core)
        fingerprinted.append(core)
    meta = {
        "格式": MANIFEST_FORMAT,
        "证据编号": evidence_id,
        "版本": str(version),
        "生成时间": generated_at,
        "复核结论": review_conclusion,
        "原样本范围": scope,
        "汇总": summary,
    }
    digest = manifest_digest(meta, fingerprinted)
    meta["清单摘要"] = digest
    meta["签名"] = sign_digest(digest)
    return {"meta": meta, "rows": fingerprinted}


def render_csv(document: dict[str, Any]) -> str:
    """渲染为可下载的 CSV 清单：抬头是元信息，下面是逐样本行。"""
    import csv
    import io

    output = io.StringIO()
    output.write("﻿")
    meta = document["meta"]
    for key in _META_FIELDS:
        value = meta.get(key, "")
        if isinstance(value, (dict, list)):
            value = _canonical(value)
        output.write(f"# {key}={value}\n")
    writer = csv.writer(output, lineterminator="\n")
    header = [*_ROW_FIELDS, ROW_FINGERPRINT_FIELD]
    writer.writerow(header)
    for row in document["rows"]:
        writer.writerow([row.get(field, "") for field in header])
    return output.getvalue()


def render_json(document: dict[str, Any]) -> str:
    payload = {**document["meta"], "样本行": document["rows"]}
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _parse_meta_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def parse_manifest(content: str | bytes) -> dict[str, Any]:
    """解析 CSV 或 JSON 清单，返回 {meta, rows}。解析失败抛 ValueError。"""
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig")
    text = content.lstrip("﻿")
    if text.lstrip().startswith("{"):
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise ValueError(f"清单 JSON 无法解析：{exc}") from exc
        rows = payload.pop("样本行", None)
        if not isinstance(rows, list):
            raise ValueError("清单缺少样本行")
        meta = payload
        return {"meta": meta, "rows": [row_core(row) | {
            ROW_FINGERPRINT_FIELD: str(row.get(ROW_FINGERPRINT_FIELD, ""))
        } for row in rows]}

    meta: dict[str, Any] = {}
    data_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("# "):
            body = line[2:]
            if "=" in body:
                key, raw = body.split("=", 1)
                meta[key.strip()] = _parse_meta_value(raw)
            continue
        data_lines.append(line)
    if not data_lines:
        raise ValueError("清单缺少样本行")
    import csv
    import io

    reader = csv.DictReader(io.StringIO("\n".join(data_lines)))
    if reader.fieldnames is None:
        raise ValueError("清单缺少表头")
    rows = []
    for raw_row in reader:
        core = row_core({key: raw_row.get(key, "") for key in _ROW_FIELDS})
        core[ROW_FINGERPRINT_FIELD] = raw_row.get(ROW_FINGERPRINT_FIELD, "")
        rows.append(core)
    return {"meta": meta, "rows": rows}


def verify_document(document: dict[str, Any]) -> dict[str, Any]:
    """校验清单自身的完整性：行指纹、汇总复算、复核结论、摘要与签名。"""
    meta = document.get("meta") or {}
    rows = document.get("rows") or []
    checks: list[dict[str, Any]] = []

    missing = [key for key in _REQUIRED_META if key not in meta]
    checks.append({
        "name": "清单抬头完整",
        "ok": not missing,
        "detail": "" if not missing else f"缺少抬头字段：{'、'.join(missing)}",
    })

    row_ok = True
    row_detail = ""
    for row in rows:
        expected = row_digest(row)
        actual = str(row.get(ROW_FINGERPRINT_FIELD, ""))
        if actual != expected:
            row_ok = False
            row_detail = f"样本 {row.get('样本编号', row.get('id', '?'))} 的行指纹与内容不一致"
            break
    checks.append({"name": "样本行指纹一致", "ok": row_ok, "detail": row_detail})

    recomputed_summary = recompute_summary(rows)
    summary_ok = recomputed_summary == meta.get("汇总")
    checks.append({
        "name": "汇总可由样本行复算",
        "ok": summary_ok,
        "detail": "" if summary_ok else "抬头汇总与样本行复算结果不一致",
    })

    recomputed_conclusion = aggregate_conclusion(rows)
    conclusion_ok = recomputed_conclusion == meta.get("复核结论")
    checks.append({
        "name": "复核结论与样本行一致",
        "ok": conclusion_ok,
        "detail": "" if conclusion_ok else "抬头复核结论与样本行判定不一致",
    })

    digest = manifest_digest(meta, rows)
    digest_ok = digest == str(meta.get("清单摘要", ""))
    checks.append({
        "name": "清单摘要一致",
        "ok": digest_ok,
        "detail": "" if digest_ok else "清单内容与摘要不符，文件可能被改动",
    })

    signature_ok = False
    if digest_ok:
        signature_ok = hmac.compare_digest(sign_digest(digest), str(meta.get("签名", "")))
    checks.append({
        "name": "清单签名有效",
        "ok": signature_ok,
        "detail": "" if signature_ok else "清单签名无效，文件不是本服务签发或已被改动",
    })

    ok = all(item["ok"] for item in checks)
    reason = "" if ok else next(item["detail"] for item in checks if not item["ok"])
    return {
        "ok": ok,
        "checks": checks,
        "reason": reason,
        "computed_summary": recomputed_summary,
        "computed_conclusion": recomputed_conclusion,
    }


def diagnose_row_replacement(
    submitted_rows: list[dict[str, Any]], evidence_rows: list[dict[str, Any]]
) -> str:
    """逐行对比提交件与原证据，定位「样本行被换过」的位置。

    这里刻意按样本身份与行内容比对，而不是只比通过数——即使替换后的
    通过数完全相同，也能发现样本行被换过。
    """
    evidence_by_id = {str(row.get("id", "")): row for row in evidence_rows}
    submitted_by_id = {str(row.get("id", "")): row for row in submitted_rows}
    missing = [row for key, row in evidence_by_id.items() if key not in submitted_by_id]
    extra = [row for key, row in submitted_by_id.items() if key not in evidence_by_id]
    changed = [
        evidence_by_id[key]
        for key in evidence_by_id.keys() & submitted_by_id.keys()
        if row_digest(evidence_by_id[key]) != row_digest(submitted_by_id[key])
    ]

    parts: list[str] = []
    if missing:
        labels = "、".join(str(row.get("样本编号", row.get("id", ""))) for row in missing)
        parts.append(f"原样本行缺失：{labels}")
    if extra:
        labels = "、".join(str(row.get("样本编号", row.get("id", ""))) for row in extra)
        parts.append(f"出现原证据之外的样本行：{labels}")
    if changed:
        labels = "、".join(str(row.get("样本编号", row.get("id", ""))) for row in changed)
        parts.append(f"样本行内容被改动：{labels}")
    if not parts and rows_digest(submitted_rows) != rows_digest(evidence_rows):
        parts.append("样本行顺序与证据原文不一致")
    if not parts:
        return "样本行与证据原文一致"
    detail = "；".join(parts)
    submitted_pass = recompute_summary(submitted_rows).get("通过", 0)
    evidence_pass = recompute_summary(evidence_rows).get("通过", 0)
    if submitted_pass == evidence_pass:
        detail += f"（替换后通过数仍为 {submitted_pass}，按行身份识别）"
    return detail


class EvidenceRegistry:
    """导出证据登记簿：保存原文快照、版本指针与改判留痕。"""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._items: list[dict[str, Any]] = []
        self._by_id: dict[str, dict[str, Any]] = {}
        self._seq = 0
        self._batch_current: dict[str, str] = {}
        self._revisions: list[dict[str, Any]] = []

    # ── 证据登记 ─────────────────────────────────────────────
    def allocate(self, scope_key: str) -> tuple[str, int]:
        self._seq += 1
        stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d")
        evidence_id = f"EV-{stamp}-{self._seq:04d}"
        version = 1 + sum(1 for item in self._items if item.get("范围键") == scope_key)
        return evidence_id, version

    def insert(self, evidence: dict[str, Any]) -> dict[str, Any]:
        evidence_id = evidence["证据编号"]
        batches = list(evidence.get("批次") or [])
        superseded: dict[str, str] = {}
        for batch in batches:
            previous = self._batch_current.get(batch)
            if previous and previous != evidence_id:
                superseded[batch] = previous
                previous_item = self._by_id.get(previous)
                if previous_item is not None:
                    previous_item.setdefault("已取代批次", [])
                    if batch not in previous_item["已取代批次"]:
                        previous_item["已取代批次"].append(batch)
        evidence["取代的批次证据"] = superseded
        self._items.append(evidence)
        self._by_id[evidence_id] = evidence
        for batch in batches:
            self._batch_current[batch] = evidence_id
        return evidence

    def get(self, evidence_id: str) -> dict[str, Any] | None:
        return self._by_id.get(evidence_id)

    def items(self) -> list[dict[str, Any]]:
        return list(self._items)

    def current_for_batch(self, batch: str) -> str | None:
        return self._batch_current.get(batch)

    def revoke(self, evidence_id: str, reason: str) -> dict[str, Any] | None:
        evidence = self.get(evidence_id)
        if evidence is None:
            return None
        evidence["状态"] = "已撤销"
        evidence["撤销原因"] = reason
        evidence["撤销时间"] = now_iso()
        for batch in evidence.get("批次") or []:
            if self._batch_current.get(batch) == evidence_id:
                # 撤销后该批次失去当前证据，需重新导出才能签收。
                self._batch_current.pop(batch, None)
        return evidence

    def record_revision(self, revision: dict[str, Any]) -> None:
        self._revisions.append(revision)

    def revisions(self, sample_id: int | None = None, batch: str | None = None) -> list[dict[str, Any]]:
        items = list(self._revisions)
        if sample_id is not None:
            items = [item for item in items if int(item.get("样本ID", 0)) == sample_id]
        if batch:
            items = [item for item in items if item.get("关联批次") == batch]
        return items


evidence_registry = EvidenceRegistry()


def evidence_view(evidence: dict[str, Any]) -> dict[str, Any]:
    """对外展示证据：不返回原文，但返回足以回查的来源与版本信息。"""
    batches = list(evidence.get("批次") or [])
    return {
        "证据编号": evidence["证据编号"],
        "模块": evidence.get("模块", "sample_check"),
        "版本": evidence["版本"],
        "生成时间": evidence["生成时间"],
        "复核结论": evidence["复核结论"],
        "原样本范围": evidence["原样本范围"],
        "汇总": evidence["汇总"],
        "样本行": evidence["样本行"],
        "清单摘要": evidence["清单摘要"],
        "签名": evidence["签名"],
        "内容摘要": evidence["内容摘要"],
        "格式": evidence["格式"],
        "状态": evidence.get("状态", "有效"),
        "撤销原因": evidence.get("撤销原因", ""),
        "撤销时间": evidence.get("撤销时间", ""),
        "批次": batches,
        "已取代批次": evidence.get("已取代批次", []),
        "取代的批次证据": evidence.get("取代的批次证据", {}),
        "批次当前版本": {
            batch: evidence_registry.current_for_batch(batch) for batch in batches
        },
    }


def parse_range_header(value: str, total: int) -> tuple[int, int] | None:
    """解析单区间 Range 头；不合法返回 None，越界抛 ValueError。"""
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if not match:
        return None
    start_raw, end_raw = match.groups()
    if not start_raw and not end_raw:
        return None
    if not start_raw:
        suffix = int(end_raw)
        if suffix <= 0:
            raise ValueError("invalid range")
        start = max(0, total - suffix)
        end = total - 1
    else:
        start = int(start_raw)
        end = int(end_raw) if end_raw else total - 1
    if start >= total:
        raise ValueError("range not satisfiable")
    if end >= total:
        end = total - 1
    if end < start:
        raise ValueError("invalid range")
    return start, end
