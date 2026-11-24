"""区域维度领域服务：维护行政与业务区域层级维度表，将网格索引归属到区域维度中生成区域聚合数据集。

区域汇总同时消费网格索引与轨迹-网格空间关联结果：

- 命中格内的分段进入区域归属汇总，并保留原已确认分段的出处；
- 只在距离容差内的候选单独列出，候选距离和汇总归属分栏记录，便于分别复核；
- 上级统计按 trip_id 去重，一趟轨迹只计一次，暂挂与已调账不重复计数。
"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.services.jurisdiction import JurisdictionLedgerMixin
from app.store import store


class RegionDimService(BaseModuleService, JurisdictionLedgerMixin):
    MODULE = "region_dim"
    ENTITY = "区域层级"
    OPERATION_TERM = "入湖"
    LIST_FIELDS = ["区域编号", "上级区域", "区域名称", "区域级别", "边界编码", "网格数量", "维度版本", "生效时间"]
    REQUIRED_FIELDS = ["区域编号", "上级区域", "区域名称"]
    STATUS_ORDER = ["草稿", "待校验", "已发布", "已下线"]
    ACTION_RULES = {"提交校验": "待校验", "发布维度": "已发布", "标记下线": "已下线"}
    NEGATIVE_ACTIONS = []

    def aggregate_by_region(self, batch_key: str, region_id: int = 1) -> dict[str, Any]:
        """读取网格索引与空间关联结果，将网格按区域归属聚合生成区域维度汇总数据集。"""
        region = store.find(self.MODULE, region_id)
        if region is None:
            region = {"区域编号": "REGION-001", "区域名称": "东城区", "区域级别": "区级",
                      "边界编码": "110101", "上级区域": "北京市"}

        # 读取网格索引
        try:
            grid_svc = self._safe_import("app.services.grid_index", "GridIndexService")()
            grids, _ = grid_svc.get_grid_index(batch_key)
        except Exception:
            grids_file = self._data_dir("grid_index") / f"{batch_key}_grid.jsonl"
            if not grids_file.exists():
                raise FileNotFoundError(f"批次 {batch_key} 的网格索引数据不存在")
            grids = self._read_jsonl(grids_file)

        if not grids:
            return {"batch_key": batch_key, "total_points_aggregated": 0}

        total_points = sum(g["point_count"] for g in grids)
        region_center_lat = sum(g["center_lat"] * g["point_count"] for g in grids) / max(total_points, 1)
        region_center_lon = sum(g["center_lon"] * g["point_count"] for g in grids) / max(total_points, 1)

        # 消费轨迹-网格关联结果，区分命中格内与仅距离内候选。
        join_rows = self._read_join_results(batch_key)
        hit_rows = [row for row in join_rows if row.get("hit_in_grid")]
        distance_rows = [row for row in join_rows if not row.get("hit_in_grid")]

        # 一趟轨迹只计一次：按 trip_id 去重；无 trip_id 时退回分段编号。
        def trip_of(row: dict[str, Any]) -> str:
            return str(row.get("trip_id") or row.get("segment_id"))

        attributed_trips: dict[str, dict[str, Any]] = {}
        for row in hit_rows:
            trip = trip_of(row)
            attributed_trips.setdefault(trip, row)

        confirmed_rows = [row for row in attributed_trips.values() if row.get("county_confirmed")]
        attributed_by_grid: dict[str, int] = {}
        for row in attributed_trips.values():
            key = str(row.get("grid_geohash"))
            attributed_by_grid[key] = attributed_by_grid.get(key, 0) + 1

        # 按实际归属县汇总；调账后的分段挂在接续县，原确认县只留出处。
        attributed_by_county: dict[str, int] = {}
        for row in attributed_trips.values():
            key = str(row.get("effective_county_code") or "未确权")
            attributed_by_county[key] = attributed_by_county.get(key, 0) + 1

        # 候选距离与用于汇总的归属分开记录，便于分别复核。
        candidate_audit = [
            {
                "segment_id": row.get("segment_id"),
                "trip_id": trip_of(row),
                "candidate_grid_geohash": row.get("grid_geohash"),
                "candidate_distance": row.get("distance"),
                "candidate_type": row.get("candidate_type"),
                "candidate_distance_list": [
                    {"grid_geohash": c.get("grid_geohash"), "distance": c.get("distance")}
                    for c in row.get("candidates") or []
                ],
                "attributed_to_grid": row.get("grid_geohash") if row.get("hit_in_grid") else None,
                "attributed_county_code": row.get("effective_county_code"),
                "confirmed_county_code": row.get("confirmed_county_code"),
                "candidate_count": len(row.get("candidates") or []),
            }
            for row in join_rows
        ]

        summary = {
            "batch_key": batch_key,
            "region_id": region_id,
            "region_name": region.get("区域名称", "未知"),
            "region_level": region.get("区域级别", "未知"),
            "boundary_code": region.get("边界编码", ""),
            "grid_count": len(grids),
            "total_points": total_points,
            "region_center_lat": round(region_center_lat, 6),
            "region_center_lon": round(region_center_lon, 6),
            # 关联结果分层
            "join_total_segments": len(join_rows),
            "hit_in_grid_segments": [row.get("segment_id") for row in hit_rows],
            "distance_only_segments": [row.get("segment_id") for row in distance_rows],
            # 汇总归属只走命中格内，且一趟轨迹只计一次
            "attributed_trips": len(attributed_trips),
            "attributed_segments": [row.get("segment_id") for row in attributed_trips.values()],
            "county_confirmed_trips": len(confirmed_rows),
            "attributed_by_grid": attributed_by_grid,
            "attributed_by_county": attributed_by_county,
            "distance_only_candidates": [
                {
                    "segment_id": row.get("segment_id"),
                    "grid_geohash": row.get("grid_geohash"),
                    "distance": row.get("distance"),
                }
                for row in distance_rows
            ],
            "candidate_audit": candidate_audit,
            # 原已确认分段的出处，供县报送核对
            "confirmation_sources": {
                str(row.get("segment_id")): row.get("confirmation_id")
                for row in hit_rows if row.get("confirmation_id")
            },
            "generated_at": time.time(),
        }

        output_file = self._data_dir("region_aggregates") / f"{batch_key}_region.jsonl"
        written = self._write_jsonl_atomic(output_file, [summary])

        # 更新区域维度记录
        if region.get("id"):
            region["网格数量"] = len(grids)

        return {
            "batch_key": batch_key,
            "region_name": summary["region_name"],
            "grids_aggregated": len(grids),
            "total_points": total_points,
            "join_total_segments": len(join_rows),
            "hit_in_grid": len(hit_rows),
            "distance_only": len(distance_rows),
            "attributed_trips": len(attributed_trips),
            "county_confirmed_trips": len(confirmed_rows),
            "attributed_by_county": attributed_by_county,
            "review_items": len(self.list_review_items(batch_key, status="待复核")),
            "output_file": str(output_file),
            "records_written": written,
        }

    def _read_join_results(self, batch_key: str) -> list[dict[str, Any]]:
        try:
            join_svc = self._safe_import("app.services.spatial_join", "SpatialJoinService")()
            rows, _ = join_svc.get_join_results(batch_key)
            return rows
        except Exception:
            return self._read_jsonl(self._data_dir("spatial_join") / f"{batch_key}_join.jsonl")

    def get_region_aggregates(self, batch_key: str) -> tuple[list[dict[str, Any]], str]:
        """读取批次的区域聚合数据集文件（最近一次重算结果）。"""
        file_path = self._data_dir("region_aggregates") / f"{batch_key}_region.jsonl"
        data = self._read_jsonl(file_path)
        return data, str(file_path)

    # -- 县级归属与待复核的模块入口 --

    def register_county_confirmation(self, batch_key: str, segment_id: str | int,
                                     county_code: str, **kwargs: Any) -> dict[str, Any]:
        return super().register_county_confirmation(
            batch_key, segment_id, county_code, **kwargs)

    def get_county_report(self, batch_key: str, submitted: bool = False) -> dict[str, Any] | None:
        """读取最近一次县报核算结果。"""
        return super().get_county_report(batch_key, submitted=submitted)

    def get_review_detail(self, batch_key: str, status: str | None = None) -> dict[str, Any]:
        """无法确认归属的分段独立待复核明细，附带所属县报口径。"""
        items = self.list_review_items(batch_key, status=status)
        return {
            "batch_key": batch_key,
            "total": len(items),
            "items": items,
            "county_report": self.get_county_report(batch_key),
        }