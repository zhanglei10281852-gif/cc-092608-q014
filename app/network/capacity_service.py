from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction
from app.network import capacity_model as model
from app.network.repository import NetworkRepository
from app.network.schema import ensure_network_schema

DRAFT = "draft"
SUBMITTED = "submitted"
APPROVED = "approved"
REJECTED = "rejected"
TERMINAL_REVIEW_STATES = {APPROVED, REJECTED}


class CapacityPlanService:
    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        ensure_network_schema(self.connection)
        self.clock = clock or SystemClock()
        self.repository = NetworkRepository(self.connection)

    # ------------------------------------------------------------------ 试算

    def evaluate_assumptions(self, assumptions: dict[str, Any]) -> dict[str, Any]:
        resolved = self._resolve_assumptions(assumptions)
        result = model.evaluate(resolved)
        return {
            "assumptions_digest": model.input_digest(resolved),
            "assumptions": resolved,
            "result": result,
        }

    # ------------------------------------------------------------------ 预案

    def create_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._require_scenario(payload["scenario_code"])
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO capacity_plans(code,name,scenario_id,description,created_by,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (payload["code"], payload["name"], self._scenario_id(payload["scenario_code"]), payload.get("description", ""), payload["actor"], now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("容量预案编码已存在") from exc
            self._event(connection, "capacity_plan", cursor.lastrowid, "created", payload["actor"], {"code": payload["code"]}, now)
            return self.plan_detail(cursor.lastrowid, connection)

    def list_plans(self, scenario_code: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if scenario_code:
            clauses.append("n.code=?")
            params.append(scenario_code)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            "SELECT p.*,n.code AS scenario_code,n.name AS scenario_name,"
            "COUNT(v.id) AS version_count,"
            "MAX(CASE WHEN v.state='approved' THEN 1 ELSE 0 END) AS has_approved "
            "FROM capacity_plans p JOIN network_scenarios n ON n.id=p.scenario_id "
            "LEFT JOIN capacity_plan_versions v ON v.plan_id=p.id" + where +
            " GROUP BY p.id ORDER BY p.created_at DESC,p.id DESC",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def plan_detail(self, plan_id: int, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        connection = connection or self.connection
        row = connection.execute(
            "SELECT p.*,n.code AS scenario_code,n.name AS scenario_name "
            "FROM capacity_plans p JOIN network_scenarios n ON n.id=p.scenario_id WHERE p.id=?",
            (plan_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("容量预案不存在")
        result = dict(row)
        result["versions"] = [self._version_summary(item) for item in connection.execute(
            "SELECT * FROM capacity_plan_versions WHERE plan_id=? ORDER BY version_no DESC",
            (plan_id,),
        ).fetchall()]
        result["events"] = self._events(connection, "capacity_plan", plan_id)
        return result

    def create_version(self, plan_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        plan = self._plan_row(plan_id)
        assumptions = payload["assumptions"]
        scenario = self.repository.scenario_by_id(plan["scenario_id"])
        if assumptions["scenario_code"] != scenario["code"]:
            raise ValidationError("版本假设的场景必须与预案一致")
        parent_no = payload.get("based_on_version_no")
        if parent_no is not None:
            parent = self.connection.execute(
                "SELECT id FROM capacity_plan_versions WHERE plan_id=? AND version_no=?",
                (plan_id, parent_no),
            ).fetchone()
            if parent is None:
                raise NotFoundError("基线版本不存在")
        evaluation = self.evaluate_assumptions(assumptions)
        resolved = evaluation["assumptions"]
        result = evaluation["result"]
        assumptions_text = model.canonical_input(resolved)
        result_text = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            version_no = int(connection.execute(
                "SELECT COALESCE(MAX(version_no),0)+1 FROM capacity_plan_versions WHERE plan_id=?",
                (plan_id,),
            ).fetchone()[0])
            cursor = connection.execute(
                "INSERT INTO capacity_plan_versions(plan_id,version_no,state,assumptions_json,assumptions_digest,"
                "result_json,result_digest,based_on_version_no,created_by,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (plan_id, version_no, DRAFT, assumptions_text, model.input_digest(resolved), result_text,
                 model.result_digest(result), parent_no, payload["actor"], now, now),
            )
            self._event(connection, "capacity_plan_version", cursor.lastrowid, "created", payload["actor"],
                        {"version_no": version_no, "based_on_version_no": parent_no, "note": payload.get("note", "")}, now)
            return self.version_detail(cursor.lastrowid, connection)

    def list_versions(self, plan_id: int) -> list[dict[str, Any]]:
        self._plan_row(plan_id)
        rows = self.connection.execute(
            "SELECT * FROM capacity_plan_versions WHERE plan_id=? ORDER BY version_no DESC",
            (plan_id,),
        ).fetchall()
        return [self._version_summary(row) for row in rows]

    def version_detail(self, version_id: int, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        connection = connection or self.connection
        row = connection.execute(
            "SELECT v.*,p.code AS plan_code,p.scenario_id FROM capacity_plan_versions v "
            "JOIN capacity_plans p ON p.id=v.plan_id WHERE v.id=?",
            (version_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("预案版本不存在")
        result = self._version_summary(row)
        result["assumptions"] = json.loads(row["assumptions_json"])
        result["result"] = json.loads(row["result_json"])
        result["events"] = self._events(connection, "capacity_plan_version", version_id)
        return result

    def submit_version(self, version_id: int, actor: str, note: str) -> dict[str, Any]:
        return self._transition(version_id, actor, note, allow_from={DRAFT, REJECTED}, target=SUBMITTED, event_type="submitted")

    def approve_version(self, version_id: int, actor: str, note: str) -> dict[str, Any]:
        return self._transition(version_id, actor, note, allow_from={SUBMITTED}, target=APPROVED, event_type="approved")

    def reject_version(self, version_id: int, actor: str, note: str) -> dict[str, Any]:
        return self._transition(version_id, actor, note, allow_from={SUBMITTED}, target=REJECTED, event_type="rejected")

    def recalculate_version(self, version_id: int) -> dict[str, Any]:
        """用冻结输入重新计算并比对摘要；任何状态都不会改写存储与实时预留。"""

        row = self._version_row(version_id)
        frozen_assumptions = json.loads(row["assumptions_json"])
        stored_result = json.loads(row["result_json"])
        recomputed = model.evaluate(frozen_assumptions)
        recomputed_digest = model.result_digest(recomputed)
        consistent = recomputed_digest == row["result_digest"] and recomputed == stored_result
        return {
            "version_id": version_id,
            "state": row["state"],
            "frozen": row["state"] in TERMINAL_REVIEW_STATES,
            "consistent": consistent,
            "stored_result_digest": row["result_digest"],
            "recomputed_result_digest": recomputed_digest,
            "assumptions_digest": row["assumptions_digest"],
            "recomputed_assumptions_digest": model.input_digest(frozen_assumptions),
            "result": recomputed,
        }

    def compare_versions(self, plan_id: int, version_nos: list[int]) -> dict[str, Any]:
        self._plan_row(plan_id)
        if len(version_nos) < 2 or len(version_nos) > 10:
            raise ValidationError("至少选择两个、最多十个版本进行比较")
        if len(version_nos) != len(set(version_nos)):
            raise ValidationError("对比版本不能重复")
        rows = self.connection.execute(
            "SELECT * FROM capacity_plan_versions WHERE plan_id=? AND version_no IN (%s) ORDER BY version_no"
            % ",".join("?" for _ in version_nos),
            [plan_id, *version_nos],
        ).fetchall()
        if len(rows) != len(version_nos):
            raise NotFoundError("部分待比较版本不存在")
        versions = [self._comparison_entry(row) for row in rows]
        baseline = versions[0]
        for entry in versions[1:]:
            entry["delta_from_first"] = self._delta(baseline["summary"], entry["summary"])
        versions[0]["delta_from_first"] = None
        return {"plan_id": plan_id, "versions": versions}

    # ------------------------------------------------------------------ 内部

    def _transition(self, version_id: int, actor: str, note: str, *, allow_from: set[str], target: str, event_type: str) -> dict[str, Any]:
        row = self._version_row(version_id)
        if target == REJECTED and not note:
            raise ValidationError("驳回必须填写原因")
        if row["state"] not in allow_from:
            raise ConflictError(f"版本当前状态 {row['state']} 不能执行该操作")
        now = to_storage(self.clock.now())
        columns = {
            SUBMITTED: ("submitted_by", "submitted_at"),
            APPROVED: ("approved_by", "approved_at"),
            REJECTED: (None, None),
        }[target]
        with transaction(immediate=True) as connection:
            sql = "UPDATE capacity_plan_versions SET state=?,review_note=?,updated_at=?"
            params: list[Any] = [target, note, now]
            if columns[0] is not None:
                sql += f",{columns[0]}=?,{columns[1]}=?"
                params.extend([actor, now])
            sql += " WHERE id=?"
            params.append(version_id)
            connection.execute(sql, params)
            self._event(connection, "capacity_plan_version", version_id, event_type, actor, {"note": note}, now)
            return self.version_detail(version_id, connection)

    def _resolve_assumptions(self, assumptions: dict[str, Any]) -> dict[str, Any]:
        scenario = self._require_scenario(assumptions["scenario_code"])
        db_segments = {item["code"]: item for item in self.repository.segments(scenario["id"]) if item["status"] == "active"}
        declared_segments = assumptions.get("segments") or []
        resolved_segments: list[model.ResolvedSegment] = []
        if declared_segments:
            total_share = sum(float(item["share"]) for item in declared_segments)
            for item in declared_segments:
                db_segment = db_segments.get(item["code"])
                if db_segment is None:
                    raise NotFoundError(f"场景区段不存在或不可用：{item['code']}")
                capacity = item.get("capacity_mbps") or float(db_segment["capacity_mbps"])
                resolved_segments.append(model.ResolvedSegment(
                    code=item["code"],
                    share=float(item["share"]) / total_share if abs(total_share - 1.0) > 0.01 else float(item["share"]),
                    capacity_mbps=float(capacity),
                ))
        else:
            if not db_segments:
                raise ValidationError("场景没有可用区段，无法生成容量情景")
            equal_share = 1.0 / len(db_segments)
            resolved_segments = [
                model.ResolvedSegment(code=code, share=equal_share, capacity_mbps=float(item["capacity_mbps"]))
                for code, item in sorted(db_segments.items())
            ]
        resolved_applications: list[model.ResolvedApplication] = []
        for item in assumptions["applications"]:
            profile = self.repository.application_by_code(item["app_code"])
            if profile is None:
                raise NotFoundError(f"应用画像不存在：{item['app_code']}")
            resolved_applications.append(model.ResolvedApplication(
                code=profile["app_code"],
                traffic_share=float(item["traffic_share"]),
                penetration=float(item["penetration"]),
                activation_rate=float(item.get("activation_rate") or 1.0),
                priority=int(item["priority"] if item.get("priority") is not None else profile["default_priority"]),
                downlink_mbps=float(item["downlink_mbps"] if item.get("downlink_mbps") is not None else profile["min_downlink_mbps"]),
                uplink_mbps=float(item["uplink_mbps"] if item.get("uplink_mbps") is not None else profile["min_uplink_mbps"]),
            ))
        event = assumptions["event"]
        slot_count = model.validate_event_window(event["starts_at"], event["ends_at"], event["slot_minutes"])
        occupancy_curve = model.blend_curves(
            assumptions.get("historical_curve"),
            assumptions.get("manual_curve"),
            slot_count,
            float(assumptions.get("historical_weight", 0.5)),
        )
        return model.build_resolved_input(
            scenario_code=scenario["code"],
            max_concurrent_sessions=int(scenario["max_concurrent_sessions"]),
            starts_at=event["starts_at"],
            ends_at=event["ends_at"],
            slot_minutes=int(event["slot_minutes"]),
            attendees=int(assumptions["attendees"]),
            occupancy_curve=occupancy_curve,
            segments=resolved_segments,
            applications=resolved_applications,
            measures=assumptions.get("measures") or {},
        )

    def _require_scenario(self, code: str) -> sqlite3.Row:
        scenario = self.repository.scenario_by_code(code)
        if scenario is None:
            raise NotFoundError("网络场景不存在")
        return scenario

    def _scenario_id(self, code: str) -> int:
        return int(self._require_scenario(code)["id"])

    def _plan_row(self, plan_id: int) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM capacity_plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFoundError("容量预案不存在")
        return row

    def _version_row(self, version_id: int) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM capacity_plan_versions WHERE id=?", (version_id,)).fetchone()
        if row is None:
            raise NotFoundError("预案版本不存在")
        return row

    @staticmethod
    def _version_summary(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result.pop("assumptions_json", None)
        result.pop("result_json", None)
        peak = json.loads(row["result_json"]).get("peak") or {}
        result["peak_slot_starts_at"] = peak.get("starts_at")
        result["peak_slot_index"] = peak.get("slot_index")
        return result

    def _comparison_entry(self, row: sqlite3.Row) -> dict[str, Any]:
        stored = json.loads(row["result_json"])
        assumptions = json.loads(row["assumptions_json"])
        summary = {
            "version_no": row["version_no"],
            "state": row["state"],
            "attendees": stored["attendees"],
            "peak_slot_starts_at": (stored.get("peak") or {}).get("starts_at"),
            "peak_requested_sessions": (stored.get("peak") or {}).get("requested_sessions", 0),
            "peak_admitted_sessions": (stored.get("peak") or {}).get("admitted_sessions", 0),
            "peak_waiting_backlog": (stored.get("peak") or {}).get("waiting_backlog", 0),
            "peak_downlink_demand_mbps": (stored.get("peak") or {}).get("downlink_demand_mbps", 0),
            "peak_uplink_demand_mbps": (stored.get("peak") or {}).get("uplink_demand_mbps", 0),
            "admission_ratio": stored["totals"]["admission_ratio"],
            "unserved_sessions": stored["totals"]["unserved_sessions"],
            "bottlenecks": stored["bottlenecks"],
            "max_downlink_shortfall_mbps": max((item["max_downlink_shortfall_mbps"] for item in stored["segment_summary"]), default=0.0),
            "max_uplink_shortfall_mbps": max((item["max_uplink_shortfall_mbps"] for item in stored["segment_summary"]), default=0.0),
            "affected_application_codes": [item["app_code"] for item in stored["affected_applications"]],
            "recommended_action_types": [item["type"] for item in stored["recommended_actions"]],
            "measures": assumptions["measures"],
        }
        return {"version_no": row["version_no"], "state": row["state"], "summary": summary,
                "recommended_actions": stored["recommended_actions"], "segment_summary": stored["segment_summary"]}

    @staticmethod
    def _delta(baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
        numeric_keys = (
            "peak_requested_sessions", "peak_admitted_sessions", "peak_waiting_backlog",
            "peak_downlink_demand_mbps", "peak_uplink_demand_mbps", "admission_ratio",
            "unserved_sessions", "max_downlink_shortfall_mbps", "max_uplink_shortfall_mbps",
        )
        delta: dict[str, Any] = {}
        for key in numeric_keys:
            delta[key] = round(float(current.get(key, 0)) - float(baseline.get(key, 0)), 6)
        delta["bottlenecks_added"] = sorted(set(current["bottlenecks"]) - set(baseline["bottlenecks"]))
        delta["bottlenecks_removed"] = sorted(set(baseline["bottlenecks"]) - set(current["bottlenecks"]))
        delta["affected_applications_added"] = sorted(set(current["affected_application_codes"]) - set(baseline["affected_application_codes"]))
        delta["affected_applications_removed"] = sorted(set(baseline["affected_application_codes"]) - set(current["affected_application_codes"]))
        return delta

    @staticmethod
    def _event(connection: sqlite3.Connection, resource_type: str, resource_id: int, event_type: str, actor: str, detail: dict[str, Any], now: str) -> None:
        connection.execute(
            "INSERT INTO operation_events(resource_type,resource_id,event_type,actor,detail_json,created_at) VALUES(?,?,?,?,?,?)",
            (resource_type, resource_id, event_type, actor, json.dumps(detail, ensure_ascii=False, sort_keys=True, default=str), now),
        )

    @staticmethod
    def _events(connection: sqlite3.Connection, resource_type: str, resource_id: int) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM operation_events WHERE resource_type=? AND resource_id=? ORDER BY id",
            (resource_type, resource_id),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json"))
            result.append(item)
        return result
