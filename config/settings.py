"""
Automated Slot Studio - Configuration & LLM Routing

PHASE 12: FULL SEND — MAX PARALLELISM + MAX QUALITY
====================================================
- GPT-5.2 (128K output, $1.75/$14) for precision agents: math, compliance, producer
- GPT-5 (128K output, $1.25/$10) for creative agents: designer, art, audio, animation
- GPT-5-mini (128K output, $0.25/$1) for fast validation: math_validator, patent, proofreader
- All agents at 128,000 max_tokens (full model capacity)
- Token budgets 4x raised — Tier 3 at 2M TPM handles it
- 10M simulation spins — converges in fewer OODA loops
- 11 total agents with 5-way production parallelism
- Cost per pipeline: ~$8-15 (well under $20 ceiling)
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent.parent
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "./output"))


# ============================================================
# HYBRID LLM ROUTING — TIER 3 MAX CAPACITY
#
# CrewAI uses litellm → model strings MUST be litellm-compatible:
#   "openai/gpt-5"             → GPT-5 (creative, $1.25/$10 per 1M)
#   "openai/gpt-5-mini"        → GPT-5-mini (fast, $0.25/$1 per 1M)
#   "openai/gpt-5.2"           → GPT-5.2 (best reasoning, $1.75/$14)
#
# GPT-5 FAMILY SPECS (all models):
#   Context window:   400,000 tokens
#   Max output:       128,000 tokens
#   Tier 3 TPM:       2,000,000 (GPT-5.2) / 2,000,000+ (GPT-5/mini)
#
# ROUTING STRATEGY:
#   PREMIUM (GPT-4.1)      → Agents where precision/reasoning = critical
#                            (math, compliance, lead_producer, proofreader convergence)
#   HEAVY (GPT-4.1)        → Agents where creativity + quality = critical
#                            (designer, art, audio, animation, research)
#   LIGHT (GPT-4.1-mini)   → Agents where speed > depth
#                            (math_validator, patent_specialist)
# ============================================================

class LLMConfig:

    # --- Model Selection ---
    PREMIUM = os.getenv("LLM_PREMIUM", "openai/gpt-4.1")   # Best reasoning — math, compliance, producer
    HEAVY   = os.getenv("LLM_HEAVY",   "openai/gpt-4.1")     # High quality — creative agents
    LIGHT   = os.getenv("LLM_LIGHT",   "openai/gpt-4.1-mini") # Fast — validation, patent checks

    # --- Image Generation ---
    IMAGE_MODEL = "dall-e-3"

    # --- Per-Agent Routing ---
    # All agents at 128K max output (full GPT-5 family capacity).
    # Premium model for precision-critical agents.
    # Heavy model for creative agents.
    # Light model for fast validation agents.
    AGENTS = {
        # ── PREMIUM: Precision-critical (GPT-4.1 — best reasoning) ──
        "lead_producer":         {"model": PREMIUM, "temperature": 0.3, "max_tokens": 128000},
        "mathematician":         {"model": PREMIUM, "temperature": 0.1, "max_tokens": 128000},
        "compliance_officer":    {"model": PREMIUM, "temperature": 0.1, "max_tokens": 128000},

        # ── HEAVY: Creative agents (GPT-4.1 — high quality) ──
        "market_analyst":        {"model": HEAVY, "temperature": 0.4, "max_tokens": 128000},
        "game_designer":         {"model": HEAVY, "temperature": 0.6, "max_tokens": 128000},
        "art_director":          {"model": HEAVY, "temperature": 0.7, "max_tokens": 128000},
        "research_synthesizer":  {"model": HEAVY, "temperature": 0.5, "max_tokens": 128000},
        "audio_engineer":        {"model": HEAVY, "temperature": 0.6, "max_tokens": 128000},
        "animation_director":    {"model": HEAVY, "temperature": 0.5, "max_tokens": 128000},

        # ── LIGHT: Fast validation (GPT-4.1-mini — speed matters) ──
        "math_validator":        {"model": LIGHT, "temperature": 0.1, "max_tokens": 128000},
        "patent_specialist":     {"model": LIGHT, "temperature": 0.1, "max_tokens": 128000},
        "gdd_proofreader":       {"model": LIGHT, "temperature": 0.1, "max_tokens": 128000},
    }

    # --- Token Budgets (soft limit per agent per run) ---
    # Tier 3 at 2M TPM: 11 agents × ~200K avg = ~2.2M per tick.
    # With parallel stages staggered, peak concurrent is ~5 agents = ~1M TPM.
    # Well within Tier 3 limits.
    TOKEN_BUDGETS = {
        "lead_producer":        1_000_000,
        "market_analyst":       2_000_000,
        "game_designer":        2_000_000,
        "mathematician":        2_000_000,
        "art_director":         1_500_000,
        "compliance_officer":   1_000_000,
        "research_synthesizer": 1_000_000,
        "audio_engineer":         800_000,
        "animation_director":   1_000_000,
        "math_validator":         500_000,
        "patent_specialist":      500_000,
        "gdd_proofreader":        500_000,
    }

    # --- Cost Rates (USD per 1M tokens) ---
    COST_INPUT = {
        "openai/gpt-4.1": 2.00, "openai/gpt-4.1-mini": 0.40, "openai/gpt-4.1-nano": 0.10,
        "openai/gpt-5": 1.25, "openai/gpt-5-mini": 0.25,
        "openai/gpt-5.1": 1.25, "openai/gpt-5.2": 1.75,
        "openai/gpt-4o": 2.50, "openai/gpt-4o-mini": 0.15,
    }
    COST_OUTPUT = {
        "openai/gpt-4.1": 8.00, "openai/gpt-4.1-mini": 1.60, "openai/gpt-4.1-nano": 0.40,
        "openai/gpt-5": 10.00, "openai/gpt-5-mini": 1.00,
        "openai/gpt-5.1": 10.00, "openai/gpt-5.2": 14.00,
        "openai/gpt-4o": 10.00, "openai/gpt-4o-mini": 0.60,
    }
    COST_IMAGE = {"1024x1024": 0.04, "1792x1024": 0.08}
    COST_AUDIO_SFX = 0.01  # Estimated per ElevenLabs sound effect generation

    @classmethod
    def get_llm(cls, agent_key: str) -> str:
        """Return the litellm model string for CrewAI's `llm` param.
        Checks ACP overlay first, falls back to hardcoded AGENTS dict."""
        if cls._acp_agents and agent_key in cls._acp_agents:
            return cls._acp_agents[agent_key].get("model", cls.LIGHT)
        return cls.AGENTS.get(agent_key, {}).get("model", cls.LIGHT)

    @classmethod
    def get_config(cls, agent_key: str) -> dict:
        """Return full config dict for an agent. ACP overlay takes priority."""
        if cls._acp_agents and agent_key in cls._acp_agents:
            return cls._acp_agents[agent_key]
        return cls.AGENTS.get(agent_key, {"model": cls.LIGHT, "temperature": 0.5, "max_tokens": 128000})

    @classmethod
    def is_agent_enabled(cls, agent_key: str) -> bool:
        """Check if an agent is enabled via ACP. Default True if ACP not loaded."""
        if cls._acp_agents and agent_key in cls._acp_agents:
            return cls._acp_agents[agent_key].get("enabled", True)
        return True

    @classmethod
    def get_flag(cls, flag_key: str, default=None):
        """Get a feature flag from ACP resolved config."""
        return cls._acp_flags.get(flag_key, default)

    @classmethod
    def load_from_acp(cls, resolved_config: dict) -> None:
        """Load ACP resolved config as overlay. Called once at job start.
        After this, get_llm/get_config/is_agent_enabled use ACP data."""
        import logging
        logger = logging.getLogger("arkainbrain.llm")

        # Extract agent configs
        agents = resolved_config.get("agents", {})
        for name, adata in agents.items():
            cls._acp_agents[name] = {
                "model": adata.get("model", cls.LIGHT),
                "temperature": adata.get("temperature", 0.5),
                "max_tokens": adata.get("max_tokens", 128000),
                "max_iterations": adata.get("max_iterations", 25),
                "enabled": adata.get("enabled", True),
            }

        # Extract feature flags (setting.* keys)
        for key, val in resolved_config.items():
            if key.startswith("setting."):
                cls._acp_flags[key.replace("setting.", "")] = val

        # Extract profile-level defaults
        profile = resolved_config.get("profile", {})
        if profile.get("model_heavy"):
            cls.HEAVY = profile["model_heavy"]
        if profile.get("model_light"):
            cls.LIGHT = profile["model_light"]

        n_agents = len(cls._acp_agents)
        n_flags = len(cls._acp_flags)
        logger.info(f"ACP loaded: {n_agents} agents, {n_flags} flags, "
                     f"profile={resolved_config.get('profile_name', '?')}")

    @classmethod
    def clear_acp(cls) -> None:
        """Reset ACP overlay (for testing or between runs)."""
        cls._acp_agents.clear()
        cls._acp_flags.clear()

    # ACP overlay storage (class-level, populated by load_from_acp)
    _acp_agents: dict = {}
    _acp_flags: dict = {}


# ============================================================
# Cost Tracker — one per pipeline run
# ============================================================

class CostTracker:
    def __init__(self):
        self.usage = {}
        self.images = 0
        self.image_cost = 0.0

    def log(self, agent_key: str, input_tokens: int = 0, output_tokens: int = 0):
        if agent_key not in self.usage:
            self.usage[agent_key] = {"input": 0, "output": 0, "calls": 0}
        self.usage[agent_key]["input"] += input_tokens
        self.usage[agent_key]["output"] += output_tokens
        self.usage[agent_key]["calls"] += 1
        total = self.usage[agent_key]["input"] + self.usage[agent_key]["output"]
        budget = LLMConfig.TOKEN_BUDGETS.get(agent_key, float("inf"))
        if total > budget:
            print(f"⚠️  {agent_key} token budget exceeded: {total:,}/{budget:,}")

    def log_image(self, size="1024x1024"):
        self.images += 1
        self.image_cost += LLMConfig.COST_IMAGE.get(size, 0.04)

    def total_tokens(self) -> int:
        return sum(v["input"] + v["output"] for v in self.usage.values())

    def total_cost(self) -> float:
        cost = 0.0
        for key, data in self.usage.items():
            model = LLMConfig.get_llm(key)
            cost += (data["input"] / 1e6) * LLMConfig.COST_INPUT.get(model, 5.0)
            cost += (data["output"] / 1e6) * LLMConfig.COST_OUTPUT.get(model, 15.0)
        return round(cost + self.image_cost, 4)

    def summary(self) -> dict:
        return {
            "per_agent": {
                k: {"model": LLMConfig.get_llm(k), **v, "budget": LLMConfig.TOKEN_BUDGETS.get(k)}
                for k, v in self.usage.items()
            },
            "total_tokens": self.total_tokens(),
            "total_images": self.images,
            "estimated_cost_usd": self.total_cost(),
        }


# ============================================================
# Pipeline Configuration
# ============================================================

class PipelineConfig:
    HITL_ENABLED = os.getenv("HITL_ENABLED", "true").lower() == "true"
    HITL_CHECKPOINTS = {"post_research": True, "post_design_math": True, "post_art_review": True}
    SIMULATION_SPINS = int(os.getenv("SIMULATION_SPINS", "10000000"))  # 10M — converges in 1 OODA loop
    COMPETITOR_BROAD_SWEEP_LIMIT = 30
    COMPETITOR_DEEP_DIVE_LIMIT = 10
    MOOD_BOARD_VARIANTS = 4
    IMAGE_SIZES = {"mood_board": "1024x1024", "symbol": "1024x1024", "background": "1792x1024"}

    # Phase 5A: Tier 3 concurrency settings
    MAX_CONCURRENT_PIPELINES = int(os.getenv("MAX_CONCURRENT_JOBS", "6"))
    # With Tier 3 at 800K TPM, 6 concurrent pipelines each averaging ~50K TPM
    # stays well under limits. Redis queue handles burst beyond this.


# ============================================================
# RAG Configuration
# ============================================================

class RAGConfig:
    QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
    QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "")
    COLLECTION_NAME = os.getenv("QDRANT_COLLECTION", "slot_regulations")
    EMBEDDING_MODEL = "text-embedding-3-small"
    EMBEDDING_DIM = 1536
    CHUNK_SIZE = 1000
    CHUNK_OVERLAP = 200
    TOP_K = 10
    DOCUMENT_SOURCES = {
        "gli_standards": "data/regulations/gli/",
        "ukgc_rules": "data/regulations/ukgc/",
        "mga_rules": "data/regulations/mga/",
        "ontario_rules": "data/regulations/ontario/",
        "company_games": "data/internal/past_games/",
    }


# ============================================================
# Jurisdiction Database (Static — RAG fallback)
#
# INTERNATIONAL markets + US STATE LOOPHOLE ANALYSIS
# Each US state entry includes:
#   - gambling_definition: How the state defines illegal gambling
#   - legal_avenues: Known legal pathways for game placement
#   - loophole_strategy: Specific game design tweaks to exploit
#   - risk_level: LOW / MEDIUM / HIGH / EXTREME
#   - key_statutes: Primary laws to watch
#   - enforcement_notes: How aggressively the state enforces
# ============================================================

JURISDICTION_REQUIREMENTS = {

    # ========== INTERNATIONAL ==========

    "UK": {
        "regulator": "UKGC", "min_rtp": 80.0, "max_win_cap": None,
        "certifiers": ["GLI", "BMM", "eCOGRA", "NMi"],
        "content_restrictions": [
            "No content appealing primarily to children",
            "Responsible gambling messaging required",
            "Reality check at 60-minute intervals",
            "Session time and loss limits mandatory",
        ],
        "data_privacy": "GDPR",
    },
    "Malta": {
        "regulator": "MGA", "min_rtp": 85.0, "max_win_cap": None,
        "certifiers": ["GLI", "BMM", "iTech Labs"],
        "content_restrictions": ["No offensive or discriminatory content", "RNG certification required"],
        "data_privacy": "GDPR",
    },
    "Ontario": {
        "regulator": "AGCO/iGO", "min_rtp": 85.0, "max_win_cap": None,
        "certifiers": ["GLI", "BMM", "iTech Labs", "Gaming Associates"],
        "content_restrictions": [
            "Responsible gambling tools mandatory",
            "Self-exclusion integration required",
            "No inducements to problem gambling",
        ],
        "data_privacy": "PIPEDA",
    },
    "New Jersey": {
        "regulator": "NJ DGE", "min_rtp": 83.0, "max_win_cap": None,
        "certifiers": ["GLI", "BMM"],
        "content_restrictions": [
            "Geolocation verification required",
            "Age verification mandatory",
            "Responsible gambling features required",
        ],
        "data_privacy": "State privacy laws",
    },
    "Curacao": {
        "regulator": "Curacao eGaming", "min_rtp": 75.0, "max_win_cap": None,
        "certifiers": ["GLI", "iTech Labs"],
        "content_restrictions": ["Basic responsible gambling messaging"],
        "data_privacy": "Minimal requirements",
    },

    # ========== US STATES ==========
    # NO STATIC DATA — All US state jurisdiction data lives in Qdrant.
    # Run the State Recon Pipeline to research any state:
    #   python -m flows.state_recon --state "North Carolina"
    # Results are auto-ingested into Qdrant and stay current.
}


# ============================================================
# DEPRECATED — Static loophole data removed.
# All US jurisdiction intelligence now lives in Qdrant,
# populated and refreshed by the State Recon Pipeline.
# Query via: RegulatoryRAGTool → search_regulations
# Research via: StateReconFlow → python -m flows.state_recon --state "X"
# ============================================================
