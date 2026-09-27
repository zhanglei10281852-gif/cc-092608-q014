from __future__ import annotations

import copy

from app.network.capacity_service import CapacityPlanService


def prepare(client):
    client.post(
        "/api/network/scenarios",
        json={"code": "arena-01", "name": "演艺中心", "scene_type": "concert", "timezone": "Asia/Shanghai", "max_concurrent_sessions": 400, "capacity_mbps": 1000},
    )
    for sequence, code, cap in ((1, "east", 300), (2, "west", 300)):
        client.post(
            "/api/network/scenarios/arena-01/segments",
            json={"code": code, "name": f"{code}-看台", "sequence_no": sequence, "expected_dwell_seconds": 600, "capacity_mbps": cap},
        )
    client.post(
        "/api/network/applications",
        json={"app_code": "live-stream", "name": "移动直播", "category": "live", "latency_target_ms": 120, "packet_loss_target": 0.02, "min_downlink_mbps": 10, "min_uplink_mbps": 8, "default_priority": 80},
    )
    client.post(
        "/api/network/applications",
        json={"app_code": "office-sync", "name": "办公同步", "category": "office", "latency_target_ms": 300, "packet_loss_target": 0.05, "min_downlink_mbps": 2, "min_uplink_mbps": 1, "default_priority": 30},
    )


def assumptions(**overrides):
    payload = {
        "scenario_code": "arena-01",
        "event": {"starts_at": "2026-10-01T18:00:00+08:00", "ends_at": "2026-10-01T19:00:00+08:00", "slot_minutes": 15},
        "attendees": 20000,
        "historical_curve": [0.3, 0.7, 1.0, 0.6],
        "manual_curve": [0.2, 0.6, 1.0, 0.5],
        "historical_weight": 0.5,
        "segments": [
            {"code": "east", "share": 0.6},
            {"code": "west", "share": 0.4},
        ],
        "applications": [
            {"app_code": "live-stream", "traffic_share": 0.3, "penetration": 0.25},
            {"app_code": "office-sync", "traffic_share": 0.7, "penetration": 0.4},
        ],
        "measures": {},
    }
    payload.update(overrides)
    return payload


def test_evaluate_returns_peak_gap_apps_and_actions(client):
    prepare(client)
    response = client.post("/api/network/capacity/evaluate", json={"assumptions": assumptions()})
    assert response.status_code == 200, response.text
    body = response.json()
    result = body["result"]
    assert result["event"]["slot_count"] == 4
    assert result["peak"]["slot_index"] == 2
    assert result["peak"]["starts_at"] == "2026-10-01T10:30:00+00:00"
    assert set(result["bottlenecks"]) >= {"downlink", "uplink", "backlog"}
    codes = {item["app_code"] for item in result["affected_applications"]}
    assert codes == {"live-stream", "office-sync"}
    action_types = {item["type"] for item in result["recommended_actions"]}
    assert {"expand_downlink", "expand_uplink", "admission_control"} <= action_types
    east = next(item for item in result["segment_summary"] if item["segment_code"] == "east")
    assert east["max_downlink_shortfall_mbps"] > 0
    # 时间槽包含各区段与应用明细
    peak_slot = result["slots"][2]
    assert {item["segment_code"] for item in peak_slot["segments"]} == {"east", "west"}
    assert len(peak_slot["segments"][0]["applications"]) == 2
    assert "assumptions_digest" in body and len(body["assumptions_digest"]) == 64


def test_evaluate_is_deterministic(client):
    prepare(client)
    first = client.post("/api/network/capacity/evaluate", json={"assumptions": assumptions()}).json()
    second = client.post("/api/network/capacity/evaluate", json={"assumptions": assumptions()}).json()
    assert first["result"] == second["result"]
    assert first["assumptions_digest"] == second["assumptions_digest"]


def test_same_curve_inputs_same_digest(client):
    prepare(client)
    payload = assumptions()
    # 应用顺序不同属于同一组假设，摘要一致
    reordered = copy.deepcopy(payload)
    reordered["applications"] = list(reversed(reordered["applications"]))
    first = client.post("/api/network/capacity/evaluate", json={"assumptions": payload}).json()
    second = client.post("/api/network/capacity/evaluate", json={"assumptions": reordered}).json()
    assert first["assumptions_digest"] == second["assumptions_digest"]


def test_requires_curve_and_valid_window(client):
    prepare(client)
    payload = assumptions()
    payload["manual_curve"] = None
    payload["historical_curve"] = None
    missing = client.post("/api/network/capacity/evaluate", json={"assumptions": payload})
    assert missing.status_code == 422
    bad_window = assumptions()
    bad_window["event"]["slot_minutes"] = 20
    bad_window["historical_curve"] = [0.3, 0.7, 1.0]
    response = client.post("/api/network/capacity/evaluate", json={"assumptions": bad_window})
    assert response.status_code == 422


def test_capacity_expansion_and_throttle_reduce_gap(client):
    prepare(client)
    baseline = client.post("/api/network/capacity/evaluate", json={"assumptions": assumptions()}).json()["result"]
    expanded_payload = assumptions(measures={"capacity_scale": 3.0})
    expanded = client.post("/api/network/capacity/evaluate", json={"assumptions": expanded_payload}).json()["result"]
    assert max(item["max_downlink_shortfall_mbps"] for item in expanded["segment_summary"]) < \
        max(item["max_downlink_shortfall_mbps"] for item in baseline["segment_summary"])
    throttled_payload = assumptions(measures={"admission_factor": 0.2})
    throttled = client.post("/api/network/capacity/evaluate", json={"assumptions": throttled_payload}).json()["result"]
    assert throttled["totals"]["peak_backlog_sessions"] < baseline["totals"]["peak_backlog_sessions"]


def test_priority_protects_high_priority_application(client):
    prepare(client)
    # 容量极小：高优先级 live-stream 必须先受理，office-sync 进入候补
    tight = assumptions(measures={"segment_capacity_overrides": {"east": 50, "west": 30}})
    result = client.post("/api/network/capacity/evaluate", json={"assumptions": tight}).json()["result"]
    peak_segment = result["slots"][2]["segments"][0]
    by_app = {item["app_code"]: item for item in peak_segment["applications"]}
    assert by_app["live-stream"]["admitted_sessions"] > 0
    # 严格按优先级受理：容量耗尽后低优先级应用一个会话都拿不到
    assert by_app["office-sync"]["admitted_sessions"] == 0
    assert by_app["office-sync"]["waiting_sessions"] == by_app["office-sync"]["requested_sessions"]
    assert by_app["live-stream"]["admitted_sessions"] >= by_app["office-sync"]["admitted_sessions"]


def test_scenario_concurrency_ceiling_bottleneck(client):
    prepare(client)
    huge = assumptions(attendees=20000, measures={"capacity_scale": 10.0})
    result = client.post("/api/network/capacity/evaluate", json={"assumptions": huge}).json()["result"]
    assert "concurrency" in result["bottlenecks"]
    assert any(action["type"] == "raise_concurrency" for action in result["recommended_actions"])


def test_plan_versions_compare_and_approve_freeze(client):
    prepare(client)
    created = client.post(
        "/api/network/capacity/plans",
        json={"code": "opening-night", "name": "开场保障预案", "scenario_code": "arena-01", "actor": "ops", "description": "开票即开场"},
    )
    assert created.status_code == 201, created.text
    plan_id = created.json()["id"]

    v1_response = client.post(f"/api/network/capacity/plans/{plan_id}/versions", json={"assumptions": assumptions(), "actor": "ops"})
    assert v1_response.status_code == 201, v1_response.text
    v1 = v1_response.json()
    assert v1["version_no"] == 1 and v1["state"] == "draft"

    expanded = assumptions(measures={"capacity_scale": 2.0})
    v2_response = client.post(
        f"/api/network/capacity/plans/{plan_id}/versions",
        json={"assumptions": expanded, "actor": "ops", "based_on_version_no": 1, "note": "翻倍扩容"},
    )
    assert v2_response.status_code == 201, v2_response.text
    v2 = v2_response.json()
    assert v2["version_no"] == 2

    compare = client.post(f"/api/network/capacity/plans/{plan_id}/compare", json={"version_nos": [1, 2]})
    assert compare.status_code == 200, compare.text
    delta = compare.json()["versions"][1]["delta_from_first"]
    assert delta["max_downlink_shortfall_mbps"] < 0
    assert delta["peak_admitted_sessions"] >= 0
    assert compare.json()["versions"][0]["delta_from_first"] is None

    # 提交 -> 审批；审批后版本冻结，不能再次提交或驳回
    submitted = client.post(f"/api/network/capacity/versions/{v1['id']}/submit", json={"actor": "lead", "note": "请审核"})
    assert submitted.status_code == 200 and submitted.json()["state"] == "submitted"
    approved = client.post(f"/api/network/capacity/versions/{v1['id']}/approve", json={"actor": "director", "note": "同意执行"})
    assert approved.status_code == 200
    assert approved.json()["state"] == "approved"
    assert approved.json()["approved_by"] == "director"
    assert approved.json()["assumptions_digest"] == v1["assumptions_digest"]
    assert client.post(f"/api/network/capacity/versions/{v1['id']}/submit", json={"actor": "x"}).status_code == 409
    assert client.post(f"/api/network/capacity/versions/{v1['id']}/approve", json={"actor": "x"}).status_code == 409
    # 驳回应附原因，且只能从已提交状态驳回
    assert client.post(f"/api/network/capacity/versions/{v2['id']}/reject", json={"actor": "director", "note": ""}).status_code == 422

    # 预案详情中保留版本列表与操作事件
    detail = client.get(f"/api/network/capacity/plans/{plan_id}").json()
    assert [item["version_no"] for item in detail["versions"]] == [2, 1]
    assert detail["events"][0]["event_type"] == "created"


def test_recalculate_is_consistent_and_never_changes_live_reservations(client):
    prepare(client)
    # 先制造一条实时容量预留
    from app.network.rules import DEFAULT_RULES
    policy = client.post("/api/network/scenarios/arena-01/policies", json={"rules": DEFAULT_RULES, "actor": "tests"}).json()
    client.post(f"/api/network/policies/{policy['id']}/publish", json={"actor": "tests", "effective_from": "2026-09-27T00:00:00Z"})
    client.post(
        "/api/network/entitlements",
        json={"subscriber_hash": "subscriber-capacity-00001", "scenario_code": "arena-01", "product_code": "concert-boost",
              "valid_from": "2026-09-27T00:00:00Z", "valid_until": "2026-10-02T00:00:00Z", "source_order_id": "order-capacity-0001"},
    )
    sample = client.post(
        "/api/network/samples",
        json={"sample_key": "capacity-sample-0001", "scenario_code": "arena-01", "segment_code": "east",
              "app_code": "live-stream", "subscriber_hash": "subscriber-capacity-00001", "device_class": "phone",
              "train_speed_kmh": 0, "latency_ms": 300, "packet_loss": 0.1, "downlink_mbps": 1, "uplink_mbps": 0.5,
              "observed_at": "2026-09-27T10:00:00Z"},
    ).json()
    started = client.post(f"/api/network/incidents/{sample['incident_id']}/accelerate", json={"actor": "tests"})
    assert started.status_code == 200, started.text
    session_id = started.json()["id"]

    plan = client.post("/api/network/capacity/plans", json={"code": "frozen-plan", "name": "冻结预案", "scenario_code": "arena-01", "actor": "ops"}).json()
    version = client.post(f"/api/network/capacity/plans/{plan['id']}/versions", json={"assumptions": assumptions(), "actor": "ops"}).json()
    client.post(f"/api/network/capacity/versions/{version['id']}/submit", json={"actor": "lead", "note": "审核"})
    client.post(f"/api/network/capacity/versions/{version['id']}/approve", json={"actor": "director", "note": "通过"})

    from app.database import get_connection
    before = dict(get_connection().execute(
        "SELECT state,downlink_mbps,uplink_mbps FROM capacity_reservations WHERE session_id=?", (session_id,)
    ).fetchone())

    for _ in range(2):
        recalc = client.post(f"/api/network/capacity/versions/{version['id']}/recalculate")
        assert recalc.status_code == 200
        body = recalc.json()
        assert body["consistent"] is True
        assert body["frozen"] is True
        assert body["stored_result_digest"] == body["recomputed_result_digest"]
        assert body["assumptions_digest"] == body["recomputed_assumptions_digest"]

    after = dict(get_connection().execute(
        "SELECT state,downlink_mbps,uplink_mbps FROM capacity_reservations WHERE session_id=?", (session_id,)
    ).fetchone())
    assert before == after


def test_version_scenario_must_match_plan(client):
    prepare(client)
    client.post("/api/network/scenarios", json={"code": "other-venue", "name": "备用场馆", "scene_type": "venue", "max_concurrent_sessions": 10, "capacity_mbps": 100})
    plan = client.post("/api/network/capacity/plans", json={"code": "mismatch-plan", "name": "错配预案", "scenario_code": "arena-01", "actor": "ops"})
    assert plan.status_code == 201
    payload = assumptions()
    payload["scenario_code"] = "other-venue"
    response = client.post(f"/api/network/capacity/plans/{plan.json()['id']}/versions", json={"assumptions": payload, "actor": "ops"})
    assert response.status_code == 422


def test_service_rejects_unknown_segment_and_application(client):
    prepare(client)
    service = CapacityPlanService()
    payload = assumptions()
    payload["segments"][0]["code"] = "north"
    try:
        service.evaluate_assumptions(payload)
        assert False, "应当拒绝未知区段"
    except Exception as exc:
        assert exc.__class__.__name__ == "NotFoundError"
