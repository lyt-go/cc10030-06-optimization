"""空间关联领域服务：将两套空间数据集按距离容差执行关联匹配，生成空间关联结果数据集。

轨迹分段与网格的关联分两层口径：

- **命中格内**：分段中心落在网格 geohash 边界内，属于已经符合属地标准的分段，
  只有这一层可以进入县级确认口径；
- **距离内候选**：中心不在格内、但与网格中心的距离不超过容差，只能算候选，
  必须留下独立待复核明细，不能直接当作县级归属。

调整距离容差后重算会以覆盖方式重写结果文件；原已确认的分段继续沿用原确认，
新增的距离内候选只登记为“待确权”，不会静默改写上一班已确认的结果。
"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.services.jurisdiction import JurisdictionLedgerMixin
from app.store import store


class SpatialJoinService(BaseModuleService, JurisdictionLedgerMixin):
    MODULE = "spatial_join"
    ENTITY = "关联任务"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["任务编号", "左数据集", "右数据集", "关联方式", "距离容差", "匹配条数", "未匹配条数", "关联版本"]
    REQUIRED_FIELDS = ["任务编号", "左数据集", "右数据集"]
    STATUS_ORDER = ["草稿", "待关联", "关联中", "已完成"]
    ACTION_RULES = {"启动关联": "关联中", "标记完成": "已完成", "标记失败": "已完成"}
    NEGATIVE_ACTIONS = []

    # -- 数据集读取 --

    def _load_segments(self, batch_key: str) -> list[dict[str, Any]]:
        try:
            seg_svc = self._safe_import("app.services.track_segment", "TrackSegmentService")()
            segments, _ = seg_svc.get_segments(batch_key)
        except Exception:
            seg_file = self._data_dir("track_segments") / f"{batch_key}_segments.jsonl"
            if not seg_file.exists():
                raise FileNotFoundError(f"批次 {batch_key} 的轨迹分段数据不存在")
            segments = self._read_jsonl(seg_file)
        return segments

    def _load_grids(self, batch_key: str) -> list[dict[str, Any]]:
        try:
            grid_svc = self._safe_import("app.services.grid_index", "GridIndexService")()
            grids, _ = grid_svc.get_grid_index(batch_key)
        except Exception:
            grids_file = self._data_dir("grid_index") / f"{batch_key}_grid.jsonl"
            if not grids_file.exists():
                raise FileNotFoundError(f"批次 {batch_key} 的网格索引数据不存在")
            grids = self._read_jsonl(grids_file)
        return grids

    # -- 归格判定 --

    @staticmethod
    def _segment_hits_grid(seg_lat: float, seg_lon: float, grid: dict[str, Any]) -> bool:
        """判断分段中心是否真正落在网格边界内（而不是仅仅在距离容差内）。

        网格由 geohash 生成，因此用同精度 geohash 前缀判断边界包含关系；
        只要分段中心的 geohash 以网格 geohash 开头，就视为命中格内。
        """
        grid_hash = str(grid.get("geohash") or "")
        if not grid_hash:
            return False
        precision = int(grid.get("precision") or len(grid_hash))
        seg_hash = BaseModuleService._geohash_encode(seg_lat, seg_lon, precision)
        return seg_hash == grid_hash

    # -- 关联主流程 --

    def join_trajectories_to_grids(self, batch_key: str, distance_tolerance: float = 500.0) -> dict[str, Any]:
        """将轨迹分段数据集与网格索引数据集按距离容差执行空间关联，生成轨迹-网格关联数据集。

        结果按“命中格内 / 距离内候选”分层；重算使用覆盖写入，原确认继续沿用。
        """
        segments = self._load_segments(batch_key)
        grids = self._load_grids(batch_key)

        if not segments or not grids:
            return {
                "batch_key": batch_key,
                "total_segments": len(segments),
                "total_grids": len(grids),
                "matches": 0,
                "unmatched": len(segments),
                "hit_in_grid": 0,
                "distance_only": 0,
            }

        ledger = self._load_ledger(batch_key)
        confirmations = ledger["confirmations"]
        existing_results = {
            row["segment_id"]: row for row in self._read_jsonl(self._join_file(batch_key))
        }

        matches: list[dict[str, Any]] = []
        unmatched = 0
        hit_count = 0
        distance_only_count = 0
        pending_review: list[dict[str, Any]] = []
        now = time.time()

        for seg in segments:
            seg_lat = (seg["start_lat"] + seg["end_lat"]) / 2
            seg_lon = (seg["start_lon"] + seg["end_lon"]) / 2
            seg_id = str(seg["segment_id"])

            # 距离容差内的全部网格候选，按距离升序。
            candidates = []
            for grid in grids:
                dist = self._haversine(seg_lat, seg_lon, grid["center_lat"], grid["center_lon"])
                if dist <= distance_tolerance:
                    candidates.append({
                        "grid_id": grid["grid_id"],
                        "grid_geohash": grid["geohash"],
                        "grid_center_lat": grid["center_lat"],
                        "grid_center_lon": grid["center_lon"],
                        "distance": round(dist, 2),
                        "hit_in_grid": self._segment_hits_grid(seg_lat, seg_lon, grid),
                    })
            candidates.sort(key=lambda item: item["distance"])

            if not candidates:
                unmatched += 1
                continue

            confirmed = confirmations.get(seg_id)
            confirmed_hash = confirmed.get("grid_geohash") if confirmed else None
            confirmed_valid = self.is_confirmation_valid(confirmed, now)

            # 上一班已确认的分段沿用原确认网格，即使当前容差下它不再是最近候选；
            # 这让“调宽阈值后新增候选”不会悄悄改写原结果。
            confirmed_candidate = None
            if confirmed_hash:
                confirmed_candidate = next(
                    (c for c in candidates if c["grid_geohash"] == confirmed_hash), None)

            # 命中格内优先：有格内候选时只在格内候选里选，距离内候选永远不能
            # 顶替成归属格。
            in_grid_candidates = [c for c in candidates if c["hit_in_grid"]]
            if confirmed_candidate is not None:
                selected = confirmed_candidate
            elif in_grid_candidates:
                selected = in_grid_candidates[0]
            else:
                selected = candidates[0]
            hit_in_grid = selected["hit_in_grid"]
            candidate_type = "命中格内" if hit_in_grid else "距离内候选"
            if hit_in_grid:
                hit_count += 1
            else:
                distance_only_count += 1

            # 只有命中格内的分段才有县级确认口径；距离内候选不能直接归属。
            county_code = self.get_grid_county(batch_key, selected["grid_geohash"]) if hit_in_grid else None

            # 调宽阈值后多出来的候选：原确认没覆盖的，只登记为待确权，不改原结果。
            # 旧结果里已经作为归属格出现过的网格不算新候选，保证重新关联幂等。
            previously_selected = str(
                ((existing_results.get(seg_id) or {}).get("grid_geohash")) or "")
            covered_hashes = {selected["grid_geohash"], previously_selected} - {""}
            unresolved_candidates = [
                c for c in candidates if c["grid_geohash"] not in covered_hashes
            ]

            # 调账后的实际归属单独给出，原确认的县仍留在 county_code 里不撤销。
            # county_confirmed 只认原确认未被接续的分段，调账后由接续县计入汇总。
            superseded_by = (confirmed or {}).get("superseded_by")
            record = {
                "segment_id": seg["segment_id"],
                "batch_key": batch_key,
                "grid_geohash": selected["grid_geohash"],
                "grid_id": selected["grid_id"],
                "seg_center_lat": seg_lat,
                "seg_center_lon": seg_lon,
                "grid_center_lat": selected["grid_center_lat"],
                "grid_center_lon": selected["grid_center_lon"],
                "distance": selected["distance"],
                "candidate_type": candidate_type,
                "hit_in_grid": hit_in_grid,
                "county_code": county_code,
                "county_confirmed": bool(county_code and confirmed_valid and not superseded_by),
                "confirmation_id": (confirmed or {}).get("confirmation_id"),
                # 调账后的实际归属单独给出，原确认的县仍留在 county_code 里不撤销。
                "confirmed_county_code": (confirmed or {}).get("county_code"),
                "effective_county_code": (
                    superseded_by
                    or (confirmed or {}).get("county_code") or county_code),
                "confirmation_status": (
                    "已接续" if superseded_by
                    else ("有效" if confirmed_valid else ("已过期" if confirmed else None))),
                "trip_id": (confirmed or {}).get("trip_id") or seg.get("trip_id") or seg["segment_id"],
                "candidates": candidates,
                "unresolved_candidates": unresolved_candidates,
                "tolerance": distance_tolerance,
                "matched_at": now,
            }

            # 保留上一班已确认结果的出处，便于对照原确认与新增候选。
            previous = existing_results.get(str(seg["segment_id"]))
            if previous and previous.get("grid_geohash"):
                record["previous_grid_geohash"] = previous["grid_geohash"]
                record["previous_candidate_type"] = previous.get("candidate_type")
                record["recomputed"] = previous["grid_geohash"] != selected["grid_geohash"]
            matches.append(record)

            # 无法确认归属的分段统一进入待复核明细：
            # 1) 只在距离内、没有命中任何格的分段，无论是否有确认都不能直接归属；
            # 2) 调宽阈值后新增的候选，原确认没覆盖的，只登记待确权。
            if not hit_in_grid:
                pending_review.append({
                    "segment_id": seg_id,
                    "trip_id": (confirmed or {}).get("trip_id") or seg.get("trip_id") or seg_id,
                    "grid_geohash": selected["grid_geohash"],
                    "current_county_code": None,
                    "candidate_county_code": self.get_grid_county(
                        batch_key, selected["grid_geohash"]),
                    "confirmed_county_code": (confirmed or {}).get("county_code"),
                    "confirmation_id": (confirmed or {}).get("confirmation_id"),
                    "reason": "仅在距离容差内，未命中网格边界",
                    "hold_scope": "county_report",
                    "trip_weight": int(seg.get("trip_weight", 1) or 1),
                    "candidates": candidates,
                })
            elif unresolved_candidates:
                nearest = unresolved_candidates[0]
                pending_review.append({
                    "segment_id": seg_id,
                    "trip_id": (confirmed or {}).get("trip_id") or seg.get("trip_id") or seg_id,
                    "grid_geohash": nearest["grid_geohash"],
                    "current_county_code": (
                        self.get_grid_county(batch_key, nearest["grid_geohash"])
                        if nearest["hit_in_grid"] else None
                    ),
                    "candidate_county_code": self.get_grid_county(
                        batch_key, nearest["grid_geohash"]),
                    "confirmed_county_code": (confirmed or {}).get("county_code"),
                    "confirmation_id": (confirmed or {}).get("confirmation_id"),
                    "reason": "调宽阈值后出现新候选，尚未确权",
                    "hold_scope": "county_report",
                    "trip_weight": int(seg.get("trip_weight", 1) or 1),
                    "candidates": unresolved_candidates,
                })

        # 覆盖写入：调整阈值后的重算只保留最近一次完整结果。
        written = self._write_jsonl_atomic(self._join_file(batch_key), matches)

        # 为无法确认归属的分段生成独立待复核明细。
        for item in pending_review:
            self.open_review_item(batch_key, **item)
        # 重算后不再有歧义的分段收口旧明细，避免待复核清单只增不减。
        self.close_stale_review_items(
            batch_key, {item["segment_id"] for item in pending_review})

        hit_in_grid_segments = [
            row["segment_id"] for row in matches if row["hit_in_grid"]]
        distance_only_segments = [
            row["segment_id"] for row in matches if not row["hit_in_grid"]]

        self._upsert_join_task(batch_key, distance_tolerance, written, unmatched)

        return {
            "batch_key": batch_key,
            "total_segments": len(segments),
            "total_grids": len(grids),
            "matches": written,
            "unmatched": unmatched,
            "hit_in_grid": hit_count,
            "distance_only": distance_only_count,
            "hit_in_grid_segments": hit_in_grid_segments,
            "distance_only_segments": distance_only_segments,
            "county_confirmed_segments": [
                row["segment_id"] for row in matches if row["county_confirmed"]],
            "review_items": len(pending_review),
            "output_file": str(self._join_file(batch_key)),
        }

    def _join_file(self, batch_key: str):
        return self._data_dir("spatial_join") / f"{batch_key}_join.jsonl"

    def _upsert_join_task(
        self,
        batch_key: str,
        distance_tolerance: float,
        matched: int,
        unmatched: int,
    ) -> dict[str, Any]:
        """同一批次只维护一个关联任务，重算时更新计数与版本，不重复登记。"""
        task_code = f"JOIN-{batch_key}"
        existing = next(
            (row for row in store.rows(self.MODULE) if row.get("任务编号") == task_code), None)
        if existing is not None:
            existing["距离容差"] = f"{distance_tolerance}m"
            existing["匹配条数"] = str(matched)
            existing["未匹配条数"] = str(unmatched)
            version = str(existing.get("关联版本") or "v1.0")
            try:
                major, minor = version.lstrip("v").split(".")
                existing["关联版本"] = f"v{major}.{int(minor) + 1}"
            except (ValueError, AttributeError):
                existing["关联版本"] = "v1.1"
            return existing
        entry, _ = self.create_entry({
            "任务编号": task_code,
            "左数据集": f"track_segments/{batch_key}",
            "右数据集": f"grid_index/{batch_key}",
            "关联方式": "distance_spatial_join",
            "距离容差": f"{distance_tolerance}m",
            "匹配条数": str(matched),
            "未匹配条数": str(unmatched),
            "关联版本": "v1.0",
        })
        if entry:
            self.run_action(entry["id"], "标记完成")
        return entry or {}

    def get_join_results(self, batch_key: str) -> tuple[list[dict[str, Any]], str]:
        """读取批次的空间关联结果数据集文件（最近一次重算结果）。"""
        file_path = self._join_file(batch_key)
        data = self._read_jsonl(file_path)
        return data, str(file_path)

    def get_join_summary(self, batch_key: str) -> dict[str, Any]:
        """汇总命中格内与距离内候选，供区域维度消费。"""
        rows, _ = self.get_join_results(batch_key)
        hit = [row for row in rows if row.get("hit_in_grid")]
        distance_only = [row for row in rows if not row.get("hit_in_grid")]
        return {
            "batch_key": batch_key,
            "total_segments": len(rows),
            "hit_in_grid": len(hit),
            "distance_only": len(distance_only),
            "county_confirmed": sum(1 for row in rows if row.get("county_confirmed")),
            "hit_in_grid_segments": [row["segment_id"] for row in hit],
            "distance_only_segments": [row["segment_id"] for row in distance_only],
        }