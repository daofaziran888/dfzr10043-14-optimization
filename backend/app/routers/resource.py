"""统一资源接口：查询、导出、登记和动作按模块 key 分派到领域服务。"""
from __future__ import annotations

import csv
import io
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services import service_for
from app.services import sample_evidence as ev
from app.services.sample_evidence import evidence_registry

router = APIRouter(prefix="/api", tags=["业务资源"])


def require_service(module: str):
    service = service_for(module)
    if service is None:
        raise HTTPException(status_code=404, detail=f"未知业务模块：{module}")
    return service


@router.get("/sample_check/evidence")
def list_sample_evidence(batch: str | None = Query(default=None)) -> dict[str, Any]:
    """列出抽检导出证据；可按关联批次回查当时的复核结论和原样本范围。"""
    service = require_service("sample_check")
    items = service.list_export_evidence(batch=batch)
    return {"items": items, "total": len(items)}


@router.get("/sample_check/evidence/{evidence_id}")
def get_sample_evidence(evidence_id: str) -> dict[str, Any]:
    service = require_service("sample_check")
    evidence = service.get_export_evidence(evidence_id)
    if evidence is None:
        raise HTTPException(status_code=404, detail=f"导出证据 {evidence_id} 不存在")
    return {**ev.evidence_view(evidence), "校验": service.verify_evidence(evidence_id)}


@router.get("/sample_check/evidence/{evidence_id}/download")
def download_sample_evidence(
    evidence_id: str,
    request: Request,
    frmt: str = Query(default="csv", alias="format", pattern="^(csv|json)$"),
) -> Response:
    """下载证据原文快照；支持 bytes 区间续传，续传始终落在同一份快照上。"""
    service = require_service("sample_check")
    evidence = service.get_export_evidence(evidence_id)
    if evidence is None:
        raise HTTPException(status_code=404, detail=f"导出证据 {evidence_id} 不存在")
    content = (evidence["json内容"] if frmt == "json" else evidence["清单内容"]).encode("utf-8")
    total = len(content)
    headers = {
        "Accept-Ranges": "bytes",
        "ETag": f'"{evidence["内容摘要"]}"',
        "Content-Disposition": (
            f'attachment; filename="{evidence_id}.{frmt}"'
        ),
        "X-Evidence-Id": evidence_id,
        "X-Evidence-Version": str(evidence["版本"]),
        "X-Evidence-Status": quote(evidence.get("状态", "有效")),
        "X-Manifest-Digest": evidence["清单摘要"],
    }
    range_header = request.headers.get("range")
    if range_header:
        try:
            span = ev.parse_range_header(range_header, total)
        except ValueError:
            return Response(
                status_code=416, headers={**headers, "Content-Range": f"bytes */{total}"})
        if span is None:
            return Response(content=content, headers=headers,
                            media_type="application/octet-stream")
        start, end = span
        headers["Content-Range"] = f"bytes {start}-{end}/{total}"
        headers["Content-Length"] = str(end - start + 1)
        return Response(status_code=206, content=content[start:end + 1],
                        headers=headers, media_type="application/octet-stream")
    headers["Content-Length"] = str(total)
    return Response(content=content, headers=headers,
                    media_type="application/octet-stream")


@router.get("/sample_check/evidence/{evidence_id}/revisions")
def get_sample_evidence_revisions(evidence_id: str) -> dict[str, Any]:
    """回查改判记录：哪份样本从什么结论改成了什么结论。"""
    service = require_service("sample_check")
    evidence = service.get_export_evidence(evidence_id)
    if evidence is None:
        raise HTTPException(status_code=404, detail=f"导出证据 {evidence_id} 不存在")
    batches = evidence.get("批次") or []
    revisions = []
    for batch in batches:
        revisions.extend(service.sample_revisions(batch=batch))
    return {"evidence_id": evidence_id, "revisions": revisions, "total": len(revisions)}


@router.get("/{module}", response_model=PageResult[dict])
def list_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, total = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{module}/export")
def export_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> Response:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    if module == "sample_check":
        return _export_sample_evidence(service, filters, status)
    items, _ = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=1, size=1_000_000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *service.LIST_FIELDS, "status"])
    for item in items:
        values = [item.get("id"), *(item.get(field, "") for field in service.LIST_FIELDS),
                  item.get("status", "")]
        writer.writerow([
            "'" + str(value) if str(value or "").startswith(("=", "+", "-", "@")) else value
            for value in values
        ])
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{module}.csv"'},
    )


def _export_sample_evidence(service: Any, filters: dict[str, str],
                            status: str | None) -> Response:
    """\u62bd\u68c0\u6837\u672c\u5bfc\u51fa\uff1a\u767b\u8bb0\u8bc1\u636e\u5e76\u8fd4\u56de\u5e26\u62ac\u5934\u3001\u884c\u6307\u7eb9\u548c\u7b7e\u540d\u7684\u6e05\u5355\u3002"""
    result = service.create_export_evidence(filters=filters, status=status)
    evidence = result["evidence"]
    evidence_id = evidence["\u8bc1\u636e\u7f16\u53f7"]
    return Response(
        content=evidence["\u6e05\u5355\u5185\u5bb9"].encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{evidence_id}.csv"',
            "X-Evidence-Id": evidence_id,
            "X-Evidence-Version": str(evidence["\u7248\u672c"]),
            "X-Manifest-Digest": evidence["\u6e05\u5355\u6458\u8981"],
            "X-Review-Conclusion": quote(evidence["\u590d\u6838\u7ed3\u8bba"]),
        },
    )


@router.get("/{module}/{entry_id}", response_model=dict)
def get_entry(module: str, entry_id: int) -> dict[str, Any]:
    service = require_service(module)
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"{service.ENTITY} {entry_id} 不存在或已归档")
    return entry


@router.post("/{module}", response_model=ActionResult)
def create_entry(module: str, payload: EntryPayload) -> ActionResult:
    service = require_service(module)
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message=f"{service.ENTITY}已登记", entry=entry)


@router.post("/{module}/{entry_id}/actions", response_model=ActionResult)
def run_action(module: str, entry_id: int, payload: ActionPayload) -> ActionResult:
    service = require_service(module)
    entry, message = service.run_action(entry_id, payload.action.strip())
    return ActionResult(ok=entry is not None, message=message, entry=entry)


@router.post("/handoff_pack/verify-delivery")
def verify_handoff_delivery(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """交接验件：核对导出证据资格，并逐行比对接收方提交的样本行。"""
    service = require_service("handoff_pack")
    evidence_id = str(payload.get("evidence_id") or "").strip()
    if not evidence_id:
        raise HTTPException(status_code=422, detail="缺少 evidence_id")
    return service.verify_delivery(
        evidence_id=evidence_id,
        pack_id=_optional_int(payload.get("pack_id")),
        batch=payload.get("batch") or None,
        submitted_content=payload.get("content"),
        submitted_rows=payload.get("rows"),
    )


@router.post("/handoff_pack/{entry_id}/signoff")
def signoff_handoff(entry_id: int, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """凭抽检导出证据办理交接签收，拒绝时返回当前资格核验版本的判定理由。"""
    service = require_service("handoff_pack")
    evidence_id = str(payload.get("evidence_id") or "").strip()
    if not evidence_id:
        raise HTTPException(status_code=422, detail="缺少 evidence_id")
    return service.signoff(
        pack_id=entry_id,
        evidence_id=evidence_id,
        submitted_content=payload.get("content"),
        submitted_rows=payload.get("rows"),
        operator=str(payload.get("operator") or "receiver"),
    )


@router.get("/handoff_pack/{entry_id}/confirmation")
def get_handoff_confirmation(entry_id: int) -> dict[str, Any]:
    """回查交接确认及其关联的抽检导出证据。"""
    service = require_service("handoff_pack")
    confirmation = service.get_confirmation(entry_id)
    if confirmation is None:
        raise HTTPException(status_code=404, detail=f"交接包 {entry_id} 尚无签收确认")
    return confirmation


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
