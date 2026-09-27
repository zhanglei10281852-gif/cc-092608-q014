from __future__ import annotations

import pytest

from app.core.security import request_fingerprint
from app.network.rules import DEFAULT_RULES
from app.network.scenario_engine import evaluate, resolve_peak_profile


# ---------------------------------------------------------------------------
# 引擎纯函数测试：不依赖数据库与时钟，必须确定性可复现
# ---------------------------------------------------------------------------
def flat_profile(slot_minutes: int = 30, factor: float = 1.0):
    count = 24 * 60 // slot_minutes
    return [{"slot": slot, "factor": factor} for slot in range(count)]


def engine_snapshot(**overrides):
    snapshot = {
        "scenario": {
            "code": "concert-sh",
            "name": "上海体育场",
            "scene_type": "concert",
            "timezone": "Asia/Shanghai",
            "max_concurrent_sessions": 100_000,
            "capacity_mbps": 100.0,
        },
        "window": {"date": "2026-10-01", "slot_minutes": 30},
        "attendees": 100,
        "segments": [{"code": "stand-a", "name": "A看台", "capacity_mbps": 100.0}],
        "segment_shares": [{"segment_code": "stand-a", "share": 1.0}],
        "applications": [
            {"app_code": "arena-game", "name": "竞技游戏", "category": "game", "min_downlink_mbps": 6.0, "min_uplink_mbps": 1.0},
        ],
        "app_mix": [{"app_code": "arena-game", "share": 1.0, "entitlement_rate": 1.0, "priority": 75}],
        "peak_profile": flat_profile(),
        "backlog_carry_ratio": 0.8,
        "measures": {"capacity_expansion_mbps": 0.0, "throttle_rate": 0.0, "priority_mode": "default"},
    }
    snapshot.update(overrides)
    return snapshot


def test_peak_preset_and_manual_curve_resolve_per_slot():
    preset = resolve_peak_profile({"preset": "concert"}, 30)
    assert len(preset) == 48
    assert preset[0] == {"slot": 0, "factor": 0.0}
    # 19:30 是演唱会模板的峰值
    assert preset[39]["factor"] == 1.0
    manual = resolve_peak_profile(
        [{"time": "00:00", "factor": 0.0}, {"time": "01:00", "factor": 1.0}, {"time": "24:00", "factor": 0.0}],
        30,
    )
    assert manual[1]["factor"] == 0.5
    assert manual[2]["factor"] == 1.0


def test_evaluation_is_deterministic_for_same_assumptions():
    snapshot = engine_snapshot()
    first = evaluate(snapshot)
    second = evaluate(snapshot)
    assert first == second
    assert request_fingerprint(first) == request_fingerprint(second)


def test_detects_downlink_uplink_concurrency_and_backlog_bottlenecks():
    # 上下行缺口：每会话 6 下行 / 10 上行，100 会话，容量只有 100
    snapshot = engine_snapshot(
        backlog_carry_ratio=0.0,
        applications=[
            {"app_code": "live-app", "name": "直播", "category": "live", "min_downlink_mbps": 6.0, "min_uplink_mbps": 10.0},
        ],
        app_mix=[{"app_code": "live-app", "share": 1.0, "entitlement_rate": 1.0, "priority": 60}],
    )
    result = evaluate(snapshot)
    assert result["summary"]["max_downlink_gap_mbps"] == 500.0
    assert result["summary"]["max_uplink_gap_mbps"] == 900.0
    all_bottlenecks = {label for slot in result["curve"] for label in slot["bottlenecks"]}
    assert {"downlink", "uplink", "waiting_backlog"} <= all_bottlenecks

    # 候补结转：80% 未满足需求滚入下一个时间槽形成积压
    carry = evaluate(engine_snapshot(
        backlog_carry_ratio=0.8,
        applications=[
            {"app_code": "live-app", "name": "直播", "category": "live", "min_downlink_mbps": 6.0, "min_uplink_mbps": 10.0},
        ],
        app_mix=[{"app_code": "live-app", "share": 1.0, "entitlement_rate": 1.0, "priority": 60}],
    ))
    assert carry["summary"]["max_backlog_sessions"] > 0
    assert carry["summary"]["max_waiting_sessions"] > result["summary"]["max_waiting_sessions"]

    # 并发上限：容量充裕但并发上限只有 10
    concurrency_snapshot = engine_snapshot(
        backlog_carry_ratio=0.0,
        scenario={**snapshot["scenario"], "max_concurrent_sessions": 10, "capacity_mbps": 1_000_000.0},
        segments=[{"code": "stand-a", "name": "A看台", "capacity_mbps": 1_000_000.0}],
        applications=[
            {"app_code": "live-app", "name": "直播", "category": "live", "min_downlink_mbps": 6.0, "min_uplink_mbps": 10.0},
        ],
        app_mix=[{"app_code": "live-app", "share": 1.0, "entitlement_rate": 1.0, "priority": 60}],
    )
    concurrency_result = evaluate(concurrency_snapshot)
    assert concurrency_result["summary"]["peak"]["demand_sessions"] == 100
    assert "concurrency" in {label for slot in concurrency_result["curve"] for label in slot["bottlenecks"]}
    assert concurrency_result["summary"]["peak"]["waiting_sessions"] == 90


def test_expansion_throttle_and_priority_measures_change_outcome():
    applications = [
        {"app_code": "arena-game", "name": "竞技游戏", "category": "game", "min_downlink_mbps": 6.0, "min_uplink_mbps": 1.0},
        {"app_code": "office-app", "name": "办公", "category": "office", "min_downlink_mbps": 2.0, "min_uplink_mbps": 0.5},
    ]
    mix = [
        {"app_code": "arena-game", "share": 0.5, "entitlement_rate": 1.0, "priority": 75},
        {"app_code": "office-app", "share": 0.5, "entitlement_rate": 1.0, "priority": 30},
    ]
    baseline = evaluate(engine_snapshot(applications=applications, app_mix=mix, backlog_carry_ratio=0.0))
    peak = baseline["summary"]["peak"]["slot"]
    peak_rows = {row["app_code"]: row for seg in baseline["curve"][peak]["segments"] for row in seg["affected_applications"]}
    assert peak_rows["arena-game"]["admitted_sessions"] == 12
    assert baseline["summary"]["max_downlink_gap_mbps"] == 300.0

    # 扩容 500M 后缺口消失
    expanded = evaluate(engine_snapshot(
        applications=applications, app_mix=mix, backlog_carry_ratio=0.0,
        measures={"capacity_expansion_mbps": 500.0, "throttle_rate": 0.0, "priority_mode": "default"},
    ))
    assert expanded["summary"]["max_downlink_gap_mbps"] == 0.0
    assert expanded["summary"]["max_waiting_sessions"] == 0

    # 限流 90% 压缩单会话带宽后无需候补
    throttled = evaluate(engine_snapshot(
        applications=applications, app_mix=mix, backlog_carry_ratio=0.0,
        measures={"capacity_expansion_mbps": 0.0, "throttle_rate": 0.9, "priority_mode": "default"},
    ))
    assert throttled["summary"]["max_waiting_sessions"] == 0

    # protect_high：高优先级游戏应用拿走 16 个会话，办公只剩 2 个
    protected = evaluate(engine_snapshot(
        applications=applications, app_mix=mix, backlog_carry_ratio=0.0,
        measures={"capacity_expansion_mbps": 0.0, "throttle_rate": 0.0, "priority_mode": "protect_high"},
    ))
    protected_peak = protected["summary"]["peak"]["slot"]
    protected_rows = {row["app_code"]: row for seg in protected["curve"][protected_peak]["segments"] for row in seg["affected_applications"]}
    assert protected_rows["arena-game"]["admitted_sessions"] == 16
    assert protected_rows["office-app"]["admitted_sessions"] == 2


def test_recommendations_cover_all_action_types():
    snapshot = engine_snapshot(
        scenario={"scenario_code": "x", "code": "concert-sh", "name": "场馆", "scene_type": "concert", "timezone": "Asia/Shanghai", "max_concurrent_sessions": 10, "capacity_mbps": 100.0},
        applications=[
            {"app_code": "live-app", "name": "直播", "category": "live", "min_downlink_mbps": 6.0, "min_uplink_mbps": 10.0},
        ],
        app_mix=[{"app_code": "live-app", "share": 1.0, "entitlement_rate": 1.0, "priority": 30}],
    )
    actions = {item["action"] for item in evaluate(snapshot)["summary"]["recommendations"]}
    assert {"expand_capacity", "raise_concurrency", "throttle", "adjust_priority"} <= actions


def test_invalid_shares_and_curve_are_rejected():
    from app.core.errors import ValidationError

    with pytest.raises(ValidationError):
        evaluate(engine_snapshot(segment_shares=[{"segment_code": "stand-a", "share": 0.5}]))
    with pytest.raises(ValidationError):
        bad = engine_snapshot()
        bad["peak_profile"] = flat_profile()[:-1]
        evaluate(bad)


# ---------------------------------------------------------------------------
# API 集成测试
# ---------------------------------------------------------------------------
APPS = [
    ("arena-game", "竞技游戏", "game", 6.0, 1.0, 75),
    ("live-app", "直播", "live", 1.0, 10.0, 60),
    ("office-app", "办公", "office", 2.0, 0.5, 30),
]


def prepare_capacity(client, *, concurrency: int = 100_000, capacity: int = 100, segment_capacity: int = 100):
    scenario = client.post("/api/network/scenarios", json={
        "code": "concert-sh", "name": "上海体育场", "scene_type": "concert",
        "timezone": "Asia/Shanghai", "max_concurrent_sessions": concurrency, "capacity_mbps": capacity,
    })
    assert scenario.status_code == 201, scenario.text
    segment = client.post("/api/network/scenarios/concert-sh/segments", json={
        "code": "stand-a", "name": "A看台", "sequence_no": 1,
        "expected_dwell_seconds": 3600, "capacity_mbps": segment_capacity,
    })
    assert segment.status_code == 201, segment.text
    for code, name, category, dl, ul, priority in APPS:
        response = client.post("/api/network/applications", json={
            "app_code": code, "name": name, "category": category,
            "latency_target_ms": 80, "packet_loss_target": 0.01,
            "min_downlink_mbps": dl, "min_uplink_mbps": ul, "default_priority": priority,
        })
        assert response.status_code == 201, response.text


def evaluate_payload(**overrides):
    payload = {
        "scenario_code": "concert-sh",
        "attendees": 100,
        "window": {"date": "2026-10-01", "slot_minutes": 30},
        "peak_curve": {"preset": "concert", "points": None},
        "segment_codes": ["stand-a"],
        "segment_shares": [{"segment_code": "stand-a", "share": 1.0}],
        "app_codes": ["live-app"],
        "app_mix": [{"app_code": "live-app", "share": 1.0, "entitlement_rate": 1.0}],
        "backlog_carry_ratio": 0.0,
        "measures": {"capacity_expansion_mbps": 0, "throttle_rate": 0, "priority_mode": "default"},
    }
    payload.update(overrides)
    return payload


def test_evaluate_endpoint_returns_peak_gap_affected_and_actions(client):
    prepare_capacity(client)
    response = client.post("/api/network/capacity/scenarios/evaluate", json=evaluate_payload())
    assert response.status_code == 200, response.text
    body = response.json()
    summary = body["summary"]
    assert summary["peak"]["starts_at"].startswith("2026-10-01T19:30")
    assert summary["peak"]["starts_at"].endswith("+08:00")
    assert summary["max_downlink_gap_mbps"] >= 0
    assert summary["max_uplink_gap_mbps"] > 0
    assert summary["affected_applications"][0]["app_code"] == "live-app"
    assert summary["affected_applications"][0]["waiting_sessions"] > 0
    assert any(action["action"] == "expand_capacity" for action in summary["recommendations"])
    assert len(body["curve"]) == 48
    assert body["assumptions_digest"]


def test_plan_versions_recompute_compare_and_freeze_on_approval(client):
    prepare_capacity(client)
    plan = client.post("/api/network/capacity/plans", json={
        "scenario_code": "concert-sh", "code": "oct01-plan", "name": "十月演唱会保障预案", "actor": "ops",
    })
    assert plan.status_code == 201, plan.text
    plan_id = plan.json()["id"]

    def add_version(label: str, measures):
        payload = evaluate_payload()
        payload.pop("scenario_code")
        response = client.post(f"/api/network/capacity/plans/{plan_id}/versions", json={
            **payload, "label": label, "measures": measures, "actor": "ops",
        })
        assert response.status_code == 201, response.text
        return response.json()

    baseline = add_version("基线", {"capacity_expansion_mbps": 0, "throttle_rate": 0, "priority_mode": "default"})
    duplicate = add_version("基线重复", {"capacity_expansion_mbps": 0, "throttle_rate": 0, "priority_mode": "default"})
    expanded = add_version("扩容1000M", {"capacity_expansion_mbps": 1000, "throttle_rate": 0, "priority_mode": "default"})
    assert duplicate["assumptions_digest"] == baseline["assumptions_digest"]
    assert duplicate["result_digest"] == baseline["result_digest"]
    assert expanded["result_digest"] != baseline["result_digest"]

    # 相同版本重算结果一致
    recomputed = client.post(f"/api/network/capacity/versions/{baseline['id']}/recompute", json={"actor": "ops"})
    assert recomputed.status_code == 200
    assert recomputed.json()["state"] == "draft"

    # 扩容版本相对基线缺口下降
    comparison = client.get(f"/api/network/capacity/plans/{plan_id}/compare", params={"left": baseline["id"], "right": expanded["id"]})
    assert comparison.status_code == 200
    gap_delta = comparison.json()["delta"]["max_uplink_gap_mbps"]
    assert gap_delta["delta"] < 0
    assert gap_delta["right"] == 0

    # 审批冻结输入与摘要
    approved = client.post(f"/api/network/capacity/versions/{baseline['id']}/approve", json={"actor": "boss", "note": "同意按预案执行"})
    assert approved.status_code == 200
    assert approved.json()["state"] == "approved"
    assert approved.json()["approved_by"] == "boss"
    assert approved.json()["assumptions_digest"] == baseline["assumptions_digest"]
    assert approved.json()["result_digest"] == baseline["result_digest"]
    again = client.post(f"/api/network/capacity/versions/{baseline['id']}/approve", json={"actor": "boss", "note": "重复审批"})
    assert again.status_code == 200

    # 冻结摘要被篡改时重算必须报错
    from app.database import get_connection
    get_connection().execute("UPDATE capacity_plan_versions SET result_digest=? WHERE id=?", ("tampered", expanded["id"]))
    drifted = client.post(f"/api/network/capacity/versions/{expanded['id']}/recompute", json={"actor": "ops"})
    assert drifted.status_code == 409
    drifted_approval = client.post(f"/api/network/capacity/versions/{expanded['id']}/approve", json={"actor": "boss", "note": ""})
    assert drifted_approval.status_code == 409

    detail = client.get(f"/api/network/capacity/plans/{plan_id}").json()
    assert [item["version_no"] for item in detail["versions"]] == [3, 2, 1]
    assert {event["event_type"] for event in detail["events"]} >= {"created", "version_created", "recomputed", "approved"}


def test_planning_does_not_change_live_reservations(client):
    prepare_capacity(client)
    # 先制造一个真实的实时加速会话与预留
    policy = client.post("/api/network/scenarios/concert-sh/policies", json={"rules": DEFAULT_RULES, "actor": "tests"})
    assert policy.status_code == 201
    published = client.post(f"/api/network/policies/{policy.json()['id']}/publish", json={
        "actor": "tests", "effective_from": "2020-01-01T00:00:00Z",
    })
    assert published.status_code == 200
    entitlement = client.post("/api/network/entitlements", json={
        "subscriber_hash": "subscriber-planning-0000001",
        "scenario_code": "concert-sh",
        "product_code": "concert-boost",
        "valid_from": "2020-01-01T00:00:00Z",
        "valid_until": "2030-01-01T00:00:00Z",
        "source_order_id": "order-planning-001",
    })
    assert entitlement.status_code == 201
    sample = client.post("/api/network/samples", json={
        "sample_key": "sample-planning-001",
        "scenario_code": "concert-sh",
        "segment_code": "stand-a",
        "app_code": "live-app",
        "subscriber_hash": "subscriber-planning-0000001",
        "device_class": "phone",
        "latency_ms": 350,
        "packet_loss": 0.08,
        "downlink_mbps": 0.4,
        "uplink_mbps": 0.2,
        "observed_at": "2026-09-27T07:30:00Z",
    })
    assert sample.status_code == 202
    started = client.post(f"/api/network/incidents/{sample.json()['incident_id']}/accelerate", json={"actor": "tests"})
    assert started.status_code == 200, started.text
    held_before = client.get("/api/network/analytics/capacity").json()["items"][0]

    # 试算、建版本、重算、审批全部走完
    evaluation = client.post("/api/network/capacity/scenarios/evaluate", json=evaluate_payload())
    assert evaluation.status_code == 200
    plan = client.post("/api/network/capacity/plans", json={
        "scenario_code": "concert-sh", "code": "live-check", "name": "实时预留隔离验证", "actor": "ops",
    }).json()
    payload = evaluate_payload()
    payload.pop("scenario_code")
    version = client.post(f"/api/network/capacity/plans/{plan['id']}/versions", json={**payload, "actor": "ops"})
    assert version.status_code == 201
    assert client.post(f"/api/network/capacity/versions/{version.json()['id']}/recompute", json={"actor": "ops"}).status_code == 200
    assert client.post(f"/api/network/capacity/versions/{version.json()['id']}/approve", json={"actor": "boss", "note": ""}).status_code == 200

    held_after = client.get("/api/network/analytics/capacity").json()["items"][0]
    assert held_after["held_downlink_mbps"] == held_before["held_downlink_mbps"]
    assert held_after["active_sessions"] == held_before["active_sessions"] == 1
    from app.database import get_connection
    assert get_connection().execute("SELECT COUNT(*) FROM capacity_reservations WHERE state='held'").fetchone()[0] == 1


def test_capacity_api_input_validation(client):
    prepare_capacity(client)
    # 占比之和不等于 1
    bad_shares = evaluate_payload(segment_shares=[{"segment_code": "stand-a", "share": 0.5}])
    assert client.post("/api/network/capacity/scenarios/evaluate", json=bad_shares).status_code == 422
    # 高峰曲线必须覆盖 00:00~24:00
    bad_curve = evaluate_payload(peak_curve={"preset": None, "points": [
        {"time": "01:00", "factor": 0.0}, {"time": "23:00", "factor": 1.0},
    ]})
    assert client.post("/api/network/capacity/scenarios/evaluate", json=bad_curve).status_code == 422
    # 不存在的区段
    missing_segment = evaluate_payload(
        segment_codes=["missing"],
        segment_shares=[{"segment_code": "missing", "share": 1.0}],
    )
    assert client.post("/api/network/capacity/scenarios/evaluate", json=missing_segment).status_code == 404
    # 不存在的场景
    missing_scenario = evaluate_payload(scenario_code="nope")
    assert client.post("/api/network/capacity/scenarios/evaluate", json=missing_scenario).status_code == 404
    # 跨预案对比被拒绝
    plan = client.post("/api/network/capacity/plans", json={
        "scenario_code": "concert-sh", "code": "cmp-plan", "name": "对比预案", "actor": "ops",
    }).json()
    other = client.post("/api/network/capacity/plans", json={
        "scenario_code": "concert-sh", "code": "other-plan", "name": "另一个预案", "actor": "ops",
    }).json()
    payload = evaluate_payload()
    payload.pop("scenario_code")
    left = client.post(f"/api/network/capacity/plans/{plan['id']}/versions", json={**payload, "actor": "ops"}).json()
    right = client.post(f"/api/network/capacity/plans/{other['id']}/versions", json={**payload, "actor": "ops"}).json()
    response = client.get(f"/api/network/capacity/plans/{plan['id']}/compare", params={"left": left["id"], "right": right["id"]})
    assert response.status_code == 422
