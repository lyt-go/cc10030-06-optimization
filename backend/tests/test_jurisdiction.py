"""县级归属重算与调账测试：覆盖容差调整、候选分层、确认有效期与县报去重。"""
from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def setup_data_dir(monkeypatch):
    """每个用例使用独立的临时数据目录，避免账本与数据集互相污染。"""
    tmpdir = Path(tempfile.mkdtemp(prefix="geo_jurisdiction_"))
    import app.services.base as base_mod
    original_root = base_mod.DATA_ROOT
    base_mod.DATA_ROOT = tmpdir
    for sub in ["track_segments", "grid_index", "spatial_join", "region_aggregates",
                "jurisdiction", "checkpoints"]:
        (tmpdir / sub).mkdir(parents=True, exist_ok=True)
    yield tmpdir
    base_mod.DATA_ROOT = original_root
    if tmpdir.exists():
        shutil.rmtree(tmpdir, ignore_errors=True)


BATCH = "COUNTY-001"
SEGMENT_ID = "SEG-001"
GRID_A_HASH = "grid-a"
GRID_B_HASH = "grid-b"
COUNTY_A = "CN-甲县"
COUNTY_B = "CN-乙县"


def _seed_segment_and_grids():
    """构造两条相邻网格：分段中心落在甲县网格内，乙县网格只在更宽距离内。"""
    from app.services.spatial_join import SpatialJoinService

    svc = SpatialJoinService()
    seg_lat, seg_lon = 39.900000, 116.400000
    # 乙县网格中心向东约 2.5km，默认 500m 容差够不到，调宽到 3000m 才成为候选。
    seg_dir = svc._data_dir("track_segments")
    svc._write_jsonl_atomic(seg_dir / f"{BATCH}_segments.jsonl", [{
        "segment_id": SEGMENT_ID,
        "batch_key": BATCH,
        "terminal": "TERM-1",
        "start_time": 1_700_000_000,
        "end_time": 1_700_000_600,
        "start_lat": seg_lat,
        "end_lat": seg_lat,
        "start_lon": seg_lon,
        "end_lon": seg_lon,
        "point_count": 10,
        "trip_weight": 1,
    }])
    precision = 6
    grid_dir = svc._data_dir("grid_index")
    svc._write_jsonl_atomic(grid_dir / f"{BATCH}_grid.jsonl", [
        {
            "grid_id": 1,
            "geohash": svc._geohash_encode(seg_lat, seg_lon, precision),
            "batch_key": BATCH,
            "precision": precision,
            "level": "L6",
            "center_lat": seg_lat,
            "center_lon": seg_lon,
            "point_count": 10,
        },
        {
            "grid_id": 2,
            "geohash": svc._geohash_encode(39.900000, 116.429000, precision),
            "batch_key": BATCH,
            "precision": precision,
            "level": "L6",
            "center_lat": 39.900000,
            "center_lon": 116.429000,
            "point_count": 8,
        },
    ])
    return svc


def _grid_hashes():
    from app.services.spatial_join import SpatialJoinService
    svc = SpatialJoinService()
    grids = svc._load_grids(BATCH)
    return grids[0]["geohash"], grids[1]["geohash"]


class TestSpatialJoinRecompute:
    def test_recompute_keeps_hit_and_distance_candidates_separate(self):
        """调宽阈值后重算：命中格内与距离内候选分开，且结果覆盖而非追加。"""
        svc = _seed_segment_and_grids()
        grid_a, grid_b = _grid_hashes()

        first = svc.join_trajectories_to_grids(BATCH, distance_tolerance=500)
        assert first["matches"] == 1
        assert first["hit_in_grid"] == 1
        assert first["distance_only"] == 0

        # 调整距离容差重算
        second = svc.join_trajectories_to_grids(BATCH, distance_tolerance=3000)
        assert second["matches"] == 1
        assert second["hit_in_grid"] == 1
        rows, _ = svc.get_join_results(BATCH)
        # 覆盖写入：同一分段只保留一行，不因重算而重复
        assert len(rows) == 1
        row = rows[0]
        assert row["candidate_type"] == "命中格内"
        assert row["grid_geohash"] == grid_a
        assert {c["grid_geohash"] for c in row["candidates"]} == {grid_a, grid_b}
        # 只有仅距离内的候选进入待复核
        assert [c["grid_geohash"] for c in row["unresolved_candidates"]] == [grid_b]

    def test_original_confirmation_survives_widened_tolerance(self):
        """上一班已确认的结果在调宽阈值后不被改写，新候选只登记待确权。"""
        svc = _seed_segment_and_grids()
        grid_a, grid_b = _grid_hashes()
        svc.register_grid_county(BATCH, grid_a, COUNTY_A)
        svc.register_grid_county(BATCH, grid_b, COUNTY_B)
        svc.register_county_confirmation(
            BATCH, SEGMENT_ID, COUNTY_A, grid_geohash=grid_a, trip_id="TRIP-1")

        svc.join_trajectories_to_grids(BATCH, distance_tolerance=3000)
        row = svc.get_join_results(BATCH)[0][0]
        assert row["grid_geohash"] == grid_a
        assert row["county_confirmed"] is True
        assert row["confirmation_id"] is not None

        confirmation = svc.get_county_confirmation(BATCH, SEGMENT_ID)
        assert confirmation["county_code"] == COUNTY_A
        assert confirmation["status"] == "有效"
        assert svc.is_confirmation_valid(confirmation)

        items = svc.list_review_items(BATCH)
        assert len(items) == 1
        assert items[0]["candidates"][0]["grid_geohash"] == grid_b
        assert items[0]["status"] == "待复核"
        assert items[0]["confirmed_county_code"] == COUNTY_A


class TestCountyReportDispositions:
    def _prepare(self):
        from app.services.region_dim import RegionDimService
        svc = _seed_segment_and_grids()
        grid_a, grid_b = _grid_hashes()
        svc.register_grid_county(BATCH, grid_a, COUNTY_A)
        svc.register_grid_county(BATCH, grid_b, COUNTY_B)
        svc.join_trajectories_to_grids(BATCH, distance_tolerance=3000)
        svc.register_county_confirmation(
            BATCH, SEGMENT_ID, COUNTY_A, grid_geohash=grid_a, trip_id="TRIP-1")
        # 重新关联一次，让关联结果带上确认出处。
        svc.join_trajectories_to_grids(BATCH, distance_tolerance=3000)
        return svc, RegionDimService()

    def test_unsubmitted_report_three_dispositions(self):
        """未提交的下期县报：继续纳入甲县 / 暂挂保留出处 / 待新证据后调账三选一。"""
        svc, _ = self._prepare()

        held = svc.compute_county_report(BATCH, disposition="hold_under_confirmation")["report"]
        held_a = next(row for row in held["counties"] if row["county_code"] == COUNTY_A)
        assert held_a["held_trips"] == 1
        assert held_a["confirmed_trips"] == 0
        assert held["total_booked"] == 0
        assert held["double_booked"] is False

        continued = svc.compute_county_report(BATCH, disposition="continue_confirmed")["report"]
        cont_a = next(row for row in continued["counties"] if row["county_code"] == COUNTY_A)
        assert cont_a["confirmed_trips"] == 1
        assert cont_a["booked_trips"] == 1
        assert continued["total_booked"] == 1
        assert continued["double_booked"] is False

        awaiting = svc.compute_county_report(BATCH, disposition="await_adjustment")["report"]
        await_a = next(row for row in awaiting["counties"] if row["county_code"] == COUNTY_A)
        assert await_a["pending_evidence_trips"] == 1
        assert await_a["booked_trips"] == 0

    def test_submitted_report_not_recomputed(self):
        """已提交的县报不再重算，避免同一趟被扣第二次。"""
        svc, _ = self._prepare()
        first = svc.compute_county_report(BATCH, submitted=True)
        assert first["ok"] is True
        snapshot = first["report"]

        second = svc.compute_county_report(BATCH, submitted=True)
        assert second["ok"] is False
        assert second["report"] == snapshot


class TestAdjustmentAndReconciliation:
    def _prepare(self):
        from app.services.region_dim import RegionDimService
        svc = _seed_segment_and_grids()
        grid_a, grid_b = _grid_hashes()
        svc.register_grid_county(BATCH, grid_a, COUNTY_A)
        svc.register_grid_county(BATCH, grid_b, COUNTY_B)
        svc.register_county_confirmation(
            BATCH, SEGMENT_ID, COUNTY_A, grid_geohash=grid_a, trip_id="TRIP-1")
        svc.join_trajectories_to_grids(BATCH, distance_tolerance=3000)
        svc.compute_county_report(BATCH, disposition="hold_under_confirmation")
        review = svc.list_review_items(BATCH)[0]
        return svc, RegionDimService(), review

    def test_evidence_gap_blocks_adjustment(self):
        """边界证据不完整时不允许调账。"""
        svc, _, review = self._prepare()
        partial = svc.attach_review_evidence(
            BATCH, review["review_id"], evidence_ref="EVID-1",
            geometry_evidence={"boundary_overlap_ratio": 0.2})
        assert partial["ok"] is False
        assert partial["review_item"]["status"] == "证据待补齐"
        assert svc.get_county_report(BATCH)["total_booked"] == 0

    def test_adjust_once_and_never_double_deduct(self):
        """补齐证据后调账一次：移出旧归属、转入新归属，且暂挂份额不再扣第二次。"""
        svc, region, review = self._prepare()
        evidence = {"boundary_overlap_ratio": 0.85, "crosses_boundary": True}
        attached = svc.attach_review_evidence(
            BATCH, review["review_id"], evidence_ref="EVID-1", geometry_evidence=evidence)
        assert attached["ok"] is True

        first = svc.apply_jurisdiction_adjustment(
            BATCH, review["review_id"], to_county_code=COUNTY_B, evidence_ref="EVID-1")
        assert first["ok"] is True
        adjustment = first["adjustment"]
        assert adjustment["from_county_code"] == COUNTY_A
        assert adjustment["to_county_code"] == COUNTY_B
        assert adjustment["trip_weight"] == 1

        # 原确认不撤销，只记录被新归属接续
        confirmation = svc.get_county_confirmation(BATCH, SEGMENT_ID)
        assert confirmation["status"] == "有效"
        assert confirmation["county_code"] == COUNTY_A
        assert confirmation["superseded_by"] == COUNTY_B

        # 重复补交同一份证据：不再记账
        again = svc.apply_jurisdiction_adjustment(
            BATCH, review["review_id"], to_county_code=COUNTY_B, evidence_ref="EVID-1")
        assert again["ok"] is False
        assert len(svc.list_adjustments(BATCH)) == 1

        # 重新对账：甲县不再计为已入账或暂挂，乙县记 1 趟，总数仍为 1
        report = svc.compute_county_report(BATCH)["report"]
        row_a = next(row for row in report["counties"] if row["county_code"] == COUNTY_A)
        row_b = next(row for row in report["counties"] if row["county_code"] == COUNTY_B)
        assert row_a["booked_trips"] == 0
        assert row_a["held_trips"] == 0
        assert row_a["adjusted_out_trips"] == 1
        assert row_b["booked_trips"] == 1
        assert row_b["adjusted_in_trips"] == 1
        assert report["total_booked"] == 1
        assert report["double_booked"] is False
        assert report["unresolved_trip_ids"] == []

        # 区域维度按一趟轨迹只计一次，候选距离与归属分开留档
        result = region.aggregate_by_region(BATCH)
        assert result["join_total_segments"] == 1
        assert result["hit_in_grid"] == 1
        assert result["attributed_trips"] == 1
        agg = region.get_region_aggregates(BATCH)[0][0]
        assert agg["distance_only_segments"] == []
        assert agg["candidate_audit"][0]["candidate_distance"] is not None
        assert agg["candidate_audit"][0]["attributed_to_grid"] is not None

    def test_review_detail_is_independent(self):
        """无法确认归属的分段有独立的待复核明细，与县报分开查询。"""
        svc, region, review = self._prepare()
        detail = region.get_review_detail(BATCH)
        assert detail["total"] == 1
        assert detail["items"][0]["review_id"] == review["review_id"]
        assert detail["items"][0]["candidates"]
        assert detail["county_report"] is not None


class TestConfirmationValidity:
    def test_confirmation_still_valid_next_period(self):
        """甲县确认登记可续用有效期，下期仍有效。"""
        svc = _seed_segment_and_grids()
        grid_a, _ = _grid_hashes()
        now = time.time()
        svc.register_county_confirmation(
            BATCH, SEGMENT_ID, COUNTY_A, grid_geohash=grid_a,
            trip_id="TRIP-1", ttl_sec=90 * 24 * 3600)
        confirmation = svc.get_county_confirmation(BATCH, SEGMENT_ID)
        next_period = now + 30 * 24 * 3600
        assert svc.is_confirmation_valid(confirmation, at=next_period) is True
        assert svc.is_confirmation_valid(confirmation, at=now + 120 * 24 * 3600) is False

        # 续用同一县的确认只延长有效期，不改变原归属
        renewed = svc.register_county_confirmation(
            BATCH, SEGMENT_ID, COUNTY_A, grid_geohash=grid_a, ttl_sec=30 * 24 * 3600)
        assert renewed["ok"] is True
        assert renewed["confirmation"]["county_code"] == COUNTY_A
        assert renewed["confirmation"]["valid_until"] > confirmation["valid_until"]

    def test_cannot_silently_reassign_county(self):
        """已在甲县确认的分段不能直接改登记成乙县，必须走调账。"""
        svc = _seed_segment_and_grids()
        grid_a, grid_b = _grid_hashes()
        svc.register_county_confirmation(
            BATCH, SEGMENT_ID, COUNTY_A, grid_geohash=grid_a, trip_id="TRIP-1")
        conflict = svc.register_county_confirmation(
            BATCH, SEGMENT_ID, COUNTY_B, grid_geohash=grid_b, trip_id="TRIP-1")
        assert conflict["ok"] is False
        assert svc.get_county_confirmation(BATCH, SEGMENT_ID)["county_code"] == COUNTY_A
