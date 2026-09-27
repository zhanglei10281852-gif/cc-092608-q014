"""容量情景计算引擎。

引擎只消费归一化后的假设快照，不访问数据库、不读取时钟、不产生随机数，
因此同一假设重复计算必然得到完全一致的需求曲线与瓶颈结论。
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.errors import ValidationError

ENGINE_VERSION = "1.0"

# 历史高峰分布模板：以本地时刻关键点描述相对活跃系数（0~1），
# 代表观众在场且可能发起加速需求的比例，线性插值到每个时间槽。
PEAK_PRESETS: dict[str, list[tuple[str, float]]] = {
    # 演唱会：午后稀疏，入场前后出现尖峰，演出期间回落，散场小幅回升
    "concert": [
        ("00:00", 0.00), ("12:00", 0.05), ("16:00", 0.20), ("17:30", 0.55),
        ("18:30", 0.95), ("19:30", 1.00), ("20:00", 0.45), ("21:30", 0.30),
        ("22:30", 0.70), ("23:30", 0.25), ("24:00", 0.05),
    ],
    # 体育场馆：白天到晚间持续较高，开赛前与中场双峰
    "venue": [
        ("00:00", 0.00), ("10:00", 0.15), ("13:00", 0.45), ("15:00", 0.60),
        ("18:30", 1.00), ("19:00", 0.55), ("20:30", 0.80), ("22:00", 0.40),
        ("23:00", 0.15), ("24:00", 0.05),
    ],
    # 轨道交通：通勤早晚高峰
    "transit": [
        ("00:00", 0.02), ("06:00", 0.25), ("08:00", 1.00), ("10:00", 0.45),
        ("12:00", 0.55), ("17:30", 0.95), ("19:30", 0.50), ("22:00", 0.15),
        ("24:00", 0.02),
    ],
}

_SLOT_MINUTES_CHOICES = (5, 10, 15, 20, 30, 60)


def resolve_peak_profile(spec: dict[str, Any] | list[Any] | None, slot_minutes: int) -> list[dict[str, float | int]]:
    """把高峰曲线（模板或人工关键点）展开为每个时间槽一个系数。"""
    slot_count = 24 * 60 // slot_minutes

    def expand(keyframes: list[tuple[str, float]]) -> list[float]:
        if len(keyframes) < 2:
            raise ValidationError("高峰曲线至少需要两个关键点")
        points = [(_minute_of_day(label), factor) for label, factor in keyframes]
        points.sort(key=lambda item: item[0])
        if points[0][0] != 0 or points[-1][0] != 24 * 60:
            raise ValidationError("高峰曲线必须从 00:00 开始并以 24:00 结束")
        factors: list[float] = []
        cursor = 0
        for slot in range(slot_count):
            minute = slot * slot_minutes
            while points[cursor + 1][0] < minute:
                cursor += 1
            start_minute, start_factor = points[cursor]
            end_minute, end_factor = points[cursor + 1]
            if end_minute == start_minute:
                value = end_factor
            else:
                ratio = (minute - start_minute) / (end_minute - start_minute)
                value = start_factor + (end_factor - start_factor) * ratio
            factors.append(round(min(1.0, max(0.0, value)), 6))
        return factors

    if isinstance(spec, dict):
        preset = spec.get("preset")
        if preset not in PEAK_PRESETS:
            raise ValidationError("未知的高峰分布模板")
        keyframes = PEAK_PRESETS[preset]
    elif isinstance(spec, list) and spec and all(isinstance(item, dict) and "factor" in item and "slot" in item for item in spec):
        # 已经是展开形态（重复计算冻结快照时走这里）
        if len(spec) != slot_count or any(int(item["slot"]) != index for index, item in enumerate(spec)):
            raise ValidationError("展开后的高峰曲线槽位不完整")
        return [{"slot": int(item["slot"]), "factor": round(float(item["factor"]), 6)} for item in spec]
    elif isinstance(spec, list):
        try:
            keyframes = [(str(item["time"]), float(item["factor"])) for item in spec]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError("高峰曲线关键点格式不正确") from exc
    else:
        raise ValidationError("必须提供高峰分布模板或人工关键点")
    factors = expand(keyframes)
    if not any(value > 0 for value in factors):
        raise ValidationError("高峰曲线至少要有一个正系数")
    return [{"slot": slot, "factor": factors[slot]} for slot in range(slot_count)]


def validate_snapshot(assumptions: dict[str, Any]) -> None:
    """校验引擎消费的归一化假设快照。"""
    window = assumptions.get("window")
    if not isinstance(window, dict):
        raise ValidationError("缺少时间窗 window")
    slot_minutes = window.get("slot_minutes")
    if slot_minutes not in _SLOT_MINUTES_CHOICES:
        raise ValidationError("时间槽粒度必须是 5/10/15/20/30/60 分钟之一")
    try:
        date.fromisoformat(str(window.get("date")))
    except (TypeError, ValueError) as exc:
        raise ValidationError("情景日期格式不正确，应为 YYYY-MM-DD") from exc
    slot_count = 24 * 60 // int(slot_minutes)

    attendees = assumptions.get("attendees")
    if not isinstance(attendees, int) or not 1 <= attendees <= 10_000_000:
        raise ValidationError("观众规模必须是 1 到 10000000 之间的整数")

    profile = assumptions.get("peak_profile")
    if not isinstance(profile, list) or len(profile) != slot_count:
        raise ValidationError("高峰分布必须覆盖全部时间槽")
    if any(not isinstance(item, dict) or item.get("slot") != index for index, item in enumerate(profile)):
        raise ValidationError("高峰分布槽位序号不连续")
    if any(not isinstance(item.get("factor"), (int, float)) or not 0 <= float(item["factor"]) <= 1 for item in profile):
        raise ValidationError("高峰系数必须在 0 到 1 之间")
    if not any(float(item["factor"]) > 0 for item in profile):
        raise ValidationError("高峰分布至少要有一个正系数")

    scenario = assumptions.get("scenario")
    if not isinstance(scenario, dict):
        raise ValidationError("缺少场景快照")
    if not isinstance(scenario.get("max_concurrent_sessions"), int) or scenario["max_concurrent_sessions"] <= 0:
        raise ValidationError("并发上限必须为正整数")
    if not isinstance(scenario.get("capacity_mbps"), (int, float)) or scenario["capacity_mbps"] <= 0:
        raise ValidationError("场景容量必须为正数")
    try:
        ZoneInfo(str(scenario.get("timezone", "UTC")))
    except ZoneInfoNotFoundError as exc:
        raise ValidationError("场景时区不存在") from exc

    segments = assumptions.get("segments")
    apps = assumptions.get("applications")
    if not isinstance(segments, list) or not segments:
        raise ValidationError("至少需要一个场景区段")
    if not isinstance(apps, list) or not apps:
        raise ValidationError("至少需要一个应用画像")
    for item in segments:
        if not isinstance(item.get("capacity_mbps"), (int, float)) or float(item["capacity_mbps"]) <= 0:
            raise ValidationError("区段容量必须为正数")
    for item in apps:
        for key in ("min_downlink_mbps", "min_uplink_mbps"):
            if not isinstance(item.get(key), (int, float)) or float(item[key]) < 0:
                raise ValidationError("应用速率画像不能为负")

    segment_shares = assumptions.get("segment_shares")
    if not isinstance(segment_shares, list) or len(segment_shares) != len(segments):
        raise ValidationError("每个区段都必须给出客流占比")
    segment_codes = {str(item["code"]) for item in segments}
    if {str(item["segment_code"]) for item in segment_shares} != segment_codes:
        raise ValidationError("区段占比与区段清单不一致")
    _validate_shares(segment_shares, "区段客流占比")

    mix = assumptions.get("app_mix")
    if not isinstance(mix, list) or not mix:
        raise ValidationError("至少需要一个应用构成项")
    app_codes = {str(item["app_code"]) for item in apps}
    if {str(item["app_code"]) for item in mix} != app_codes:
        raise ValidationError("应用构成必须覆盖全部所选应用")
    _validate_shares(mix, "应用构成占比")
    for item in mix:
        rate = item.get("entitlement_rate")
        if not isinstance(rate, (int, float)) or not 0 <= float(rate) <= 1:
            raise ValidationError("权益渗透率必须在 0 到 1 之间")
        priority = item.get("priority")
        if not isinstance(priority, int) or not 0 <= priority <= 100:
            raise ValidationError("应用优先级必须在 0 到 100 之间")

    carry = assumptions.get("backlog_carry_ratio", 0.8)
    if not isinstance(carry, (int, float)) or not 0 <= float(carry) <= 1:
        raise ValidationError("候补积压结转比例必须在 0 到 1 之间")

    measures = assumptions.get("measures") or {}
    expansion = measures.get("capacity_expansion_mbps", 0)
    throttle = measures.get("throttle_rate", 0.0)
    mode = measures.get("priority_mode", "default")
    if not isinstance(expansion, (int, float)) or float(expansion) < 0:
        raise ValidationError("扩容容量不能为负")
    if not isinstance(throttle, (int, float)) or not 0 <= float(throttle) <= 0.9:
        raise ValidationError("限流比例必须在 0 到 0.9 之间")
    if mode not in {"default", "protect_high"}:
        raise ValidationError("优先级模式只能是 default 或 protect_high")


def evaluate(assumptions: dict[str, Any]) -> dict[str, Any]:
    """按归一化假设计算时间槽需求曲线、瓶颈与建议动作。"""
    validate_snapshot(assumptions)
    scenario = assumptions["scenario"]
    window = assumptions["window"]
    day = str(window["date"])
    timezone_name = str(scenario.get("timezone", "UTC"))
    slot_minutes = int(window["slot_minutes"])
    slot_count = 24 * 60 // slot_minutes
    attendees = int(assumptions["attendees"])
    measures = assumptions.get("measures") or {}
    expansion = float(measures.get("capacity_expansion_mbps", 0))
    throttle = float(measures.get("throttle_rate", 0.0))
    priority_mode = str(measures.get("priority_mode", "default"))
    carry_ratio = float(assumptions.get("backlog_carry_ratio", 0.8))

    # 扩容假设同时作用于每个区段边界与场景整体边界。
    segments = [
        {
            "code": str(item["code"]),
            "name": str(item.get("name", item["code"])),
            "capacity_mbps": float(item["capacity_mbps"]) + expansion,
            "share": float(_share(assumptions["segment_shares"], item["code"], "segment_code")),
        }
        for item in assumptions["segments"]
    ]
    applications = {
        str(item["app_code"]): {
            "app_code": str(item["app_code"]),
            "name": str(item.get("name", item["app_code"])),
            "category": str(item.get("category", "")),
            "rate_dl": round(float(item["min_downlink_mbps"]) * (1.0 - throttle), 6),
            "rate_ul": round(float(item["min_uplink_mbps"]) * (1.0 - throttle), 6),
        }
        for item in assumptions["applications"]
    }
    mix = {
        str(item["app_code"]): {
            "share": float(item["share"]),
            "entitlement_rate": float(item["entitlement_rate"]),
            "priority": int(item["priority"]),
        }
        for item in assumptions["app_mix"]
    }

    concurrency_limit = int(scenario["max_concurrent_sessions"])
    scenario_capacity = float(scenario["capacity_mbps"]) + expansion
    backlog: dict[tuple[str, str], int] = {}
    curve: list[dict[str, Any]] = []
    app_stats = {code: {"new_demand": 0, "waiting": 0, "peak_waiting": 0, "worst_slot": None} for code in applications}
    max_dl_gap = 0.0
    max_ul_gap = 0.0
    max_waiting = 0
    max_backlog = 0

    for slot in range(slot_count):
        factor = float(assumptions["peak_profile"][slot]["factor"])
        rows: list[dict[str, Any]] = []
        active_users = math.floor(attendees * factor)
        for segment in segments:
            segment_users = math.floor(attendees * segment["share"] * factor)
            for app_code, app in applications.items():
                setting = mix[app_code]
                new_demand = math.floor(segment_users * setting["share"] * setting["entitlement_rate"])
                key = (segment["code"], app_code)
                arrivals = new_demand + backlog.get(key, 0)
                app_stats[app_code]["new_demand"] += new_demand
                if arrivals or new_demand:
                    rows.append({
                        "segment_code": segment["code"],
                        "app_code": app_code,
                        "priority": setting["priority"],
                        "arrivals": arrivals,
                        "new_demand": new_demand,
                        "rate_dl": app["rate_dl"],
                        "rate_ul": app["rate_ul"],
                        "admitted": 0,
                    })

        bottlenecks: set[str] = set()
        # 第一阶段：受区段上下行容量约束
        for segment in segments:
            group = [row for row in rows if row["segment_code"] == segment["code"] and row["arrivals"] > 0]
            if not group:
                continue
            admitted, binding = _admit(group, segment["capacity_mbps"], segment["capacity_mbps"], None, priority_mode, "segment")
            for row, value in zip(group, admitted):
                row["admitted"] = value
            bottlenecks.update(binding)

        # 第二阶段：受场景并发上限与上下行总容量约束
        candidates = [row for row in rows if row["admitted"] > 0]
        if candidates:
            admitted, binding = _admit(candidates, scenario_capacity, scenario_capacity, concurrency_limit, priority_mode, "scenario")
            for row, value in zip(candidates, admitted):
                row["admitted"] = value
            bottlenecks.update(binding)

        slot_demand = sum(row["arrivals"] for row in rows)
        slot_admitted = sum(row["admitted"] for row in rows)
        slot_waiting = slot_demand - slot_admitted
        demand_dl = round(sum(row["arrivals"] * row["rate_dl"] for row in rows), 3)
        demand_ul = round(sum(row["arrivals"] * row["rate_ul"] for row in rows), 3)
        admitted_dl = round(sum(row["admitted"] * row["rate_dl"] for row in rows), 3)
        admitted_ul = round(sum(row["admitted"] * row["rate_ul"] for row in rows), 3)
        dl_gap = round(max(0.0, demand_dl - scenario_capacity), 3)
        ul_gap = round(max(0.0, demand_ul - scenario_capacity), 3)
        max_dl_gap = max(max_dl_gap, dl_gap)
        max_ul_gap = max(max_ul_gap, ul_gap)
        max_waiting = max(max_waiting, slot_waiting)
        if slot_waiting > 0:
            bottlenecks.add("waiting_backlog")

        segment_rows: list[dict[str, Any]] = []
        slot_backlog = 0
        for segment in segments:
            group = [row for row in rows if row["segment_code"] == segment["code"]]
            seg_demand = sum(row["arrivals"] for row in group)
            seg_admitted = sum(row["admitted"] for row in group)
            seg_waiting = seg_demand - seg_admitted
            affected = []
            for row in group:
                waiting = row["arrivals"] - row["admitted"]
                if waiting:
                    affected.append({"app_code": row["app_code"], "demand_sessions": row["arrivals"], "admitted_sessions": row["admitted"], "waiting_sessions": waiting})
                stats = app_stats[row["app_code"]]
                stats["waiting"] += waiting
                if waiting > stats["peak_waiting"]:
                    stats["peak_waiting"] = waiting
                    stats["worst_slot"] = slot
                next_backlog = math.floor(waiting * carry_ratio)
                backlog[(segment["code"], row["app_code"])] = next_backlog
                slot_backlog += next_backlog
            if seg_demand:
                segment_rows.append({
                    "segment_code": segment["code"],
                    "demand_sessions": seg_demand,
                    "admitted_sessions": seg_admitted,
                    "waiting_sessions": seg_waiting,
                    "demand_downlink_mbps": round(sum(row["arrivals"] * row["rate_dl"] for row in group), 3),
                    "demand_uplink_mbps": round(sum(row["arrivals"] * row["rate_ul"] for row in group), 3),
                    "capacity_mbps": round(segment["capacity_mbps"], 3),
                    "affected_applications": sorted(affected, key=lambda item: (-item["waiting_sessions"], item["app_code"])),
                })
        max_backlog = max(max_backlog, slot_backlog)

        curve.append({
            "slot": slot,
            "starts_at": _slot_label(day, slot, slot_minutes, timezone_name),
            "active_users": active_users,
            "demand_sessions": slot_demand,
            "admitted_sessions": slot_admitted,
            "waiting_sessions": slot_waiting,
            "backlog_sessions": slot_backlog,
            "demand_downlink_mbps": demand_dl,
            "admitted_downlink_mbps": admitted_dl,
            "demand_uplink_mbps": demand_ul,
            "admitted_uplink_mbps": admitted_ul,
            "capacity_downlink_mbps": round(scenario_capacity, 3),
            "capacity_uplink_mbps": round(scenario_capacity, 3),
            "concurrent_limit": concurrency_limit,
            "downlink_gap_mbps": dl_gap,
            "uplink_gap_mbps": ul_gap,
            "bottlenecks": sorted(bottlenecks),
            "segments": segment_rows,
        })

    peak_slot = max(range(slot_count), key=lambda index: (curve[index]["demand_sessions"], -index))
    affected_applications = []
    for code, stats in app_stats.items():
        if stats["new_demand"] or stats["waiting"]:
            app = applications[code]
            affected_applications.append({
                "app_code": code,
                "name": app["name"],
                "category": app["category"],
                "demand_sessions": stats["new_demand"],
                "waiting_sessions": stats["waiting"],
                "peak_waiting_sessions": stats["peak_waiting"],
                "worst_slot": stats["worst_slot"],
                "worst_slot_starts_at": curve[stats["worst_slot"]]["starts_at"] if stats["worst_slot"] is not None else None,
            })
    affected_applications.sort(key=lambda item: (-item["waiting_sessions"], item["app_code"]))

    summary = {
        "engine_version": ENGINE_VERSION,
        "window": window,
        "attendees": attendees,
        "slot_count": slot_count,
        "slot_minutes": slot_minutes,
        "measures": {"capacity_expansion_mbps": round(expansion, 3), "throttle_rate": round(throttle, 3), "priority_mode": priority_mode},
        "peak": {
            "slot": peak_slot,
            "starts_at": curve[peak_slot]["starts_at"],
            "demand_sessions": curve[peak_slot]["demand_sessions"],
            "admitted_sessions": curve[peak_slot]["admitted_sessions"],
            "waiting_sessions": curve[peak_slot]["waiting_sessions"],
            "demand_downlink_mbps": curve[peak_slot]["demand_downlink_mbps"],
            "demand_uplink_mbps": curve[peak_slot]["demand_uplink_mbps"],
        },
        "max_downlink_gap_mbps": round(max_dl_gap, 3),
        "max_uplink_gap_mbps": round(max_ul_gap, 3),
        "max_waiting_sessions": max_waiting,
        "max_backlog_sessions": max_backlog,
        "bottleneck_slot_count": sum(1 for item in curve if item["bottlenecks"]),
        "concurrent_limit": concurrency_limit,
        "scenario_capacity_mbps": round(scenario_capacity, 3),
        "affected_applications": affected_applications,
        "recommendations": _recommendations(curve, assumptions, measures, concurrency_limit, scenario_capacity, max_dl_gap, max_ul_gap, max_waiting),
    }
    return {"summary": summary, "curve": curve}


def _admit(rows: list[dict[str, Any]], dl_cap: float, ul_cap: float, concurrency_cap: int | None, mode: str, stage: str) -> tuple[list[int], set[str]]:
    """按容量约束返回每行放行会话数与触发性瓶颈。"""
    demand = sum(row["arrivals"] for row in rows)
    if demand == 0:
        return [0] * len(rows), set()
    dl_demand = sum(row["arrivals"] * row["rate_dl"] for row in rows)
    ul_demand = sum(row["arrivals"] * row["rate_ul"] for row in rows)
    ratios = []
    if dl_demand > dl_cap:
        ratios.append(dl_cap / dl_demand if dl_demand else 1.0)
    if ul_demand > ul_cap:
        ratios.append(ul_cap / ul_demand if ul_demand else 1.0)
    if concurrency_cap is not None and demand > concurrency_cap:
        ratios.append(concurrency_cap / demand)
    if not ratios:
        return [row["arrivals"] for row in rows], set()

    prefix = "segment_" if stage == "segment" else ""
    binding: set[str] = set()
    if dl_demand > dl_cap:
        binding.add(prefix + "downlink")
    if ul_demand > ul_cap:
        binding.add(prefix + "uplink")
    if concurrency_cap is not None and demand > concurrency_cap:
        binding.add("concurrency")

    if mode == "protect_high":
        ordered = sorted(rows, key=lambda row: (-row["priority"], row["segment_code"], row["app_code"]))
        remain_dl, remain_ul = dl_cap, ul_cap
        remain_conc = concurrency_cap if concurrency_cap is not None else demand
        grants = {id(row): 0 for row in rows}
        for row in ordered:
            grant = row["arrivals"]
            if row["rate_dl"] > 0:
                grant = min(grant, math.floor(remain_dl / row["rate_dl"]))
            if row["rate_ul"] > 0:
                grant = min(grant, math.floor(remain_ul / row["rate_ul"]))
            if concurrency_cap is not None:
                grant = min(grant, remain_conc)
            grant = max(0, min(grant, row["arrivals"]))
            grants[id(row)] = grant
            remain_dl -= grant * row["rate_dl"]
            remain_ul -= grant * row["rate_ul"]
            remain_conc -= grant
        return [grants[id(row)] for row in rows], binding
    ratio = min(ratios)
    return [math.floor(row["arrivals"] * ratio) for row in rows], binding


def _recommendations(curve: list[dict[str, Any]], assumptions: dict[str, Any], measures: dict[str, Any], concurrency_limit: int, scenario_capacity: float, max_dl_gap: float, max_ul_gap: float, max_waiting: int) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    if max_dl_gap > 0:
        actions.append({
            "action": "expand_capacity",
            "direction": "downlink",
            "target": "scenario",
            "required_extra_mbps": math.ceil(max_dl_gap),
            "current_capacity_mbps": round(scenario_capacity, 3),
            "reason": "峰值时段下行需求超过容量",
        })
    if max_ul_gap > 0:
        actions.append({
            "action": "expand_capacity",
            "direction": "uplink",
            "target": "scenario",
            "required_extra_mbps": math.ceil(max_ul_gap),
            "current_capacity_mbps": round(scenario_capacity, 3),
            "reason": "峰值时段上行需求超过容量",
        })
    peak_demand = max((item["demand_sessions"] for item in curve), default=0)
    if peak_demand > concurrency_limit:
        actions.append({
            "action": "raise_concurrency",
            "target": "scenario",
            "current_limit": concurrency_limit,
            "required_limit": peak_demand,
            "reason": "峰值并发会话超过场景上限",
        })
    throttle = float(measures.get("throttle_rate", 0.0))
    if max_waiting > 0 and throttle < 0.9:
        required_rate = 0.0
        for item in curve:
            if item["waiting_sessions"] > 0 and item["demand_downlink_mbps"] > 0:
                required_rate = max(required_rate, 1.0 - item["capacity_downlink_mbps"] / item["demand_downlink_mbps"])
        suggested = min(0.9, math.ceil(required_rate * 100) / 100)
        if suggested > throttle:
            actions.append({
                "action": "throttle",
                "target": "scenario",
                "current_rate": round(throttle, 2),
                "suggested_rate": suggested,
                "reason": "限流压缩单会话带宽以消化候补积压",
            })
    if max_waiting > 0 and measures.get("priority_mode", "default") == "default":
        sacrificed = sorted(str(item["app_code"]) for item in assumptions["app_mix"] if int(item["priority"]) < 60)
        actions.append({
            "action": "adjust_priority",
            "target": "scenario",
            "suggested_mode": "protect_high",
            "lower_priority_applications": sacrificed,
            "reason": "按优先级保护高价值应用，低优先级应用转入候补",
        })
    return actions


def _validate_shares(items: list[dict[str, Any]], label: str) -> None:
    total = 0.0
    for item in items:
        value = item.get("share")
        if not isinstance(value, (int, float)) or not 0 <= float(value) <= 1:
            raise ValidationError(f"{label}必须在 0 到 1 之间")
        total += float(value)
    if abs(total - 1.0) > 0.0001:
        raise ValidationError(f"{label}之和必须等于 1")


def _share(items: list[dict[str, Any]], code: str, key: str) -> float:
    for item in items:
        if str(item[key]) == code:
            return float(item["share"])
    raise ValidationError(f"缺少占比配置：{code}")


def _minute_of_day(label: str) -> int:
    try:
        hour_text, minute_text = str(label).split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (TypeError, ValueError) as exc:
        raise ValidationError("时间关键点格式应为 HH:MM") from exc
    if not 0 <= hour <= 24 or not 0 <= minute < 60 or (hour == 24 and minute != 0):
        raise ValidationError("时间关键点必须在 00:00 到 24:00 之间")
    return hour * 60 + minute


def _slot_label(day: str, slot: int, slot_minutes: int, timezone_name: str) -> str:
    parsed_day = date.fromisoformat(day)
    minute = slot * slot_minutes
    local = datetime(parsed_day.year, parsed_day.month, parsed_day.day, minute // 60, minute % 60, tzinfo=ZoneInfo(timezone_name))
    return local.isoformat()
