"""通用接口契约；模块字段由领域服务持有，不再为每个模块复制一份模型类。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int = 1
    size: int = 20


class ActionResult(BaseModel):
    ok: bool
    message: str
    entry: dict[str, Any] | None = None


class EntryPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None


class ActionPayload(BaseModel):
    action: str
    remark: str | None = None


class ConfirmationPayload(BaseModel):
    """县级归属确认登记：同一分段重复登记只续用有效期，不覆盖原确认。"""

    batch_key: str
    segment_id: str
    county_code: str
    grid_geohash: str | None = None
    trip_id: str | None = None
    ttl_days: int | None = None
    evidence_ref: str | None = None
    note: str | None = None


class GridCountyPayload(BaseModel):
    """网格属地标准登记。"""

    batch_key: str
    grid_geohash: str
    county_code: str
    evidence_ref: str | None = None


class ReviewEvidencePayload(BaseModel):
    """补齐待复核明细的边界证据。"""

    batch_key: str
    review_id: str
    evidence_ref: str
    geometry_evidence: dict[str, Any] = Field(default_factory=dict)


class AdjustPayload(BaseModel):
    """从待复核明细发起一次县级调账。"""

    batch_key: str
    review_id: str
    to_county_code: str
    evidence_ref: str
    trip_weight: int = 1
    operator_note: str | None = None


class CountyReportPayload(BaseModel):
    """核算未提交的下期县报。"""

    batch_key: str
    disposition: str = "hold_under_confirmation"
    submitted: bool = False
    as_of: float | None = None


class RecomputePayload(BaseModel):
    """调整距离容差后重算轨迹-网格关联。"""

    batch_key: str
    distance_tolerance: float = 500.0
