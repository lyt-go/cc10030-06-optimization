"""单趟轨迹在调宽阈值后的口径回归：把题干里的场景按到账顺序走一遍。

场景：
1. 甲县先确认原一趟行程的归属，并登记可续用有效期，下期仍在有效期内；
2. 调宽距离容差后，乙县网格成为同一条分段的第二个候选；乙县暂未确权，
   甲县确认没有撤销；
3. 只对尚未提交的下期县报核算，分别验证继续纳入甲县 1 趟、暂挂并保留出处、
   待新证据后调账三种口径；
4. 补交乙县确权后再次对账，不能从尚未记账的县报扣第二次，也不能两县都入账。
"""
from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def setup_data_dir(monkeypatch):
    tmpdir = Path(tempfile.mkdtemp(prefix="geo_scenario_"))
    import app.services.base as base_mod
    original_root = base_mod.DATA_ROOT
    base_mod.DATA_ROOT = tmpdir
    for sub in ["track_segments", "grid_index", "spatial_join", "region_aggregates",
                "jurisdiction"]:
        (tmpdir / sub).mkdir(parents=True, exist_ok=True)
    yield tmpdir
    base_mod.DATA_ROOT = original_root
    if tmpdir.exists():
        shutil.rmtree(tmpdir, ignore_errors=True)


BATCH = "COUNTY-SCENARIO"
SEGMENT = "SEG-A"
TRIP = "TRIP-A"
COUNTY_A = "甲县"
COUNTY_B = "乙县"


def _setup():
    from app.services.spatial_join import SpatialJoinService
    svc = SpatialJoinService()
    lat, lon = 39.900000, 116.400000
    seg_file = svc._data_dir("track_segments") / f"{BATCH}_segments.jsonl"
    svc._write_jsonl_atomic(seg_file, [{
        "segment_id": SEGMENT, "batch_key": BATCH, "terminal": "T1",
        "start_time": 1_700_000_000, "end_time": 1_700_000_600,
        "start_lat": lat, "end_lat": lat, "start_lon": lon, "end_lon": lon,
        "point_count": 12, "trip_weight": 1,
    }])
    grid_file = svc._data_dir("grid_index") / f"{BATCH}_grid.jsonl"
    hash_a = svc._geohash_encode(lat, lon, 6)
    hash_b = svc._geohash_encode(39.9, 116.429, 6)
    svc._write_jsonl_atomic(grid_file, [
        {"grid_id": 1, "geohash": hash_a, "batch_key": BATCH, "precision": 6,
         "level": "L6", "center_lat": lat, "center_lon": lon, "point_count": 12},
        # 相邻县边界贴近：乙县网格中心约 2.5km 外，宽阈值才成为候选。
        {"grid_id": 2, "geohash": hash_b, "batch_key": BATCH, "precision": 6,
         "level": "L6", "center_lat": 39.9, "center_lon": 116.429, "point_count": 9},
    ])
    svc.register_grid_county(BATCH, hash_a, COUNTY_A)
    svc.register_grid_county(BATCH, hash_b, COUNTY_B)
    return svc, hash_a, hash_b


def test_confirmation_renewable_and_valid_next_period():
    svc, hash_a, _ = _setup()
    svc.register_county_confirmation(
        BATCH, SEGMENT, COUNTY_A, grid_geohash=hash_a, trip_id=TRIP, ttl_sec=30 * 86400)
    renewed = svc.register_county_confirmation(
        BATCH, SEGMENT, COUNTY_A, grid_geohash=hash_a, ttl_sec=60 * 86400)
    confirmation = renewed["confirmation"]
    next_period = time.time() + 40 * 86400
    assert svc.is_confirmation_valid(confirmation, at=next_period) is True
    assert confirmation["county_code"] == COUNTY_A


def test_widened_tolerance_keeps_original_result_and_flags_candidate():
    svc, hash_a, hash_b = _setup()
    svc.register_county_confirmation(
        BATCH, SEGMENT, COUNTY_A, grid_geohash=hash_a, trip_id=TRIP)

    narrow = svc.join_trajectories_to_grids(BATCH, distance_tolerance=500)
    assert narrow["hit_in_grid"] == 1

    wide = svc.join_trajectories_to_grids(BATCH, distance_tolerance=3000)
    assert wide["matches"] == 1
    row, _ = svc.get_join_results(BATCH)
    assert len(row) == 1
    assert row[0]["grid_geohash"] == hash_a
    assert row[0]["county_code"] == COUNTY_A
    assert [c["grid_geohash"] for c in row[0]["candidates"]] == [hash_a, hash_b]
    assert [c["grid_geohash"] for c in row[0]["unresolved_candidates"]] == [hash_b]

    items = svc.list_review_items(BATCH)
    assert len(items) == 1
    assert items[0]["status"] == "待复核"
    assert items[0]["confirmed_county_code"] == COUNTY_A
    assert items[0]["candidate_county_code"] == COUNTY_B
    # 甲县确认没有撤销
    assert svc.get_county_confirmation(BATCH, SEGMENT)["county_code"] == COUNTY_A


@pytest.mark.parametrize(
    "disposition,field,expected",
    [
        ("continue_confirmed", "confirmed_trips", 1),
        ("hold_under_confirmation", "held_trips", 1),
        ("await_adjustment", "pending_evidence_trips", 1),
    ],
)
def test_unsubmitted_report_three_choices(disposition, field, expected):
    svc, hash_a, hash_b = _setup()
    svc.register_county_confirmation(
        BATCH, SEGMENT, COUNTY_A, grid_geohash=hash_a, trip_id=TRIP)
    svc.join_trajectories_to_grids(BATCH, distance_tolerance=3000)

    report = svc.compute_county_report(BATCH, disposition=disposition)["report"]
    row_a = next(row for row in report["counties"] if row["county_code"] == COUNTY_A)
    assert row_a[field] == expected
    assert report["total_trips"] == 1
    if field != "confirmed_trips":
        assert row_a["booked_trips"] == 0
        assert report["total_booked"] == 0


def test_reconciliation_after_county_b_evidence():
    svc, hash_a, hash_b = _setup()
    svc.register_county_confirmation(
        BATCH, SEGMENT, COUNTY_A, grid_geohash=hash_a, trip_id=TRIP)
    svc.join_trajectories_to_grids(BATCH, distance_tolerance=3000)
    svc.compute_county_report(BATCH, disposition="hold_under_confirmation")

    item = svc.list_review_items(BATCH)[0]
    svc.attach_review_evidence(
        BATCH, item["review_id"], evidence_ref="EVID-B",
        geometry_evidence={"boundary_overlap_ratio": 0.9, "crosses_boundary": True})
    adjusted = svc.apply_jurisdiction_adjustment(
        BATCH, item["review_id"], to_county_code=COUNTY_B, evidence_ref="EVID-B")
    assert adjusted["ok"] is True

    # 补交确权后再次对账：乙县入账 1 趟，甲县不再入账也不暂挂，同一趟只算一次。
    report = svc.compute_county_report(BATCH, disposition="hold_under_confirmation")["report"]
    row_a = next(row for row in report["counties"] if row["county_code"] == COUNTY_A)
    row_b = next(row for row in report["counties"] if row["county_code"] == COUNTY_B)
    assert (row_b["booked_trips"], row_b["adjusted_in_trips"]) == (1, 1)
    assert (row_a["booked_trips"], row_a["held_trips"], row_a["adjusted_out_trips"]) == (0, 0, 1)
    assert report["total_booked"] == 1
    assert report["double_booked"] is False
    assert report["unresolved_trip_ids"] == []

    # 原确认保留出处，不撤销
    confirmation = svc.get_county_confirmation(BATCH, SEGMENT)
    assert confirmation["county_code"] == COUNTY_A
    assert confirmation["status"] == "有效"
    assert confirmation["superseded_by"] == COUNTY_B


def test_report_records_are_separate_for_candidate_distance_vs_attribution():
    from app.services.region_dim import RegionDimService

    svc, hash_a, hash_b = _setup()
    svc.register_county_confirmation(
        BATCH, SEGMENT, COUNTY_A, grid_geohash=hash_a, trip_id=TRIP)
    svc.join_trajectories_to_grids(BATCH, distance_tolerance=3000)

    result = RegionDimService().aggregate_by_region(BATCH)
    assert result["attributed_trips"] == 1
    agg = RegionDimService().get_region_aggregates(BATCH)[0][0]
    audit = agg["candidate_audit"][0]
    # 候选距离列表与归属分开：两个候选距离都留档，归属只走命中格内。
    assert len(audit["candidate_distance_list"]) == 2
    assert audit["attributed_to_grid"] == hash_a
    assert audit["attributed_county_code"] == COUNTY_A
    detail = RegionDimService().get_review_detail(BATCH)
    assert detail["total"] == 1
    assert "county_report" in detail
