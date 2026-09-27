from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import request_fingerprint
from app.database import get_connection, transaction
from app.network.repository import NetworkRepository
from app.network.scenario_engine import evaluate, resolve_peak_profile
from app.network.scenario_schema import ensure_capacity_scenario_schema
from app.network.schema import ensure_network_schema


class CapacityScenarioService:
    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        ensure_network_schema(self.connection)
        ensure_capacity_scenario_schema(self.connection)
        self.clock = clock or SystemClock()
        self.repository = NetworkRepository(self.connection)

    # ------------------------------------------------------------------
    # 一次性试算：不落库、不触碰实时预留
    # ------------------------------------------------------------------
    def evaluate(self, payload: dict[str, Any]) -> dict[str, Any]:
        snapshot = self.build_snapshot(payload)
        result = evaluate(snapshot)
        return {
            "assumptions_digest": self._digest(snapshot),
            "summary": result["summary"],
            "curve": result["curve"],
        }

    # ------------------------------------------------------------------
    # 预案与版本
    # ------------------------------------------------------------------
    def create_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        scenario = self._scenario(payload["scenario_code"])
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO capacity_plans(scenario_id,code,name,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (scenario["id"], payload["code"], payload["name"], payload["actor"], now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("容量预案编码已存在") from exc
            self._event(connection, cursor.lastrowid, None, "created", payload["actor"], {"code": payload["code"]}, now)
            return self.plan_detail(cursor.lastrowid, connection)

    def list_plans(self, scenario_code: str | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT p.*,n.code AS scenario_code,n.name AS scenario_name "
            "FROM capacity_plans p JOIN network_scenarios n ON n.id=p.scenario_id"
        )
        params: list[Any] = []
        if scenario_code:
            sql += " WHERE n.code=?"
            params.append(scenario_code)
        sql += " ORDER BY p.created_at DESC,p.id DESC"
        return [dict(row) for row in self.connection.execute(sql, params).fetchall()]

    def plan_detail(self, plan_id: int, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        connection = connection or self.connection
        row = connection.execute(
            "SELECT p.*,n.code AS scenario_code,n.name AS scenario_name,n.scene_type AS scene_type "
            "FROM capacity_plans p JOIN network_scenarios n ON n.id=p.scenario_id WHERE p.id=?",
            (plan_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("容量预案不存在")
        result = dict(row)
        result["versions"] = [self._version_summary(version_row) for version_row in connection.execute(
            "SELECT * FROM capacity_plan_versions WHERE plan_id=? ORDER BY version_no DESC,id DESC",
            (plan_id,),
        ).fetchall()]
        result["events"] = self._events(connection, plan_id)
        return result

    def create_version(self, plan_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        plan = self._plan(plan_id)
        scenario = self.repository.scenario_by_id(plan["scenario_id"])
        snapshot = self.build_snapshot({"scenario_code": scenario["code"], **payload})
        result = evaluate(snapshot)
        assumptions_text, assumptions_digest = self._canonical(snapshot)
        result_text, result_digest = self._canonical(result)
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            version_no = int(connection.execute(
                "SELECT COALESCE(MAX(version_no),0)+1 FROM capacity_plan_versions WHERE plan_id=?",
                (plan_id,),
            ).fetchone()[0])
            cursor = connection.execute(
                "INSERT INTO capacity_plan_versions(plan_id,version_no,label,state,assumptions_json,assumptions_digest,"
                "result_json,result_digest,peak_slot,peak_demand_mbps,max_shortfall_mbps,max_waiting,created_by,computed_at,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    plan_id, version_no, payload.get("label", ""), "draft", assumptions_text, assumptions_digest,
                    result_text, result_digest, result["summary"]["peak"]["starts_at"],
                    result["summary"]["peak"]["demand_downlink_mbps"],
                    max(result["summary"]["max_downlink_gap_mbps"], result["summary"]["max_uplink_gap_mbps"]),
                    result["summary"]["max_waiting_sessions"], payload["actor"], now, now, now,
                ),
            )
            connection.execute("UPDATE capacity_plans SET current_version_id=?,updated_at=? WHERE id=?", (cursor.lastrowid, now, plan_id))
            self._event(connection, plan_id, cursor.lastrowid, "version_created", payload["actor"], {"version_no": version_no, "assumptions_digest": assumptions_digest[:12]}, now)
            return self.version_detail(cursor.lastrowid, connection)

    def list_versions(self, plan_id: int) -> dict[str, Any]:
        self._plan(plan_id)
        rows = self.connection.execute(
            "SELECT * FROM capacity_plan_versions WHERE plan_id=? ORDER BY version_no DESC,id DESC",
            (plan_id,),
        ).fetchall()
        return {"items": [self._version_summary(row) for row in rows]}

    def version_detail(self, version_id: int, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        connection = connection or self.connection
        row = connection.execute("SELECT * FROM capacity_plan_versions WHERE id=?", (version_id,)).fetchone()
        if row is None:
            raise NotFoundError("预案版本不存在")
        return self._version(connection, row)

    def recompute_version(self, version_id: int, actor: str) -> dict[str, Any]:
        """用冻结输入重新跑确定性引擎。

        结果摘要必须与首次计算完全一致，否则说明引擎发生了破坏性变更；
        整个过程只读实时业务表，不会写入或释放任何实时预留。
        """
        row = self._version_row(version_id)
        snapshot = json.loads(row["assumptions_json"])
        result = evaluate(snapshot)
        _, result_digest = self._canonical(result)
        if result_digest != row["result_digest"]:
            raise ConflictError("重算结果与冻结摘要不一致，版本输入或引擎已发生变化")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            self._event(connection, row["plan_id"], version_id, "recomputed", actor, {"result_digest": result_digest[:12]}, now)
        return self.version_detail(version_id)

    def approve_version(self, version_id: int, actor: str, note: str = "") -> dict[str, Any]:
        row = self._version_row(version_id)
        if row["state"] == "approved":
            return self.version_detail(version_id)
        if row["state"] == "archived":
            raise ConflictError("已归档版本不能审批")
        snapshot = json.loads(row["assumptions_json"])
        _, result_digest = self._canonical(evaluate(snapshot))
        if result_digest != row["result_digest"]:
            raise ConflictError("审批前校验失败：重算摘要与冻结摘要不一致")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE capacity_plan_versions SET state='approved',approved_by=?,approved_at=?,updated_at=? WHERE id=?",
                (actor, now, now, version_id),
            )
            self._event(connection, row["plan_id"], version_id, "approved", actor, {"note": note}, now)
        return self.version_detail(version_id)

    def archive_version(self, version_id: int, actor: str) -> dict[str, Any]:
        row = self._version_row(version_id)
        if row["state"] == "archived":
            return self.version_detail(version_id)
        if row["state"] != "approved":
            raise ConflictError("只有审批通过的版本可以归档")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            connection.execute("UPDATE capacity_plan_versions SET state='archived',updated_at=? WHERE id=?", (now, version_id))
            self._event(connection, row["plan_id"], version_id, "archived", actor, {}, now)
        return self.version_detail(version_id)

    def compare_versions(self, plan_id: int, left_id: int, right_id: int) -> dict[str, Any]:
        self._plan(plan_id)
        left = self._version_row(left_id)
        right = self._version_row(right_id)
        if left["plan_id"] != plan_id or right["plan_id"] != plan_id:
            raise ValidationError("对比版本必须属于同一预案")
        left_summary = json.loads(left["result_json"])["summary"]
        right_summary = json.loads(right["result_json"])["summary"]
        metrics = (
            ("peak_demand_sessions", lambda item: item["peak"]["demand_sessions"]),
            ("peak_waiting_sessions", lambda item: item["peak"]["waiting_sessions"]),
            ("max_downlink_gap_mbps", lambda item: item["max_downlink_gap_mbps"]),
            ("max_uplink_gap_mbps", lambda item: item["max_uplink_gap_mbps"]),
            ("max_waiting_sessions", lambda item: item["max_waiting_sessions"]),
            ("max_backlog_sessions", lambda item: item["max_backlog_sessions"]),
            ("bottleneck_slot_count", lambda item: item["bottleneck_slot_count"]),
        )
        delta: dict[str, Any] = {}
        for key, getter in metrics:
            delta[key] = {"left": getter(left_summary), "right": getter(right_summary), "delta": round(getter(right_summary) - getter(left_summary), 3)}
        delta["measures"] = {"left": left_summary["measures"], "right": right_summary["measures"]}
        return {
            "plan_id": plan_id,
            "left": self._version_summary(left),
            "right": self._version_summary(right),
            "left_recommendations": left_summary["recommendations"],
            "right_recommendations": right_summary["recommendations"],
            "delta": delta,
        }

    # ------------------------------------------------------------------
    # 快照组装：把数据库中的区段/画像与人工假设合并为冻结输入
    # ------------------------------------------------------------------
    def build_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        scenario = self._scenario(payload["scenario_code"])
        segment_codes = payload["segment_codes"]
        segments: list[dict[str, Any]] = []
        segment_shares: list[dict[str, Any]] = []
        for code in segment_codes:
            segment = self.repository.segment_by_code(scenario["id"], code)
            if segment is None:
                raise NotFoundError(f"场景区段不存在：{code}")
            if segment["status"] != "active":
                raise ValidationError(f"场景区段当前不可用于建模：{code}")
            segments.append({"code": segment["code"], "name": segment["name"], "capacity_mbps": segment["capacity_mbps"]})
        for item in payload["segment_shares"]:
            segment_shares.append({"segment_code": item["segment_code"], "share": round(float(item["share"]), 6)})

        applications: list[dict[str, Any]] = []
        mix_priority: dict[str, int] = {}
        for code in payload["app_codes"]:
            app = self.repository.application_by_code(code)
            if app is None:
                raise NotFoundError(f"应用画像不存在：{code}")
            if app["status"] != "active":
                raise ValidationError(f"应用画像当前不可用于建模：{code}")
            applications.append({
                "app_code": app["app_code"],
                "name": app["name"],
                "category": app["category"],
                "min_downlink_mbps": app["min_downlink_mbps"],
                "min_uplink_mbps": app["min_uplink_mbps"],
            })
            mix_priority[code] = app["default_priority"]
        app_mix = []
        for item in payload["app_mix"]:
            app_mix.append({
                "app_code": item["app_code"],
                "share": round(float(item["share"]), 6),
                "entitlement_rate": round(float(item["entitlement_rate"]), 6),
                "priority": int(item["priority"]) if item.get("priority") is not None else mix_priority[item["app_code"]],
            })

        window = {"date": payload["window"].date if hasattr(payload["window"], "date") else payload["window"]["date"],
                  "slot_minutes": payload["window"].slot_minutes if hasattr(payload["window"], "slot_minutes") else payload["window"]["slot_minutes"]}
        peak_spec: Any
        curve = payload["peak_curve"]
        if hasattr(curve, "preset"):
            peak_spec = {"preset": curve.preset} if curve.preset else [point.model_dump() for point in curve.points]
        elif isinstance(curve, dict):
            peak_spec = {"preset": curve["preset"]} if curve.get("preset") else curve.get("points", [])
        else:
            peak_spec = curve
        peak_profile = resolve_peak_profile(peak_spec, int(window["slot_minutes"]))

        measures_input = payload.get("measures")
        if hasattr(measures_input, "model_dump"):
            measures = measures_input.model_dump()
        elif measures_input is None:
            measures = {"capacity_expansion_mbps": 0, "throttle_rate": 0, "priority_mode": "default"}
        else:
            measures = dict(measures_input)

        return {
            "scenario": {
                "code": scenario["code"],
                "name": scenario["name"],
                "scene_type": scenario["scene_type"],
                "timezone": scenario["timezone"],
                "max_concurrent_sessions": scenario["max_concurrent_sessions"],
                "capacity_mbps": scenario["capacity_mbps"],
            },
            "window": window,
            "attendees": int(payload["attendees"]),
            "segments": segments,
            "segment_shares": segment_shares,
            "applications": applications,
            "app_mix": app_mix,
            "peak_profile": peak_profile,
            "backlog_carry_ratio": round(float(payload.get("backlog_carry_ratio", 0.8)), 6),
            "measures": {
                "capacity_expansion_mbps": round(float(measures["capacity_expansion_mbps"]), 3),
                "throttle_rate": round(float(measures["throttle_rate"]), 3),
                "priority_mode": measures["priority_mode"],
            },
        }

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def _scenario(self, code: str) -> sqlite3.Row:
        row = self.repository.scenario_by_code(code)
        if row is None:
            raise NotFoundError("网络场景不存在")
        return row

    def _plan(self, plan_id: int) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM capacity_plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFoundError("容量预案不存在")
        return row

    def _version_row(self, version_id: int) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM capacity_plan_versions WHERE id=?", (version_id,)).fetchone()
        if row is None:
            raise NotFoundError("预案版本不存在")
        return row

    def _version(self, connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        result = self._version_summary(row)
        result["assumptions"] = json.loads(row["assumptions_json"])
        result["result"] = json.loads(row["result_json"]) if row["result_json"] else None
        result["events"] = [
            self._event_dict(item)
            for item in connection.execute(
                "SELECT * FROM capacity_plan_events WHERE version_id=? ORDER BY id",
                (row["id"],),
            ).fetchall()
        ]
        return result

    @staticmethod
    def _version_summary(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result.pop("assumptions_json", None)
        result.pop("result_json", None)
        result["measures"] = json.loads(row["assumptions_json"]).get("measures")
        return result

    @staticmethod
    def _canonical(payload: dict[str, Any]) -> tuple[str, str]:
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return text, request_fingerprint(payload)

    @staticmethod
    def _digest(payload: dict[str, Any]) -> str:
        return request_fingerprint(payload)

    @staticmethod
    def _event(connection: sqlite3.Connection, plan_id: int, version_id: int | None, event_type: str, actor: str, detail: dict[str, Any], now: str) -> None:
        connection.execute(
            "INSERT INTO capacity_plan_events(plan_id,version_id,event_type,actor,detail_json,created_at) VALUES(?,?,?,?,?,?)",
            (plan_id, version_id, event_type, actor, json.dumps(detail, ensure_ascii=False, sort_keys=True), now),
        )

    @staticmethod
    def _events(connection: sqlite3.Connection, plan_id: int, version_id: int | None = None) -> list[dict[str, Any]]:
        if version_id is None:
            rows = connection.execute("SELECT * FROM capacity_plan_events WHERE plan_id=? ORDER BY id", (plan_id,)).fetchall()
        else:
            rows = connection.execute("SELECT * FROM capacity_plan_events WHERE plan_id=? AND (version_id=? OR version_id IS NULL) ORDER BY id", (plan_id, version_id)).fetchall()
        return [CapacityScenarioService._event_dict(row) for row in rows]

    @staticmethod
    def _event_dict(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["detail"] = json.loads(item.pop("detail_json"))
        return item
