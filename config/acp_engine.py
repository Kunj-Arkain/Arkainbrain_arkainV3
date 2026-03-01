"""
ARKAINBRAIN — Agent Control Plane (ACP) Engine

Runtime config management with:
  - DB-backed settings (no code edits for knob changes)
  - Named agent profiles (budgets, models, temperatures)
  - Agent roster (enable/disable, tool permissions)
  - Workflow templates (ordered agent graphs per job type)
  - Config versioning + diff + rollback
  - Deterministic resolution: each job stamps resolved_config_json

Usage:
    from config.acp_engine import ACP
    acp = ACP(db)

    # At job start:
    config = acp.resolve_config("slot_pipeline")
    job.resolved_config_json = json.dumps(config)

    # In pipeline:
    if acp.flag("enable_vision_qa"):
        run_vision_qa()

    # Admin:
    acp.set_setting("max_concurrent_jobs", 5, user_id="admin1", reason="scaling up")
"""

from __future__ import annotations

import json
import hashlib
import logging
import os
import uuid
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger("arkainbrain.acp")


# ═══════════════════════════════════════════════════════════
# SQL Schema
# ═══════════════════════════════════════════════════════════

ACP_SCHEMA = """
-- ACP: System settings (key-value with types)
CREATE TABLE IF NOT EXISTS system_settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL DEFAULT '""',
    type TEXT NOT NULL DEFAULT 'string',
    description TEXT DEFAULT '',
    category TEXT DEFAULT 'general',
    requires_stepup BOOLEAN DEFAULT 0,
    restart_required BOOLEAN DEFAULT 0,
    updated_by TEXT DEFAULT '',
    updated_at TEXT DEFAULT (datetime('now'))
);

-- ACP: Named runtime profiles
CREATE TABLE IF NOT EXISTS agent_profiles (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT DEFAULT '',
    profile_json TEXT NOT NULL DEFAULT '{}',
    is_active BOOLEAN DEFAULT 0,
    created_by TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    updated_by TEXT DEFAULT '',
    updated_at TEXT DEFAULT (datetime('now'))
);

-- ACP: Agent roster
CREATE TABLE IF NOT EXISTS agent_definitions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    role TEXT DEFAULT '',
    description TEXT DEFAULT '',
    tools_allowed_json TEXT DEFAULT '[]',
    default_profile_id TEXT DEFAULT '',
    model_override TEXT DEFAULT '',
    max_tokens INTEGER DEFAULT 128000,
    temperature REAL DEFAULT 0.5,
    max_iterations INTEGER DEFAULT 25,
    is_enabled BOOLEAN DEFAULT 1,
    sort_order INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

-- ACP: Workflow templates
CREATE TABLE IF NOT EXISTS workflow_templates (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    job_type TEXT NOT NULL,
    description TEXT DEFAULT '',
    agent_sequence_json TEXT NOT NULL DEFAULT '[]',
    is_active BOOLEAN DEFAULT 0,
    created_by TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    updated_by TEXT DEFAULT '',
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_wt_jobtype ON workflow_templates(job_type);

-- ACP: Config version history
CREATE TABLE IF NOT EXISTS config_versions (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    target_id TEXT NOT NULL,
    version_num INTEGER NOT NULL,
    config_json TEXT NOT NULL,
    change_reason TEXT DEFAULT '',
    created_by TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_cv_scope ON config_versions(scope, target_id);
CREATE INDEX IF NOT EXISTS idx_cv_version ON config_versions(scope, target_id, version_num);
"""

# Jobs table extensions (run as ALTER TABLEs)
JOBS_EXTENSIONS = [
    "ALTER TABLE jobs ADD COLUMN selected_profile_id TEXT DEFAULT ''",
    "ALTER TABLE jobs ADD COLUMN selected_workflow_id TEXT DEFAULT ''",
    "ALTER TABLE jobs ADD COLUMN resolved_config_json TEXT DEFAULT '{}'",
    "ALTER TABLE jobs ADD COLUMN config_version_ref TEXT DEFAULT ''",
]


# ═══════════════════════════════════════════════════════════
# Default Seeds
# ═══════════════════════════════════════════════════════════

DEFAULT_SETTINGS = {
    # Feature flags
    "enable_vision_qa":         {"value": True,  "type": "bool", "cat": "feature",  "desc": "GPT-4 vision quality checks on generated images"},
    "enable_sound_design":      {"value": True,  "type": "bool", "cat": "feature",  "desc": "ElevenLabs audio generation"},
    "enable_patent_scan":       {"value": True,  "type": "bool", "cat": "feature",  "desc": "IP/patent conflict scanning"},
    "enable_web3_output":       {"value": False, "type": "bool", "cat": "feature",  "desc": "Solidity contract generation"},
    "enable_convergence_loop":  {"value": True,  "type": "bool", "cat": "feature",  "desc": "Math↔Design OODA convergence"},
    "enable_adversarial_review":{"value": True,  "type": "bool", "cat": "feature",  "desc": "Adversarial review checkpoints"},
    "enable_mood_boards":       {"value": True,  "type": "bool", "cat": "feature",  "desc": "Art direction mood board stage"},
    "enable_geo_research":      {"value": True,  "type": "bool", "cat": "feature",  "desc": "Geographic market research"},
    "enable_animation":         {"value": True,  "type": "bool", "cat": "feature",  "desc": "Animation spec generation"},
    "enable_hitl_gates":        {"value": True,  "type": "bool", "cat": "feature",  "desc": "Human-in-the-loop review gates"},
    "enable_deep_research":     {"value": True,  "type": "bool", "cat": "feature",  "desc": "Multi-source deep research"},
    "minigame_auto_register":   {"value": True,  "type": "bool", "cat": "feature",  "desc": "Auto-register mini-games in library"},
    "minigame_wallet_bridge":   {"value": True,  "type": "bool", "cat": "feature",  "desc": "Inject wallet bridge into games"},
    "jackpot_enabled":          {"value": True,  "type": "bool", "cat": "feature",  "desc": "Progressive jackpot system"},
    # Capacity
    "max_concurrent_jobs":      {"value": 3,     "type": "int",   "cat": "capacity", "desc": "Max parallel pipeline jobs"},
    "queue_paused":             {"value": False, "type": "bool",  "cat": "capacity", "desc": "Pause job queue (drain mode)"},
    "job_timeout_seconds":      {"value": 3600,  "type": "int",   "cat": "capacity", "desc": "Total job timeout (seconds)"},
    "stage_timeout_seconds":    {"value": 600,   "type": "int",   "cat": "capacity", "desc": "Per-stage timeout (seconds)"},
    # Safety / cost
    "max_cost_per_job":         {"value": 25.0,  "type": "float", "cat": "safety",   "desc": "Cost ceiling per job ($)", "stepup": True},
    "max_cost_per_day":         {"value": 200.0, "type": "float", "cat": "safety",   "desc": "Daily cost ceiling ($)",    "stepup": True},
    "max_cost_per_user_day":    {"value": 50.0,  "type": "float", "cat": "safety",   "desc": "Per-user daily cost ceiling ($)"},
    "max_agent_iterations":     {"value": 25,    "type": "int",   "cat": "safety",   "desc": "Max LLM calls per agent per task"},
    "emergency_stop":           {"value": False, "type": "bool",  "cat": "safety",   "desc": "STOP ALL — kill switch", "stepup": True},
    # Routing
    "default_model_heavy":      {"value": "openai/gpt-4.1",      "type": "string", "cat": "routing", "desc": "Default model for creative agents"},
    "default_model_light":      {"value": "openai/gpt-4.1-mini", "type": "string", "cat": "routing", "desc": "Default model for validation agents"},
    "default_model_vision":     {"value": "gpt-4.1",             "type": "string", "cat": "routing", "desc": "Model for vision QA"},
}

DEFAULT_AGENTS = {
    "lead_producer":      {"role": "Lead Producer — orchestrates pipeline",       "model": "openai/gpt-4.1",      "temp": 0.3, "tier": "premium"},
    "market_analyst":     {"role": "Market Research Analyst",                     "model": "openai/gpt-4.1",      "temp": 0.4, "tier": "heavy"},
    "game_designer":      {"role": "Senior Game Designer — GDD author",           "model": "openai/gpt-4.1",      "temp": 0.6, "tier": "heavy"},
    "mathematician":      {"role": "Slot Mathematician — RTP & paytable",         "model": "openai/gpt-4.1",      "temp": 0.1, "tier": "premium"},
    "art_director":       {"role": "Art Director — visuals & audio",              "model": "openai/gpt-4.1",      "temp": 0.7, "tier": "heavy"},
    "compliance_officer": {"role": "Regulatory Compliance Officer",               "model": "openai/gpt-4.1",      "temp": 0.1, "tier": "premium"},
    "adversarial_reviewer":{"role": "Adversarial Reviewer — quality gates",       "model": "openai/gpt-4.1",      "temp": 0.3, "tier": "premium"},
    "research_synthesizer":{"role": "Research Synthesizer — report writer",       "model": "openai/gpt-4.1",      "temp": 0.5, "tier": "heavy"},
    "audio_engineer":     {"role": "Audio Director & Sound Designer",             "model": "openai/gpt-4.1",      "temp": 0.6, "tier": "heavy"},
    "animation_director": {"role": "Animation Director — motion specs",           "model": "openai/gpt-4.1",      "temp": 0.5, "tier": "heavy"},
    "math_validator":     {"role": "Math Validator — cross-checks RTP",           "model": "openai/gpt-4.1-mini", "temp": 0.1, "tier": "light"},
    "patent_specialist":  {"role": "Patent & IP Specialist",                      "model": "openai/gpt-4.1-mini", "temp": 0.1, "tier": "light"},
    "gdd_proofreader":    {"role": "GDD Proofreader — catches errors",            "model": "openai/gpt-4.1-mini", "temp": 0.1, "tier": "light"},
}

DEFAULT_PROFILES = {
    "default": {
        "desc": "Standard profile — balanced quality and cost",
        "config": {
            "model_heavy": "openai/gpt-4.1",
            "model_light": "openai/gpt-4.1-mini",
            "model_vision": "gpt-4.1",
            "max_tokens": 128000,
            "max_iterations": 25,
            "max_cost_per_job": 25.0,
            "convergence_loops": 3,
        },
    },
    "fast": {
        "desc": "Fast mode — all agents use mini, fewer iterations",
        "config": {
            "model_heavy": "openai/gpt-4.1-mini",
            "model_light": "openai/gpt-4.1-mini",
            "model_vision": "gpt-4.1-mini",
            "max_tokens": 64000,
            "max_iterations": 10,
            "max_cost_per_job": 10.0,
            "convergence_loops": 1,
        },
    },
    "deep": {
        "desc": "Deep mode — maximum quality, higher budget",
        "config": {
            "model_heavy": "openai/gpt-4.1",
            "model_light": "openai/gpt-4.1",
            "model_vision": "gpt-4.1",
            "max_tokens": 200000,
            "max_iterations": 40,
            "max_cost_per_job": 50.0,
            "convergence_loops": 5,
        },
    },
}

DEFAULT_WORKFLOWS = {
    "slot_pipeline": {
        "name": "Slot Pipeline (Full)",
        "desc": "Complete slot game production pipeline",
        "agents": [
            {"agent_id": "lead_producer", "stage": "initialize"},
            {"agent_id": "market_analyst", "stage": "research"},
            {"agent_id": "adversarial_reviewer", "stage": "checkpoint_research"},
            {"agent_id": "game_designer", "stage": "design_and_math"},
            {"agent_id": "mathematician", "stage": "design_and_math"},
            {"agent_id": "math_validator", "stage": "design_and_math"},
            {"agent_id": "gdd_proofreader", "stage": "design_and_math"},
            {"agent_id": "adversarial_reviewer", "stage": "checkpoint_design"},
            {"agent_id": "art_director", "stage": "mood_boards"},
            {"agent_id": "art_director", "stage": "production"},
            {"agent_id": "audio_engineer", "stage": "production"},
            {"agent_id": "animation_director", "stage": "production"},
            {"agent_id": "compliance_officer", "stage": "production"},
            {"agent_id": "patent_specialist", "stage": "production"},
            {"agent_id": "lead_producer", "stage": "assemble_package"},
        ],
    },
    "mini_rmg": {
        "name": "Mini-Game RMG Pipeline",
        "desc": "Automated mini-game generation (crash, dice, etc.)",
        "agents": [
            {"agent_id": "mathematician", "stage": "math_model"},
            {"agent_id": "game_designer", "stage": "game_design"},
            {"agent_id": "lead_producer", "stage": "code_gen"},
        ],
    },
    "novel_game": {
        "name": "Novel Game Generator",
        "desc": "AI-invented novel game mechanics",
        "agents": [
            {"agent_id": "game_designer", "stage": "invention"},
            {"agent_id": "mathematician", "stage": "math_model"},
            {"agent_id": "lead_producer", "stage": "build"},
        ],
    },
}


# ═══════════════════════════════════════════════════════════
# ACP Engine
# ═══════════════════════════════════════════════════════════

class ACP:
    """Agent Control Plane — runtime config management."""

    def __init__(self, db):
        """Initialize with a Flask/sqlite3 database connection."""
        self.db = db
        self._ensure_schema()

    # ── Schema ────────────────────────────────────────────

    def _ensure_schema(self):
        """Create ACP tables if they don't exist."""
        self.db.executescript(ACP_SCHEMA)
        for alter in JOBS_EXTENSIONS:
            try:
                self.db.execute(alter)
            except Exception:
                pass  # Column already exists
        self.db.commit()

    # ── Settings (key-value) ──────────────────────────────

    def get_setting(self, key: str, default: Any = None) -> Any:
        """Get a setting value, parsed by type."""
        row = self.db.execute(
            "SELECT value_json, type FROM system_settings WHERE key=?", (key,)
        ).fetchone()
        if not row:
            # Check DEFAULT_SETTINGS
            d = DEFAULT_SETTINGS.get(key)
            return d["value"] if d else default
        return self._parse_value(row["value_json"], row["type"])

    def set_setting(self, key: str, value: Any, user_id: str = "system",
                    reason: str = "") -> None:
        """Set a setting value with versioning."""
        # Get current for version snapshot
        old = self.db.execute(
            "SELECT value_json FROM system_settings WHERE key=?", (key,)
        ).fetchone()

        value_json = json.dumps(value)
        stype = DEFAULT_SETTINGS.get(key, {}).get("type", "string")
        desc = DEFAULT_SETTINGS.get(key, {}).get("desc", "")
        cat = DEFAULT_SETTINGS.get(key, {}).get("cat", "general")
        stepup = DEFAULT_SETTINGS.get(key, {}).get("stepup", False)

        self.db.execute("""
            INSERT INTO system_settings (key, value_json, type, description, category,
                                          requires_stepup, updated_by, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value_json=excluded.value_json, updated_by=excluded.updated_by,
                updated_at=excluded.updated_at
        """, (key, value_json, stype, desc, cat, stepup, user_id,
              datetime.now().isoformat()))

        # Version
        self._create_version("system", key,
                             {"old": old["value_json"] if old else None, "new": value_json},
                             user_id, reason)
        self.db.commit()
        logger.info(f"ACP: set {key}={value} by {user_id}")

    def get_all_settings(self, category: str = None) -> list:
        """Get all settings, optionally filtered by category."""
        if category:
            rows = self.db.execute(
                "SELECT * FROM system_settings WHERE category=? ORDER BY key",
                (category,)
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT * FROM system_settings ORDER BY category, key"
            ).fetchall()

        result = [dict(r) for r in rows] if rows else []

        # Merge defaults not yet in DB
        existing_keys = {r["key"] for r in result}
        for key, d in DEFAULT_SETTINGS.items():
            if key not in existing_keys:
                if category and d.get("cat") != category:
                    continue
                result.append({
                    "key": key,
                    "value_json": json.dumps(d["value"]),
                    "type": d.get("type", "string"),
                    "description": d.get("desc", ""),
                    "category": d.get("cat", "general"),
                    "requires_stepup": d.get("stepup", False),
                    "updated_by": "default",
                    "updated_at": "",
                })
        return sorted(result, key=lambda r: (r.get("category", ""), r["key"]))

    def flag(self, key: str) -> bool:
        """Shortcut for boolean feature flags."""
        return bool(self.get_setting(key, False))

    # ── Profiles ──────────────────────────────────────────

    def get_profiles(self) -> list:
        rows = self.db.execute("SELECT * FROM agent_profiles ORDER BY name").fetchall()
        return [dict(r) for r in rows] if rows else []

    def get_profile(self, profile_id: str) -> Optional[dict]:
        row = self.db.execute("SELECT * FROM agent_profiles WHERE id=?", (profile_id,)).fetchone()
        return dict(row) if row else None

    def get_active_profile(self) -> Optional[dict]:
        row = self.db.execute("SELECT * FROM agent_profiles WHERE is_active=1").fetchone()
        return dict(row) if row else None

    def create_profile(self, name: str, config: dict, user_id: str = "system",
                       description: str = "") -> str:
        pid = str(uuid.uuid4())[:12]
        self.db.execute("""
            INSERT INTO agent_profiles (id, name, description, profile_json, is_active,
                                         created_by, updated_by)
            VALUES (?, ?, ?, ?, 0, ?, ?)
        """, (pid, name, description, json.dumps(config), user_id, user_id))
        self._create_version("profile", pid, config, user_id, f"Created profile '{name}'")
        self.db.commit()
        return pid

    def update_profile(self, profile_id: str, config: dict, user_id: str = "system",
                       reason: str = "") -> None:
        self.db.execute("""
            UPDATE agent_profiles SET profile_json=?, updated_by=?, updated_at=?
            WHERE id=?
        """, (json.dumps(config), user_id, datetime.now().isoformat(), profile_id))
        self._create_version("profile", profile_id, config, user_id, reason)
        self.db.commit()

    def activate_profile(self, profile_id: str, user_id: str = "system") -> None:
        self.db.execute("UPDATE agent_profiles SET is_active=0")
        self.db.execute("UPDATE agent_profiles SET is_active=1 WHERE id=?", (profile_id,))
        self._audit(user_id, "activate_profile", "profile", profile_id)
        self.db.commit()

    def clone_profile(self, source_id: str, new_name: str, user_id: str = "system") -> str:
        src = self.get_profile(source_id)
        if not src:
            raise ValueError(f"Profile {source_id} not found")
        config = json.loads(src["profile_json"])
        return self.create_profile(new_name, config, user_id,
                                   f"Cloned from '{src['name']}'")

    # ── Agent Definitions ─────────────────────────────────

    def get_agents(self, enabled_only: bool = False) -> list:
        sql = "SELECT * FROM agent_definitions"
        if enabled_only:
            sql += " WHERE is_enabled=1"
        sql += " ORDER BY sort_order, name"
        rows = self.db.execute(sql).fetchall()
        return [dict(r) for r in rows] if rows else []

    def get_agent(self, agent_id: str) -> Optional[dict]:
        row = self.db.execute("SELECT * FROM agent_definitions WHERE id=?", (agent_id,)).fetchone()
        return dict(row) if row else None

    def update_agent(self, agent_id: str, updates: dict, user_id: str = "system") -> None:
        agent = self.get_agent(agent_id)
        if not agent:
            raise ValueError(f"Agent {agent_id} not found")

        allowed = {"role", "description", "model_override", "max_tokens", "temperature",
                    "max_iterations", "is_enabled", "sort_order", "tools_allowed_json"}
        for k, v in updates.items():
            if k in allowed:
                self.db.execute(f"UPDATE agent_definitions SET {k}=?, updated_at=? WHERE id=?",
                                (v, datetime.now().isoformat(), agent_id))

        self._create_version("agent", agent_id, {**agent, **updates}, user_id, "Updated agent")
        self.db.commit()

    def toggle_agent(self, agent_id: str, enabled: bool, user_id: str = "system") -> None:
        self.db.execute("UPDATE agent_definitions SET is_enabled=?, updated_at=? WHERE id=?",
                        (1 if enabled else 0, datetime.now().isoformat(), agent_id))
        self._audit(user_id, "toggle_agent", "agent", agent_id,
                    {"enabled": enabled})
        self.db.commit()

    # ── Workflow Templates ────────────────────────────────

    def get_workflows(self, job_type: str = None) -> list:
        if job_type:
            rows = self.db.execute(
                "SELECT * FROM workflow_templates WHERE job_type=? ORDER BY name",
                (job_type,)
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT * FROM workflow_templates ORDER BY job_type, name"
            ).fetchall()
        return [dict(r) for r in rows] if rows else []

    def get_active_workflow(self, job_type: str) -> Optional[dict]:
        row = self.db.execute(
            "SELECT * FROM workflow_templates WHERE job_type=? AND is_active=1",
            (job_type,)
        ).fetchone()
        return dict(row) if row else None

    def create_workflow(self, name: str, job_type: str, agents: list,
                        user_id: str = "system", description: str = "") -> str:
        wid = str(uuid.uuid4())[:12]
        self.db.execute("""
            INSERT INTO workflow_templates (id, name, job_type, description,
                                            agent_sequence_json, is_active, created_by, updated_by)
            VALUES (?, ?, ?, ?, ?, 0, ?, ?)
        """, (wid, name, job_type, description, json.dumps(agents), user_id, user_id))
        self._create_version("workflow", wid, {"name": name, "agents": agents},
                             user_id, f"Created workflow '{name}'")
        self.db.commit()
        return wid

    def update_workflow(self, wf_id: str, agents: list, user_id: str = "system",
                        reason: str = "") -> None:
        self.db.execute("""
            UPDATE workflow_templates SET agent_sequence_json=?, updated_by=?, updated_at=?
            WHERE id=?
        """, (json.dumps(agents), user_id, datetime.now().isoformat(), wf_id))
        self._create_version("workflow", wf_id, {"agents": agents}, user_id, reason)
        self.db.commit()

    def activate_workflow(self, wf_id: str, user_id: str = "system") -> None:
        wf = self.db.execute("SELECT job_type FROM workflow_templates WHERE id=?",
                             (wf_id,)).fetchone()
        if wf:
            self.db.execute(
                "UPDATE workflow_templates SET is_active=0 WHERE job_type=?",
                (wf["job_type"],)
            )
        self.db.execute("UPDATE workflow_templates SET is_active=1 WHERE id=?", (wf_id,))
        self._audit(user_id, "activate_workflow", "workflow", wf_id)
        self.db.commit()

    # ── Config Resolution ─────────────────────────────────

    def resolve_config(self, job_type: str, profile_id: str = None,
                       workflow_id: str = None, overrides: dict = None) -> dict:
        """
        Resolve complete config for a job.

        Resolution order (later overrides earlier):
        1. System settings defaults
        2. Active agent profile
        3. Workflow template
        4. Agent definitions
        5. Job-level overrides
        """
        config = {}

        # 1. System settings
        for row in self.get_all_settings():
            config[f"setting.{row['key']}"] = self._parse_value(
                row["value_json"], row.get("type", "string"))

        # 2. Profile
        profile = None
        if profile_id:
            profile = self.get_profile(profile_id)
        if not profile:
            profile = self.get_active_profile()
        if profile:
            config["profile_id"] = profile["id"]
            config["profile_name"] = profile["name"]
            pj = json.loads(profile["profile_json"]) if isinstance(profile["profile_json"], str) else profile["profile_json"]
            config["profile"] = pj
        else:
            config["profile_id"] = "default"
            config["profile_name"] = "default"
            config["profile"] = DEFAULT_PROFILES["default"]["config"]

        # 3. Workflow
        workflow = None
        if workflow_id:
            wf = self.db.execute("SELECT * FROM workflow_templates WHERE id=?",
                                 (workflow_id,)).fetchone()
            if wf:
                workflow = dict(wf)
        if not workflow:
            workflow_row = self.get_active_workflow(job_type)
            if workflow_row:
                workflow = workflow_row
        if workflow:
            config["workflow_id"] = workflow["id"]
            config["workflow_name"] = workflow["name"]
            config["agent_sequence"] = json.loads(workflow["agent_sequence_json"])
        else:
            config["workflow_id"] = ""
            config["workflow_name"] = ""
            wf_default = DEFAULT_WORKFLOWS.get(job_type, {})
            config["agent_sequence"] = wf_default.get("agents", [])

        # 4. Agent definitions
        agents = {}
        for agent in self.get_agents():
            agents[agent["name"]] = {
                "id": agent["id"],
                "enabled": bool(agent["is_enabled"]),
                "model": agent["model_override"] or config.get("profile", {}).get("model_heavy", "openai/gpt-4.1"),
                "temperature": agent["temperature"],
                "max_tokens": agent["max_tokens"],
                "max_iterations": agent["max_iterations"],
                "tools": json.loads(agent["tools_allowed_json"]) if agent["tools_allowed_json"] else [],
            }
        config["agents"] = agents

        # 5. Overrides
        if overrides:
            config["overrides"] = overrides
            for k, v in overrides.items():
                config[k] = v

        config["resolved_at"] = datetime.now().isoformat()
        config["job_type"] = job_type
        return config

    # ── Versioning & Rollback ─────────────────────────────

    def _create_version(self, scope: str, target_id: str, config: Any,
                        user_id: str, reason: str = "") -> int:
        """Create a version snapshot. Returns version number."""
        last = self.db.execute(
            "SELECT MAX(version_num) as v FROM config_versions WHERE scope=? AND target_id=?",
            (scope, target_id)
        ).fetchone()
        vnum = (last["v"] or 0) + 1 if last else 1

        self.db.execute("""
            INSERT INTO config_versions (id, scope, target_id, version_num, config_json,
                                          change_reason, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (str(uuid.uuid4())[:12], scope, target_id, vnum,
              json.dumps(config, default=str), reason, user_id))
        return vnum

    def get_versions(self, scope: str, target_id: str, limit: int = 20) -> list:
        rows = self.db.execute("""
            SELECT * FROM config_versions WHERE scope=? AND target_id=?
            ORDER BY version_num DESC LIMIT ?
        """, (scope, target_id, limit)).fetchall()
        return [dict(r) for r in rows] if rows else []

    def get_version(self, scope: str, target_id: str, version_num: int) -> Optional[dict]:
        row = self.db.execute("""
            SELECT * FROM config_versions WHERE scope=? AND target_id=? AND version_num=?
        """, (scope, target_id, version_num)).fetchone()
        return dict(row) if row else None

    def rollback(self, scope: str, target_id: str, version_num: int,
                 user_id: str = "system") -> dict:
        """Rollback to a previous version. Creates a new version (forward-only history)."""
        v = self.get_version(scope, target_id, version_num)
        if not v:
            raise ValueError(f"Version {version_num} not found for {scope}/{target_id}")

        config = json.loads(v["config_json"])

        if scope == "profile":
            self.update_profile(target_id, config, user_id,
                                f"Rollback to v{version_num}")
        elif scope == "system":
            self.set_setting(target_id, config.get("new", config),
                             user_id, f"Rollback to v{version_num}")
        elif scope == "workflow":
            agents = config.get("agents", config)
            if isinstance(agents, list):
                self.update_workflow(target_id, agents, user_id,
                                     f"Rollback to v{version_num}")

        self._audit(user_id, "rollback", scope, target_id,
                    {"to_version": version_num})
        return config

    def diff_versions(self, scope: str, target_id: str, v1: int, v2: int) -> dict:
        """Compare two versions. Returns {added, removed, changed}."""
        ver1 = self.get_version(scope, target_id, v1)
        ver2 = self.get_version(scope, target_id, v2)
        if not ver1 or not ver2:
            return {"error": "Version not found"}

        c1 = json.loads(ver1["config_json"])
        c2 = json.loads(ver2["config_json"])

        # Flatten for comparison
        f1 = self._flatten(c1)
        f2 = self._flatten(c2)

        added = {k: f2[k] for k in f2 if k not in f1}
        removed = {k: f1[k] for k in f1 if k not in f2}
        changed = {k: {"old": f1[k], "new": f2[k]} for k in f1 if k in f2 and f1[k] != f2[k]}

        return {"v1": v1, "v2": v2, "added": added, "removed": removed, "changed": changed}

    # ── Audit ─────────────────────────────────────────────

    def _audit(self, user_id: str, action: str, target_type: str = None,
               target_id: str = None, details: dict = None):
        try:
            self.db.execute("""
                INSERT INTO admin_audit_log (id, admin_id, action, target_type, target_id,
                                              details, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (str(uuid.uuid4())[:12], user_id, action, target_type, target_id,
                  json.dumps(details) if details else None, datetime.now().isoformat()))
        except Exception as e:
            logger.warning(f"Audit log failed: {e}")

    def get_audit_log(self, limit: int = 50, action: str = None,
                      target_type: str = None) -> list:
        sql = "SELECT * FROM admin_audit_log WHERE 1=1"
        params = []
        if action:
            sql += " AND action=?"
            params.append(action)
        if target_type:
            sql += " AND target_type=?"
            params.append(target_type)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self.db.execute(sql, params).fetchall()
        return [dict(r) for r in rows] if rows else []

    # ── Seeding ───────────────────────────────────────────

    def seed_defaults(self, user_id: str = "system"):
        """Populate ACP tables with defaults from current code.
        Safe to call multiple times — only inserts if not exists."""

        # Settings
        for key, d in DEFAULT_SETTINGS.items():
            exists = self.db.execute(
                "SELECT 1 FROM system_settings WHERE key=?", (key,)
            ).fetchone()
            if not exists:
                self.db.execute("""
                    INSERT INTO system_settings (key, value_json, type, description, category,
                                                  requires_stepup, updated_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (key, json.dumps(d["value"]), d.get("type", "string"),
                      d.get("desc", ""), d.get("cat", "general"),
                      d.get("stepup", False), user_id))

        # Profiles
        for name, pdata in DEFAULT_PROFILES.items():
            exists = self.db.execute(
                "SELECT 1 FROM agent_profiles WHERE name=?", (name,)
            ).fetchone()
            if not exists:
                pid = str(uuid.uuid4())[:12]
                is_active = 1 if name == "default" else 0
                self.db.execute("""
                    INSERT INTO agent_profiles (id, name, description, profile_json, is_active,
                                                 created_by, updated_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (pid, name, pdata["desc"], json.dumps(pdata["config"]),
                      is_active, user_id, user_id))

        # Agents
        for i, (name, adata) in enumerate(DEFAULT_AGENTS.items()):
            exists = self.db.execute(
                "SELECT 1 FROM agent_definitions WHERE name=?", (name,)
            ).fetchone()
            if not exists:
                aid = str(uuid.uuid4())[:12]
                self.db.execute("""
                    INSERT INTO agent_definitions (id, name, role, model_override,
                                                    temperature, sort_order, is_enabled)
                    VALUES (?, ?, ?, ?, ?, ?, 1)
                """, (aid, name, adata["role"], adata["model"], adata["temp"], i * 10))

        # Workflows
        for job_type, wdata in DEFAULT_WORKFLOWS.items():
            exists = self.db.execute(
                "SELECT 1 FROM workflow_templates WHERE job_type=? AND name=?",
                (job_type, wdata["name"])
            ).fetchone()
            if not exists:
                wid = str(uuid.uuid4())[:12]
                self.db.execute("""
                    INSERT INTO workflow_templates (id, name, job_type, description,
                                                    agent_sequence_json, is_active, created_by, updated_by)
                    VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                """, (wid, wdata["name"], job_type, wdata.get("desc", ""),
                      json.dumps(wdata["agents"]), user_id, user_id))

        self.db.commit()
        logger.info("ACP: Defaults seeded successfully")

    # ── Dashboard Stats ───────────────────────────────────

    def get_dashboard_stats(self) -> dict:
        """Aggregate stats for the ACP dashboard."""
        stats = {}

        # Jobs
        stats["active_jobs"] = self.db.execute(
            "SELECT COUNT(*) as c FROM jobs WHERE status IN ('queued','running')"
        ).fetchone()["c"]
        stats["total_jobs_today"] = self.db.execute(
            "SELECT COUNT(*) as c FROM jobs WHERE created_at >= date('now')"
        ).fetchone()["c"]

        # Agents
        stats["total_agents"] = self.db.execute(
            "SELECT COUNT(*) as c FROM agent_definitions"
        ).fetchone()["c"]
        stats["enabled_agents"] = self.db.execute(
            "SELECT COUNT(*) as c FROM agent_definitions WHERE is_enabled=1"
        ).fetchone()["c"]

        # Profiles
        active = self.get_active_profile()
        stats["active_profile"] = active["name"] if active else "default"

        # Settings
        stats["total_settings"] = len(self.get_all_settings())
        stats["queue_paused"] = self.flag("queue_paused")
        stats["emergency_stop"] = self.flag("emergency_stop")

        # Recent changes
        stats["recent_changes"] = self.db.execute("""
            SELECT * FROM config_versions ORDER BY created_at DESC LIMIT 5
        """).fetchall()
        stats["recent_changes"] = [dict(r) for r in stats["recent_changes"]] if stats["recent_changes"] else []

        # Cost today
        try:
            cost_row = self.db.execute("""
                SELECT COALESCE(SUM(CAST(json_extract(details, '$.cost') AS REAL)), 0) as total
                FROM admin_audit_log
                WHERE action='cost_event' AND created_at >= date('now')
            """).fetchone()
            stats["cost_today"] = cost_row["total"] if cost_row else 0
        except Exception:
            stats["cost_today"] = 0

        return stats

    # ── Helpers ────────────────────────────────────────────

    @staticmethod
    def _parse_value(value_json: str, vtype: str) -> Any:
        try:
            v = json.loads(value_json)
        except (json.JSONDecodeError, TypeError):
            return value_json
        if vtype == "bool":
            return bool(v)
        if vtype == "int":
            return int(v)
        if vtype == "float":
            return float(v)
        return v

    @staticmethod
    def _flatten(d: Any, prefix: str = "") -> dict:
        """Flatten nested dict for comparison."""
        result = {}
        if isinstance(d, dict):
            for k, v in d.items():
                new_key = f"{prefix}.{k}" if prefix else k
                if isinstance(v, dict):
                    result.update(ACP._flatten(v, new_key))
                else:
                    result[new_key] = v
        else:
            result[prefix or "_root"] = d
        return result
