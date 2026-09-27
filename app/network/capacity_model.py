"""容量情景计算引擎。

纯函数、无副作用：只接收已经解析确定的输入（场景区段、应用画像、历史高峰曲线、
人工假设与扩容/限流/优先级措施），输出按时间槽的需求曲线与瓶颈识别结果。

相同输入必然得到相同结果与摘要，便于版本冻结、复核与重算比对。引擎不读取
实时容量预留，也不修改任何运行数据。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from app.core.clock import from_storage, to_storage
from app.core.errors import ValidationError
from app.core.security import request_fingerprint

MODEL_VERSION = "1.0"
MBPS_PRECISION = 3
COUNT_PRECISION = 3
FACTOR_PRECISION = 6
_SLOT_MINUTES_ALLOWED = (5, 10, 15, 20, 30, 60)


@dataclass(frozen=True, slots=True)
class ResolvedApplication:
    code: str
    traffic_share: float
    penetration: float
    activation_rate: float
    priority: int
    downlink_mbps: float
    uplink_mbps: float


@dataclass(frozen=True, slots=True)
class ResolvedSegment:
    code: str
    share: float
    capacity_mbps: float


def canonical_input(resolved: dict[str, Any]) -> str:
    """冻结输入的规范 JSON（按键排序、无空白）。"""

    return json.dumps(resolved, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def input_digest(resolved: dict[str, Any]) -> str:
    return request_fingerprint(resolved)


def result_digest(result: dict[str, Any]) -> str:
    return request_fingerprint(result)


def validate_event_window(starts_at: str, ends_at: str, slot_minutes: int) -> int:
    if slot_minutes not in _SLOT_MINUTES_ALLOWED:
        raise ValidationError("时间槽粒度必须是 5/10/15/20/30/60 分钟之一")
    try:
        start = from_storage(starts_at)
        end = from_storage(ends_at)
    except (TypeError, ValueError) as exc:
        raise ValidationError("保障时间窗格式不正确") from exc
    if start is None or end is None or end <= start:
        raise ValidationError("保障结束时间必须晚于开始时间")
    total_minutes = (end - start).total_seconds() / 60
    if total_minutes > 24 * 60:
        raise ValidationError("容量情景时间窗不能超过 24 小时")
    if int(total_minutes) % slot_minutes != 0:
        raise ValidationError("保障时间窗必须能被时间槽粒度整除")
    slot_count = int(total_minutes // slot_minutes)
    if slot_count <= 0:
        raise ValidationError("保障时间窗至少包含一个时间槽")
    return slot_count


def normalized_shares(values: dict[str, float], *, label: str, tolerance: float = 0.01) -> dict[str, float]:
    cleaned = {key: float(value) for key, value in values.items()}
    if not cleaned or any(value < 0 for value in cleaned.values()):
        raise ValidationError(f"{label}占比必须是非负数且至少包含一项")
    total = sum(cleaned.values())
    if total <= 0:
        raise ValidationError(f"{label}占比之和必须大于零")
    if abs(total - 1.0) > tolerance:
        raise ValidationError(f"{label}占比之和必须等于 1（允许 0.01 误差）")
    return {key: value / total for key, value in cleaned.items()}


def normalize_curve(values: list[float], slot_count: int, *, label: str) -> list[float]:
    if len(values) != slot_count:
        raise ValidationError(f"{label}长度必须等于时间槽数量 {slot_count}")
    curve = [float(value) for value in values]
    if any(value < 0 for value in curve):
        raise ValidationError(f"{label}不能包含负数")
    peak = max(curve)
    if peak <= 0:
        raise ValidationError(f"{label}至少需要一个正值")
    return [round(value / peak, FACTOR_PRECISION) for value in curve]


def blend_curves(historical: list[float] | None, manual: list[float] | None, slot_count: int, historical_weight: float) -> list[float]:
    if not 0.0 <= historical_weight <= 1.0:
        raise ValidationError("历史分布权重必须在 0 到 1 之间")
    hist = normalize_curve(historical, slot_count, label="历史高峰曲线") if historical else None
    plan = normalize_curve(manual, slot_count, label="人工到场曲线") if manual else None
    if hist is None and plan is None:
        raise ValidationError("必须提供历史高峰分布或人工到场曲线")
    if hist is None:
        return plan  # type: ignore[return-value]
    if plan is None:
        return hist
    weight = round(historical_weight, FACTOR_PRECISION)
    merged = [weight * hist[index] + (1.0 - weight) * plan[index] for index in range(slot_count)]
    return normalize_curve(merged, slot_count, label="合并到场曲线")


def build_resolved_input(
    *,
    scenario_code: str,
    max_concurrent_sessions: int,
    starts_at: str,
    ends_at: str,
    slot_minutes: int,
    attendees: int,
    occupancy_curve: list[float],
    segments: list[ResolvedSegment],
    applications: list[ResolvedApplication],
    measures: dict[str, Any],
) -> dict[str, Any]:
    slot_count = validate_event_window(starts_at, ends_at, slot_minutes)
    if attendees <= 0:
        raise ValidationError("观众人数必须大于零")
    occupancy = normalize_curve(occupancy_curve, slot_count, label="到场曲线")
    if not segments:
        raise ValidationError("至少需要一个场景区段")
    if not applications:
        raise ValidationError("至少需要一个应用画像")
    segment_map = normalized_shares({segment.code: segment.share for segment in segments}, label="区段承载")
    traffic = normalized_shares({app.code: app.traffic_share for app in applications}, label="应用构成")
    capacity_scale = _bounded(measures.get("capacity_scale", 1.0), "扩容倍数", minimum=0.01, maximum=10.0)
    concurrency_scale = _bounded(measures.get("concurrency_scale", 1.0), "并发扩容倍数", minimum=0.01, maximum=10.0)
    admission_factor = _bounded(measures.get("admission_factor", 1.0), "限流准入比例", minimum=0.0, maximum=1.0)
    overrides = measures.get("segment_capacity_overrides") or {}
    if not isinstance(overrides, dict) or any(not isinstance(value, (int, float)) or float(value) <= 0 for value in overrides.values()):
        raise ValidationError("区段扩容容量必须为正数")
    resolved_segments = []
    for segment in segments:
        baseline = float(overrides.get(segment.code, segment.capacity_mbps))
        resolved_segments.append({
            "code": segment.code,
            "share": round(segment_map[segment.code], FACTOR_PRECISION),
            "capacity_mbps": round(baseline * capacity_scale, MBPS_PRECISION),
            "concurrency_ceiling": round(max_concurrent_sessions * segment_map[segment.code] * concurrency_scale, COUNT_PRECISION),
        })
    resolved_applications = []
    for app in applications:
        if not 0 <= app.penetration <= 1 or not 0 <= app.activation_rate <= 1:
            raise ValidationError(f"应用 {app.code} 的渗透率与激活率必须在 0 到 1 之间")
        if not 0 <= app.priority <= 100:
            raise ValidationError(f"应用 {app.code} 的优先级必须在 0 到 100 之间")
        if app.downlink_mbps < 0 or app.uplink_mbps < 0:
            raise ValidationError(f"应用 {app.code} 的速率假设不能为负")
        resolved_applications.append({
            "code": app.code,
            "traffic_share": round(traffic[app.code], FACTOR_PRECISION),
            "entitlement_penetration": round(float(app.penetration), FACTOR_PRECISION),
            "activation_rate": round(float(app.activation_rate), FACTOR_PRECISION),
            "priority": int(app.priority),
            "downlink_mbps": round(float(app.downlink_mbps), MBPS_PRECISION),
            "uplink_mbps": round(float(app.uplink_mbps), MBPS_PRECISION),
        })
    resolved_applications.sort(key=lambda item: item["code"])
    resolved_segments.sort(key=lambda item: item["code"])
    return {
        "model_version": MODEL_VERSION,
        "scenario_code": scenario_code,
        "max_concurrent_sessions": int(max_concurrent_sessions),
        "event": {
            "starts_at": to_storage(from_storage(starts_at)),  # type: ignore[arg-type]
            "ends_at": to_storage(from_storage(ends_at)),  # type: ignore[arg-type]
            "slot_minutes": int(slot_minutes),
            "slot_count": slot_count,
        },
        "attendees": int(attendees),
        "occupancy_curve": occupancy,
        "segments": resolved_segments,
        "applications": resolved_applications,
        "measures": {
            "capacity_scale": round(capacity_scale, FACTOR_PRECISION),
            "concurrency_scale": round(concurrency_scale, FACTOR_PRECISION),
            "admission_factor": round(admission_factor, FACTOR_PRECISION),
            "segment_capacity_overrides": {code: float(value) for code, value in sorted(overrides.items())},
        },
    }


def evaluate(resolved: dict[str, Any]) -> dict[str, Any]:
    """根据冻结输入计算按时间槽的需求曲线、瓶颈与建议动作。"""

    event = resolved["event"]
    slot_minutes = int(event["slot_minutes"])
    slot_count = int(event["slot_count"])
    start = from_storage(event["starts_at"])
    segments = resolved["segments"]
    applications = resolved["applications"]
    admission_factor = float(resolved["measures"]["admission_factor"])
    scenario_ceiling = float(resolved["max_concurrent_sessions"]) * float(resolved["measures"]["concurrency_scale"])

    carry: dict[tuple[str, str], float] = {
        (segment["code"], app["code"]): 0.0 for segment in segments for app in applications
    }
    slots: list[dict[str, Any]] = []
    peak: dict[str, Any] | None = None
    segment_worst: dict[str, dict[str, float]] = {
        segment["code"]: {"downlink_shortfall": 0.0, "uplink_shortfall": 0.0, "backlog": 0.0, "attendance": 0.0}
        for segment in segments
    }
    app_wait_slots: dict[str, int] = {app["code"]: 0 for app in applications}
    app_wait_total: dict[str, float] = {app["code"]: 0.0 for app in applications}
    app_downlink_gap: dict[str, float] = {app["code"]: 0.0 for app in applications}
    app_uplink_gap: dict[str, float] = {app["code"]: 0.0 for app in applications}
    bottlenecks: set[str] = set()
    peak_backlog = 0.0
    total_new = 0.0
    total_admitted = 0.0

    for index in range(slot_count):
        occupancy = float(resolved["occupancy_curve"][index])
        slot_start = to_storage(start + timedelta(minutes=slot_minutes * index))  # type: ignore[operator]
        new_demand: dict[tuple[str, str], float] = {}
        segment_inputs: list[tuple[dict[str, Any], float, dict[str, float]]] = []
        slot_new = 0.0
        for segment in segments:
            attendance = float(resolved["attendees"]) * float(segment["share"]) * occupancy
            requests: dict[str, float] = {}
            for app in applications:
                fresh = (
                    attendance
                    * float(app["traffic_share"])
                    * float(app["entitlement_penetration"])
                    * float(app["activation_rate"])
                    * admission_factor
                )
                key = (segment["code"], app["code"])
                new_demand[key] = fresh
                requests[app["code"]] = fresh + carry[key]
                slot_new += fresh
            segment_inputs.append((segment, attendance, requests))

        segment_views: list[dict[str, Any]] = []
        slot_requested = 0.0
        for segment, attendance, requests in segment_inputs:
            allocated = _allocate_segment(segment, applications, requests, attendance)
            segment_views.append(allocated["view"])
            slot_requested += allocated["requested"]

        slot_admitted = sum(view["admitted_sessions"] for view in segment_views)
        scenario_trimmed = slot_admitted > scenario_ceiling and slot_admitted > 0
        if scenario_trimmed:
            _trim_scenario_concurrency(applications, segment_views, scenario_ceiling)
            slot_admitted = sum(view["admitted_sessions"] for view in segment_views)
            bottlenecks.add("concurrency")

        # 以最终受理结果更新跨槽候补积压与统计。
        slot_backlog = 0.0
        slot_downlink_demand = 0.0
        slot_uplink_demand = 0.0
        waited_apps: set[str] = set()
        for view in segment_views:
            segment_code = view["segment_code"]
            slot_backlog += view["waiting_backlog"]
            slot_downlink_demand += view["downlink_demand_mbps"]
            slot_uplink_demand += view["uplink_demand_mbps"]
            for bound in view["bounded_by"]:
                bottlenecks.add(bound)
            for app in applications:
                app_view = next(item for item in view["applications"] if item["app_code"] == app["code"])
                waiting = float(app_view["waiting_sessions"])
                carry[(segment_code, app["code"])] = waiting
                if waiting > 0:
                    waited_apps.add(app["code"])
                    app_wait_total[app["code"]] += waiting
                    app_downlink_gap[app["code"]] += waiting * float(app["downlink_mbps"])
                    app_uplink_gap[app["code"]] += waiting * float(app["uplink_mbps"])
            worst = segment_worst[segment_code]
            worst["downlink_shortfall"] = max(worst["downlink_shortfall"], view["downlink_shortfall_mbps"])
            worst["uplink_shortfall"] = max(worst["uplink_shortfall"], view["uplink_shortfall_mbps"])
            worst["backlog"] = max(worst["backlog"], view["waiting_backlog"])
            worst["attendance"] = max(worst["attendance"], float(view["attendance"]))
        for code in waited_apps:
            app_wait_slots[code] += 1
        if slot_backlog > 0:
            bottlenecks.add("backlog")
        peak_backlog = max(peak_backlog, slot_backlog)
        total_new += slot_new
        total_admitted += slot_admitted

        segment_views.sort(key=lambda item: item["segment_code"])
        slot_view = {
            "index": index,
            "starts_at": slot_start,
            "attendance": round(float(resolved["attendees"]) * occupancy, COUNT_PRECISION),
            "occupancy_factor": round(occupancy, FACTOR_PRECISION),
            "requested_sessions": round(slot_requested, COUNT_PRECISION),
            "new_sessions": round(slot_new, COUNT_PRECISION),
            "admitted_sessions": round(slot_admitted, COUNT_PRECISION),
            "waiting_backlog": round(slot_backlog, COUNT_PRECISION),
            "downlink_demand_mbps": round(slot_downlink_demand, MBPS_PRECISION),
            "uplink_demand_mbps": round(slot_uplink_demand, MBPS_PRECISION),
            "concurrency_ceiling": round(scenario_ceiling, COUNT_PRECISION),
            "concurrency_limited": scenario_trimmed,
            "segments": segment_views,
        }
        slots.append(slot_view)
        # 峰值时段按新增需求（到场峰值）选取，取最早出现的槽位，避免被跨槽候补积压掩盖。
        if peak is None or slot_new > peak["new_sessions"]:
            peak = {
                "slot_index": index,
                "starts_at": slot_start,
                "requested_sessions": round(slot_requested, COUNT_PRECISION),
                "new_sessions": round(slot_new, COUNT_PRECISION),
                "admitted_sessions": round(slot_admitted, COUNT_PRECISION),
                "waiting_backlog": round(slot_backlog, COUNT_PRECISION),
                "downlink_demand_mbps": round(slot_downlink_demand, MBPS_PRECISION),
                "uplink_demand_mbps": round(slot_uplink_demand, MBPS_PRECISION),
            }

    affected = _affected_applications(applications, app_wait_slots, app_wait_total, app_downlink_gap, app_uplink_gap)
    segment_summary = _segment_summary(segments, segment_worst, slots)
    ending_backlog = sum(carry.values())
    actions = _recommended_actions(resolved, segments, applications, slots, segment_worst, scenario_ceiling, app_wait_slots)
    totals = {
        "new_sessions": round(total_new, COUNT_PRECISION),
        "admitted_sessions": round(total_admitted, COUNT_PRECISION),
        "unserved_sessions": round(ending_backlog, COUNT_PRECISION),
        "peak_backlog_sessions": round(peak_backlog, COUNT_PRECISION),
        "admission_ratio": round(total_admitted / total_new, FACTOR_PRECISION) if total_new else 1.0,
    }
    return {
        "model_version": MODEL_VERSION,
        "event": event,
        "attendees": int(resolved["attendees"]),
        "slots": slots,
        "peak": peak,
        "totals": totals,
        "bottlenecks": sorted(bottlenecks),
        "segment_summary": segment_summary,
        "affected_applications": affected,
        "recommended_actions": actions,
    }


def _allocate_segment(
    segment: dict[str, Any],
    applications: list[dict[str, Any]],
    requests: dict[str, float],
    attendance: float,
) -> dict[str, Any]:
    ceiling = float(segment["concurrency_ceiling"])
    capacity = float(segment["capacity_mbps"])
    ordered = sorted(applications, key=lambda app: (-int(app["priority"]), app["code"]))
    remaining_sessions = ceiling
    remaining_downlink = capacity
    remaining_uplink = capacity
    admitted: dict[str, float] = {app["code"]: 0.0 for app in applications}
    for app in ordered:
        code = app["code"]
        allow = requests[code]
        allow = min(allow, remaining_sessions)
        dl_rate = float(app["downlink_mbps"])
        ul_rate = float(app["uplink_mbps"])
        if dl_rate > 0:
            allow = min(allow, remaining_downlink / dl_rate)
        if ul_rate > 0:
            allow = min(allow, remaining_uplink / ul_rate)
        allow = max(0.0, min(allow, requests[code]))
        admitted[code] = allow
        remaining_sessions -= allow
        remaining_downlink -= allow * dl_rate
        remaining_uplink -= allow * ul_rate

    requested_total = sum(requests.values())
    admitted_total = sum(admitted.values())
    downlink_demand = sum(requests[app["code"]] * float(app["downlink_mbps"]) for app in applications)
    uplink_demand = sum(requests[app["code"]] * float(app["uplink_mbps"]) for app in applications)
    admitted_downlink = sum(admitted[app["code"]] * float(app["downlink_mbps"]) for app in applications)
    admitted_uplink = sum(admitted[app["code"]] * float(app["uplink_mbps"]) for app in applications)
    downlink_shortfall = max(0.0, downlink_demand - capacity)
    uplink_shortfall = max(0.0, uplink_demand - capacity)
    bounds: list[str] = []
    if requested_total > ceiling + 1e-6 and admitted_total < requested_total - 1e-6:
        bounds.append("concurrency")
    if downlink_shortfall > 1e-6:
        bounds.append("downlink")
    if uplink_shortfall > 1e-6:
        bounds.append("uplink")
    app_views = []
    for app in ordered:
        code = app["code"]
        app_views.append({
            "app_code": code,
            "priority": int(app["priority"]),
            "requested_sessions": round(requests[code], COUNT_PRECISION),
            "admitted_sessions": round(admitted[code], COUNT_PRECISION),
            "waiting_sessions": round(requests[code] - admitted[code], COUNT_PRECISION),
            "downlink_demand_mbps": round(requests[code] * float(app["downlink_mbps"]), MBPS_PRECISION),
            "uplink_demand_mbps": round(requests[code] * float(app["uplink_mbps"]), MBPS_PRECISION),
        })
    view = {
        "segment_code": segment["code"],
        "attendance": round(attendance, COUNT_PRECISION),
        "requested_sessions": round(requested_total, COUNT_PRECISION),
        "admitted_sessions": round(admitted_total, COUNT_PRECISION),
        "waiting_backlog": round(requested_total - admitted_total, COUNT_PRECISION),
        "concurrency_ceiling": round(ceiling, COUNT_PRECISION),
        "capacity_mbps": round(capacity, MBPS_PRECISION),
        "downlink_demand_mbps": round(downlink_demand, MBPS_PRECISION),
        "uplink_demand_mbps": round(uplink_demand, MBPS_PRECISION),
        "downlink_admitted_mbps": round(admitted_downlink, MBPS_PRECISION),
        "uplink_admitted_mbps": round(admitted_uplink, MBPS_PRECISION),
        "downlink_shortfall_mbps": round(downlink_shortfall, MBPS_PRECISION),
        "uplink_shortfall_mbps": round(uplink_shortfall, MBPS_PRECISION),
        "bounded_by": bounds,
        "applications": app_views,
    }
    return {"view": view, "requested": requested_total}


def _trim_scenario_concurrency(
    applications: list[dict[str, Any]],
    segment_views: list[dict[str, Any]],
    scenario_ceiling: float,
) -> None:
    rates = {app["code"]: (float(app["downlink_mbps"]), float(app["uplink_mbps"])) for app in applications}
    priority = {app["code"]: int(app["priority"]) for app in applications}
    admitted_map: dict[tuple[str, str], float] = {}
    order: list[tuple[int, str, str]] = []
    admitted_total = 0.0
    for view in segment_views:
        segment_code = view["segment_code"]
        for app_view in view["applications"]:
            code = app_view["app_code"]
            amount = float(app_view["admitted_sessions"])
            admitted_map[(segment_code, code)] = amount
            admitted_total += amount
            if amount > 0:
                order.append((priority[code], code, segment_code))
    order.sort(key=lambda item: (item[0], item[1], item[2]))
    excess = admitted_total - scenario_ceiling
    for _, app_code, segment_code in order:
        if excess <= 1e-9:
            break
        key = (segment_code, app_code)
        removable = min(admitted_map[key], excess)
        admitted_map[key] -= removable
        excess -= removable

    for view in segment_views:
        segment_code = view["segment_code"]
        requested_sum = 0.0
        admitted_sum = 0.0
        downlink_demand = 0.0
        uplink_demand = 0.0
        admitted_downlink = 0.0
        admitted_uplink = 0.0
        for app_view in view["applications"]:
            code = app_view["app_code"]
            requested = float(app_view["requested_sessions"])
            admitted = max(0.0, admitted_map[(segment_code, code)])
            dl_rate, ul_rate = rates[code]
            app_view["admitted_sessions"] = round(admitted, COUNT_PRECISION)
            app_view["waiting_sessions"] = round(max(0.0, requested - admitted), COUNT_PRECISION)
            requested_sum += requested
            admitted_sum += admitted
            downlink_demand += requested * dl_rate
            uplink_demand += requested * ul_rate
            admitted_downlink += admitted * dl_rate
            admitted_uplink += admitted * ul_rate
        capacity = float(view["capacity_mbps"])
        view["admitted_sessions"] = round(admitted_sum, COUNT_PRECISION)
        view["waiting_backlog"] = round(max(0.0, requested_sum - admitted_sum), COUNT_PRECISION)
        view["downlink_demand_mbps"] = round(downlink_demand, MBPS_PRECISION)
        view["uplink_demand_mbps"] = round(uplink_demand, MBPS_PRECISION)
        view["downlink_admitted_mbps"] = round(admitted_downlink, MBPS_PRECISION)
        view["uplink_admitted_mbps"] = round(admitted_uplink, MBPS_PRECISION)
        view["downlink_shortfall_mbps"] = round(max(0.0, downlink_demand - capacity), MBPS_PRECISION)
        view["uplink_shortfall_mbps"] = round(max(0.0, uplink_demand - capacity), MBPS_PRECISION)
        if "concurrency" not in view["bounded_by"]:
            view["bounded_by"] = sorted(set(view["bounded_by"]) | {"concurrency"})


def _affected_applications(
    applications: list[dict[str, Any]],
    wait_slots: dict[str, int],
    wait_total: dict[str, float],
    downlink_gap: dict[str, float],
    uplink_gap: dict[str, float],
) -> list[dict[str, Any]]:
    affected = []
    for app in applications:
        code = app["code"]
        if wait_total[code] <= 1e-6:
            continue
        affected.append({
            "app_code": code,
            "priority": int(app["priority"]),
            "waiting_slot_count": int(wait_slots[code]),
            "waiting_sessions": round(wait_total[code], COUNT_PRECISION),
            "downlink_gap_mbps_slots": round(downlink_gap[code], MBPS_PRECISION),
            "uplink_gap_mbps_slots": round(uplink_gap[code], MBPS_PRECISION),
        })
    affected.sort(key=lambda item: (-item["waiting_sessions"], item["app_code"]))
    return affected


def _segment_summary(
    segments: list[dict[str, Any]],
    worst: dict[str, dict[str, float]],
    slots: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    summary = []
    for segment in sorted(segments, key=lambda item: item["code"]):
        code = segment["code"]
        peak_slot = max(
            slots,
            key=lambda slot: next(item["requested_sessions"] for item in slot["segments"] if item["segment_code"] == code),
        )
        segment_slot = next(item for item in peak_slot["segments"] if item["segment_code"] == code)
        bounds = sorted({
            bound
            for slot in slots
            for item in slot["segments"]
            if item["segment_code"] == code
            for bound in item["bounded_by"]
        })
        entry = worst[code]
        summary.append({
            "segment_code": code,
            "peak_slot_index": int(peak_slot["index"]),
            "peak_slot_starts_at": peak_slot["starts_at"],
            "peak_attendance": round(entry["attendance"], COUNT_PRECISION),
            "peak_requested_sessions": segment_slot["requested_sessions"],
            "max_downlink_shortfall_mbps": round(entry["downlink_shortfall"], MBPS_PRECISION),
            "max_uplink_shortfall_mbps": round(entry["uplink_shortfall"], MBPS_PRECISION),
            "peak_waiting_backlog": round(entry["backlog"], COUNT_PRECISION),
            "capacity_mbps": segment["capacity_mbps"],
            "concurrency_ceiling": segment["concurrency_ceiling"],
            "bounded_by": bounds,
        })
    return summary


def _recommended_actions(
    resolved: dict[str, Any],
    segments: list[dict[str, Any]],
    applications: list[dict[str, Any]],
    slots: list[dict[str, Any]],
    worst: dict[str, dict[str, float]],
    scenario_ceiling: float,
    wait_slots: dict[str, int],
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    base_cap = float(resolved["max_concurrent_sessions"])
    peak_concurrency = max((slot["requested_sessions"] for slot in slots), default=0.0)
    if any(slot["concurrency_limited"] for slot in slots) or peak_concurrency > scenario_ceiling + 1e-6:
        ratio = peak_concurrency / max(base_cap, 1e-6)
        actions.append({
            "type": "raise_concurrency",
            "severity": _severity(peak_concurrency - scenario_ceiling, peak_concurrency),
            "segment_code": None,
            "message": "峰值并发超过场景上限，建议提高并发会话上限或提前扩容信令处理能力",
            "evidence": {
                "peak_requested_sessions": round(peak_concurrency, COUNT_PRECISION),
                "concurrency_ceiling": round(scenario_ceiling, COUNT_PRECISION),
                "suggested_concurrency_scale": _scale_hint(ratio),
            },
        })
    for segment in sorted(segments, key=lambda item: item["code"]):
        code = segment["code"]
        entry = worst[code]
        if entry["downlink_shortfall"] > 1e-6:
            ratio = (segment["capacity_mbps"] + entry["downlink_shortfall"]) / max(segment["capacity_mbps"], 1e-6)
            actions.append({
                "type": "expand_downlink",
                "severity": _severity(entry["downlink_shortfall"], segment["capacity_mbps"]),
                "segment_code": code,
                "message": f"区段 {code} 峰值下行容量存在缺口，建议扩容下行载波或临时增补容量",
                "evidence": {
                    "max_shortfall_mbps": round(entry["downlink_shortfall"], MBPS_PRECISION),
                    "capacity_mbps": segment["capacity_mbps"],
                    "suggested_capacity_scale": _scale_hint(ratio),
                },
            })
        if entry["uplink_shortfall"] > 1e-6:
            ratio = (segment["capacity_mbps"] + entry["uplink_shortfall"]) / max(segment["capacity_mbps"], 1e-6)
            actions.append({
                "type": "expand_uplink",
                "severity": _severity(entry["uplink_shortfall"], segment["capacity_mbps"]),
                "segment_code": code,
                "message": f"区段 {code} 峰值上行容量存在缺口，建议扩容上行能力（直播/视频通话场景）",
                "evidence": {
                    "max_shortfall_mbps": round(entry["uplink_shortfall"], MBPS_PRECISION),
                    "capacity_mbps": segment["capacity_mbps"],
                    "suggested_capacity_scale": _scale_hint(ratio),
                },
            })
    peak_backlog = max((slot["waiting_backlog"] for slot in slots), default=0.0)
    if peak_backlog > 1e-6:
        crowded = next(slot for slot in slots if slot["waiting_backlog"] == peak_backlog)
        factor = crowded["admitted_sessions"] / crowded["requested_sessions"] if crowded["requested_sessions"] else 1.0
        throttled = [app["code"] for app in sorted(applications, key=lambda item: (item["priority"], item["code"])) if wait_slots[app["code"]] > 0][:3]
        actions.append({
            "type": "admission_control",
            "severity": _severity(peak_backlog, crowded["requested_sessions"]),
            "segment_code": None,
            "message": "候补积压在峰值前后无法出清，建议对低优先级应用限流并平滑开票后请求",
            "evidence": {
                "peak_backlog_sessions": round(peak_backlog, COUNT_PRECISION),
                "suggested_admission_factor": round(max(0.0, min(1.0, factor)), FACTOR_PRECISION),
                "candidate_app_codes": throttled,
            },
        })
        if throttled:
            actions.append({
                "type": "reprioritize",
                "severity": "minor",
                "segment_code": None,
                "message": "建议上调关键应用优先级，确保直播/视频通话在容量受限时先于低优先级应用受理",
                "evidence": {"candidate_app_codes": throttled},
            })
    order = {"critical": 0, "major": 1, "minor": 2}
    actions.sort(key=lambda item: (order[item["severity"]], item["type"], item["segment_code"] or ""))
    return actions


def _bounded(value: Any, label: str, *, minimum: float, maximum: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not minimum <= float(value) <= maximum:
        raise ValidationError(f"{label}必须在 {minimum} 到 {maximum} 之间")
    return float(value)


def _severity(gap: float, base: float) -> str:
    if base <= 0:
        return "critical" if gap > 0 else "minor"
    ratio = gap / base
    if ratio >= 0.2:
        return "critical"
    if ratio >= 0.05:
        return "major"
    return "minor"


def _scale_hint(ratio: float) -> float:
    """将所需倍数向上取整到 0.05，给出确定性扩容建议。"""

    return round(math.ceil(max(1.0, ratio) * 20) / 20, 2)
