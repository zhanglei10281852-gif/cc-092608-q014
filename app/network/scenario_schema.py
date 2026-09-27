from __future__ import annotations

import sqlite3

CAPACITY_SCENARIO_SCHEMA = r'''
CREATE TABLE IF NOT EXISTS capacity_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id INTEGER NOT NULL REFERENCES network_scenarios(id),
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    current_version_id INTEGER,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(scenario_id, code)
);
CREATE TABLE IF NOT EXISTS capacity_plan_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL REFERENCES capacity_plans(id) ON DELETE CASCADE,
    version_no INTEGER NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','approved','archived')),
    assumptions_json TEXT NOT NULL,
    assumptions_digest TEXT NOT NULL,
    result_json TEXT,
    result_digest TEXT,
    peak_slot TEXT,
    peak_demand_mbps REAL,
    max_shortfall_mbps REAL,
    max_waiting INTEGER,
    created_by TEXT NOT NULL,
    computed_at TEXT,
    approved_by TEXT,
    approved_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(plan_id, version_no)
);
CREATE INDEX IF NOT EXISTS idx_plan_versions_state ON capacity_plan_versions(plan_id,state,version_no);
CREATE TABLE IF NOT EXISTS capacity_plan_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL REFERENCES capacity_plans(id) ON DELETE CASCADE,
    version_id INTEGER,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plan_events ON capacity_plan_events(plan_id,id);
'''


def ensure_capacity_scenario_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(CAPACITY_SCENARIO_SCHEMA)
