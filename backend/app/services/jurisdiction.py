"""跨模块证据与对账账本：把县级确认、候选复核、调账和县报核算落到独立的JSON账本。

这个账本不引入新的业务模块，只作为 `data/jurisdiction` 下的运行期状态，由
`spatial_join` 与 `region_dim` 两个服务共同读写：

- 县级确认登记（confirmation）：某条原已确认分段归属哪个县、确认的是哪个网格、
  有效期到什么时候；
- 待复核明细（review_item）：边界证据补齐前无法确认归属的分段，以及只在距离
  容差内、尚未符合属地标准的候选；
- 调账记录（adjustment）：从旧归属移出、转入新归属的实际贡献，带幂等键，
  同一证据只能记一次；
- 县报（county_report）：县级上报口径，区分已确权、暂挂、待调账三种状态，
  保证一趟轨迹在上级统计里只计一次。

所有写入都走 `_save` 的原子覆盖，读取方不会看到半份账本。
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

STATE_VERSION = 1

# 县级归属确认的默认有效期（秒），默认 180 天；口径可续用。
DEFAULT_CONFIRMATION_TTL_SEC = 180 * 24 * 3600


class JurisdictionLedgerMixin:
    """县级归属确认、待复核明细、调账与县报核算的共享账本。"""

    # -- 账本读写 --

    def _jurisdiction_file(self, batch_key: str) -> Path:
        return self._data_dir("jurisdiction") / f"{batch_key}_jurisdiction.json"

    def _load_ledger(self, batch_key: str) -> dict[str, Any]:
        ledger = self._read_json(self._jurisdiction_file(batch_key))
        if not isinstance(ledger, dict) or ledger.get("version") != STATE_VERSION:
            ledger = {
                "version": STATE_VERSION,
                "batch_key": batch_key,
                "confirmations": {},
                "review_items": {},
                "adjustments": [],
                "county_reports": {},
                "grid_counties": {},
                "updated_at": time.time(),
            }
        # 兼容手工构造或旧版本账本：缺失的键补空结构。
        ledger.setdefault("confirmations", {})
        ledger.setdefault("review_items", {})
        ledger.setdefault("adjustments", [])
        ledger.setdefault("county_reports", {})
        ledger.setdefault("grid_counties", {})
        ledger.setdefault("confirmation_history", [])
        ledger.setdefault("audit_log", [])
        return ledger

    def _save_ledger(self, batch_key: str, ledger: dict[str, Any]) -> Path:
        ledger["updated_at"] = time.time()
        path = self._jurisdiction_file(batch_key)
        self._write_json(path, ledger)
        return path

    @staticmethod
    def _evidence_key(*parts: Any) -> str:
        raw = "|".join(str(part) for part in parts)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    # -- 县级确认登记 --

    def register_county_confirmation(
        self,
        batch_key: str,
        segment_id: str | int,
        county_code: str,
        *,
        grid_geohash: str | None = None,
        trip_id: str | None = None,
        ttl_sec: int = DEFAULT_CONFIRMATION_TTL_SEC,
        evidence_ref: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """登记一条原已确认分段的县级归属，并给出可续用的有效期。

        同一分段重复登记只延长有效期，不覆盖原确认的县与网格，避免把上一班
        已确认的结果悄悄改掉。
        """
        ledger = self._load_ledger(batch_key)
        seg_key = str(segment_id)
        now = time.time()
        existing = ledger["confirmations"].get(seg_key)
        if existing:
            if county_code != existing.get("county_code"):
                return {
                    "ok": False,
                    "message": (
                        f"分段 {segment_id} 已确认归属 {existing.get('county_code')}，"
                        f"不能直接改登记为 {county_code}；请先走调账"
                    ),
                    "confirmation": existing,
                }
            existing["valid_until"] = max(float(existing.get("valid_until", 0)), now) + ttl_sec
            existing["renewed_at"] = now
            existing["ttl_sec"] = ttl_sec
            self._save_ledger(batch_key, ledger)
            return {"ok": True, "message": "县级确认有效期已续用", "confirmation": existing}

        confirmation = {
            "confirmation_id": f"CONF-{batch_key}-{seg_key}",
            "batch_key": batch_key,
            "segment_id": seg_key,
            "trip_id": trip_id or seg_key,
            "county_code": county_code,
            "grid_geohash": grid_geohash,
            "confirmed_at": now,
            "valid_from": now,
            "valid_until": now + ttl_sec,
            "ttl_sec": ttl_sec,
            "status": "有效",
            "evidence_ref": evidence_ref,
            "note": note,
        }
        ledger["confirmations"][seg_key] = confirmation
        self._save_ledger(batch_key, ledger)
        return {"ok": True, "message": f"分段 {segment_id} 已登记县级确认", "confirmation": confirmation}

    def get_county_confirmation(self, batch_key: str, segment_id: str | int) -> dict[str, Any] | None:
        ledger = self._load_ledger(batch_key)
        return ledger["confirmations"].get(str(segment_id))

    def is_confirmation_valid(self, confirmation: dict[str, Any] | None, at: float | None = None) -> bool:
        """确认是否在有效期内；未撤销且 valid_until 不早于给定时间。"""
        if not confirmation:
            return False
        if confirmation.get("status") == "已撤销":
            return False
        moment = time.time() if at is None else at
        return float(confirmation.get("valid_until", 0)) >= moment

    # -- 网格属地标准 --

    def register_grid_county(
        self,
        batch_key: str,
        grid_geohash: str,
        county_code: str,
        *,
        evidence_ref: str | None = None,
    ) -> dict[str, Any]:
        """登记网格的属地标准（该网格属于哪个县）。只有命中这些标准的分段才算“符合属地标准”。"""
        ledger = self._load_ledger(batch_key)
        ledger["grid_counties"][grid_geohash] = {
            "grid_geohash": grid_geohash,
            "county_code": county_code,
            "evidence_ref": evidence_ref,
            "registered_at": time.time(),
        }
        self._save_ledger(batch_key, ledger)
        return {"ok": True, "message": f"网格 {grid_geohash} 属地标准已登记为 {county_code}",
                "grid_county": ledger["grid_counties"][grid_geohash]}

    def get_grid_county(self, batch_key: str, grid_geohash: str | None) -> str | None:
        if not grid_geohash:
            return None
        row = self._load_ledger(batch_key)["grid_counties"].get(grid_geohash)
        return row.get("county_code") if row else None

    # -- 待复核明细 --

    def _upsert_review_item(self, ledger: dict[str, Any], item: dict[str, Any]) -> str:
        item_id = item["review_id"]
        previous = ledger["review_items"].get(item_id, {})
        item["created_at"] = previous.get("created_at", time.time())
        item["updated_at"] = time.time()
        item.setdefault("status", "待复核")
        ledger["review_items"][item_id] = item
        return item_id

    def open_review_item(
        self,
        batch_key: str,
        *,
        segment_id: str | int,
        trip_id: str | None,
        grid_geohash: str | None,
        current_county_code: str | None,
        confirmed_county_code: str | None = None,
        confirmation_id: str | None = None,
        candidate_county_code: str | None = None,
        reason: str = "无法确认归属",
        hold_scope: str | None = None,
        trip_weight: int = 1,
        candidates: list[dict[str, Any]] | None = None,
        geometry_evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """新增或刷新一条待复核明细；同一分段只维护一条，不重复挂账。"""
        ledger = self._load_ledger(batch_key)
        review_id = f"REV-{batch_key}-{segment_id}"
        item = {
            "review_id": review_id,
            "batch_key": batch_key,
            "segment_id": str(segment_id),
            "trip_id": trip_id or str(segment_id),
            "grid_geohash": grid_geohash,
            "current_county_code": current_county_code,
            "confirmed_county_code": confirmed_county_code,
            "confirmation_id": confirmation_id,
            "candidate_county_code": candidate_county_code,
            "reason": reason,
            "hold_scope": hold_scope,
            "trip_weight": trip_weight,
            "candidates": candidates or [],
            "geometry_evidence": geometry_evidence,
            "evidence_ref": None,
            "status": "待复核",
        }
        previous = ledger["review_items"].get(review_id)
        if previous and previous.get("status") == "已调账":
            return {"ok": False, "message": f"待复核明细 {review_id} 已调账，不能重新挂账",
                    "review_item": previous}
        if previous:
            # 重算只刷新候选，不覆盖已经补交的证据与处理状态。
            for field in ("evidence_ref", "geometry_evidence", "evidence_attached_at"):
                if previous.get(field) is not None:
                    item[field] = previous[field]
            if previous.get("status") in ("证据待补齐", "待调账", "证据已补齐"):
                item["status"] = previous["status"]
        self._upsert_review_item(ledger, item)
        self._save_ledger(batch_key, ledger)
        return {"ok": True, "message": f"已生成待复核明细 {review_id}", "review_item": ledger["review_items"][review_id]}

    def attach_review_evidence(
        self,
        batch_key: str,
        review_id: str,
        *,
        evidence_ref: str,
        geometry_evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """补齐边界证据；证据缺口未补完时不会把明细改成可调账。"""
        ledger = self._load_ledger(batch_key)
        item = ledger["review_items"].get(review_id)
        if item is None:
            return {"ok": False, "message": f"待复核明细 {review_id} 不存在"}
        if item.get("status") == "已调账":
            return {"ok": False, "message": f"待复核明细 {review_id} 已调账，不能重复补证据",
                    "review_item": item}
        item["evidence_ref"] = evidence_ref
        if geometry_evidence is not None:
            item["geometry_evidence"] = geometry_evidence
        missing = item.get("geometry_evidence") or {}
        required = [key for key in ("boundary_overlap_ratio", "crosses_boundary") if key not in missing]
        if required:
            item["status"] = "证据待补齐"
            self._save_ledger(batch_key, ledger)
            return {
                "ok": False,
                "message": f"证据仍缺 {('、'.join(required))}，不能确认归属",
                "review_item": item,
            }
        item["status"] = "待调账" if item.get("reason") != "无法确认归属" else "证据已补齐"
        item["evidence_attached_at"] = time.time()
        self._save_ledger(batch_key, ledger)
        return {"ok": True, "message": "边界证据已补齐，可从待复核明细发起县级调账", "review_item": item}

    def list_review_items(self, batch_key: str, status: str | None = None) -> list[dict[str, Any]]:
        """列出无法确认归属的独立待复核明细。"""
        ledger = self._load_ledger(batch_key)
        items = list(ledger["review_items"].values())
        if status:
            items = [item for item in items if item.get("status") == status]
        return sorted(items, key=lambda item: item.get("created_at", 0))

    def close_stale_review_items(
        self,
        batch_key: str,
        active_segment_ids: set[str],
        *,
        reason: str = "重算后该分段已有明确归属",
    ) -> list[str]:
        """重算后收口不再成立的待复核明细。

        只收口尚未补交证据的“待复核”明细；已补证据待调账、已调账的明细保持原状，
        避免重算把正在处理的调账线索抹掉。
        """
        ledger = self._load_ledger(batch_key)
        closed: list[str] = []
        for item in ledger["review_items"].values():
            segment_id = str(item.get("segment_id"))
            if segment_id in active_segment_ids:
                continue
            if item.get("status") != "待复核":
                continue
            item["status"] = "已消解"
            item["resolved_reason"] = reason
            item["resolved_at"] = time.time()
            closed.append(item["review_id"])
        if closed:
            self._save_ledger(batch_key, ledger)
        return closed

    def get_review_item(self, batch_key: str, review_id: str) -> dict[str, Any] | None:
        return self._load_ledger(batch_key)["review_items"].get(review_id)

    # -- 调账 --

    def apply_jurisdiction_adjustment(
        self,
        batch_key: str,
        review_id: str,
        *,
        to_county_code: str,
        evidence_ref: str,
        trip_weight: int = 1,
        operator_note: str | None = None,
    ) -> dict[str, Any]:
        """从待复核明细发起一次县级调账：记录移出旧归属、转入新归属的实际贡献。

        幂等：同一 review_id + evidence_ref 只记一次，重复补交证据不会把贡献
        重复计入新归属。调账只改归属，不重复扣减上游已经暂挂的份额。
        """
        ledger = self._load_ledger(batch_key)
        item = ledger["review_items"].get(review_id)
        if item is None:
            return {"ok": False, "message": f"待复核明细 {review_id} 不存在"}
        if item.get("status") == "已调账":
            existing = next(
                (a for a in ledger["adjustments"] if a.get("review_id") == review_id), None)
            return {
                "ok": False,
                "message": f"待复核明细 {review_id} 已调账，不能重复记账",
                "adjustment": existing,
            }
        if item.get("status") not in ("待调账", "证据已补齐"):
            return {
                "ok": False,
                "message": f"待复核明细 {review_id} 尚未补齐边界证据，不能调账",
                "review_item": item,
            }
        if item.get("evidence_ref") != evidence_ref:
            return {
                "ok": False,
                "message": "调账引用的边界证据与待复核明细已补交的证据不一致",
                "review_item": item,
            }

        from_county = item.get("current_county_code") or item.get("confirmed_county_code")
        if from_county == to_county_code:
            return {"ok": False, "message": f"转入县与当前归属相同（{to_county_code}），无需调账"}

        evidence_id = self._evidence_key(batch_key, review_id, to_county_code, evidence_ref)
        for adjustment in ledger["adjustments"]:
            if adjustment.get("evidence_id") == evidence_id:
                return {
                    "ok": False,
                    "message": "同一份边界证据已记过调账，未重复入账",
                    "adjustment": adjustment,
                }

        now = time.time()
        # 调账记录里的 trip_id 与确认登记保持一致，避免同一趟被当成两趟。
        confirmation = ledger["confirmations"].get(str(item.get("segment_id")))
        trip_id = str((confirmation or {}).get("trip_id") or item.get("trip_id")
                      or item.get("segment_id"))
        adjustment = {
            "adjustment_id": f"ADJ-{batch_key}-{len(ledger['adjustments']) + 1:03d}",
            "evidence_id": evidence_id,
            "batch_key": batch_key,
            "review_id": review_id,
            "segment_id": item.get("segment_id"),
            "trip_id": trip_id,
            "grid_geohash": item.get("grid_geohash"),
            "from_county_code": from_county,
            "to_county_code": to_county_code,
            "trip_weight": trip_weight,
            "evidence_ref": evidence_ref,
            "recorded_at": now,
            "status": "已调账",
            "operator_note": operator_note,
        }
        ledger["adjustments"].append(adjustment)

        item["status"] = "已调账"
        item["resolved_at"] = now
        item["resolved_to_county_code"] = to_county_code
        item["updated_at"] = now

        # 原确认不撤销，只把新县补成待生效确认，旧县确认保留出处。
        old_confirmation = ledger["confirmations"].get(str(item.get("segment_id")))
        if old_confirmation:
            old_confirmation["superseded_by"] = to_county_code
            old_confirmation["superseded_at"] = now
        ledger["confirmation_history"].append({
            "confirmation_id": (old_confirmation or {}).get("confirmation_id"),
            "segment_id": item.get("segment_id"),
            "trip_id": trip_id,
            "county_code": to_county_code,
            "previous_county_code": from_county,
            "adjustment_id": adjustment["adjustment_id"],
            "recorded_at": now,
            "status": "待生效",
        })
        self._save_ledger(batch_key, ledger)

        return {
            "ok": True,
            "message": (
                f"已从 {from_county or '未归属'} 移出 {trip_weight} 趟，转入 {to_county_code}；"
                "原确认保留出处，不重复扣减暂挂份额"
            ),
            "adjustment": adjustment,
            "review_item": item,
        }

    def list_adjustments(self, batch_key: str) -> list[dict[str, Any]]:
        return list(self._load_ledger(batch_key)["adjustments"])

    # -- 县报核算 --

    DISPOSITIONS = ("continue_confirmed", "hold_under_confirmation", "await_adjustment")

    def compute_county_report(
        self,
        batch_key: str,
        *,
        as_of: float | None = None,
        submitted: bool = False,
        disposition: str = "hold_under_confirmation",
    ) -> dict[str, Any]:
        """按县级口径核算一趟轨迹只计一次，并区分已确权/暂挂/待调账。

        - `submitted=True` 的县报处于已提交状态，不再被重算修改；未提交的下期
          县报才允许重新核算；
        - 每趟轨迹只落入一个口径：已确权、暂挂（原确认名下保留出处）、待新证据
          后调账三者互斥；
        - 调账只记录归属移动，不会从尚未记账的县报里重复扣减，也不会让两个县
          同时把同一趟算成已入账。
        """
        moment = time.time() if as_of is None else as_of
        if disposition not in self.DISPOSITIONS:
            return {"ok": False, "message": f"未知的县报口径：{disposition}"}
        ledger = self._load_ledger(batch_key)
        if submitted and ledger["county_reports"].get("submitted"):
            report = ledger["county_reports"]["submitted"]
            return {"ok": False, "message": "本期县报已提交，不再重算", "report": report}

        counties: dict[str, dict[str, Any]] = {}

        def bucket(code: str | None) -> dict[str, Any]:
            key = code or "未归属"
            return counties.setdefault(key, {
                "county_code": key,
                "confirmed_trips": 0,
                "held_trips": 0,
                "pending_evidence_trips": 0,
                "adjusted_in_trips": 0,
                "adjusted_out_trips": 0,
                "booked_trips": 0,
                "trip_ids": [],
            })

        # 分段编号 → 确认所属趟次：待复核明细和调账常常只带分段编号，
        # 统一折算到确认登记时的 trip_id，保证一趟轨迹只落在同一个口径。
        segment_trip = {
            str(seg_key): str(confirmation.get("trip_id") or seg_key)
            for seg_key, confirmation in ledger["confirmations"].items()
        }

        def resolve_trip(record: dict[str, Any]) -> str:
            segment_id = str(record.get("segment_id") or "")
            if segment_id in segment_trip:
                return segment_trip[segment_id]
            return str(record.get("trip_id") or segment_id)

        # 按分段归并确认；同一趟轨迹优先取仍在有效期内的确认。
        # 调账不撤销原确认：若确认已被新归属接续，实际入账县以调账目标为准。
        superseded = {
            str(seg_key): confirmation.get("superseded_by")
            for seg_key, confirmation in ledger["confirmations"].items()
            if confirmation.get("superseded_by")
        }
        trips: dict[str, dict[str, Any]] = {}
        for seg_key, confirmation in ledger["confirmations"].items():
            trip_id = str(confirmation.get("trip_id") or seg_key)
            candidate = {
                "segment_id": seg_key,
                "county_code": superseded.get(seg_key) or confirmation.get("county_code"),
                "origin_county_code": confirmation.get("county_code"),
                "valid": self.is_confirmation_valid(confirmation, moment),
            }
            previous = trips.get(trip_id)
            if previous is None or (candidate["valid"] and not previous["valid"]):
                trips[trip_id] = candidate

        # 待复核明细按 trip_id 归并，只看仍未调账的。
        pending_by_trip: dict[str, list[dict[str, Any]]] = {}
        for item in ledger["review_items"].values():
            if item.get("status") == "已调账":
                continue
            pending_by_trip.setdefault(resolve_trip(item), []).append(item)

        # 调账按 trip_id 归并；同一趟只认一次移动，避免重复扣减。
        adjustment_by_trip: dict[str, dict[str, Any]] = {}
        for adjustment in ledger["adjustments"]:
            adjustment_by_trip.setdefault(resolve_trip(adjustment), adjustment)

        for trip_id, trip in trips.items():
            original_county = trip["county_code"]
            adjustment = adjustment_by_trip.get(trip_id)
            if adjustment is not None:
                # 调账后归属只算新县；旧县只留移动记录，不再计入已入账。
                target = bucket(adjustment.get("to_county_code"))
                target["adjusted_in_trips"] += int(adjustment.get("trip_weight", 1))
                target["booked_trips"] += int(adjustment.get("trip_weight", 1))
                target["trip_ids"].append(trip_id)
                # 移出记录挂在原归属县上，而不是已被接续后的新县。
                origin = bucket(adjustment.get("from_county_code") or trip.get("origin_county_code"))
                origin["adjusted_out_trips"] += int(adjustment.get("trip_weight", 1))
                continue
            item_bucket = bucket(original_county)
            item_bucket["trip_ids"].append(trip_id)
            unresolved = pending_by_trip.get(trip_id)
            if unresolved and trip["valid"]:
                # 有竞争候选但原确认未撤销：三种口径互斥，默认暂挂在原确认名下。
                if disposition == "continue_confirmed":
                    item_bucket["confirmed_trips"] += 1
                    item_bucket["booked_trips"] += 1
                elif disposition == "await_adjustment":
                    item_bucket["pending_evidence_trips"] += 1
                else:
                    item_bucket["held_trips"] += 1
                continue
            if trip["valid"]:
                item_bucket["confirmed_trips"] += 1
                item_bucket["booked_trips"] += 1
            else:
                item_bucket["pending_evidence_trips"] += 1

        # 没有确认、只有待复核明细的分段：单独挂到未归属，不重复计入已确权。
        for trip_id, items in pending_by_trip.items():
            if trip_id in trips:
                continue
            item = items[0]
            row = bucket(item.get("current_county_code") or item.get("confirmed_county_code"))
            row["held_trips" if item.get("hold_scope") == "county_report" else "pending_evidence_trips"] += int(
                item.get("trip_weight", 1))
            row["trip_ids"].append(trip_id)

        ordered = sorted(counties.values(), key=lambda row: row["county_code"])
        total_booked = sum(row["booked_trips"] for row in ordered)
        total_trips = len(trips) + sum(
            1 for trip_id in pending_by_trip if trip_id not in trips)
        double_booked = total_booked > total_trips
        booked_trip_ids = {
            trip_id for row in ordered if row["booked_trips"] for trip_id in row["trip_ids"]}

        report = {
            "batch_key": batch_key,
            "as_of": moment,
            "disposition": disposition,
            "counties": ordered,
            "total_trips": total_trips,
            "total_booked": total_booked,
            "double_booked": double_booked,
            # 仍有候选未确权、但本期已按原确认入账的趟次：保留线索不扣份额。
            "pending_review_trip_ids": sorted(
                trip_id for trip_id in pending_by_trip if trip_id not in adjustment_by_trip),
            # 尚未入账、也没有明确归属的趟次。
            "unresolved_trip_ids": sorted(
                trip_id for trip_id in pending_by_trip
                if trip_id not in adjustment_by_trip and trip_id not in booked_trip_ids),
            "submitted": bool(submitted),
            "generated_at": time.time(),
        }
        ledger["county_reports"]["draft" if not submitted else "submitted"] = report
        self._save_ledger(batch_key, ledger)
        return {"ok": True, "message": "县报核算完成", "report": report}

    def get_county_report(self, batch_key: str, submitted: bool = False) -> dict[str, Any] | None:
        ledger = self._load_ledger(batch_key)
        return ledger["county_reports"].get("submitted" if submitted else "draft")
