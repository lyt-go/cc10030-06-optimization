"""统一资源接口：查询、导出、登记和动作按模块 key 分派到领域服务。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import (
    ActionPayload,
    ActionResult,
    AdjustPayload,
    ConfirmationPayload,
    CountyReportPayload,
    EntryPayload,
    GridCountyPayload,
    PageResult,
    RecomputePayload,
    ReviewEvidencePayload,
)
from app.services import service_for

router = APIRouter(prefix="/api", tags=["业务资源"])


def require_service(module: str):
    service = service_for(module)
    if service is None:
        raise HTTPException(status_code=404, detail=f"未知业务模块：{module}")
    return service


def require_method(service: Any, name: str):
    """固定路径动作必须由领域服务提供对应方法，路由层不做业务判断。"""
    method = getattr(service, name, None)
    if method is None:
        raise HTTPException(
            status_code=404, detail=f"模块 {service.MODULE} 不支持该动作")
    return method


# -- 县级归属：固定路径必须注册在 /{module}/{entry_id} 之前 --

@router.post("/jurisdiction/confirmations", response_model=dict)
def register_confirmation(payload: ConfirmationPayload) -> dict[str, Any]:
    """登记/续用县级归属确认。同一分段改登记到别的县必须走调账。"""
    service = service_for("region_dim")
    ttl_sec = payload.ttl_days * 24 * 3600 if payload.ttl_days else None
    kwargs: dict[str, Any] = {
        "grid_geohash": payload.grid_geohash,
        "trip_id": payload.trip_id,
        "evidence_ref": payload.evidence_ref,
        "note": payload.note,
    }
    if ttl_sec:
        kwargs["ttl_sec"] = ttl_sec
    return service.register_county_confirmation(
        payload.batch_key, payload.segment_id, payload.county_code, **kwargs)


@router.get("/jurisdiction/confirmations", response_model=dict)
def get_confirmation(batch_key: str, segment_id: str) -> dict[str, Any]:
    service = service_for("region_dim")
    return {"confirmation": service.get_county_confirmation(batch_key, segment_id)}


@router.post("/jurisdiction/grid-counties", response_model=dict)
def register_grid_county(payload: GridCountyPayload) -> dict[str, Any]:
    """登记网格属地标准；只有命中格内的分段才进入县级确认口径。"""
    service = service_for("region_dim")
    return service.register_grid_county(
        payload.batch_key, payload.grid_geohash, payload.county_code,
        evidence_ref=payload.evidence_ref)


@router.get("/jurisdiction/review-items", response_model=dict)
def list_review_items(batch_key: str, status: str | None = None) -> dict[str, Any]:
    """无法确认归属的分段独立待复核明细。"""
    service = service_for("region_dim")
    return service.get_review_detail(batch_key, status=status)


@router.post("/jurisdiction/review-items/evidence", response_model=dict)
def attach_review_evidence(payload: ReviewEvidencePayload) -> dict[str, Any]:
    """补齐边界证据；缺口未补完时不允许调账。"""
    service = service_for("region_dim")
    return service.attach_review_evidence(
        payload.batch_key, payload.review_id, evidence_ref=payload.evidence_ref,
        geometry_evidence=payload.geometry_evidence or None)


@router.post("/jurisdiction/adjustments", response_model=dict)
def apply_adjustment(payload: AdjustPayload) -> dict[str, Any]:
    """从待复核明细发起一次县级调账，同一份证据只记一次。"""
    service = service_for("region_dim")
    return service.apply_jurisdiction_adjustment(
        payload.batch_key, payload.review_id,
        to_county_code=payload.to_county_code, evidence_ref=payload.evidence_ref,
        trip_weight=payload.trip_weight, operator_note=payload.operator_note)


@router.get("/jurisdiction/adjustments", response_model=dict)
def list_adjustments(batch_key: str) -> dict[str, Any]:
    service = service_for("region_dim")
    return {"batch_key": batch_key, "items": service.list_adjustments(batch_key)}


@router.post("/jurisdiction/county-report", response_model=dict)
def compute_county_report(payload: CountyReportPayload) -> dict[str, Any]:
    """只对尚未提交的下期县报核算；已提交县报不再重算。"""
    service = service_for("region_dim")
    return service.compute_county_report(
        payload.batch_key, disposition=payload.disposition,
        submitted=payload.submitted, as_of=payload.as_of)


@router.get("/jurisdiction/county-report", response_model=dict)
def get_county_report(batch_key: str, submitted: bool = False) -> dict[str, Any]:
    service = service_for("region_dim")
    return {"batch_key": batch_key,
            "report": service.get_county_report(batch_key, submitted=submitted)}


@router.post("/spatial_join/recompute", response_model=dict)
def recompute_spatial_join(payload: RecomputePayload) -> dict[str, Any]:
    """调整距离容差后重算轨迹-网格关联，结果覆盖写入并分层归集。"""
    service = require_service("spatial_join")
    return service.join_trajectories_to_grids(
        payload.batch_key, distance_tolerance=payload.distance_tolerance)


@router.get("/spatial_join/summary", response_model=dict)
def spatial_join_summary(batch_key: str) -> dict[str, Any]:
    service = require_service("spatial_join")
    return service.get_join_summary(batch_key)


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
) -> StreamingResponse:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
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
