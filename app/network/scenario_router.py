from __future__ import annotations

from fastapi import APIRouter, Query

from app.network.scenario_schemas import (
    CapacityPlanCreate,
    PlanApprove,
    PlanRecompute,
    PlanVersionCreate,
    ScenarioEvaluate,
)
from app.network.scenario_service import CapacityScenarioService

router = APIRouter(prefix="/api/network/capacity", tags=["容量情景模型"])


def service() -> CapacityScenarioService:
    return CapacityScenarioService()


@router.post("/scenarios/evaluate")
def evaluate_scenario(payload: ScenarioEvaluate):
    """提交一组确定假设，返回峰值时段、缺口、受影响应用与建议动作（不落库）。"""
    return service().evaluate(payload.model_dump())


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
def create_version(plan_id: int, payload: PlanVersionCreate):
    return service().create_version(plan_id, payload.model_dump())


@router.get("/plans/{plan_id}/versions")
def list_versions(plan_id: int):
    return service().list_versions(plan_id)


@router.get("/plans/{plan_id}/compare")
def compare_versions(
    plan_id: int,
    left: int = Query(gt=0, description="左侧版本 ID"),
    right: int = Query(gt=0, description="右侧版本 ID"),
):
    return service().compare_versions(plan_id, left, right)


@router.get("/versions/{version_id}")
def version_detail(version_id: int):
    return service().version_detail(version_id)


@router.post("/versions/{version_id}/recompute")
def recompute_version(version_id: int, payload: PlanRecompute):
    """用冻结输入重算；结果摘要必须保持一致。"""
    return service().recompute_version(version_id, payload.actor)


@router.post("/versions/{version_id}/approve")
def approve_version(version_id: int, payload: PlanApprove):
    """审批通过后冻结输入与计算摘要。"""
    return service().approve_version(version_id, payload.actor, payload.note)


@router.post("/versions/{version_id}/archive")
def archive_version(version_id: int, payload: PlanApprove):
    return service().archive_version(version_id, payload.actor)
