from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class PeakCurvePoint(BaseModel):
    time: str = Field(pattern=r"^\d{2}:\d{2}$")
    factor: float = Field(ge=0, le=1)


class PeakCurveSpec(BaseModel):
    preset: Literal["concert", "venue", "transit"] | None = None
    points: list[PeakCurvePoint] | None = None

    @model_validator(mode="after")
    def require_one(self) -> "PeakCurveSpec":
        if self.preset is None and not self.points:
            raise ValueError("高峰分布必须选择模板或提供人工关键点")
        if self.points:
            labels = [point.time for point in self.points]
            if len(labels) != len(set(labels)):
                raise ValueError("高峰关键点时间不能重复")
            if "00:00" not in labels or "24:00" not in labels:
                raise ValueError("人工高峰曲线必须包含 00:00 与 24:00 两个端点")
        return self


class ScenarioWindow(BaseModel):
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    slot_minutes: Literal[5, 10, 15, 20, 30, 60] = 30


class SegmentShare(BaseModel):
    segment_code: str = Field(min_length=1, max_length=64)
    share: float = Field(ge=0, le=1)


class AppMixItem(BaseModel):
    app_code: str = Field(min_length=2, max_length=80)
    share: float = Field(ge=0, le=1)
    entitlement_rate: float = Field(ge=0, le=1)
    priority: int | None = Field(default=None, ge=0, le=100)


class CapacityMeasures(BaseModel):
    capacity_expansion_mbps: float = Field(default=0, ge=0, le=10_000_000)
    throttle_rate: float = Field(default=0, ge=0, le=0.9)
    priority_mode: Literal["default", "protect_high"] = "default"


class CapacityPlanCreate(BaseModel):
    scenario_code: str = Field(min_length=2, max_length=64)
    code: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=2, max_length=160)
    actor: str = Field(min_length=1, max_length=120)


class PlanVersionCreate(BaseModel):
    label: str = Field(default="", max_length=200)
    attendees: int = Field(ge=1, le=10_000_000)
    window: ScenarioWindow
    peak_curve: PeakCurveSpec
    segment_codes: list[str] = Field(min_length=1, max_length=500)
    segment_shares: list[SegmentShare] = Field(min_length=1, max_length=500)
    app_codes: list[str] = Field(min_length=1, max_length=200)
    app_mix: list[AppMixItem] = Field(min_length=1, max_length=200)
    backlog_carry_ratio: float = Field(default=0.8, ge=0, le=1)
    measures: CapacityMeasures = Field(default_factory=CapacityMeasures)
    actor: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def validate_matching(self) -> "PlanVersionCreate":
        if len(set(self.segment_codes)) != len(self.segment_codes):
            raise ValueError("区段不能重复")
        if len(set(self.app_codes)) != len(self.app_codes):
            raise ValueError("应用不能重复")
        share_codes = [item.segment_code for item in self.segment_shares]
        if sorted(share_codes) != sorted(self.segment_codes):
            raise ValueError("区段客流占比必须覆盖且仅覆盖所选区段")
        total = round(sum(item.share for item in self.segment_shares), 6)
        if abs(total - 1.0) > 0.0001:
            raise ValueError("区段客流占比之和必须等于 1")
        mix_codes = [item.app_code for item in self.app_mix]
        if sorted(mix_codes) != sorted(self.app_codes):
            raise ValueError("应用构成必须覆盖且仅覆盖所选应用")
        if len(set(mix_codes)) != len(mix_codes):
            raise ValueError("应用构成项不能重复")
        total = round(sum(item.share for item in self.app_mix), 6)
        if abs(total - 1.0) > 0.0001:
            raise ValueError("应用构成占比之和必须等于 1")
        return self


class PlanRecompute(BaseModel):
    actor: str = Field(min_length=1, max_length=120)


class PlanApprove(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=500)


class ScenarioEvaluate(BaseModel):
    """一次性试算：提交确定假设，直接返回峰值、缺口、受影响应用与建议动作。"""

    scenario_code: str = Field(min_length=2, max_length=64)
    attendees: int = Field(ge=1, le=10_000_000)
    window: ScenarioWindow
    peak_curve: PeakCurveSpec
    segment_codes: list[str] = Field(default_factory=list, max_length=500)
    segment_shares: list[SegmentShare] = Field(default_factory=list, max_length=500)
    app_codes: list[str] = Field(min_length=1, max_length=200)
    app_mix: list[AppMixItem] = Field(min_length=1, max_length=200)
    backlog_carry_ratio: float = Field(default=0.8, ge=0, le=1)
    measures: CapacityMeasures = Field(default_factory=CapacityMeasures)

    @model_validator(mode="after")
    def validate_matching(self) -> "ScenarioEvaluate":
        if not self.segment_codes:
            raise ValueError("至少选择一个场景区段")
        if len(set(self.segment_codes)) != len(self.segment_codes):
            raise ValueError("区段不能重复")
        if len(set(self.app_codes)) != len(self.app_codes):
            raise ValueError("应用不能重复")
        share_codes = [item.segment_code for item in self.segment_shares]
        if sorted(share_codes) != sorted(self.segment_codes):
            raise ValueError("区段客流占比必须覆盖且仅覆盖所选区段")
        if abs(round(sum(item.share for item in self.segment_shares), 6) - 1.0) > 0.0001:
            raise ValueError("区段客流占比之和必须等于 1")
        mix_codes = [item.app_code for item in self.app_mix]
        if sorted(mix_codes) != sorted(self.app_codes):
            raise ValueError("应用构成必须覆盖且仅覆盖所选应用")
        if len(set(mix_codes)) != len(mix_codes):
            raise ValueError("应用构成项不能重复")
        if abs(round(sum(item.share for item in self.app_mix), 6) - 1.0) > 0.0001:
            raise ValueError("应用构成占比之和必须等于 1")
        return self
