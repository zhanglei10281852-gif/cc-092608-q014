from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class CapacityEventWindow(BaseModel):
    starts_at: str = Field(min_length=4, max_length=40)
    ends_at: str = Field(min_length=4, max_length=40)
    slot_minutes: Literal[5, 10, 15, 20, 30, 60] = 15


class CapacitySegmentAssumption(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    share: float = Field(gt=0, le=1)
    capacity_mbps: float | None = Field(default=None, gt=0, le=10_000_000)


class CapacityApplicationAssumption(BaseModel):
    app_code: str = Field(min_length=2, max_length=80)
    traffic_share: float = Field(gt=0, le=1)
    penetration: float = Field(ge=0, le=1)
    activation_rate: float = Field(default=1.0, ge=0, le=1)
    priority: int | None = Field(default=None, ge=0, le=100)
    downlink_mbps: float | None = Field(default=None, ge=0, le=100_000)
    uplink_mbps: float | None = Field(default=None, ge=0, le=100_000)


class CapacityMeasures(BaseModel):
    capacity_scale: float = Field(default=1.0, ge=0.01, le=10.0)
    concurrency_scale: float = Field(default=1.0, ge=0.01, le=10.0)
    admission_factor: float = Field(default=1.0, ge=0.0, le=1.0)
    segment_capacity_overrides: dict[str, float] = Field(default_factory=dict)


class CapacityAssumptions(BaseModel):
    scenario_code: str = Field(min_length=2, max_length=64)
    event: CapacityEventWindow
    attendees: int = Field(gt=0, le=10_000_000)
    historical_curve: list[float] | None = Field(default=None, max_length=288)
    manual_curve: list[float] | None = Field(default=None, max_length=288)
    historical_weight: float = Field(default=0.5, ge=0, le=1)
    segments: list[CapacitySegmentAssumption] = Field(default_factory=list, max_length=500)
    applications: list[CapacityApplicationAssumption] = Field(min_length=1, max_length=200)
    measures: CapacityMeasures = Field(default_factory=CapacityMeasures)

    @model_validator(mode="after")
    def validate_curve_and_codes(self) -> "CapacityAssumptions":
        if not self.historical_curve and not self.manual_curve:
            raise ValueError("必须提供历史高峰分布或人工到场曲线")
        if self.historical_curve and any(value < 0 for value in self.historical_curve):
            raise ValueError("历史高峰分布不能包含负数")
        if self.manual_curve and any(value < 0 for value in self.manual_curve):
            raise ValueError("人工到场曲线不能包含负数")
        segment_codes = [item.code for item in self.segments]
        if self.segments and len(segment_codes) != len(set(segment_codes)):
            raise ValueError("区段假设不能重复")
        app_codes = [item.app_code for item in self.applications]
        if len(app_codes) != len(set(app_codes)):
            raise ValueError("应用假设不能重复")
        overrides = self.measures.segment_capacity_overrides
        if any(value <= 0 for value in overrides.values()):
            raise ValueError("区段扩容容量必须为正数")
        unknown_overrides = set(overrides) - set(segment_codes)
        if self.segments and unknown_overrides:
            raise ValueError("扩容覆盖只能引用假设中声明的区段")
        return self


class CapacityPlanCreate(BaseModel):
    code: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=2, max_length=160)
    scenario_code: str = Field(min_length=2, max_length=64)
    description: str = Field(default="", max_length=1000)
    actor: str = Field(min_length=1, max_length=120)


class CapacityVersionCreate(BaseModel):
    assumptions: CapacityAssumptions
    actor: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=1000)
    based_on_version_no: int | None = Field(default=None, gt=0)


class CapacityReviewAction(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=1000)


class CapacityEvaluateRequest(BaseModel):
    assumptions: CapacityAssumptions


class CapacityCompareRequest(BaseModel):
    version_nos: list[int] = Field(min_length=2, max_length=10)

    @model_validator(mode="after")
    def unique_versions(self) -> "CapacityCompareRequest":
        if len(self.version_nos) != len(set(self.version_nos)):
            raise ValueError("对比版本不能重复")
        return self
