from __future__ import annotations

from fastapi import APIRouter

from app.network.capacity_schemas import (
    CapacityCompareRequest,
    CapacityEvaluateRequest,
    CapacityPlanCreate,
    CapacityReviewAction,
    CapacityVersionCreate,
)
from app.network.capacity_service import CapacityPlanService

router = APIRouter(prefix="/api/network/capacity", tags=["容量情景保障预案"])


def service() -> CapacityPlanService:
    return CapacityPlanService()


@router.post("/evaluate")
def evaluate_assumptions(payload: CapacityEvaluateRequest):
    """提交一组确定假设，返回峰值时段、缺口、受影响应用与建议动作（不落库）。"""

    return service().evaluate_assumptions(payload.assumptions.model_dump())


@router.post("/plans", status_code=201)
def create_plan(payload: CapacityPlanCreate):
    return service().create_plan(payload.model_dump())


@router.get("/plans")
def list_plans(scenario_code: str | None = None):
    return {"items": service().list_plans(scenario_code)}


@router.get("/plans/{plan_id}")
def plan_detail(plan_id: int):
    return service().plan_detail(plan_id)


@router.post("/plans/{plan_id}/versions", status_code=201)
def create_version(plan_id: int, payload: CapacityVersionCreate):
    return service().create_version(plan_id, payload.model_dump(exclude_none=True))


@router.get("/plans/{plan_id}/versions")
def list_versions(plan_id: int):
    return {"items": service().list_versions(plan_id)}


@router.post("/plans/{plan_id}/compare")
def compare_versions(plan_id: int, payload: CapacityCompareRequest):
    return service().compare_versions(plan_id, payload.version_nos)


@router.get("/versions/{version_id}")
def version_detail(version_id: int):
    return service().version_detail(version_id)


@router.post("/versions/{version_id}/submit")
def submit_version(version_id: int, payload: CapacityReviewAction):
    return service().submit_version(version_id, payload.actor, payload.note)


@router.post("/versions/{version_id}/approve")
def approve_version(version_id: int, payload: CapacityReviewAction):
    return service().approve_version(version_id, payload.actor, payload.note)


@router.post("/versions/{version_id}/reject")
def reject_version(version_id: int, payload: CapacityReviewAction):
    return service().reject_version(version_id, payload.actor, payload.note)


@router.post("/versions/{version_id}/recalculate")
def recalculate_version(version_id: int):
    return service().recalculate_version(version_id)
