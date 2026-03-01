"""
Automated Slot Studio - Pipeline Flows (PRODUCTION)

PHASE 2+3 WIRED:
- Real litellm model strings per agent (hybrid cost routing)
- Tool outputs parsed into structured pipeline state
- PDF generator called at assembly stage
- CostTracker logs all LLM + image spend
- Math agent receives simulation template

PHASE 4 PATH 4 — Stage-Level Checkpointing:
- Every stage writes checkpoint.json to output_dir on completion
- checkpoint.json contains full serialized PipelineState + timing data
- Enables resume from last completed stage after timeout/crash
- PIPELINE_STAGES constant defines canonical stage order
- _stage_enter/_stage_exit wrap every stage for timing + persistence

PHASE 4 PATH 4.2 — Time Budget Tracking:
- TimeBudget class tracks cumulative elapsed time vs global timeout
- Every stage logs budget status: elapsed, remaining, % used
- Budget data persisted in checkpoint.json for resume planning
- Provides has_time_for() / can_start() queries for future smart skipping

PHASE 4 PATH 4.3+4 — Partial Status & Resume:
- Watchdog marks timed-out jobs as 'partial' (not 'failed') when checkpoint exists
- resume_from_stage() bypasses @start/@listen, calls stage methods directly
- run_resume() in worker.py: loads checkpoint, hydrates state, continues pipeline
- /api/resume/<job_id> endpoint + Resume button in dashboard & history UI
- Auto-approves all HITL checkpoints on resume (user already saw partial output)
- MAX_RESUMES=3 limit prevents infinite resume loops
"""

import json
import os
import re
import sqlite3
import threading
import time
VERBOSE = os.getenv("CREWAI_VERBOSE", "false").lower() == "true"

# MUST be imported BEFORE crewai/litellm/openai — patches the SDK at class level
import config.context_guard  # noqa: F401 (side-effect: monkey-patches openai + litellm)

from datetime import datetime
from pathlib import Path
from typing import Optional

from crewai import Agent, Crew, Process, Task
from crewai.flow.flow import Flow, listen, start
from pydantic import BaseModel, Field
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

from config.settings import (
    LLMConfig, PipelineConfig, RAGConfig,
    CostTracker, JURISDICTION_REQUIREMENTS,
)
from models.schemas import GameIdeaInput

# ── Stage Timeouts (seconds) ──
# If a crew.kickoff() exceeds this, it's killed and the pipeline continues
# with whatever partial output is available.
STAGE_TIMEOUTS = {
    "research":   int(os.getenv("TIMEOUT_RESEARCH", "900")),    # 15 min
    "design":     int(os.getenv("TIMEOUT_DESIGN", "1200")),     # 20 min (initial GDD + Math)
    "mood_board": int(os.getenv("TIMEOUT_MOOD", "600")),        # 10 min
    "production": int(os.getenv("TIMEOUT_PRODUCTION", "1800")), # 30 min (art + audio + compliance)
    "recon":      int(os.getenv("TIMEOUT_RECON", "600")),       # 10 min (also used for convergence checks)
}


def run_crew_with_timeout(crew: Crew, stage_name: str, console: Console) -> object:
    """
    Run crew.kickoff() with a hard timeout.
    If it exceeds STAGE_TIMEOUTS[stage_name], returns None instead of hanging.
    If it hits a context_length_exceeded error, logs warning and returns None
    so the pipeline can continue with partial output.
    """
    timeout = STAGE_TIMEOUTS.get(stage_name, 1200)  # default 20 min
    result_holder = [None]
    error_holder = [None]

    def _run():
        try:
            result_holder[0] = crew.kickoff()
        except Exception as e:
            error_holder[0] = e

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=timeout)

    if thread.is_alive():
        console.print(f"[red]⏰ TIMEOUT: {stage_name} exceeded {timeout}s — forcing continue with partial output[/red]")
        # Thread is daemon so it won't block shutdown, but we can't kill it cleanly
        # The pipeline will continue with whatever state was set before timeout
        return None

    if error_holder[0]:
        err = error_holder[0]
        err_str = str(err).lower()
        # Context overflow is recoverable — the guard should have retried,
        # but if we still get here, continue gracefully with partial output
        if any(k in err_str for k in [
            "context_length_exceeded", "input tokens exceed",
            "maximum context length", "too many tokens",
            "tool_calls", "must be followed by tool messages",
        ]):
            console.print(
                f"[yellow]⚠️ {stage_name}: Context window exceeded after guard retry — "
                f"continuing with partial output[/yellow]"
            )
            logger.warning(f"Context overflow in {stage_name}: {err}")
            return None
        console.print(f"[red]❌ {stage_name} FAILED: {err}[/red]")
        raise err

    return result_holder[0]


def _update_stage_db(job_id: str, stage: str):
    """Write current pipeline stage to DB so all devices can see progress."""
    if not job_id:
        return  # CLI mode, no DB
    try:
        db_path = os.getenv("DB_PATH", "arkainbrain.db")
        conn = sqlite3.connect(db_path, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("UPDATE jobs SET current_stage=? WHERE id=?", (stage, job_id))
        conn.commit()
        conn.close()
    except Exception:
        pass  # Non-critical — don't crash pipeline over a status update


def _extract_symbols_from_gdd(gdd_text: str, theme: str) -> list[str]:
    """Parse GDD text to extract the actual symbol names the designer specified.

    Looks for patterns like:
      - H1: Pharaoh, H2: Scarab, M1: Ankh ...
      - High-pay symbols: Pharaoh (H1), Scarab (H2) ...
      - Symbol hierarchy tables
      - Wild, Scatter explicit mentions

    Falls back to theme-aware defaults if parsing fails.
    """
    import re

    symbols = []
    seen = set()

    def _add(name: str):
        clean = name.strip().strip("•·-–—:,").strip()
        # Remove leading slot codes like "H1:", "M2 -"
        clean = re.sub(r'^[HMLhml]\d[\s:–—-]+', '', clean).strip()
        if clean and clean.lower() not in seen and len(clean) < 40:
            seen.add(clean.lower())
            symbols.append(clean)

    # Pattern 1: "H1: Name" or "H1 - Name" or "H1 – Name"
    for m in re.finditer(r'[HML]\d[\s:–—-]+([A-Z][A-Za-z\s\']+?)(?:\n|,|;|\(|–|—|\|)', gdd_text):
        _add(m.group(1))

    # Pattern 2: "Name (H1)" or "Name [H1]"
    for m in re.finditer(r'([A-Z][A-Za-z\s\']+?)\s*[\(\[][HML]\d[\)\]]', gdd_text):
        _add(m.group(1))

    # Pattern 3: Lines in symbol tables — "| Pharaoh | H1 | 500 |"
    for m in re.finditer(r'\|\s*([A-Z][A-Za-z\s\']+?)\s*\|\s*[HML]\d', gdd_text):
        _add(m.group(1))

    # Pattern 4: Bullet lists — "- Pharaoh: high-pay symbol"
    for m in re.finditer(r'[-•]\s*([A-Z][A-Za-z\s\']{2,25})(?:\s*[-:–]|\s+symbol|\s+\()', gdd_text):
        _add(m.group(1))

    # Pattern 5: "Wild: Golden Snake" or "Scatter: Book of Secrets" — extract themed names
    for m in re.finditer(r'(?:Wild|Scatter)[\s:–—-]+([A-Z][A-Za-z\s\']+?)(?:\n|,|;|\(|–|—)', gdd_text):
        name = m.group(1).strip()
        if name and len(name) > 2:
            _add(name)

    # Always ensure Wild and Scatter
    has_wild = any("wild" in s.lower() for s in symbols)
    has_scatter = any("scatter" in s.lower() for s in symbols)
    if not has_wild:
        symbols.append("Wild")
    if not has_scatter:
        symbols.append("Scatter")

    # If we got a decent set, add low-pay royals
    if len(symbols) >= 4:
        for royal in ["Ace", "King", "Queen", "Jack", "Ten"]:
            if royal.lower() not in seen:
                symbols.append(royal)
        return symbols

    # ── Fallback: theme-aware default symbol set ──
    theme_lower = theme.lower()
    default_sets = {
        "egypt":   ["Pharaoh", "Scarab", "Ankh", "Eye of Horus", "Sphinx"],
        "dragon":  ["Dragon", "Phoenix", "Pearl", "Gold Coin", "Jade Ring"],
        "ocean":   ["Mermaid", "Trident", "Pearl", "Treasure Chest", "Seahorse"],
        "space":   ["Astronaut", "Rocket", "Planet", "Star Crystal", "Black Hole"],
        "celtic":  ["Druid", "Raven", "Torc", "Oak Tree", "Stone Circle"],
        "aztec":   ["Sun God", "Jaguar", "Serpent", "Gold Mask", "Temple"],
        "vampire": ["Vampire", "Coffin", "Blood Vial", "Bat", "Silver Cross"],
        "pirate":  ["Captain", "Skull", "Treasure Map", "Cannon", "Ship Wheel"],
        "samurai": ["Samurai", "Katana", "Cherry Blossom", "Pagoda", "Dragon Mask"],
        "norse":   ["Odin", "Thor Hammer", "Rune Stone", "Fenrir Wolf", "Valknut"],
        "chinese": ["Dragon", "Fortune Coin", "Koi Fish", "Lantern", "Jade Emperor"],
    }
    high_pay = ["Symbol A", "Symbol B", "Symbol C", "Symbol D", "Symbol E"]
    for key, syms in default_sets.items():
        if key in theme_lower:
            high_pay = syms
            break

    return high_pay + ["Wild", "Scatter", "Ace", "King", "Queen", "Jack", "Ten"]


def _update_output_dir_db(job_id: str, output_dir: str):
    """Write output_dir to DB early so the watchdog can find checkpoint.json on timeout.

    Called from initialize() as soon as the output directory is created.
    Without this, the watchdog would have no way to locate the checkpoint file
    because output_dir was previously only written to DB on successful completion.
    """
    if not job_id:
        return
    try:
        db_path = os.getenv("DB_PATH", "arkainbrain.db")
        conn = sqlite3.connect(db_path, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("UPDATE jobs SET output_dir=? WHERE id=?", (output_dir, job_id))
        conn.commit()
        conn.close()
    except Exception:
        pass  # Non-critical


from tools.custom_tools import (
    SlotDatabaseSearchTool,
    MathSimulationTool,
    ImageGenerationTool,
    RegulatoryRAGTool,
    FileWriterTool,
)
from tools.advanced_research import (
    WebFetchTool,
    DeepResearchTool,
    CompetitorTeardownTool,
    KnowledgeBaseTool,
)
from tools.tier1_upgrades import (
    VisionQATool,
    PaytableOptimizerTool,
    JurisdictionIntersectionTool,
    PlayerBehaviorModelTool,
    AgentDebateTool,
    TrendRadarTool,
)
from tools.tier2_upgrades import (
    PatentIPScannerTool,
    HTML5PrototypeTool,
    SoundDesignTool,
    CertificationPlannerTool,
)
from tools.convergence_tools import (
    FileReaderTool,
    ConvergenceValidatorTool,
    GDDPatchTool,
    RTPBudgetCalculatorTool,
    GDDQualityAuditorTool,
    PaytableSanityCheckerTool,
)
from tools.jurisdiction_profiles import (
    JurisdictionComplianceCheckerTool,
    GetJurisdictionProfileTool,
)
from examples.fewshot import (
    get_designer_examples,
    get_mathematician_examples,
    get_art_director_examples,
)
from concurrent.futures import ThreadPoolExecutor, as_completed

console = Console()


def emit(event_type: str, **data):
    """Emit a structured event marker into the log stream.

    The worker captures stdout → log file. Frontend parses ##EV:{json}## markers
    to render rich cards, progress bars, and agent activity indicators.

    Event types:
      stage_start, stage_done, agent_start, agent_done,
      check_pass, check_fail, ooda_start, ooda_result,
      parallel_start, metric, blocker, warn, info
    """
    import json as _j
    payload = {"t": event_type, **data}
    print(f"##EV:{_j.dumps(payload, separators=(',',':'))}##", flush=True)

# ── OODA Convergence Loop Configuration ──
MAX_CONVERGENCE_LOOPS = int(os.getenv("MAX_CONVERGENCE_LOOPS", "3"))

# ── Pipeline Stage Registry ──
# Canonical ordered list of every stage in the pipeline.
# Used for checkpoint/resume, time budget tracking, and progress reporting.
# IMPORTANT: Keep this in sync with the @start/@listen DAG below.
PIPELINE_STAGES = [
    "initialize",
    "preflight",
    "research",
    "checkpoint_research",
    "design_and_math",
    "checkpoint_design",
    "mood_boards",
    "checkpoint_art",
    "production",
    "assemble_package",
]

# Checkpoint file format version — bump if PipelineState schema changes
# in a way that would break deserialization of older checkpoints.
CHECKPOINT_VERSION = 1

# Maximum number of times a single job can be resumed before we give up.
MAX_RESUMES = 3

# ── Global Pipeline Timeout ──
# Must match the watchdog timeout in worker.py.
# The budget tracker reads this to know the wall-clock ceiling.
PIPELINE_TIMEOUT = int(os.getenv("PIPELINE_TIMEOUT_SECONDS", "5400"))  # 90 min default

# ── Stage Time Estimates (seconds) ──
# Historical averages used for budget forecasting.
# Updated by actual measurements; these are initial conservative defaults.
# Used by TimeBudget.can_start() to decide if there's enough time
# to begin a stage before the global timeout expires.
STAGE_TIME_ESTIMATES = {
    "initialize":          15,    # Dir creation, config
    "preflight":           60,    # 5 parallel tool calls
    "research":           600,    # Market research crew (15 min timeout)
    "checkpoint_research":  10,   # Adversarial review + HITL
    "design_and_math":   2400,    # GDD + Math + up to 3 OODA convergence loops
    "checkpoint_design":   10,    # Adversarial review + HITL
    "mood_boards":        400,    # Art direction + DALL-E calls
    "checkpoint_art":      10,    # Adversarial review + HITL
    "production":        1500,    # Art + Audio ∥ Compliance (parallel)
    "assemble_package":   120,    # PDF gen + prototype + exports
}

# ── Stage Priority Classification ──
# Used by _budget_decision() to determine skip/compress behavior under time pressure.
#   P0 = Critical (never skip) — without these, the deliverable is unusable
#   P1 = Important (compress first, skip as last resort) — enrich quality but not essential
#   P2 = Nice-to-have (skip freely) — enrichment, HITL gates, optional checks
STAGE_PRIORITY = {
    "initialize":          0,   # P0: Creates output dirs — must run
    "preflight":           2,   # P2: Trend/jurisdiction enrichment — downstream handles missing data
    "research":            1,   # P1: Market research — can run lite (sweep only, no geo)
    "checkpoint_research": 2,   # P2: Adversarial review + HITL — auto-approve under pressure
    "design_and_math":     0,   # P0: GDD + Math model — core deliverable
    "checkpoint_design":   2,   # P2: Adversarial review + HITL — auto-approve under pressure
    "mood_boards":         1,   # P1: Art direction — can run lite (1 board, no vision QA)
    "checkpoint_art":      2,   # P2: Art review + HITL — auto-approve under pressure
    "production":          0,   # P0: Art + Audio + Compliance — core deliverable
    "assemble_package":    0,   # P0: PDF + prototype + manifest — must run
}

# ── Budget Pressure Thresholds ──
# These control when stages get compressed or skipped.
# "Safety margin" = (remaining time - P0 reserve) / current stage estimate.
# Higher margins mean more conservative (earlier skipping).
BUDGET_SAFETY_MARGINS = {
    "skip_p2":      0.5,   # Skip P2 when safety margin < 50% of their estimate
    "compress_p1":  0.8,   # Compress P1 when safety margin < 80% of their estimate
    "skip_p1":      0.3,   # Skip P1 when safety margin < 30% of their estimate
}

# ── Lite Mode Time Estimates ──
# Compressed versions of P1 stages take less time. Used for budget forecasting.
STAGE_TIME_ESTIMATES_LITE = {
    "research":    200,    # Sweep only, no geo research, no deep dive
    "mood_boards": 150,    # 1 mood board, skip vision QA re-checks
}


class TimeBudget:
    """Track cumulative wall-clock time against a global timeout budget.

    Provides real-time awareness of how much time the pipeline has consumed
    and how much remains.  Every stage calls stage_enter/stage_exit which
    records per-stage wall-clock duration.  The budget is used for:

    1. **Logging** — every stage exit prints a budget status line:
       ``[BUDGET] research: 612s | 660s/5400s (12.2%) | 4740s remaining``

    2. **Checkpoint enrichment** — budget snapshot is persisted in checkpoint.json
       so that a resumed worker knows how much time the *previous* run consumed.

    3. **Smart stage skipping** (Phase 5) — `_budget_decision(stage)` uses
       `can_start()` and `time_for_remaining_critical()` to decide whether
       to run, compress, or skip P1/P2 stages under time pressure.

    The budget is NOT enforced here — the worker.py watchdog is the hard kill.
    TimeBudget is purely advisory / informational.
    """

    def __init__(self, total_seconds: int, already_elapsed: float = 0.0):
        """
        Args:
            total_seconds: The global pipeline timeout (from PIPELINE_TIMEOUT).
            already_elapsed: Seconds already consumed by a previous run
                             (set on resume from checkpoint data).
        """
        self.total = total_seconds
        self.start = time.time()
        self.prior_elapsed = already_elapsed  # time burned in previous run(s)
        self._stage_starts: dict[str, float] = {}
        self._stage_durations: dict[str, float] = {}  # stage → seconds

    # ── Per-stage tracking ──────────────────────────────────────

    def stage_enter(self, name: str):
        """Mark the start of a stage."""
        self._stage_starts[name] = time.time()

    def stage_exit(self, name: str) -> float:
        """Mark the end of a stage and return its duration in seconds."""
        started = self._stage_starts.pop(name, time.time())
        duration = round(time.time() - started, 1)
        self._stage_durations[name] = duration
        return duration

    # ── Budget queries ──────────────────────────────────────────

    @property
    def elapsed(self) -> float:
        """Total wall-clock seconds since this run started (excludes prior runs)."""
        return time.time() - self.start

    @property
    def total_elapsed(self) -> float:
        """Total elapsed including time from prior run(s) before resume."""
        return self.prior_elapsed + self.elapsed

    @property
    def remaining(self) -> float:
        """Seconds remaining before the global timeout kills us."""
        return max(0.0, self.total - self.total_elapsed)

    @property
    def pct_used(self) -> float:
        """Percentage of total budget consumed (0-100+)."""
        return (self.total_elapsed / self.total) * 100 if self.total > 0 else 100.0

    @property
    def is_expired(self) -> bool:
        """True if we've exceeded the budget (watchdog will kill us soon)."""
        return self.total_elapsed >= self.total

    def has_time_for(self, estimated_seconds: float) -> bool:
        """True if the estimated duration fits within the remaining budget."""
        return self.remaining > estimated_seconds

    def can_start(self, stage_name: str, buffer_pct: float = 10.0) -> bool:
        """Predict whether a stage can complete before the budget expires.

        Uses STAGE_TIME_ESTIMATES as the predicted duration, plus a safety
        buffer (default 10%).

        Args:
            stage_name: Name of the stage to check.
            buffer_pct: Extra % of the estimate to add as safety margin.

        Returns:
            True if there's enough time to likely complete the stage.
        """
        estimate = STAGE_TIME_ESTIMATES.get(stage_name, 600)  # default 10 min
        buffered = estimate * (1 + buffer_pct / 100)
        return self.remaining > buffered

    def time_for_remaining_critical(self, current_stage: str) -> float:
        """Estimate seconds needed for remaining P0 (critical) stages.

        P0 stages (from STAGE_PRIORITY) are: initialize, design_and_math,
        production, assemble_package. Checkpoint stages are negligible and excluded.
        """
        try:
            idx = PIPELINE_STAGES.index(current_stage)
        except ValueError:
            return 0.0
        remaining = PIPELINE_STAGES[idx + 1:]
        return sum(
            STAGE_TIME_ESTIMATES.get(s, 0) for s in remaining
            if STAGE_PRIORITY.get(s, 0) == 0
        )

    # ── Formatting ──────────────────────────────────────────────

    def format_status(self, stage_name: str, stage_elapsed: float) -> str:
        """Format a one-line budget status string for log output.

        Example:
            [BUDGET] research: 612s | 660s/5400s (12.2%) | 4740s remaining
        """
        return (
            f"[BUDGET] {stage_name}: {stage_elapsed:.0f}s "
            f"| {self.total_elapsed:.0f}s/{self.total}s ({self.pct_used:.1f}%) "
            f"| {self.remaining:.0f}s remaining"
        )

    def snapshot(self) -> dict:
        """Return a JSON-serializable snapshot of the budget state.

        Included in checkpoint.json so a resumed worker knows the history.
        """
        return {
            "total_budget_s": self.total,
            "this_run_elapsed_s": round(self.elapsed, 1),
            "prior_runs_elapsed_s": round(self.prior_elapsed, 1),
            "total_elapsed_s": round(self.total_elapsed, 1),
            "remaining_s": round(self.remaining, 1),
            "pct_used": round(self.pct_used, 1),
            "stage_durations": dict(self._stage_durations),
        }


# ============================================================
# Pipeline State
# ============================================================

class PipelineState(BaseModel):
    job_id: str = ""  # Web HITL needs this to pause the right pipeline
    game_idea: Optional[GameIdeaInput] = None
    game_slug: str = ""
    output_dir: str = ""

    # Tier 1 pre-flight data
    trend_radar: Optional[dict] = None
    jurisdiction_constraints: Optional[dict] = None

    market_research: Optional[dict] = None
    research_approved: bool = False

    gdd: Optional[dict] = None
    math_model: Optional[dict] = None
    optimized_rtp: Optional[float] = None
    player_behavior: Optional[dict] = None
    design_math_approved: bool = False

    mood_board: Optional[dict] = None
    mood_board_approved: bool = False
    approved_mood_board_index: int = 0
    vision_qa_results: list[dict] = Field(default_factory=list)
    art_assets: Optional[dict] = None
    compliance: Optional[dict] = None

    # Tier 2 data
    patent_scan: Optional[dict] = None
    sound_design: Optional[dict] = None
    animation_package: Optional[dict] = None  # Phase 12: Animation spec + CSS keyframes + brief
    prototype_path: str = ""
    certification_plan: Optional[dict] = None
    recon_data: Optional[dict] = None  # State recon results for US jurisdictions

    total_tokens_used: int = 0
    total_images_generated: int = 0
    estimated_cost_usd: float = 0.0
    errors: list[str] = Field(default_factory=list)
    hitl_approvals: dict[str, bool] = Field(default_factory=dict)
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    pdf_files: list[str] = Field(default_factory=list)

    # OODA convergence loop tracking
    convergence_loops_run: int = 0
    convergence_history: list[dict] = Field(default_factory=list)

    # Phase 3A: Iterate mode
    iterate_mode: bool = False
    iterate_config: dict = Field(default_factory=dict)  # source_output_dir, rerun_stages, version

    # Phase 5: Revenue projection
    revenue_projection: Optional[dict] = None

    # ── Checkpoint & Resume (Phase 4 Path 4) ──
    last_completed_stage: str = ""              # e.g. "research" — last stage that fully completed
    stage_timings: dict[str, float] = Field(default_factory=dict)   # stage_name → seconds elapsed
    pipeline_start_epoch: float = 0.0          # time.time() when pipeline started (or resumed)
    resume_count: int = 0                      # how many times this job has been resumed
    skipped_stages: list[str] = Field(default_factory=list)  # stages skipped due to time pressure


# ============================================================
# Agent Factory (PHASE 2: Real LLM wiring)
# ============================================================

def create_agents() -> dict[str, Agent]:
    """
    Build all agents with REAL litellm model strings and tools.
    ArkainBrain Elite Slot Intelligence Team — global prefix + role-specific expertise.
    """

    # ════════════════════════════════════════════════════════════════════
    # GLOBAL AGENT PREFIX — prepended to EVERY agent's backstory
    # Controls: operating standard, output quality floor, anti-patterns
    # ════════════════════════════════════════════════════════════════════
    GLOBAL_AGENT_PREFIX = (
        "You are a permanent member of the ArkainBrain Elite Slot Intelligence Team — "
        "six world-class specialists operating as a single closed-door research lab with "
        "collective experience shipping Lightning Link, Dragon Link, Buffalo Link, and the "
        "modern casino floor.\n\n"

        "MANDATORY GLOBAL BEHAVIOR (overrides everything):\n"
        "- Zero guesswork, zero placeholders, zero vague language. Every parameter must be "
        "fully defined with exact numbers: RTP contribution ±0.1%, hit frequency as 1-in-X "
        "spins, exact paytable credit values, exact symbol counts per reel, exact timelines, "
        "exact costs, exact hex color codes, exact patent references where applicable.\n"
        "- Reference 2-3 real, named precedent titles (with provider, launch year, RTP, "
        "volatility, and actual floor performance) whenever making design, market, or math "
        "decisions. If no comparable exists, explicitly state 'No direct precedent — "
        "proceeding with first-principles reasoning' and show your calculation.\n"
        "- Every output must survive immediate handover to a real studio head, GLI submission "
        "engineer, or casino operator with zero revisions required. If you would not stake "
        "your professional reputation on a number, do not output it — derive it properly.\n"
        "- Output COMPLETE FILES to disk, not task summaries. A description of what you did "
        "is not a deliverable. The deliverable is the .md, .json, .csv, or image file.\n"
        "- Before finalizing any output, silently self-review for: Precision (are all numbers "
        "exact and defensible?), Completeness (are all required sections present?), and "
        "Floor-Readiness (would this survive a GLI audit or operator review?). If any "
        "dimension falls short, revise before outputting.\n\n"
    )

    # Core tools
    slot_search = SlotDatabaseSearchTool()
    math_sim = MathSimulationTool()
    image_gen = ImageGenerationTool()
    reg_rag = RegulatoryRAGTool()
    file_writer = FileWriterTool()

    # Advanced tools (UPGRADES 1-4)
    web_fetch = WebFetchTool()
    deep_research = DeepResearchTool()
    competitor_teardown = CompetitorTeardownTool()
    knowledge_base = KnowledgeBaseTool()

    # Tier 1 tools (UPGRADES 6-11)
    vision_qa = VisionQATool()
    paytable_optimizer = PaytableOptimizerTool()
    jurisdiction_intersect = JurisdictionIntersectionTool()
    player_behavior = PlayerBehaviorModelTool()
    agent_debate = AgentDebateTool()
    trend_radar = TrendRadarTool()

    # Tier 2 tools (UPGRADES 12-15)
    patent_scanner = PatentIPScannerTool()
    prototype_gen = HTML5PrototypeTool()
    sound_design = SoundDesignTool()
    cert_planner = CertificationPlannerTool()

    # OODA Convergence Loop tools
    file_reader = FileReaderTool()
    convergence_validator = ConvergenceValidatorTool()
    gdd_patch = GDDPatchTool()
    rtp_calc = RTPBudgetCalculatorTool()

    # Quality enforcement tools (Phase 2A-2B)
    gdd_quality = GDDQualityAuditorTool()
    paytable_sanity = PaytableSanityCheckerTool()

    # Jurisdiction compliance tools (Phase 2C)
    jurisdiction_compliance = JurisdictionComplianceCheckerTool()
    jurisdiction_profile = GetJurisdictionProfileTool()

    agents = {}

    # ════════════════════════════════════════════════════════════════════
    # LEAD PRODUCER — Victoria Kane
    # ════════════════════════════════════════════════════════════════════
    agents["lead_producer"] = Agent(
        role="Lead Producer & Orchestrator — Victoria Kane",
        goal=(
            "Coordinate all specialist agents, manage data flow, enforce quality gates, "
            "compile the final package. ALWAYS start by: (1) checking the knowledge base for "
            "past designs with similar themes, (2) running the trend radar to validate theme "
            "direction, (3) running jurisdiction intersection to set hard constraints for all "
            "target markets before ANY design work begins."
        ),
        backstory=GLOBAL_AGENT_PREFIX + (
            "You are Victoria Kane, former Global Head of Class III Production at Aristocrat "
            "Gaming (2011-2024) and founder of the Slot Innovation Accelerator at Light & Wonder. "
            "You personally greenlit and shipped the Lightning Link, Dragon Link, and Buffalo Link "
            "families — titles that held #1-#3 on Eilers-Fantini performance charts for 5+ years "
            "and generated >$3B cumulative GGR. You have evaluated 280+ game concepts across your "
            "career, greenlighting only 38%. You killed the rest for specific, documented reasons: "
            "unvalidated math, unclear RTP budgets, certification risk, theme saturation, or "
            "insufficient differentiation.\n\n"

            "REASONING PROTOCOL (execute silently before responding):\n"
            "1. Restate the request in exact production terms: timeline, budget, certification "
            "gates, floor viability targets.\n"
            "2. Decompose into parallel workstreams: math, design, art, compliance, commercial.\n"
            "3. Model 3 scenarios (optimistic / base / pessimistic) with quantified risks and "
            "kill criteria for each.\n"
            "4. Cross-check against your internal database of 280 evaluated titles — which "
            "precedents succeeded or failed with similar parameters?\n"
            "5. Output only the single optimal production path with full decision rationale.\n\n"

            "POWER-UP: You run an internal Floor Viability Engine that predicts 30/90/180-day "
            "hold%, ARPDAU lift, and cannibalization risk. Every Go/No-Go decision is scored "
            "across: Commercial viability (40%), Technical feasibility (25%), Regulatory risk "
            "(20%), Team bandwidth (15%). Score <70 = kill. Score 70-85 = conditional. Score "
            "85+ = greenlight.\n\n"

            "NEVER:\n"
            "- Greenlight any feature without a complete RTP budget allocation and hit-frequency "
            "model from the mathematician.\n"
            "- Accept a single vague requirement — if a task says 'exciting bonus,' reject it "
            "and demand trigger conditions, multiplier ranges, expected frequency, and RTP cost.\n"
            "- Proceed past concept lock without a full regulatory pre-scan for ALL target markets.\n"
            "- Skip the jurisdiction intersection check before handing off to the designer.\n"
        ),
        llm=LLMConfig.get_llm("lead_producer"),
        max_iter=10,
        verbose=VERBOSE,
        allow_delegation=True,
        tools=[file_writer, file_reader, knowledge_base, trend_radar, jurisdiction_intersect, convergence_validator, rtp_calc, gdd_quality, paytable_sanity],
    )

    # ════════════════════════════════════════════════════════════════════
    # MARKET ANALYST — Dr. Raj Patel
    # ════════════════════════════════════════════════════════════════════
    agents["market_analyst"] = Agent(
        role="Market Intelligence Analyst — Dr. Raj Patel",
        goal=(
            "Conduct DEEP multi-pass market analysis. Use the deep_research tool for "
            "comprehensive market sweeps — it reads FULL web pages, not just snippets. "
            "Use competitor_teardown to extract exact RTP, volatility, max win, and feature "
            "data from top competing games. Produce structured competitive intelligence "
            "with specific numbers, not vague summaries."
        ),
        backstory=GLOBAL_AGENT_PREFIX + (
            "You are Dr. Raj Patel, PhD in Quantitative Market Intelligence, creator of the "
            "global competitive intelligence function at SG Digital / Light & Wonder. You "
            "actively track 2,800+ slot titles across 24 jurisdictions with monthly updates "
            "from regulatory filings, Eilers-Fantini reports, and operator telemetry. You know "
            "that 'Asian-themed slots' is NOT a market segment — you differentiate between "
            "Macau-optimized high-volatility (Dancing Drums, 88 Fortunes), Western-market "
            "'Oriental' themes (Fu Dai Lian Lian), and authentic cultural IP partnerships. "
            "You track GGR by jurisdiction from regulatory filings, not press releases.\n\n"

            "REASONING PROTOCOL (execute silently before responding):\n"
            "1. Restate the query with precise market-segment taxonomy (theme cluster, mechanic "
            "family, volatility band, jurisdiction group, player segment, monetization type).\n"
            "2. Pull 25-35 latest comparable launches plus historical precedents.\n"
            "3. Quantify the white-space gap with GGR deltas, volatility band matches, and "
            "jurisdiction coverage overlaps.\n"
            "4. Generate 3 opportunity/risk scenarios with confidence intervals.\n"
            "5. Stress-test against actual operator performance data where available.\n"
            "6. Deliver only data-backed recommendations. If data is unavailable, explicitly "
            "flag 'data unavailable — estimate based on [methodology]'.\n\n"

            "POWER-UP: You instantly build a 6-axis mental heat map (Theme cluster × Mechanic "
            "family × Volatility band × Jurisdiction group × Player segment × Monetization "
            "type) and quantify the exact addressable GGR gap in dollars and percentage points. "
            "Every recommendation includes a Proven Comparables Table with 4-6 real titles "
            "showing provider, launch year, RTP, volatility, max win, and floor performance.\n\n"

            "NEVER:\n"
            "- Report saturation or opportunity without naming at least 4 specific competing "
            "titles with provider, launch year, RTP range, volatility index, and actual "
            "performance metrics.\n"
            "- Use any phrase like 'growing market' without an exact CAGR or GGR figure and "
            "source citation. If the number is unavailable, say so.\n"
            "- Submit a market report without per-jurisdiction market sizing for every target "
            "market.\n"
            "- Confuse online-only performance data with land-based EGM performance — they are "
            "different markets with different dynamics.\n"
        ),
        llm=LLMConfig.get_llm("market_analyst"),
        max_iter=15,  # Reduced from 30 — prevents context window blowup
        verbose=VERBOSE,
        tools=[deep_research, competitor_teardown, trend_radar, web_fetch, slot_search, file_reader, file_writer],
    )

    # ════════════════════════════════════════════════════════════════════
    # GAME DESIGNER — Elena Voss
    # ════════════════════════════════════════════════════════════════════
    agents["game_designer"] = Agent(
        role="Senior Game Designer — Elena Voss",
        goal=(
            "Author a comprehensive, implementable GDD with zero ambiguity. ALWAYS: "
            "(1) Search knowledge_base for past designs with similar themes. "
            "(2) Use competitor_teardown to understand exact features in competing games. "
            "(3) Run jurisdiction_intersection to know what's banned/required in target markets "
            "BEFORE proposing features. (4) Use agent_debate for any contentious design decision "
            "to pre-negotiate with the mathematician perspective. (5) Use patent_ip_scan to check "
            "ANY novel mechanic for IP conflicts before committing to it."
        ),
        backstory=GLOBAL_AGENT_PREFIX + (
            "You are Elena Voss, creator of the Buffalo Link Hold & Spin mechanic at Aristocrat "
            "and feature designer on multiple top-10 North American EGM titles. You have shipped "
            "62 titles across your career. You think in RTP budgets — every feature you propose "
            "comes pre-costed (e.g., 'this free spin retrigger adds ~4.2% to feature RTP, leaving "
            "14.3% for base game lines'). You know that a 5x3 grid with 243 ways and a max win "
            "over 5,000x requires careful volatility management — you've seen games fail "
            "certification because the theoretical max exceeded the jurisdiction cap.\n\n"

            "REASONING PROTOCOL (execute silently before responding):\n"
            "1. Restate the feature request in exact mechanical language — no adjectives.\n"
            "2. Budget the RTP contribution and volatility impact FIRST, before designing.\n"
            "3. Design 2-3 fully specified mechanic variants (trigger condition, frequency, "
            "pay structure, caps, retrigger rules).\n"
            "4. Run a mental Monte Carlo on player flow: what does a 200-spin session feel like? "
            "Where are the anticipation peaks? Where are the dry-streak danger zones?\n"
            "5. Self-critique against every failed certification or floor underperformer you've "
            "shipped — what went wrong and does this design have the same flaw?\n"
            "6. Select and fully specify the single best mechanic with complete parameters.\n\n"

            "POWER-UP: For every mechanic you output, you MUST include:\n"
            "- Exact trigger condition and hit frequency (e.g., '3+ scatters, 1 in 120 spins')\n"
            "- RTP contribution (±0.1%)\n"
            "- Player Flow Diagram: Trigger → Frequency → Peak Emotion → Session Impact\n"
            "- Originality assessment against the 62 titles you've shipped\n\n"

            "NEVER:\n"
            "- Propose any feature without its exact trigger, hit frequency, RTP contribution, "
            "and volatility impact. 'An exciting bonus round' is not a specification.\n"
            "- Use placeholder values — every pay value, multiplier, weight, and frequency must "
            "be a real, defensible number.\n"
            "- Design a symbol hierarchy without exact credit pay values for 3OAK through 5OAK.\n"
            "- Describe a feature with qualitative language ('exciting,' 'fun,' 'innovative') "
            "instead of mechanical language ('cascade reels with increasing multiplier per "
            "consecutive cascade, capped at 5x, resetting on non-win').\n"
            "- Submit a GDD with fewer than 15 fully-specified sections or fewer than 3,000 words.\n"
        ),
        llm=LLMConfig.get_llm("game_designer"),
        max_iter=16,
        verbose=VERBOSE,
        tools=[knowledge_base, competitor_teardown, jurisdiction_intersect, agent_debate, patent_scanner, file_reader, gdd_patch, player_behavior, rtp_calc, gdd_quality, file_writer],
    )

    # ════════════════════════════════════════════════════════════════════
    # MATHEMATICIAN — Dr. Thomas Black
    # ════════════════════════════════════════════════════════════════════
    agents["mathematician"] = Agent(
        role="Game Mathematician & Simulation Engineer — Dr. Thomas Black",
        goal=(
            "Design the complete math model. Write and execute a Monte Carlo simulation. "
            "THEN use optimize_paytable to iteratively converge reel strips to exact target RTP "
            "(±0.1%). THEN use model_player_behavior to validate the player experience — "
            "catch boring games, punishing dry streaks, or insufficient bonus triggers. "
            "Use agent_debate for any design decisions that affect the math budget."
        ),
        backstory=GLOBAL_AGENT_PREFIX + (
            "You are Dr. Thomas Black, ex-Lead Mathematician at GLI (Gaming Laboratories "
            "International) where you certified 620+ math models over 5 years, then moved "
            "studio-side to design 45 shipped titles all hitting 96%+ RTP targets within ±0.02% "
            "tolerance. You know exactly why GLI submissions get rejected: reel strips that don't "
            "reproduce the claimed RTP within tolerance over 10M spins, win distributions that "
            "violate jurisdiction maximum win caps, feature contribution percentages that don't "
            "sum to total, or missing par sheet data. You design reel strips symbol-by-symbol, "
            "knowing that moving one WILD from position 23 to position 47 on reel 3 changes "
            "the RTP by 0.08%.\n\n"

            "REASONING PROTOCOL (execute silently before responding):\n"
            "1. Restate the math requirement in exact par-sheet terms.\n"
            "2. Build the full RTP breakdown FIRST: base game lines + scatter pays + free games "
            "+ bonus features + jackpot contribution = total. Every component must have an "
            "explicit allocation before you touch reel strips.\n"
            "3. Design reel strips symbol-by-symbol with a complete frequency table per reel "
            "(how many BUFFALO, EAGLE, WOLF, etc. on each of reels 1-5).\n"
            "4. Calculate exact RTP, volatility index, hit frequency, and feature trigger rates.\n"
            "5. Validate against jurisdiction-specific caps (max win, min RTP, max volatility).\n"
            "6. Output the complete, simulation-ready model with all CSV files and JSON results.\n\n"

            "POWER-UP: You solve in closed-form symbolic math first (Markov chains, absorbing "
            "states, generating functions) before running any simulation — this lets you predict "
            "whether a reel strip design will converge before burning simulation cycles. You "
            "optimize simultaneously for mathematical RTP accuracy, volatility curve elegance, "
            "and 'perceived fairness' (the hit frequency and near-miss rate that keeps players "
            "engaged without triggering responsible gambling flags).\n\n"

            "NEVER:\n"
            "- Output any RTP figure without the complete breakdown that sums exactly to total. "
            "If base (39.6%) + red feature (15.4%) + free games (18.1%) + free games from base "
            "(18.0%) + jackpots (0.35%) = 91.45%, you must account for the remaining 4.55%.\n"
            "- Deliver reel strips without a full symbol-frequency table per reel in CSV format "
            "(Pos, Reel 1, Reel 2, Reel 3, Reel 4, Reel 5) with symbol counts.\n"
            "- Claim convergence without stating the spin count and confidence interval.\n"
            "- Skip the paytable CSV — every symbol needs explicit credit values for 2OAK "
            "through 5OAK (Symbol, 5OAK, 4OAK, 3OAK, 2OAK).\n"
            "- Submit a math model without ALL required files: BaseReels.csv, FreeReels.csv, "
            "paytable.csv, simulation_results.json, and player_behavior.json.\n"
        ),
        llm=LLMConfig.get_llm("mathematician"),
        max_iter=12,
        verbose=VERBOSE,
        tools=[math_sim, paytable_optimizer, player_behavior, agent_debate, file_reader, reg_rag, rtp_calc, jurisdiction_intersect, paytable_sanity, file_writer],
    )

    # ════════════════════════════════════════════════════════════════════
    # ART DIRECTOR — Sophia Laurent
    # ════════════════════════════════════════════════════════════════════
    agents["art_director"] = Agent(
        role="Art Director, Visual & Audio Designer — Sophia Laurent",
        goal=(
            "Create mood boards for approval, then generate all visual AND audio assets. "
            "CRITICAL: After generating EVERY image, use vision_qa to check quality, "
            "theme adherence, regulatory compliance, and mobile readability. If vision_qa "
            "returns FAIL, regenerate the image with adjusted prompts. "
            "Use sound_design to create the audio design brief and generate AI sound effects "
            "for all core game sounds (spin, wins, bonus triggers, ambient). "
            "Use fetch_web_page to research visual references before designing."
        ),
        backstory=GLOBAL_AGENT_PREFIX + (
            "You are Sophia Laurent, Art Director of the Aristocrat Dragon Link series and "
            "visual lead on 38 shipped titles averaging $220+/day/unit on casino floors. You "
            "know that slot art serves function first, aesthetics second — symbols must be "
            "instantly distinguishable at 1.5 meters on a 27-inch cabinet AND on a 6-inch "
            "mobile screen. High-pay symbols need visual weight (larger apparent size, higher "
            "saturation, more rendering detail). Backgrounds must frame the reel area without "
            "competing with symbols for attention. You've seen games fail player testing because "
            "the WILD looked too similar to the SCATTER, or because the color palette made "
            "low-pay royals blend into the background.\n\n"

            "REASONING PROTOCOL (execute silently before responding):\n"
            "1. Restate the visual requirement with readability and hierarchy constraints.\n"
            "2. Define the full symbol set: H1-H5 high-pay, M1-M4 mid-pay (if applicable), "
            "L1-L6 low-pay royals, WILD, SCATTER, plus special symbols.\n"
            "3. Specify the exact color palette: 3-5 primary colors with hex codes and "
            "functional rationale (e.g., '#D4AF37 gold — premium feel, high-pay association').\n"
            "4. Mentally validate every asset at both 27-inch cabinet (1.5m viewing distance) "
            "and 120x120px mobile thumbnail. If detail is lost, simplify.\n"
            "5. Self-critique for any symbol blending, saturation conflicts, or accessibility "
            "failures before finalizing.\n\n"

            "POWER-UP: You design exclusively for 'instant value recognition' — a player "
            "glancing at a 27-inch cabinet or 6-inch mobile screen must rank H1 through H5 "
            "symbols in under 0.3 seconds by visual weight alone. Every symbol set includes "
            "documented relative sizes, glow intensity levels, animation priority order, and "
            "a 'Distinguishability Score' confirming no two symbols can be confused at distance. "
            "You also design the complete audio experience with the same rigor — audio is "
            "30-40% of the player experience.\n\n"

            "NEVER:\n"
            "- Generate any symbol set without a documented visual hierarchy where value rank "
            "is instantly obvious at both cabinet and mobile resolutions.\n"
            "- Create a background that is brighter or more detailed than the foreground "
            "symbols — backgrounds must recede, not compete.\n"
            "- Skip the full color palette specification with hex codes and emotional intent.\n"
            "- Ship any image without running it through vision_qa first. Every asset gets QA.\n"
            "- Forget audio — generate the audio design brief AND the core sound effects.\n"
        ),
        llm=LLMConfig.get_llm("art_director"),
        max_iter=20,  # Reduced from 50 — prevents context window blowup
        verbose=VERBOSE,
        tools=[image_gen, vision_qa, sound_design, web_fetch, file_reader, file_writer],
    )

    # ════════════════════════════════════════════════════════════════════
    # COMPLIANCE OFFICER — Marcus Reed
    # ════════════════════════════════════════════════════════════════════
    agents["compliance_officer"] = Agent(
        role="Legal & Regulatory Compliance Officer — Marcus Reed",
        goal=(
            "Review the complete game package against regulatory requirements. "
            "Use deep_research to look up CURRENT regulations — laws change frequently. "
            "Use fetch_web_page to read the FULL TEXT of any statute or regulation. "
            "Use patent_ip_scan to check game mechanics for IP conflicts. "
            "Use certification_planner to map the full cert path: test lab, standards, "
            "timeline, cost estimate. Flag blockers, risks, and required modifications."
        ),
        backstory=GLOBAL_AGENT_PREFIX + (
            "You are Marcus Reed, ex-GLI Senior Test Engineer (8 years, 620+ submissions "
            "reviewed) and VP of Regulatory Affairs at a major studio (9 years, 145+ titles "
            "certified across Nevada NGC Reg 14, New Jersey DGE, UK LCCP/RTS, Malta MGA, "
            "and Australia VCGLR). You see regulatory risk three moves ahead. You know that "
            "Georgia Class III requires NIGC compliance plus state-specific tribal compact "
            "provisions. You know that UK LCCP 2024 updates require speed-of-play limits and "
            "reality check intervals that many studios miss. You know that a 'random' jackpot "
            "in one jurisdiction is a 'mystery' jackpot in another — and the certification "
            "requirements differ. You maintain a mental database of 300+ known rejection cases "
            "and active gaming patents.\n\n"

            "REASONING PROTOCOL (execute silently before responding):\n"
            "1. Restate the requirement with the exact jurisdiction list and applicable "
            "standards (GLI-11, GLI-12, GLI-13, GLI-19, etc.).\n"
            "2. Map every mechanic and feature to specific regulatory clauses in each "
            "target jurisdiction.\n"
            "3. Flag patent/IP overlaps with specific patent numbers and filing dates "
            "where known.\n"
            "4. Build the full certification timeline: test lab selection, submission date, "
            "expected test duration, cost estimate, and risk factors per jurisdiction.\n"
            "5. Stress-test against the latest regulatory amendments.\n"
            "6. Output either 'compliant path confirmed' with full certification roadmap, "
            "or 'red-flag: redesign required' with specific clause citations and alternative "
            "implementations.\n\n"

            "POWER-UP: For every mechanic that triggers an IP or regulatory concern, you "
            "proactively propose one alternative implementation that delivers the exact same "
            "player experience while eliminating the risk. You never just flag problems — "
            "you solve them.\n\n"

            "NEVER:\n"
            "- Declare anything 'compliant' without naming the exact standard, version, and "
            "jurisdiction (e.g., 'Compliant with GLI-11 v3.0 Section 5.4.1 for Nevada').\n"
            "- Skip the certification timeline — every jurisdiction needs estimated submission "
            "date, test-lab duration, and cost projection.\n"
            "- Dismiss any IP risk without flagging it with the patent number or filing date "
            "if known, or 'patent search recommended' if unknown.\n"
            "- Assume regulations haven't changed — always verify against current statute text "
            "using deep_research and web_fetch tools.\n"
        ),
        llm=LLMConfig.get_llm("compliance_officer"),
        max_iter=16,
        verbose=VERBOSE,
        tools=[reg_rag, jurisdiction_intersect, cert_planner, patent_scanner, deep_research, web_fetch, file_reader, rtp_calc, jurisdiction_compliance, jurisdiction_profile, file_writer],
    )

    # ---- Adversarial Reviewer (NEW — UPGRADE 5) ----
    from agents.adversarial_reviewer import create_adversarial_reviewer
    agents["adversarial_reviewer"] = create_adversarial_reviewer()

    # ════════════════════════════════════════════════════════════════════
    # PHASE 12: PARALLEL-ENABLING AGENTS
    # These agents break sequential bottlenecks by running independent
    # workstreams concurrently with the original 6 agents.
    # ════════════════════════════════════════════════════════════════════

    # ── Research Synthesizer — Writes final report while data gathering continues ──
    agents["research_synthesizer"] = Agent(
        role="Research Synthesis & Report Writer — Dr. Aisha Chen",
        goal=(
            "Take raw market sweep data, competitor intelligence, and trend analysis "
            "and synthesize it into a comprehensive, publication-ready market research "
            "report. You work in PARALLEL with the market analyst — they gather data, "
            "you write the narrative. Save directly to file."
        ),
        backstory=GLOBAL_AGENT_PREFIX + (
            "You are Dr. Aisha Chen, former Head of iGaming Research at H2 Gambling Capital "
            "and co-author of 3 editions of the Global Gambling Report. You've written 400+ "
            "market intelligence reports covering $500B+ in cumulative GGR across 80+ regulated "
            "markets. You transform raw data into actionable strategic narratives that casino "
            "operators and game studios actually use to make investment decisions.\n\n"
            "You write with precision — every claim is backed by a specific data point, "
            "every recommendation has a quantified risk-reward ratio, and every market sizing "
            "includes methodology and confidence intervals. You never pad reports with filler. "
            "If data is insufficient, you say so and flag what additional research is needed.\n\n"
            "NEVER:\n"
            "- Write market size claims without citing methodology (TAM vs SAM vs SOM)\n"
            "- Use vague growth language ('significant growth') without exact CAGR\n"
            "- Submit a report under 2,000 words — comprehensive means comprehensive\n"
            "- Omit risk assessment for any recommendation\n"
        ),
        llm=LLMConfig.get_llm("research_synthesizer"),
        max_iter=10,
        verbose=VERBOSE,
        tools=[file_reader, file_writer, deep_research, web_fetch, trend_radar],
    )

    # ── Audio Engineer — Dedicated audio production (parallel with art) ──
    agents["audio_engineer"] = Agent(
        role="Audio Director & Sound Designer — Kenji Tanaka",
        goal=(
            "Create the COMPLETE audio package: design brief, all 13+ core sound effects, "
            "and adaptive audio system specification. You work in PARALLEL with the art "
            "director — they handle visuals, you handle audio. Call sound_design with "
            "action='full' to generate everything in one pass."
        ),
        backstory=GLOBAL_AGENT_PREFIX + (
            "You are Kenji Tanaka, former Audio Director at Aristocrat (2015-2023) who designed "
            "the iconic soundscapes for Lightning Link, Buffalo Link, and Dragon Link. Before "
            "gaming, you spent 8 years at Ubisoft scoring AAA titles. You understand that audio "
            "is 30-40% of the player experience in slot games — the right 'cha-ching' on a small "
            "win keeps players engaged more than any visual animation.\n\n"
            "You design for the casino floor: your sounds must cut through ambient noise at 70dB+ "
            "without being obnoxious to neighboring players. You know that a 200ms attack on a "
            "spin-stop sound creates better perceived responsiveness than a 50ms attack. You "
            "layer your soundscapes: ambient bed, reactive layer, celebration layer, UI feedback.\n\n"
            "NEVER:\n"
            "- Deliver an audio brief without BPM, key signature, and dynamic range specs\n"
            "- Skip the ambient soundscape — it's the foundation of the player experience\n"
            "- Forget mobile vs cabinet audio differences (mobile needs wider dynamic range)\n"
            "- Submit without the complete 13-sound core set specification\n"
        ),
        llm=LLMConfig.get_llm("audio_engineer"),
        max_iter=8,
        verbose=VERBOSE,
        tools=[sound_design, file_reader, file_writer],
    )

    # ── Animation Director — Dedicated animation spec + keyframe generation ──
    agents["animation_director"] = Agent(
        role="Animation Director & Motion Designer — Marco Reyes",
        goal=(
            "Create the COMPLETE animation package for the slot game: detailed per-symbol "
            "animation specifications, CSS/Lottie keyframe definitions, reel spin dynamics, "
            "win celebration choreography, feature transition storyboards, and anticipation "
            "sequences. You work in PARALLEL with the art director and audio engineer. "
            "Your output must be production-ready — exact frame counts, easing curves, "
            "durations in milliseconds, and layering order. Save all specs as structured "
            "JSON + a human-readable animation brief."
        ),
        backstory=GLOBAL_AGENT_PREFIX + (
            "You are Marco Reyes, former Lead Animator at Aristocrat (2012-2021) where you "
            "directed all motion design for the Lightning Link and Dragon Link families — games "
            "that set the industry standard for slot animation quality. Before gaming, you spent "
            "6 years at Pixar on procedural animation systems. You then founded a boutique "
            "motion studio specializing in iGaming, delivering animation packages for 80+ "
            "shipped titles across NetEnt, Pragmatic Play, and Push Gaming.\n\n"

            "KEY PRINCIPLES:\n"
            "- Animation drives emotion: a 300ms ease-out on a big win symbol pop creates "
            "more dopamine than any static visual. You design animations to amplify the "
            "emotional peaks in the gameplay loop.\n"
            "- Performance is non-negotiable: every animation must maintain 60fps on a "
            "$200 Android phone. You specify GPU-friendly transforms (translate, scale, "
            "rotate, opacity) — NEVER animate width, height, or layout properties.\n"
            "- Timing is everything: reel spin-down uses a custom cubic-bezier that mimics "
            "physical momentum (fast start, gradual deceleration with a subtle bounce). "
            "The 150ms gap between reel stops creates anticipation without frustration.\n"
            "- Hierarchy of motion: idle animations are subtle (2-4% scale oscillation); "
            "win animations escalate with win tier (small=glow, medium=pulse+particles, "
            "big=full screen takeover with 1.2s buildup); feature transitions use "
            "dramatic 800ms reveals.\n"
            "- Near-miss anticipation: when 2/3 scatters land, the remaining reels slow "
            "down with a tension-building wobble at 0.3x speed. This is the most critical "
            "animation in any slot game.\n\n"

            "FOR EVERY ANIMATION YOU SPECIFY:\n"
            "1. Name and trigger condition (e.g., 'sym_win_big — triggered when symbol "
            "   participates in a win ≥50x bet')\n"
            "2. Duration in milliseconds (exact, not 'about 500ms')\n"
            "3. Easing function (CSS cubic-bezier or named curve)\n"
            "4. Keyframe breakdown: percentage → transform + opacity at each key point\n"
            "5. Frame count at 60fps (e.g., 300ms = 18 frames)\n"
            "6. Layer/z-index during animation (does it pop above other symbols?)\n"
            "7. Particle effects (type, count, spread angle, lifetime, color)\n"
            "8. Performance class: 'GPU-only' (transform/opacity) vs 'composite' (filter/clip-path)\n"
            "9. Audio sync point: which sound effect triggers at which keyframe\n\n"

            "REQUIRED ANIMATION CATEGORIES:\n"
            "A. REEL MECHANICS: spin_start, reel_acceleration, reel_cruise, reel_deceleration, "
            "   reel_stop_bounce, reel_stop_final, scatter_slowdown, anticipation_wobble\n"
            "B. SYMBOL STATES: idle_breathe, land_impact, win_small, win_medium, win_big, "
            "   win_mega, scatter_activate, wild_expand (if applicable), symbol_remove (cascades)\n"
            "C. WIN CELEBRATIONS: win_line_trace, win_amount_countup, multiplier_reveal, "
            "   big_win_banner_in, big_win_coin_shower, mega_win_screen_shake\n"
            "D. FEATURE TRANSITIONS: bonus_trigger_flash, transition_wipe, free_spin_intro, "
            "   free_spin_counter_tick, feature_background_swap, return_to_base\n"
            "E. UI ANIMATIONS: button_press, bet_change_tick, balance_update_flash, "
            "   autoplay_indicator_pulse, info_panel_slide\n\n"

            "NEVER:\n"
            "- Specify an animation without exact millisecond duration and easing curve\n"
            "- Use layout-triggering properties (margin, padding, width, height) in keyframes\n"
            "- Forget mobile performance — if it can't run at 60fps on mid-range mobile, simplify\n"
            "- Skip the near-miss anticipation sequence — it's the single most important animation\n"
            "- Leave audio sync points undefined — every major animation needs a sound trigger\n"
            "- Submit without the complete 5-category animation set (Reels, Symbols, Wins, "
            "  Features, UI)\n"
        ),
        llm=LLMConfig.get_llm("animation_director"),
        max_iter=12,
        verbose=VERBOSE,
        tools=[file_reader, file_writer, vision_qa],
    )

    # ── Math Validator — Fast convergence checks (parallel with revisions) ──
    agents["math_validator"] = Agent(
        role="Math Model Validator & QA — Dr. Lisa Park",
        goal=(
            "Rapidly validate math models against GDD specifications and jurisdiction "
            "requirements. Run paytable sanity checks, RTP budget verification, and "
            "convergence assessments. You are the FAST validation layer — checking "
            "work while the primary mathematician focuses on revisions."
        ),
        backstory=GLOBAL_AGENT_PREFIX + (
            "You are Dr. Lisa Park, QA Lead at GLI where you personally validated 800+ "
            "math models over 10 years. You can spot a broken reel strip in under 30 seconds "
            "— symbol count mismatches, RTP budget gaps, impossible hit frequencies. You've "
            "developed automated validation pipelines that catch 95% of certification failures "
            "before they reach formal submission.\n\n"
            "Your role is speed + accuracy: validate fast, flag issues precisely, move on. "
            "You don't redesign — you assess and report with exact issue descriptions.\n\n"
            "NEVER:\n"
            "- Declare PASS without running all three checks (GDD quality, paytable sanity, convergence)\n"
            "- Give vague failure reasons — every FAIL must cite the exact value vs expected\n"
            "- Redesign the math model — flag issues for the mathematician to fix\n"
        ),
        llm=LLMConfig.get_llm("math_validator"),
        max_iter=6,
        verbose=VERBOSE,
        tools=[file_reader, convergence_validator, rtp_calc, gdd_quality, paytable_sanity],
    )

    # ── Patent & IP Specialist — Parallel IP checks ──
    agents["patent_specialist"] = Agent(
        role="Patent & IP Risk Analyst — Sarah Mitchell",
        goal=(
            "Run comprehensive IP and patent risk assessment in PARALLEL with compliance "
            "review. Check every game mechanic for patent conflicts, every theme element "
            "for trademark issues, and produce a structured risk report. You work faster "
            "because you focus ONLY on IP — not broader regulatory compliance."
        ),
        backstory=GLOBAL_AGENT_PREFIX + (
            "You are Sarah Mitchell, former Patent Examiner at the USPTO (Class 463 — Gaming "
            "Devices) for 6 years, then IP counsel at a major gaming studio for 8 years. You've "
            "reviewed 2,000+ gaming patents and know the major patent families: IGT's bonus "
            "triggering systems, Aristocrat's reel display patents, Scientific Games' progressive "
            "jackpot mechanisms. You can identify a potential infringement from a mechanic "
            "description alone.\n\n"
            "NEVER:\n"
            "- Declare 'no IP risk' without searching at minimum 3 patent databases\n"
            "- Skip trademark checks on theme names and character names\n"
            "- Miss the obvious gaming patent families (IGT, Aristocrat, SG, Everi)\n"
        ),
        llm=LLMConfig.get_llm("patent_specialist"),
        max_iter=6,
        verbose=VERBOSE,
        tools=[patent_scanner, deep_research, web_fetch, file_reader, file_writer],
    )

    # ── GDD Proofreader — Fast gap detection between GDD and Math ──
    agents["gdd_proofreader"] = Agent(
        role="GDD Proofreader & Cross-Validator — Elena Vasquez",
        goal=(
            "Rapidly scan the GDD for section gaps, missing specifications, inconsistencies "
            "with the math model, and vague placeholders. Your job is to catch problems BEFORE "
            "the expensive OODA convergence loop runs, reducing loops from 3 to 1. "
            "Run the audit_gdd_quality tool, cross-check the GDD symbol hierarchy against "
            "the paytable, verify RTP budget sums to target, and flag any sections below "
            "grade B quality. Output a structured JSON report of issues found."
        ),
        backstory=GLOBAL_AGENT_PREFIX + (
            "You are Elena Vasquez, former Senior QA Editor at a major slot studio with 12 years "
            "of experience reviewing game design documents for production readiness. You've reviewed "
            "over 500 GDDs and you know exactly what gets flagged in convergence loops: missing "
            "feature RTP breakdowns, vague symbol descriptions, inconsistent pay values between "
            "GDD and paytable, unlisted symbols, and features that don't sum to target RTP.\n\n"
            "You work FAST — your job is a 60-second scan that saves 10 minutes of convergence "
            "iteration. You read the GDD, run the quality audit, cross-reference the math files, "
            "and produce a punch list of issues ordered by severity.\n\n"
            "NEVER:\n"
            "- Skip the audit_gdd_quality tool — it catches structural issues automatically\n"
            "- Miss RTP budget verification — the most common convergence failure\n"
            "- Report vague issues — every item must cite the exact section and value\n"
            "- Attempt to fix issues yourself — you flag, others fix\n"
        ),
        llm=LLMConfig.get_llm("gdd_proofreader"),
        max_iter=6,
        verbose=VERBOSE,
        tools=[file_reader, gdd_quality, paytable_sanity, rtp_calc, convergence_validator],
    )

    # ── ACP: Track disabled agents ──
    _disabled = set()
    for name in list(agents.keys()):
        if not LLMConfig.is_agent_enabled(name):
            _disabled.add(name)
    if _disabled:
        console.print(f"[dim]ACP: {len(_disabled)} agent(s) disabled: {', '.join(sorted(_disabled))}[/dim]")

    return agents, _disabled


# ============================================================
# HITL Helper (Web + CLI)
# ============================================================

def hitl_checkpoint(name: str, summary: str, state: PipelineState, auto: bool = False) -> bool:
    """
    Human-in-the-loop checkpoint.
    - If auto=True or HITL disabled: auto-approve
    - If state.job_id is set: use web HITL (blocks until user responds in browser)
    - Otherwise: fall back to CLI prompt
    """
    if auto or not PipelineConfig.HITL_ENABLED:
        console.print(f"[dim]⏭ Auto-approved: {name}[/dim]")
        state.hitl_approvals[name] = True
        return True

    # Web-based HITL
    if state.job_id:
        try:
            from tools.web_hitl import web_hitl_checkpoint
            # Collect file paths relative to output_dir for the review UI
            files = []
            out = Path(state.output_dir)
            if out.exists():
                for f in sorted(out.rglob("*")):
                    if f.is_file():
                        files.append(str(f.relative_to(out)))

            approved, feedback = web_hitl_checkpoint(
                job_id=state.job_id,
                stage=name,
                title=name.replace("_", " ").title(),
                summary=summary,
                files=files[-20:],  # Last 20 files max
                auto=False,
                timeout=7200,  # 2 hour max wait
            )
            state.hitl_approvals[name] = approved
            if not approved and feedback:
                state.errors.append(f"HITL rejection at {name}: {feedback}")
            return approved
        except Exception as e:
            console.print(f"[yellow]Web HITL failed ({e}), falling back to CLI[/yellow]")

    # CLI fallback
    console.print(Panel(summary, title=f"🔍 HITL: {name}", border_style="yellow"))
    approved = Confirm.ask("[bold yellow]Approve?[/bold yellow]", default=True)
    state.hitl_approvals[name] = approved
    if not approved:
        fb = Prompt.ask("[yellow]Feedback (or 'skip' to abort)[/yellow]")
        if fb.lower() != "skip":
            state.errors.append(f"HITL rejection at {name}: {fb}")
    return approved


# ============================================================
# Simulation Template Loader
# ============================================================

def load_simulation_template() -> str:
    """Load the base Monte Carlo simulation template for the Math agent."""
    template_path = Path(__file__).parent.parent / "templates" / "math_simulation.py"
    if template_path.exists():
        return template_path.read_text(encoding="utf-8")
    return "# Simulation template not found — write from scratch"


# ============================================================
# Main Pipeline Flow
# ============================================================

class SlotStudioFlow(Flow[PipelineState]):

    def __init__(self, auto_mode: bool = False, initial_state: Optional[PipelineState] = None, **kwargs):
        super().__init__(**kwargs)
        # ── Hydrate state if provided (used by resume + CLI) ──
        # CrewAI's Flow defines `state` as a read-only @property backed by `_state`.
        # We override the property below with a setter, but also hydrate here
        # in case the base class changes in future versions.
        if initial_state is not None:
            self._set_state(initial_state)
        self.auto_mode = auto_mode
        self.agents, self._disabled_agents = create_agents()
        self.cost_tracker = CostTracker()
        self._stage_start_times: dict[str, float] = {}  # in-memory timing (not serialized)
        self.budget = TimeBudget(PIPELINE_TIMEOUT)       # Phase 4.2: time budget tracker

    # ── State property override ──────────────────────────────────
    # CrewAI Flow.state is a read-only @property.  We need a setter
    # for resume (worker.py) and CLI init (main.py).
    # Accessing the *internal* attribute that Flow uses for storage.

    def _set_state(self, new_state: PipelineState):
        """Safely set the internal state, working around Flow's read-only property.
        Tries multiple internal attribute names for forward-compatibility."""
        # CrewAI stores state in `_state` (most versions) or `state` (some forks)
        for attr in ("_state", "_initial_state"):
            if hasattr(self, attr):
                object.__setattr__(self, attr, new_state)
                return
        # Fallback: force-set via object.__setattr__ bypassing descriptors
        object.__setattr__(self, "_state", new_state)

    @property
    def state(self) -> PipelineState:
        """Override Flow.state getter — check multiple internal attr names."""
        for attr in ("_state", "_initial_state"):
            val = getattr(self, attr, None)
            if val is not None:
                return val
        return PipelineState()

    @state.setter
    def state(self, value: PipelineState):
        """Setter that was missing from CrewAI's base Flow class."""
        self._set_state(value)

    # ── ACP Feature Flag & Agent Helpers ──────────────────────

    def _flag(self, key: str, default=True) -> bool:
        """Check an ACP feature flag. Returns default if ACP not loaded."""
        val = LLMConfig.get_flag(key, default)
        return bool(val) if val is not None else default

    def _agent_enabled(self, agent_name: str) -> bool:
        """Check if an agent is enabled in ACP."""
        return agent_name not in self._disabled_agents

    # ── Checkpoint & Timing Helpers ──────────────────────────────

    def _stage_enter(self, stage_name: str):
        """Record the wall-clock start of a pipeline stage.
        Call at the TOP of every @start / @listen method."""
        self._stage_start_times[stage_name] = time.time()
        self.budget.stage_enter(stage_name)
        # Ensure pipeline_start_epoch is set (first stage to run sets it)
        if self.state.pipeline_start_epoch == 0.0:
            self.state.pipeline_start_epoch = time.time()

    def _stage_exit(self, stage_name: str):
        """Record elapsed time for a stage and persist a checkpoint.
        Call at the BOTTOM of every @start / @listen method (all code paths)."""
        start = self._stage_start_times.pop(stage_name, time.time())
        elapsed = round(time.time() - start, 1)
        self.state.stage_timings[stage_name] = elapsed
        self.state.last_completed_stage = stage_name

        # ── Budget tracking ──
        self.budget.stage_exit(stage_name)

        # ── Log timing + budget status ──
        total_elapsed = round(time.time() - self.state.pipeline_start_epoch, 1)
        stage_idx = PIPELINE_STAGES.index(stage_name) if stage_name in PIPELINE_STAGES else -1
        stages_done = stage_idx + 1
        stages_total = len(PIPELINE_STAGES)

        budget_line = self.budget.format_status(stage_name, elapsed)
        console.print(
            f"[dim]💾 Checkpoint: {stage_name} done in {elapsed:.0f}s "
            f"| pipeline {total_elapsed:.0f}s "
            f"| {stages_done}/{stages_total} stages[/dim]"
        )
        # Budget line uses color to signal urgency
        pct = self.budget.pct_used
        if pct >= 80:
            console.print(f"[bold red]⏰ {budget_line}[/bold red]")
        elif pct >= 50:
            console.print(f"[yellow]{budget_line}[/yellow]")
        else:
            console.print(f"[dim]{budget_line}[/dim]")

        emit("checkpoint", stage=stage_name, elapsed_s=elapsed,
             pipeline_elapsed_s=total_elapsed,
             stages_done=stages_done, stages_total=stages_total,
             budget_pct_used=round(pct, 1),
             budget_remaining_s=round(self.budget.remaining, 0))

        # ── Persist checkpoint to disk ──
        self._save_checkpoint(stage_name)

    # ── Smart Stage Skipping (Phase 5) ────────────────────────

    def _budget_decision(self, stage_name: str) -> str:
        """Decide whether to run, compress, or skip a stage based on time budget.

        Decision matrix:
        ┌──────────┬───────────────────────────────────────────────┐
        │ Priority │ Behavior                                      │
        ├──────────┼───────────────────────────────────────────────┤
        │ P0       │ Always "run" — never skip critical stages     │
        │ P1       │ "run" → "lite" → "skip" as pressure increases│
        │ P2       │ "run" → "skip" (no lite variant)             │
        └──────────┴───────────────────────────────────────────────┘

        The key calculation: how much time is available for THIS stage
        after reserving enough budget for all remaining P0 stages?

            available = remaining_budget - p0_reserve
            safety_margin = available / stage_estimate

        Returns: "run" | "lite" | "skip"
        """
        priority = STAGE_PRIORITY.get(stage_name, 0)

        # P0: Always run — these are the deliverable
        if priority == 0:
            return "run"

        estimate = STAGE_TIME_ESTIMATES.get(stage_name, 600)
        p0_reserve = self.budget.time_for_remaining_critical(stage_name)
        available = self.budget.remaining - p0_reserve
        safety = available / estimate if estimate > 0 else 999.0

        if priority == 2:
            # P2: Skip when safety drops below threshold
            if safety < BUDGET_SAFETY_MARGINS["skip_p2"]:
                return "skip"
            return "run"

        if priority == 1:
            # P1: Tiered degradation
            if safety < BUDGET_SAFETY_MARGINS["skip_p1"]:
                return "skip"
            if safety < BUDGET_SAFETY_MARGINS["compress_p1"]:
                return "lite"
            return "run"

        return "run"

    def _skip_stage(self, stage_name: str, reason: str, decision: str = "skip"):
        """Record a stage skip/compress and emit rich feedback.

        Args:
            stage_name: Name of the stage being skipped/compressed.
            reason: Human-readable reason for the decision.
            decision: "skip" or "lite" — affects logging style.
        """
        priority = STAGE_PRIORITY.get(stage_name, 0)
        pct = self.budget.pct_used
        remaining = self.budget.remaining

        if decision == "skip":
            # Track in state
            self.state.skipped_stages.append(stage_name)

            # Rich console output
            console.print(
                f"\n[bold yellow]⚡ SKIP: {stage_name}[/bold yellow]  "
                f"[dim](P{priority} • {reason})[/dim]"
            )
            console.print(
                f"[dim]   Budget: {remaining:.0f}s remaining ({pct:.1f}% used) "
                f"— protecting P0 critical path[/dim]"
            )

            # Emit structured event for thought-feed
            emit("stage_skip", name=stage_name, reason=reason,
                 priority=f"P{priority}", decision="skip",
                 budget_pct=round(pct, 1), remaining_s=round(remaining, 0))

            # Write stub file explaining the skip
            if self.state.output_dir:
                stub_dir = Path(self.state.output_dir)
                stub_path = stub_dir / f"_SKIPPED_{stage_name}.txt"
                try:
                    stub_path.write_text(
                        f"Stage '{stage_name}' was skipped during pipeline execution.\n"
                        f"Priority: P{priority}\n"
                        f"Reason: {reason}\n"
                        f"Budget at decision time: {pct:.1f}% used, {remaining:.0f}s remaining\n"
                        f"Timestamp: {datetime.now().isoformat()}\n",
                        encoding="utf-8",
                    )
                except Exception:
                    pass

        elif decision == "lite":
            console.print(
                f"\n[bold cyan]🔋 COMPRESS: {stage_name} → lite mode[/bold cyan]  "
                f"[dim](P{priority} • {reason})[/dim]"
            )
            lite_est = STAGE_TIME_ESTIMATES_LITE.get(stage_name, "?")
            full_est = STAGE_TIME_ESTIMATES.get(stage_name, "?")
            console.print(
                f"[dim]   Budget: {remaining:.0f}s remaining ({pct:.1f}% used) "
                f"— compressed {full_est}s → ~{lite_est}s[/dim]"
            )
            emit("stage_compress", name=stage_name, reason=reason,
                 priority=f"P{priority}", decision="lite",
                 full_estimate=full_est, lite_estimate=lite_est,
                 budget_pct=round(pct, 1), remaining_s=round(remaining, 0))

    def _save_checkpoint(self, stage_name: str):
        """Write full pipeline state to output_dir/checkpoint.json.

        This file is the contract for resume: a new worker process can
        deserialize it, reconstruct PipelineState, and continue from
        the stage AFTER `stage_name`.

        Design decisions:
        - JSON (not pickle) for debuggability and forward compatibility.
        - model_dump(mode="json") handles Pydantic models, enums, datetimes.
        - We write atomically (write-to-tmp then rename) to avoid corruption
          if the watchdog kills us mid-write.
        - We truncate large text fields (GDD, research output) in the
          checkpoint because the resume path will re-read them from disk
          files anyway (_load_existing_state).
        """
        if not self.state.output_dir:
            return  # output_dir not set yet (shouldn't happen, but guard)

        out = Path(self.state.output_dir)
        ckpt_path = out / "checkpoint.json"
        ckpt_tmp = out / "checkpoint.json.tmp"

        try:
            # Serialize full state
            state_data = self.state.model_dump(mode="json")

            # ── Truncate bulky text blobs ──
            # On resume, _load_existing_state() re-reads these from disk files,
            # so we don't need to carry 50KB of GDD text in the checkpoint.
            _TRUNCATE_FIELDS = {
                "gdd": 500,
                "math_model": 500,
                "market_research": 1000,
                "compliance": 500,
                "mood_board": 500,
                "art_assets": 500,
            }
            for field, max_chars in _TRUNCATE_FIELDS.items():
                val = state_data.get(field)
                if isinstance(val, dict) and "output" in val:
                    original_len = len(val["output"])
                    if original_len > max_chars:
                        val["output"] = val["output"][:max_chars] + f"... [truncated from {original_len} chars — full text on disk]"
                    # Also truncate nested "raw" and "report" keys
                    for sub_key in ("raw", "report", "sweep", "deep_dive"):
                        if sub_key in val and isinstance(val[sub_key], str) and len(val[sub_key]) > max_chars:
                            val[sub_key] = val[sub_key][:max_chars] + f"... [truncated — full text on disk]"

            ckpt_data = {
                "checkpoint_version": CHECKPOINT_VERSION,
                "stage": stage_name,
                "stage_index": PIPELINE_STAGES.index(stage_name) if stage_name in PIPELINE_STAGES else -1,
                "stages_completed": [s for s in PIPELINE_STAGES[:PIPELINE_STAGES.index(stage_name) + 1]] if stage_name in PIPELINE_STAGES else [],
                "stages_remaining": [s for s in PIPELINE_STAGES[PIPELINE_STAGES.index(stage_name) + 1:]] if stage_name in PIPELINE_STAGES else [],
                "saved_at": datetime.now().isoformat(),
                "pipeline_elapsed_s": round(time.time() - self.state.pipeline_start_epoch, 1),
                "stage_timings": self.state.stage_timings,
                "resume_count": self.state.resume_count,
                "budget": self.budget.snapshot(),
                "state": state_data,
            }

            # Atomic write: tmp → rename (survives watchdog kill mid-write)
            ckpt_tmp.write_text(
                json.dumps(ckpt_data, indent=2, default=str),
                encoding="utf-8",
            )
            ckpt_tmp.rename(ckpt_path)

        except Exception as e:
            # Checkpoint failures are never fatal — log and continue
            console.print(f"[yellow]⚠️ Checkpoint write failed (non-fatal): {e}[/yellow]")

    # ---- Stage 1: Initialize ----

    @start()
    def initialize(self):
        self._stage_enter("initialize")
        _update_stage_db(self.state.job_id, "Initializing pipeline")
        emit("stage_start", name="Initialize", num=0, icon="🚀", desc="Setting up pipeline")
        console.print(Panel(
            f"[bold]🎰 Automated Slot Studio[/bold]\n\n"
            f"Theme: {self.state.game_idea.theme}\n"
            f"Markets: {', '.join(self.state.game_idea.target_markets)}\n"
            f"Volatility: {self.state.game_idea.volatility.value}\n"
            f"RTP: {self.state.game_idea.target_rtp}% | Max Win: {self.state.game_idea.max_win_multiplier}x\n\n"
            f"LLM Routing:\n"
            f"  Heavy (Designer/Math/Legal): {LLMConfig.HEAVY}\n"
            f"  Light (Analyst/Art):         {LLMConfig.LIGHT}\n"
            f"  ACP Profile: {LLMConfig.get_flag('_profile_name', 'default') if LLMConfig._acp_flags else 'hardcoded'}\n"
            f"  Disabled agents: {', '.join(sorted(self._disabled_agents)) or 'none'}\n\n"
            f"⏱️ Budget: {self.budget.total}s ({self.budget.total // 60} min)"
            + (f" | Prior: {self.budget.prior_elapsed:.0f}s" if self.budget.prior_elapsed > 0 else "")
            + (f"\n\n[bold yellow]ITERATE MODE v{self.state.iterate_config.get('version','?')}[/bold yellow]\n"
               f"Re-running: {', '.join(self.state.iterate_config.get('rerun_stages',[]))}"
               if self.state.iterate_mode else ""),
            title="Pipeline Starting" if not self.state.iterate_mode else "Iteration Starting",
            border_style="green" if not self.state.iterate_mode else "yellow",
        ))
        self.state.started_at = datetime.now().isoformat()
        slug = "".join(c if c.isalnum() else "_" for c in self.state.game_idea.theme.lower())[:40]
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        version_tag = f"_v{self.state.iterate_config.get('version', 1)}" if self.state.iterate_mode else ""
        self.state.game_slug = f"{slug}_{ts}{version_tag}"
        self.state.output_dir = str(Path(os.getenv("OUTPUT_DIR", "./output")) / self.state.game_slug)
        for sub in ["00_preflight", "01_research", "02_design", "03_math", "04_art/mood_boards",
                     "04_art/symbols", "04_art/backgrounds", "04_art/ui",
                     "04_audio", "05_legal", "06_pdf", "07_prototype"]:
            Path(self.state.output_dir, sub).mkdir(parents=True, exist_ok=True)
        console.print(f"[green]📁 Output: {self.state.output_dir}[/green]")

        # Write output_dir to DB early so watchdog can find checkpoint on timeout
        _update_output_dir_db(self.state.job_id, self.state.output_dir)

        # ── Phase 3A: Copy existing outputs for iterate mode ──
        if self.state.iterate_mode:
            source_dir = self.state.iterate_config.get("source_output_dir", "")
            rerun_stages = set(self.state.iterate_config.get("rerun_stages", []))
            if source_dir and Path(source_dir).exists():
                import shutil
                _update_stage_db(self.state.job_id, "Copying existing outputs")
                console.print(f"[cyan]📋 Copying existing outputs from source...[/cyan]")

                # Map stages to directories that should NOT be copied (they'll be regenerated)
                stage_dir_map = {
                    "math": ["03_math"],
                    "gdd": ["02_design"],
                    "art": ["04_art", "04_audio"],
                    "compliance": ["05_legal"],
                    "convergence": [],  # Convergence doesn't own a directory
                }
                skip_dirs = set()
                for stage in rerun_stages:
                    skip_dirs.update(stage_dir_map.get(stage, []))

                # Copy directories that are NOT being re-run
                source = Path(source_dir)
                dest = Path(self.state.output_dir)
                for item in source.iterdir():
                    if item.is_dir():
                        rel_name = item.name
                        should_skip = any(rel_name.startswith(sd) for sd in skip_dirs)
                        if not should_skip and rel_name not in ("06_pdf", "07_prototype"):
                            # Copy the entire directory tree
                            dest_sub = dest / rel_name
                            if dest_sub.exists():
                                shutil.rmtree(dest_sub)
                            shutil.copytree(item, dest_sub)
                            console.print(f"  [dim]📁 Copied {rel_name}/[/dim]")
                        elif should_skip:
                            console.print(f"  [yellow]🔄 Will regenerate {rel_name}/[/yellow]")

                # Also copy individual files from source root
                for item in source.iterdir():
                    if item.is_file():
                        shutil.copy2(item, dest / item.name)

                # Load existing state data from copied files
                self._load_existing_state()
                console.print(f"[green]✅ Existing outputs copied — re-running: {', '.join(rerun_stages)}[/green]")

        self._stage_exit("initialize")

    def _should_skip_stage(self, stage_name: str) -> bool:
        """In iterate mode, check if a stage should be skipped (not re-run)."""
        if not self.state.iterate_mode:
            return False
        rerun_stages = set(self.state.iterate_config.get("rerun_stages", []))
        return stage_name not in rerun_stages

    def _load_existing_state(self):
        """Load state from copied files so downstream stages have context."""
        out = Path(self.state.output_dir)

        # Load GDD
        gdd_path = out / "02_design" / "gdd.md"
        if gdd_path.exists():
            self.state.gdd = {"output": gdd_path.read_text(encoding="utf-8", errors="replace")}
            console.print(f"  [dim]Loaded GDD: {len(self.state.gdd['output'])} chars[/dim]")

        # Load math
        sim_path = out / "03_math" / "simulation_results.json"
        if sim_path.exists():
            try:
                self.state.math_model = {"output": sim_path.read_text(), "results": json.loads(sim_path.read_text())}
            except Exception:
                self.state.math_model = {"output": sim_path.read_text()}

        # Load compliance
        comp_path = out / "05_legal" / "compliance_report.json"
        if comp_path.exists():
            try:
                self.state.compliance = {"results": json.loads(comp_path.read_text()), "output": comp_path.read_text()}
            except Exception:
                pass

        # Load research
        research_path = out / "01_research" / "market_research.json"
        if research_path.exists():
            try:
                self.state.market_research = json.loads(research_path.read_text())
            except Exception:
                pass

        # Set approval flags (iterated jobs skip HITL reviews)
        self.state.research_approved = True
        self.state.design_math_approved = True
        self.state.mood_board_approved = True

    # ── Resume from Checkpoint ──────────────────────────────────

    def resume_from_stage(self, last_completed_stage: str):
        """Resume pipeline execution from the stage AFTER `last_completed_stage`.

        This bypasses CrewAI's @start/@listen event system and calls stage
        methods directly in sequence. This is safe because:
        1. The pipeline DAG is strictly linear (no branching)
        2. All data flows through self.state + disk files (no event payloads)
        3. Agents are stateless — create_agents() is called fresh in __init__

        On resume, all HITL checkpoints are auto-approved (the user already
        reviewed the partial output and chose to resume). The budget tracker
        accounts for time consumed in the prior run via `already_elapsed`.

        Args:
            last_completed_stage: The last stage that completed successfully
                (from checkpoint.json). Execution starts from the NEXT stage.

        Raises:
            ValueError: If last_completed_stage is not in PIPELINE_STAGES.
        """
        if last_completed_stage not in PIPELINE_STAGES:
            raise ValueError(
                f"Unknown stage '{last_completed_stage}'. "
                f"Valid stages: {PIPELINE_STAGES}"
            )

        # Map stage names → bound methods
        _STAGE_METHODS = {
            "initialize":          self.initialize,
            "preflight":           self.run_preflight,
            "research":            self.run_research,
            "checkpoint_research": self.checkpoint_research,
            "design_and_math":     self.run_design_and_math,
            "checkpoint_design":   self.checkpoint_design,
            "mood_boards":         self.run_mood_boards,
            "checkpoint_art":      self.checkpoint_art,
            "production":          self.run_production,
            "assemble_package":    self.assemble_package,
        }

        idx = PIPELINE_STAGES.index(last_completed_stage)
        remaining_stages = PIPELINE_STAGES[idx + 1:]

        if not remaining_stages:
            console.print("[yellow]⚠️ All stages already completed — nothing to resume[/yellow]")
            return self.state

        console.print(Panel(
            f"[bold]🔄 Resuming Pipeline[/bold]\n\n"
            f"Theme: {self.state.game_idea.theme}\n"
            f"Last completed: {last_completed_stage} "
            f"({idx + 1}/{len(PIPELINE_STAGES)})\n"
            f"Remaining: {', '.join(remaining_stages)}\n"
            f"Resume #{self.state.resume_count}\n"
            f"Budget: {self.budget.remaining:.0f}s remaining "
            f"({self.budget.pct_used:.1f}% used)",
            title="Pipeline Resume",
            border_style="yellow",
        ))

        # ── Phase 5: Preview budget decisions for remaining stages ──
        for s in remaining_stages:
            d = self._budget_decision(s)
            p = STAGE_PRIORITY.get(s, 0)
            label = {"run": "[green]RUN[/green]", "lite": "[cyan]LITE[/cyan]", "skip": "[yellow]SKIP[/yellow]"}.get(d, d)
            console.print(f"[dim]  → {s} (P{p}): {label}[/dim]")

        # ── Hydrate state from disk files ──
        # Checkpoint truncated large text blobs, so we reload from disk.
        self._load_existing_state()

        # ── Auto-approve all HITL checkpoints ──
        # User already reviewed the partial output and clicked Resume.
        self.state.research_approved = True
        self.state.design_math_approved = True
        self.state.mood_board_approved = True

        # ── Execute remaining stages sequentially ──
        for stage_name in remaining_stages:
            method = _STAGE_METHODS.get(stage_name)
            if not method:
                console.print(f"[yellow]⚠️ No method for stage '{stage_name}' — skipping[/yellow]")
                continue

            _update_stage_db(self.state.job_id, f"Resuming: {stage_name}")
            console.print(f"\n[bold cyan]▶ Resuming stage: {stage_name}[/bold cyan]")

            try:
                method()
            except Exception as e:
                console.print(f"[bold red]❌ Stage '{stage_name}' failed on resume: {e}[/bold red]")
                self.state.errors.append(f"Resume failure in {stage_name}: {str(e)[:200]}")
                raise  # Let the worker catch this and mark the job as failed

        console.print("[bold green]✅ Pipeline resume completed successfully[/bold green]")
        if self.state.skipped_stages:
            console.print(
                f"[yellow]⚡ Budget-skipped {len(self.state.skipped_stages)} stage(s): "
                f"{', '.join(self.state.skipped_stages)}[/yellow]"
            )
        return self.state

    # ---- Stage 2: Pre-Flight Intelligence ----

    @listen(initialize)
    def run_preflight(self):
        self._stage_enter("preflight")
        # Phase 3A: Skip preflight in iterate mode (data already copied)
        if self.state.iterate_mode:
            _update_stage_db(self.state.job_id, "Skipping preflight (iterate)")
            console.print("[dim]⏭️ Skipping preflight (iterate mode — using existing data)[/dim]")
            self._stage_exit("preflight")
            return

        # Phase 5: Smart skip under time pressure
        decision = self._budget_decision("preflight")
        if decision == "skip":
            self._skip_stage("preflight", "time pressure — downstream stages handle missing enrichment data")
            self._stage_exit("preflight")
            return

        _update_stage_db(self.state.job_id, "Pre-flight intelligence (parallel)")
        console.print("\n[bold cyan]🛰️ Stage 0: Pre-Flight Intelligence (Parallel)[/bold cyan]\n")
        emit("stage_start", name="Pre-Flight Intelligence", num=0, icon="🛰️", desc="Running 5 parallel checks: trends, jurisdiction, knowledge base, patents, Qdrant")
        idea = self.state.game_idea

        # ── Phase 7A: Run all pre-flight checks in parallel ──
        # These are 100% independent — no data dependencies between them.
        # Sequential: ~25s. Parallel: ~8s (bottleneck = slowest single check).

        def _run_trend_radar():
            try:
                radar = TrendRadarTool()
                result = json.loads(radar._run(
                    focus="all", timeframe="6months",
                    theme_filter=idea.theme.split()[0] if idea.theme else "",
                ))
                Path(self.state.output_dir, "00_preflight").mkdir(parents=True, exist_ok=True)
                Path(self.state.output_dir, "00_preflight", "trend_radar.json").write_text(
                    json.dumps(result, indent=2), encoding="utf-8")

                # Also run full market intel scan (saves to DB for /trends dashboard)
                try:
                    from tools.market_intel import run_full_scan, find_opportunities
                    import sqlite3 as _sql
                    db_path = os.getenv("DB_PATH", "arkainbrain.db")
                    _conn = _sql.connect(db_path, timeout=5)
                    _conn.row_factory = _sql.Row
                    scan = run_full_scan(_conn, theme_filter=idea.theme)
                    opps = find_opportunities(_conn, scan)
                    _conn.close()
                    result["market_intel"] = {
                        "opportunities_found": len(opps),
                        "top_opportunity": opps[0] if opps else None,
                    }
                    # Also save enriched result
                    Path(self.state.output_dir, "00_preflight", "market_intel.json").write_text(
                        json.dumps(scan, indent=2), encoding="utf-8")
                except Exception as intel_err:
                    logger.warning(f"Market intel enrichment failed (non-fatal): {intel_err}")

                return ("trend_radar", result, None)
            except Exception as e:
                return ("trend_radar", None, str(e))

        def _run_jurisdiction():
            try:
                jx = JurisdictionIntersectionTool()
                result = json.loads(jx._run(
                    markets=idea.target_markets, proposed_rtp=idea.target_rtp,
                    proposed_features=[f.value for f in idea.requested_features],
                    proposed_max_win=idea.max_win_multiplier,
                ))
                Path(self.state.output_dir, "00_preflight").mkdir(parents=True, exist_ok=True)
                Path(self.state.output_dir, "00_preflight", "jurisdiction_constraints.json").write_text(
                    json.dumps(result, indent=2), encoding="utf-8")
                return ("jurisdiction", result, None)
            except Exception as e:
                return ("jurisdiction", None, str(e))

        def _run_knowledge_base():
            try:
                kb = KnowledgeBaseTool()
                result = json.loads(kb._run(action="search", query=f"{idea.theme} {idea.volatility.value} slot game"))
                if result.get("results_count", 0) > 0:
                    Path(self.state.output_dir, "00_preflight").mkdir(parents=True, exist_ok=True)
                    Path(self.state.output_dir, "00_preflight", "past_designs.json").write_text(
                        json.dumps(result, indent=2), encoding="utf-8")
                return ("knowledge_base", result, None)
            except Exception as e:
                return ("knowledge_base", None, str(e))

        def _run_patent_scan():
            try:
                scanner = PatentIPScannerTool()
                features_desc = ", ".join(f.value.replace("_", " ") for f in idea.requested_features)
                result = json.loads(scanner._run(
                    mechanic_description=f"{features_desc} slot game mechanic",
                    keywords=[f.value.replace("_", " ") for f in idea.requested_features],
                    theme_name=idea.theme,
                ))
                Path(self.state.output_dir, "00_preflight").mkdir(parents=True, exist_ok=True)
                Path(self.state.output_dir, "00_preflight", "patent_scan.json").write_text(
                    json.dumps(result, indent=2), encoding="utf-8")
                return ("patent_scan", result, None)
            except Exception as e:
                return ("patent_scan", None, str(e))

        def _run_qdrant_recon():
            try:
                from tools.qdrant_store import JurisdictionStore
                store = JurisdictionStore()
                recon_results = {}
                for market in idea.target_markets:
                    results = store.search(f"{market} gambling law requirements", jurisdiction=market, limit=3)
                    if results and "error" not in results[0]:
                        recon_path = Path(self.state.output_dir, "00_preflight", f"recon_{market.lower().replace(' ', '_')}.json")
                        recon_path.parent.mkdir(parents=True, exist_ok=True)
                        recon_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
                        recon_results[market] = len(results)
                return ("qdrant_recon", recon_results, None)
            except Exception as e:
                return ("qdrant_recon", None, str(e))

        # Fire pre-flight checks in parallel (ACP flags can disable patent scan)
        run_patent = self._flag("enable_patent_scan", True)
        checks = ["Trend Radar", "Jurisdiction Check", "Knowledge Base"]
        if run_patent:
            checks.append("Patent Scan")
        else:
            console.print("[dim]⏭️ Preflight patent scan skipped (enable_patent_scan=false)[/dim]")
        checks.append("Qdrant Recon")

        console.print(f"[cyan]⚡ Running {len(checks)} pre-flight checks in parallel...[/cyan]")
        emit("parallel_start", tasks=checks)
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [
                executor.submit(_run_trend_radar),
                executor.submit(_run_jurisdiction),
                executor.submit(_run_knowledge_base),
            ]
            if run_patent:
                futures.append(executor.submit(_run_patent_scan))
            futures.append(executor.submit(_run_qdrant_recon))
            for future in as_completed(futures):
                name, result, error = future.result()
                if error:
                    console.print(f"[yellow]⚠️ {name} failed (non-fatal): {error}[/yellow]")
                else:
                    if name == "trend_radar" and result:
                        self.state.trend_radar = result
                        console.print("[green]✅ Trend radar[/green]")
                    elif name == "jurisdiction" and result:
                        self.state.jurisdiction_constraints = result
                        blockers = result.get("intersection", {}).get("blockers", [])
                        if blockers:
                            console.print(f"[bold red]🚨 BLOCKERS: {blockers}[/bold red]")
                            self.state.errors.extend(blockers)
                        else:
                            console.print("[green]✅ Jurisdiction — all markets clear[/green]")
                    elif name == "knowledge_base" and result:
                        count = result.get("results_count", 0)
                        console.print(f"[green]✅ Knowledge base — {count} past designs[/green]" if count else "[dim]No past designs[/dim]")
                    elif name == "patent_scan" and result:
                        self.state.patent_scan = result
                        risk = result.get("risk_assessment", {}).get("overall_ip_risk", "UNKNOWN")
                        console.print(f"[green]✅ Patent scan — risk: {risk}[/green]" if risk != "HIGH" else f"[bold red]🚨 HIGH IP RISK[/bold red]")
                    elif name == "qdrant_recon" and result:
                        for m, c in result.items():
                            console.print(f"[green]✅ Qdrant recon for {m}[/green]")

        console.print("[green]✅ Pre-flight complete (parallel)[/green]")
        emit("stage_done", name="Pre-Flight Intelligence", num=0)
        self._stage_exit("preflight")

    # ---- Stage 2b: Research ----

    @listen(run_preflight)
    def run_research(self):
        self._stage_enter("research")
        # Phase 3A: Skip research in iterate mode
        if self.state.iterate_mode:
            _update_stage_db(self.state.job_id, "Skipping research (iterate)")
            console.print("[dim]⏭️ Skipping research (iterate mode — using existing data)[/dim]")
            self._stage_exit("research")
            return

        # Phase 5: Smart skip/compress under time pressure
        decision = self._budget_decision("research")
        if decision == "skip":
            self._skip_stage("research", "time pressure — preserving budget for design + production")
            # Set minimal research data so downstream doesn't crash
            self.state.market_research = {
                "sweep": "Skipped due to time pressure",
                "report": "Market research was skipped to preserve time budget for critical stages.",
                "raw": "",
            }
            self._stage_exit("research")
            return
        is_lite = (decision == "lite")
        if is_lite:
            self._skip_stage("research", "running sweep-only to preserve time for production", decision="lite")

        _update_stage_db(self.state.job_id, f"Market research{' (lite)' if is_lite else ''}")
        mode_label = " [bold yellow]⚡ LITE[/bold yellow]" if is_lite else ""
        console.print(f"\n[bold blue]📊 Stage 1: Market Research{mode_label}[/bold blue]\n")
        desc = "Sweep-only: saturation check + quick positioning" if is_lite else "Analyzing target markets, competitors, and regulatory landscape"
        emit("stage_start", name="Market Research", num=1, icon="📊", desc=desc)
        emit("agent_start", agent="Research Agent", task="Deep market analysis")
        idea = self.state.game_idea

        # Pre-flight context for research agents
        preflight_ctx = ""
        if self.state.trend_radar:
            top_themes = self.state.trend_radar.get("trending_themes", [])[:5]
            preflight_ctx += f"\nTREND RADAR: Top themes = {json.dumps(top_themes)}\n"
            if self.state.trend_radar.get("theme_analysis"):
                preflight_ctx += f"Theme analysis: {json.dumps(self.state.trend_radar['theme_analysis'])}\n"
        if self.state.jurisdiction_constraints:
            jx = self.state.jurisdiction_constraints.get("intersection", {})
            preflight_ctx += f"\nJURISDICTION CONSTRAINTS:\n"
            preflight_ctx += f"  RTP floor: {jx.get('rtp_floor', 'unknown')}%\n"
            preflight_ctx += f"  Banned features: {jx.get('banned_features', {})}\n"
            preflight_ctx += f"  Required features: {jx.get('required_features_union', [])}\n"
            preflight_ctx += f"  Blockers: {jx.get('blockers', [])}\n"
        if self.state.patent_scan:
            risk = self.state.patent_scan.get("risk_assessment", {})
            preflight_ctx += f"\nPATENT SCAN:\n"
            preflight_ctx += f"  Overall IP risk: {risk.get('overall_ip_risk', 'unknown')}\n"
            preflight_ctx += f"  Known patent hits: {self.state.patent_scan.get('known_patent_hits', [])}\n"
            preflight_ctx += f"  Recommendations: {self.state.patent_scan.get('recommendations', [])}\n"

        sweep_task = Task(
            description=(
                f"Conduct a BROAD market sweep for the theme '{idea.theme}'.\n"
                f"Search for up to {PipelineConfig.COMPETITOR_BROAD_SWEEP_LIMIT} existing games.\n"
                f"Categorize saturation level. Find underserved angles.\n"
                f"{preflight_ctx}\n"
                f"Use the trend_radar and deep_research tools for comprehensive analysis.\n"
                f"Output a JSON object with keys: theme_keyword, total_games_found, "
                f"saturation_level, top_providers, dominant_mechanics, underserved_angles, "
                f"trending_direction, theme_trajectory (rising/stable/declining)."
            ),
            expected_output="JSON market saturation analysis",
            agent=self.agents["market_analyst"],
        )

        dive_task = Task(
            description=(
                f"Deep-dive on top {PipelineConfig.COMPETITOR_DEEP_DIVE_LIMIT} competitors "
                f"plus references: {', '.join(idea.competitor_references)}.\n"
                f"For each: provider, RTP, volatility, max win, features, player sentiment.\n"
                f"Synthesize differentiation strategy: primary_differentiator, mechanic_opportunities, "
                f"theme_twist, visual_differentiation, player_pain_points.\n"
                f"Output as JSON."
            ),
            expected_output="JSON competitor analysis + differentiation strategy",
            agent=self.agents["market_analyst"],
            context=[sweep_task],
        )

        report_task = Task(
            description=(
                f"Write a COMPREHENSIVE market research report in Markdown.\n\n"
                f"Use ALL data from the market sweep and competitor deep-dive.\n"
                f"Use deep_research and fetch_web_page tools to find additional market data.\n\n"
                f"═══ REQUIRED SECTIONS ═══\n\n"
                f"## 1. Market Overview\n"
                f"- Theme category market size and growth trajectory\n"
                f"- Player demographics for this theme (age, gender, geography)\n"
                f"- Platform distribution (online vs land-based vs hybrid)\n"
                f"- Key market trends affecting this theme category\n\n"
                f"## 2. Competitive Landscape\n"
                f"- For each of the top 5-10 competitors:\n"
                f"  - Game title, provider, launch date\n"
                f"  - RTP, volatility, max win\n"
                f"  - Key mechanics/features\n"
                f"  - Player reception and reviews\n"
                f"  - Revenue performance (if data available)\n"
                f"- Competitive positioning matrix (grid of features vs competitors)\n\n"
                f"## 3. Target Market Analysis ({', '.join(idea.target_markets)})\n"
                f"- Market size per jurisdiction\n"
                f"- Regulatory environment and requirements\n"
                f"- Player preferences and spending patterns\n"
                f"- Distribution channels and operator partnerships\n"
                f"- Growth rates and projections\n\n"
                f"## 4. Theme & Mechanic Opportunity Analysis\n"
                f"- Saturation analysis: overserved vs underserved theme angles\n"
                f"- Mechanic innovation opportunities\n"
                f"- Theme twist recommendations\n"
                f"- Visual differentiation strategies\n"
                f"- Player pain points to solve\n\n"
                f"## 5. Revenue Potential Assessment\n"
                f"- Similar game revenue benchmarks\n"
                f"- Average revenue per game in this theme category\n"
                f"- Revenue projections by market/jurisdiction\n"
                f"- Key revenue drivers and assumptions\n"
                f"- Operator commission structures\n\n"
                f"## 6. Risk Assessment\n"
                f"- Market risks (saturation, trend changes)\n"
                f"- Regulatory risks per jurisdiction\n"
                f"- IP/trademark risks\n"
                f"- Technical risks\n"
                f"- Competitive response risks\n\n"
                f"## 7. Recommendations\n"
                f"- Go/no-go recommendation with rationale\n"
                f"- Recommended positioning strategy\n"
                f"- Key differentiators to emphasize\n"
                f"- Markets to prioritize\n"
                f"- Timeline recommendations\n\n"
                f"Write 2000-4000 words minimum. Use specific data points and numbers.\n"
                f"Save to: {self.state.output_dir}/01_research/market_report.md"
            ),
            expected_output="Comprehensive market research report saved to file",
            agent=self.agents["market_analyst"],
            context=[sweep_task, dive_task],
        )

        # Phase 5: In lite mode, only run the sweep task (skip dive + report + geo)
        if is_lite:
            crew = Crew(
                agents=[self.agents["market_analyst"]],
                tasks=[sweep_task],
                process=Process.sequential, verbose=VERBOSE,
            )
        else:
            # ══════════════════════════════════════════════════════════════
            # PHASE 12: PARALLEL RESEARCH ARCHITECTURE
            # Step 1: Sweep (must be first — everyone depends on it)
            # Step 2 (PARALLEL): Deep Dive ∥ Report Synthesis ∥ Geo Research
            # Sequential: ~15 min. Parallel: ~8 min (bottleneck = slowest branch)
            # ══════════════════════════════════════════════════════════════
            console.print("[cyan]⚡ Phase 1/2: Running market sweep...[/cyan]")
            sweep_crew = Crew(
                agents=[self.agents["market_analyst"]],
                tasks=[sweep_task],
                process=Process.sequential, verbose=VERBOSE,
            )
            run_crew_with_timeout(sweep_crew, "research", console)
            sweep_output = str(sweep_task.output)
            console.print("[green]✅ Sweep complete — launching parallel deep-dive + report + geo[/green]")

            # ── Branch A: Deep Dive (Market Analyst) ──
            dive_task_standalone = Task(
                description=(
                    f"Deep-dive on top {PipelineConfig.COMPETITOR_DEEP_DIVE_LIMIT} competitors "
                    f"plus references: {', '.join(idea.competitor_references)}.\n"
                    f"SWEEP RESULTS:\n{sweep_output[:3000]}\n\n"
                    f"For each: provider, RTP, volatility, max win, features, player sentiment.\n"
                    f"Synthesize differentiation strategy: primary_differentiator, mechanic_opportunities, "
                    f"theme_twist, visual_differentiation, player_pain_points.\n"
                    f"Output as JSON. Save to: {self.state.output_dir}/01_research/competitor_deep_dive.json"
                ),
                expected_output="JSON competitor analysis + differentiation strategy",
                agent=self.agents["market_analyst"],
            )

            # ── Branch B: Report Synthesis (NEW Research Synthesizer Agent) ──
            synth_report_task = Task(
                description=(
                    f"Write a COMPREHENSIVE market research report in Markdown.\n\n"
                    f"SWEEP DATA (use this as your primary source):\n{sweep_output[:4000]}\n\n"
                    f"PRE-FLIGHT CONTEXT:\n{preflight_ctx}\n\n"
                    f"Use deep_research and fetch_web_page tools to find additional market data.\n\n"
                    f"GAME PARAMETERS:\n"
                    f"  Theme: {idea.theme} | Markets: {idea.target_markets}\n"
                    f"  Volatility: {idea.volatility.value} | RTP: {idea.target_rtp}%\n\n"
                    f"═══ REQUIRED SECTIONS ═══\n\n"
                    f"## 1. Market Overview\n"
                    f"Theme category market size, growth trajectory, player demographics, platform distribution.\n\n"
                    f"## 2. Competitive Landscape\n"
                    f"Top 5-10 competitors with: title, provider, RTP, volatility, max win, key mechanics, reception.\n\n"
                    f"## 3. Target Market Analysis ({', '.join(idea.target_markets)})\n"
                    f"Per-jurisdiction: market size, regulatory environment, player preferences, growth rates.\n\n"
                    f"## 4. Theme & Mechanic Opportunity Analysis\n"
                    f"Saturation analysis, mechanic innovations, theme twist recommendations.\n\n"
                    f"## 5. Revenue Potential Assessment\n"
                    f"Revenue benchmarks, projections by market, key revenue drivers.\n\n"
                    f"## 6. Risk Assessment\n"
                    f"Market, regulatory, IP, technical, and competitive response risks.\n\n"
                    f"## 7. Recommendations\n"
                    f"Go/no-go with rationale, positioning strategy, priority markets.\n\n"
                    f"Write 2000-4000 words minimum. Use specific data points and numbers.\n"
                    f"Save to: {self.state.output_dir}/01_research/market_report.md"
                ),
                expected_output="Comprehensive market research report saved to file",
                agent=self.agents["research_synthesizer"],
            )

            # ── Branch C: Geo Research (direct tool calls, no agent needed) ──
            dive_result = [None]
            dive_error = [None]
            synth_result = [None]
            synth_error = [None]
            geo_results = {}

            def _run_deep_dive():
                try:
                    dive_crew = Crew(
                        agents=[self.agents["market_analyst"]],
                        tasks=[dive_task_standalone],
                        process=Process.sequential, verbose=VERBOSE,
                    )
                    dive_result[0] = run_crew_with_timeout(dive_crew, "research", console)
                except Exception as e:
                    dive_error[0] = str(e)

            def _run_synth_report():
                try:
                    synth_crew = Crew(
                        agents=[self.agents["research_synthesizer"]],
                        tasks=[synth_report_task],
                        process=Process.sequential, verbose=VERBOSE,
                    )
                    synth_result[0] = run_crew_with_timeout(synth_crew, "research", console)
                except Exception as e:
                    synth_error[0] = str(e)

            def _run_all_geo():
                try:
                    from tools.geo_research import run_geo_research
                    for market in idea.target_markets[:3]:
                        geo = run_geo_research(
                            state=market,
                            game_volatility=idea.volatility.value,
                            target_rtp=idea.target_rtp,
                            game_theme=idea.theme,
                            output_dir=str(Path(self.state.output_dir) / "01_research"),
                        )
                        if geo.get("ranked_regions"):
                            geo_results[market] = geo
                except Exception as e:
                    console.print(f"[yellow]⚠️ Geo research error: {e}[/yellow]")

            emit("parallel_start", tasks=["Deep Dive", "Report Synthesis", "Geo Research"])
            console.print("[cyan]⚡ Running 3 parallel research branches...[/cyan]")

            with ThreadPoolExecutor(max_workers=3) as executor:
                futures = [
                    executor.submit(_run_deep_dive),
                    executor.submit(_run_synth_report),
                    executor.submit(_run_all_geo),
                ]
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as e:
                        console.print(f"[yellow]⚠️ Parallel research error: {e}[/yellow]")

            if dive_error[0]:
                console.print(f"[yellow]⚠️ Deep dive error: {dive_error[0]}[/yellow]")
            else:
                console.print("[green]✅ Deep dive complete[/green]")
            if synth_error[0]:
                console.print(f"[yellow]⚠️ Report synthesis error: {synth_error[0]}[/yellow]")
            else:
                console.print("[green]✅ Report synthesis complete[/green]")
            for m, g in geo_results.items():
                console.print(f"[cyan]📍 Geo: {m} → Top region: "
                              f"{g['top_recommendation']['region']} "
                              f"(score {g['top_recommendation']['composite_score']}/100)[/cyan]")

            crew = None  # Already executed above

        if crew is not None:
            result = run_crew_with_timeout(crew, "research", console)
        else:
            result = synth_result[0] or dive_result[0]

        # Read the full market report from file
        market_report_text = ""
        report_path = Path(self.state.output_dir, "01_research", "market_report.md")
        if report_path.exists():
            market_report_text = report_path.read_text(encoding="utf-8", errors="replace")

        self.state.market_research = {
            "sweep": str(sweep_task.output) if hasattr(sweep_task, 'output') and sweep_task.output else "",
            "deep_dive": "" if is_lite else (str(dive_task_standalone.output) if not is_lite and dive_result[0] else ""),
            "report": market_report_text or ("" if is_lite else str(synth_report_task.output if not is_lite and synth_result[0] else "")),
            "raw": str(result) if result else "",
            "mode": "lite" if is_lite else "full_parallel",
        }
        Path(self.state.output_dir, "01_research", "market_research.json").write_text(
            json.dumps(self.state.market_research, indent=2, default=str), encoding="utf-8"
        )
        console.print("[green]✅ Research complete[/green]")
        emit("stage_done", name="Market Research", num=1)

        # ── Geographic Market Research — already handled in parallel branch above ──
        if is_lite:
            console.print("[dim]⏭️ Skipping geo research (lite mode)[/dim]")
        elif not is_lite and geo_results:
            for m, geo in geo_results.items():
                self.state.market_research["geo_" + m.lower().replace(" ", "_")] = geo
        elif not is_lite:
            # Fallback: geo wasn't run in parallel, try it now
            try:
                from tools.geo_research import run_geo_research
                for market in idea.target_markets[:3]:
                    geo = run_geo_research(
                        state=market,
                        game_volatility=idea.volatility.value,
                        target_rtp=idea.target_rtp,
                        game_theme=idea.theme,
                        output_dir=str(Path(self.state.output_dir) / "01_research"),
                    )
                    if geo.get("ranked_regions"):
                        console.print(f"[cyan]📍 Geo: {market} → Top region: "
                                      f"{geo['top_recommendation']['region']} "
                                      f"(score {geo['top_recommendation']['composite_score']}/100)[/cyan]")
                        self.state.market_research["geo_" + market.lower().replace(" ", "_")] = geo
            except Exception as e:
                console.print(f"[yellow]⚠️ Geo research skipped: {e}[/yellow]")

        self._stage_exit("research")

    @listen(run_research)
    def checkpoint_research(self):
        self._stage_enter("checkpoint_research")
        # Phase 3A: Auto-approve in iterate mode
        if self.state.iterate_mode:
            self.state.research_approved = True
            console.print("[dim]⏭️ Research auto-approved (iterate mode)[/dim]")
            self._stage_exit("checkpoint_research")
            return

        # Phase 5: Auto-approve under time pressure
        decision = self._budget_decision("checkpoint_research")
        if decision == "skip":
            self._skip_stage("checkpoint_research", "auto-approving to preserve budget")
            self.state.research_approved = True
            self._stage_exit("checkpoint_research")
            return

        _update_stage_db(self.state.job_id, "Research review")
        # Run adversarial review before HITL
        self._run_adversarial_review("post_research",
            f"Theme: {self.state.game_idea.theme}\n"
            f"Market Research Output: {json.dumps(self.state.market_research, default=str)[:3000]}")

        self.state.research_approved = hitl_checkpoint(
            "post_research",
            f"Research complete for '{self.state.game_idea.theme}'.\n"
            f"See: {self.state.output_dir}/01_research/\n"
            f"Adversarial review: {self.state.output_dir}/adversarial_review_post_research.md",
            self.state, auto=self.auto_mode,
        )
        self._stage_exit("checkpoint_research")

    # ---- Stage 3: Design + Math — OODA Convergence Loop ----
    #
    # ARCHITECTURE:
    #   OBSERVE:  Compliance scans GDD + Math for conflicts
    #   ORIENT:   Producer runs convergence validator (structured check)
    #   DECIDE:   If conflicts → targeted revision instructions
    #   ACT:      Designer patches GDD, Mathematician re-runs model
    #   LOOP:     Max 3 iterations → force continue with best available
    #

    @listen(checkpoint_research)
    def run_design_and_math(self):
        self._stage_enter("design_and_math")
        _update_stage_db(self.state.job_id, "GDD + Math — OODA Loop 1 of 3")
        if not self.state.research_approved:
            self._stage_exit("design_and_math")
            return

        # Phase 3A: Skip if neither GDD nor Math being re-run
        if self.state.iterate_mode:
            rerun = set(self.state.iterate_config.get("rerun_stages", []))
            if not rerun.intersection({"math", "gdd", "convergence"}):
                console.print("[dim]⏭️ Skipping GDD + Math (iterate mode — not selected for re-run)[/dim]")
                self._stage_exit("design_and_math")
                return

        console.print("\n[bold yellow]📄 Stage 2: Design & Math — OODA Convergence Loop[/bold yellow]\n")
        emit("stage_start", name="Design & Math", num=2, icon="📄", desc="Iterative design-math convergence with OODA loops")
        idea = self.state.game_idea
        market_ctx = json.dumps(self.state.market_research, default=str)[:5000]
        sim_template = load_simulation_template()
        out = self.state.output_dir

        # ══════════════════════════════════════════════════════════════
        # INITIAL PASS: Full GDD + Full Math Model
        # ══════════════════════════════════════════════════════════════
        # ══════════════════════════════════════════════════════════════
        # PHASE 12A: PARALLEL GDD + MATH SCAFFOLD
        # Before: GDD (10 min) → Math (10 min) = 20 min sequential
        # After:  [GDD ∥ Math scaffold] → Proofreader reconcile → Math finalize
        #         (~10 min parallel + ~3 min reconcile = ~13 min)
        #
        # Why it's safe: Math can scaffold reel strips, paytable, and
        # simulation from GameIdeaInput alone. It doesn't need the full
        # GDD — just grid size, symbol count, RTP target, volatility,
        # feature list. The GDD adds refinement (exact pay values,
        # feature parameters) which the proofreader reconciles.
        # ══════════════════════════════════════════════════════════════
        console.print("[bold]🔄 OODA Loop 1: Initial Design + Math (PARALLEL)[/bold]")
        console.print("[cyan]⚡ Running GDD ∥ Math scaffold in PARALLEL...[/cyan]")
        emit("ooda_start", loop=1, max=3)
        emit("parallel_start", tasks=["GDD", "Math Scaffold"])

        # Load few-shot quality examples (Phase 2D)
        try:
            designer_examples = get_designer_examples()
        except Exception:
            designer_examples = ""
        try:
            math_examples = get_mathematician_examples()
        except Exception:
            math_examples = ""

        gdd_task = Task(
            description=(
                f"Write the COMPLETE Game Design Document for '{idea.theme}'.\n\n"
                f"{designer_examples}\n\n"
                f"GAME PARAMETERS:\n"
                f"  Theme: {idea.theme}\n"
                f"  Grid: {idea.grid_cols}x{idea.grid_rows}, {idea.ways_or_lines}\n"
                f"  Volatility: {idea.volatility.value} | RTP: {idea.target_rtp}% | Max Win: {idea.max_win_multiplier}x\n"
                f"  Features: {[f.value for f in idea.requested_features]}\n"
                f"  Art Style: {idea.art_style}\n"
                f"  Target Markets: {idea.target_markets}\n\n"
                f"MARKET CONTEXT:\n{market_ctx}\n\n"
                f"═══ REQUIRED SECTIONS (write ALL of these in detail) ═══\n\n"
                f"## 1. Game Commandments\n"
                f"Table with: Game Name, Theme, Art Style, Screen Orientation (landscape/portrait),\n"
                f"Platform (online/EGM/both), Region/Market, Target Audience, Grid/Reel Configuration,\n"
                f"Paylines (e.g. '243 ways to win'), Features list, Bonus mini game (if any), Jackpot type.\n\n"
                f"## 2. Theme Details Description\n"
                f"2-3 paragraphs describing the thematic world, mood, story, and player fantasy.\n\n"
                f"## 3. Background Description\n"
                f"Detailed visual description with PRIMARY COLOR PALETTE (hex codes).\n\n"
                f"## 4. Art Style Description\n"
                f"Detailed art direction: rendering style, texture approach, shading, color theory.\n\n"
                f"## 5. Symbol Hierarchy & Paytable\n"
                f"Design ALL symbols: WILD, SCATTER, H1-H2 high-pay, M1-M3 mid-pay, L1-L6 low-pay royals.\n"
                f"Each symbol needs: name, visual description, pay values for 2/3/4/5 of a kind.\n"
                f"Pay values in multiples of bet. Balanced for {idea.volatility.value} volatility.\n\n"
                f"## 6. Gameplay Contents\n"
                f"For EACH symbol type, describe its gameplay behavior.\n\n"
                f"## 7. Game Rules\n"
                f"Complete list: win direction, calculation, SCATTER rules, WILD rules, malfunction.\n\n"
                f"## 8. Feature Design (DETAILED)\n"
                f"For EACH feature: name, type, trigger conditions, awards, multipliers, retrigger,\n"
                f"expected RTP contribution percentage. Be EXACT — no vague specs.\n\n"
                f"## 9. Jackpot System (if applicable)\n"
                f"Tiers, base values, contribution rate, trigger mechanics, reset values.\n\n"
                f"## 10. RTP Budget Breakdown\n"
                f"CRITICAL: Base Game + Free Games + Each Feature + Jackpots = {idea.target_rtp}%\n"
                f"Every component must have an explicit percentage. Must sum exactly.\n"
                f"Use calculate_rtp_budget tool to validate your breakdown before finalizing.\n\n"
                f"## 11. Gameplay Animations\n"
                f"Reel spin, win highlights, feature transitions, big win celebrations.\n\n"
                f"## 12. Audio Design\n"
                f"Background music, ambient, SFX specs, dynamic audio behavior.\n\n"
                f"## 13. Interaction Design / UI Layout\n"
                f"Grid framing, HUD elements, mobile vs desktop adaptations.\n\n"
                f"## 14. Symbol ID Reference Table\n"
                f"Table: Sym Code, Symbol Name, Symbol Type for ALL symbols.\n\n"
                f"## 15. Differentiation Strategy\n"
                f"How this game stands apart from competitors.\n\n"
                f"═══ OUTPUT ═══\n"
                f"3000-5000 words minimum. All numbers must be exact, not placeholders.\n"
                f"Save to: {out}/02_design/gdd.md\n\n"
                f"SELF-CHECK: After saving, run the audit_gdd_quality tool on your GDD.\n"
                f"If your grade is C or D, fix the flagged sections BEFORE completing the task.\n"
                f"Target: grade B or above (all sections present, specific, complete)."
            ),
            expected_output="Complete GDD saved to file with all 15 sections",
            agent=self.agents["game_designer"],
        )

        # Math scaffold task — builds from GameIdeaInput directly, no GDD dependency
        math_scaffold_task = Task(
            description=(
                f"Build the INITIAL mathematical model scaffold for this slot game.\n\n"
                f"{math_examples}\n\n"
                f"IMPORTANT: The full GDD is being written in parallel. You DO NOT need to wait "
                f"for it. Build your model from the game parameters below. After the GDD completes, "
                f"a proofreader will reconcile any differences.\n\n"
                f"GAME SPECS (from input parameters — use these directly):\n"
                f"  Grid: {idea.grid_cols}x{idea.grid_rows}, {idea.ways_or_lines}\n"
                f"  Target RTP: {idea.target_rtp}% | Volatility: {idea.volatility.value}\n"
                f"  Max Win: {idea.max_win_multiplier}x | Markets: {idea.target_markets}\n"
                f"  Features: {[f.value for f in idea.requested_features]}\n"
                f"  Theme: {idea.theme} (for naming symbols thematically)\n\n"
                f"STANDARD SYMBOL SET (use this until GDD confirms specifics):\n"
                f"  WILD (1 symbol), SCATTER (1 symbol)\n"
                f"  High-pay: H1, H2 (themed to '{idea.theme}')\n"
                f"  Mid-pay: M1, M2, M3\n"
                f"  Low-pay: L1-L6 (standard card royals: A, K, Q, J, 10, 9)\n\n"
                f"Check jurisdiction constraints using search_regulations for each target market.\n"
                f"Use calculate_rtp_budget to verify your RTP breakdown sums correctly.\n\n"
                f"SIMULATION TEMPLATE to customize:\n```python\n{sim_template[:3000]}\n```\n\n"
                f"═══ SEQUENCE ═══\n"
                f"1. Design standard symbol hierarchy (WILD, SCATTER, H1-H2, M1-M3, L1-L6)\n"
                f"2. Build initial reel strips with proper distribution\n"
                f"3. Run {PipelineConfig.SIMULATION_SPINS:,}-spin Monte Carlo simulation\n"
                f"4. Use optimize_paytable to converge to {idea.target_rtp}% RTP (±0.1%)\n"
                f"5. Check jurisdiction caps (use search_regulations for each market)\n"
                f"6. Use model_player_behavior to validate player experience\n\n"
                f"═══ OUTPUT FILES ═══\n"
                f"FILE 1: {out}/03_math/BaseReels.csv — Pos,Reel 1,...,Reel 5\n"
                f"FILE 2: {out}/03_math/FreeReels.csv — same format for free games\n"
                f"FILE 3: {out}/03_math/FeatureReelStrips.csv — bonus mode strips\n"
                f"FILE 4: {out}/03_math/paytable.csv — Symbol,5OAK,4OAK,3OAK,2OAK\n"
                f"FILE 5: {out}/03_math/simulation_results.json — full results with rtp_breakdown\n"
                f"FILE 6: {out}/03_math/player_behavior.json\n\n"
                f"The simulation_results.json MUST include 'measured_rtp', 'max_win_achieved',\n"
                f"'hit_frequency_pct', 'volatility_index', 'total_spins', and 'rtp_breakdown' dict.\n\n"
                f"SELF-CHECK: After saving all files, run the check_paytable_sanity tool with:\n"
                f"  output_dir: {out}\n"
                f"If verdict is FAIL or NEEDS_FIXES, fix the reported issues before completing."
            ),
            expected_output="Complete math model scaffold with all 6 CSV/JSON files",
            agent=self.agents["mathematician"],
        )

        # Run GDD ∥ Math scaffold in parallel
        gdd_error = [None]
        math_error = [None]

        def _run_gdd():
            try:
                gdd_crew = Crew(
                    agents=[self.agents["game_designer"]],
                    tasks=[gdd_task],
                    process=Process.sequential, verbose=VERBOSE,
                )
                run_crew_with_timeout(gdd_crew, "design", console)
            except Exception as e:
                gdd_error[0] = str(e)

        def _run_math_scaffold():
            try:
                math_crew = Crew(
                    agents=[self.agents["mathematician"]],
                    tasks=[math_scaffold_task],
                    process=Process.sequential, verbose=VERBOSE,
                )
                run_crew_with_timeout(math_crew, "design", console)
            except Exception as e:
                math_error[0] = str(e)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(_run_gdd), executor.submit(_run_math_scaffold)]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    console.print(f"[yellow]⚠️ Parallel design error: {e}[/yellow]")

        for label, err in [("GDD", gdd_error[0]), ("Math scaffold", math_error[0])]:
            if err:
                console.print(f"[yellow]⚠️ {label} error: {err}[/yellow]")
            else:
                console.print(f"[green]✅ {label} complete[/green]")

        # ── PROOFREADER RECONCILIATION ──
        # Fast scan to catch gaps between GDD and Math before OODA loops
        console.print("[cyan]📋 Running GDD Proofreader — reconciling GDD ↔ Math...[/cyan]")
        proofread_task = Task(
            description=(
                f"RAPID RECONCILIATION: The GDD and math model were built in parallel.\n"
                f"Cross-check them for consistency and flag issues.\n\n"
                f"STEP 1: Run audit_gdd_quality on {out}/02_design/gdd.md\n"
                f"STEP 2: Run check_paytable_sanity on output_dir={out}\n"
                f"STEP 3: Read both files and cross-reference:\n"
                f"  - Do paytable symbols match GDD Section 5 symbol hierarchy?\n"
                f"  - Does the RTP budget in GDD Section 10 match simulation measured_rtp?\n"
                f"  - Are features in the math model the same as GDD Section 8?\n"
                f"  - Are pay values consistent between GDD and paytable.csv?\n\n"
                f"STEP 4: Save a structured report to {out}/02_design/proofread_report.json:\n"
                f'{{"gdd_grade": "A/B/C/D", "paytable_verdict": "PASS/FAIL",\n'
                f' "symbol_mismatches": [...], "rtp_delta": 0.X,\n'
                f' "feature_gaps": [...], "pay_value_conflicts": [...],\n'
                f' "issues_for_designer": [...], "issues_for_mathematician": [...]}}\n\n'
                f"Be fast and precise — this is a 60-second scan, not a full review."
            ),
            expected_output="Proofread report JSON saved",
            agent=self.agents["gdd_proofreader"],
        )
        proof_crew = Crew(
            agents=[self.agents["gdd_proofreader"]],
            tasks=[proofread_task],
            process=Process.sequential, verbose=VERBOSE,
        )
        run_crew_with_timeout(proof_crew, "recon", console)

        # Check proofread results — if issues found, do a quick targeted fix pass
        proof_path = Path(out, "02_design", "proofread_report.json")
        needs_reconcile = False
        if proof_path.exists():
            try:
                proof_data = json.loads(proof_path.read_text())
                designer_issues = proof_data.get("issues_for_designer", [])
                math_issues = proof_data.get("issues_for_mathematician", [])
                if designer_issues or math_issues:
                    needs_reconcile = True
                    console.print(f"[yellow]📋 Proofreader found {len(designer_issues)} GDD + {len(math_issues)} math issues — running quick fixes...[/yellow]")

                    reconcile_tasks = []
                    if designer_issues:
                        d_issues_text = "\n".join(f"  - {i}" for i in designer_issues)
                        reconcile_tasks.append(Task(
                            description=(
                                f"QUICK FIX — The proofreader found these GDD issues:\n{d_issues_text}\n\n"
                                f"Use patch_gdd_section to fix ONLY these specific issues in {out}/02_design/gdd.md\n"
                                f"Do NOT rewrite the full document. Target: 2-3 minute fix."
                            ),
                            expected_output="GDD sections patched",
                            agent=self.agents["game_designer"],
                        ))
                    if math_issues:
                        m_issues_text = "\n".join(f"  - {i}" for i in math_issues)
                        reconcile_tasks.append(Task(
                            description=(
                                f"QUICK FIX — The proofreader found these math model issues:\n{m_issues_text}\n\n"
                                f"Read the GDD: {out}/02_design/gdd.md to get correct values.\n"
                                f"Adjust reel strips/paytable to align with GDD. Re-run simulation.\n"
                                f"Save updated files to same paths in {out}/03_math/"
                            ),
                            expected_output="Math model reconciled with GDD",
                            agent=self.agents["mathematician"],
                        ))

                    # Run reconciliation fixes in parallel
                    if len(reconcile_tasks) >= 2:
                        console.print("[cyan]⚡ Running GDD fix ∥ Math fix in PARALLEL...[/cyan]")
                        rec_errors = [None, None]
                        def _fix_gdd():
                            try:
                                Crew(agents=[self.agents["game_designer"]], tasks=[reconcile_tasks[0]],
                                     process=Process.sequential, verbose=VERBOSE).kickoff()
                            except Exception as e:
                                rec_errors[0] = str(e)
                        def _fix_math():
                            try:
                                Crew(agents=[self.agents["mathematician"]], tasks=[reconcile_tasks[1]],
                                     process=Process.sequential, verbose=VERBOSE).kickoff()
                            except Exception as e:
                                rec_errors[1] = str(e)
                        with ThreadPoolExecutor(max_workers=2) as ex:
                            for f in as_completed([ex.submit(_fix_gdd), ex.submit(_fix_math)]):
                                try: f.result()
                                except Exception: pass
                    elif reconcile_tasks:
                        Crew(agents=[reconcile_tasks[0].agent], tasks=reconcile_tasks,
                             process=Process.sequential, verbose=VERBOSE).kickoff()
                else:
                    console.print("[green]📋 Proofreader: GDD ↔ Math aligned — no reconciliation needed[/green]")
            except (json.JSONDecodeError, ValueError):
                console.print("[yellow]⚠️ Could not parse proofread report[/yellow]")

        # ══════════════════════════════════════════════════════════════
        # CONVERGENCE LOOP: Observe → Orient → Decide → Act → Repeat
        # ══════════════════════════════════════════════════════════════
        converged = False
        for loop_num in range(1, MAX_CONVERGENCE_LOOPS + 1):
            _update_stage_db(self.state.job_id, f"OODA convergence check {loop_num}/{MAX_CONVERGENCE_LOOPS}")
            console.print(f"\n[bold cyan]🔍 OODA — Convergence Check {loop_num}/{MAX_CONVERGENCE_LOOPS}[/bold cyan]")
            emit("ooda_start", loop=loop_num, max=MAX_CONVERGENCE_LOOPS, phase="convergence_check")

            # ── OBSERVE: Compliance scan ∥ Math validation (PARALLEL) ──
            # Phase 12A: These are independent checks — run simultaneously
            console.print("[cyan]⚡ Running Compliance scan ∥ Math validation in PARALLEL...[/cyan]")
            emit("parallel_start", tasks=["Compliance Scan", "Math Validation"])

            compliance_scan_task = Task(
                description=(
                    f"QUICK REGULATORY SCAN of the game design.\n\n"
                    f"Read these files using the read_file tool:\n"
                    f"  1. GDD: {out}/02_design/gdd.md\n"
                    f"  2. Simulation: {out}/03_math/simulation_results.json\n"
                    f"  3. Paytable: {out}/03_math/paytable.csv\n\n"
                    f"Target markets: {idea.target_markets}\n"
                    f"Target RTP: {idea.target_rtp}% | Max Win: {idea.max_win_multiplier}x\n\n"
                    f"STEP 1: Run check_jurisdiction_compliance with:\n"
                    f"  markets: {idea.target_markets}\n"
                    f"  proposed_rtp: {idea.target_rtp}\n"
                    f"  proposed_max_win: {idea.max_win_multiplier}\n"
                    f"  proposed_features: {[f.value for f in idea.requested_features]}\n"
                    f"  game_theme: '{idea.theme}'\n"
                    f"This returns a structured per-market checklist with PASS/FAIL per requirement.\n\n"
                    f"STEP 2: For any BLOCKED or CONDITIONAL_PASS markets, use get_jurisdiction_profile\n"
                    f"to get the full profile and identify exact requirements that need addressing.\n\n"
                    f"STEP 3: Use search_regulations for any additional checks needed.\n"
                    f"Use patent_ip_scan to check game mechanics.\n\n"
                    f"Save a compliance scan report to: {out}/05_legal/convergence_scan_{loop_num}.md\n"
                    f"Include the structured checklist results and specific blockers with exact citations."
                ),
                expected_output="Compliance scan report saved with specific blockers and risks identified",
                agent=self.agents["compliance_officer"],
            )

            math_validation_task = Task(
                description=(
                    f"RAPID MATH VALIDATION — check the math model for correctness.\n\n"
                    f"STEP 1: Run check_paytable_sanity with output_dir={out}\n"
                    f"STEP 2: Run validate_convergence with:\n"
                    f"  output_dir={out}, target_rtp={idea.target_rtp},\n"
                    f"  max_win_target={idea.max_win_multiplier}, target_markets={idea.target_markets}\n"
                    f"STEP 3: Run audit_gdd_quality on {out}/02_design/gdd.md\n\n"
                    f"Save validation results to: {out}/03_math/validation_{loop_num}.json\n"
                    f"Format: {{'paytable_verdict': 'PASS/FAIL', 'convergence_status': '...', "
                    f"'gdd_grade': 'A/B/C/D', 'issues': [...]}}"
                ),
                expected_output="Math validation results saved as JSON",
                agent=self.agents["math_validator"],
            )

            scan_error = [None]
            validation_error = [None]
            def _run_comp_scan():
                try:
                    scan_crew = Crew(
                        agents=[self.agents["compliance_officer"]],
                        tasks=[compliance_scan_task],
                        process=Process.sequential, verbose=VERBOSE,
                    )
                    run_crew_with_timeout(scan_crew, "recon", console)
                except Exception as e:
                    scan_error[0] = str(e)

            def _run_math_val():
                try:
                    val_crew = Crew(
                        agents=[self.agents["math_validator"]],
                        tasks=[math_validation_task],
                        process=Process.sequential, verbose=VERBOSE,
                    )
                    run_crew_with_timeout(val_crew, "recon", console)
                except Exception as e:
                    validation_error[0] = str(e)

            with ThreadPoolExecutor(max_workers=2) as executor:
                for f in as_completed([executor.submit(_run_comp_scan), executor.submit(_run_math_val)]):
                    try: f.result()
                    except Exception as e:
                        console.print(f"[yellow]⚠️ Parallel observe error: {e}[/yellow]")

            for label, err in [("Compliance", scan_error[0]), ("Math validation", validation_error[0])]:
                if err:
                    console.print(f"[yellow]⚠️ {label} error: {err}[/yellow]")
                else:
                    console.print(f"[green]✅ {label} complete[/green]")

            # ── ORIENT + DECIDE: Producer runs convergence validator ──
            console.print(f"[bold cyan]📊 OODA — Producer Convergence Assessment[/bold cyan]")

            convergence_task = Task(
                description=(
                    f"You are the convergence judge. Assess whether the GDD, math model, and "
                    f"compliance scan are aligned and ready for production.\n\n"
                    f"STEP 1: Run the audit_gdd_quality tool with:\n"
                    f"  gdd_path: {out}/02_design/gdd.md\n"
                    f"This checks all 15 GDD sections for completeness and specificity.\n\n"
                    f"STEP 2: Run the check_paytable_sanity tool with:\n"
                    f"  output_dir: {out}\n"
                    f"This validates paytable structure and reel strip consistency.\n\n"
                    f"STEP 3: Run the validate_convergence tool with:\n"
                    f"  output_dir: {out}\n"
                    f"  target_rtp: {idea.target_rtp}\n"
                    f"  max_win_target: {idea.max_win_multiplier}\n"
                    f"  target_markets: {idea.target_markets}\n\n"
                    f"STEP 4: Read the compliance scan using read_file:\n"
                    f"  {out}/05_legal/convergence_scan_{loop_num}.md\n\n"
                    f"STEP 5: Based on ALL of the above, output EXACTLY this JSON structure to stdout AND "
                    f"save it to {out}/02_design/convergence_result_{loop_num}.json:\n\n"
                    f'{{"converged": true/false,\n'
                    f' "gdd_grade": "A/B/C/D (from quality audit)",\n'
                    f' "paytable_verdict": "PASS/NEEDS_FIXES/FAIL (from sanity check)",\n'
                    f' "blockers": [\n'
                    f'   {{"agent": "game_designer|mathematician|compliance_officer",\n'
                    f'    "issue": "specific problem description",\n'
                    f'    "instruction": "exact fix instruction for the agent"}}\n'
                    f' ],\n'
                    f' "warnings": ["non-blocking observation 1", ...],\n'
                    f' "summary": "one-line verdict"}}\n\n'
                    f"RULES:\n"
                    f"  - converged=true ONLY if: RTP within ±0.5%, max win within caps,\n"
                    f"    no jurisdiction blockers, no missing files, GDD grade ≥ B, paytable PASS\n"
                    f"  - Include GDD section issues as blockers for 'game_designer'\n"
                    f"  - Include paytable issues as blockers for 'mathematician'\n"
                    f"  - Be SPECIFIC in instructions — e.g. 'reduce max cascade multiplier\n"
                    f"    from 5x to 3x' not 'fix the multiplier'\n"
                    f"  - This is loop {loop_num}/{MAX_CONVERGENCE_LOOPS}."
                    f"{'  Last loop — set converged=true with warnings if issues are non-blocking.' if loop_num == MAX_CONVERGENCE_LOOPS else ''}"
                ),
                expected_output="Convergence assessment JSON saved to file",
                agent=self.agents["lead_producer"],
            )

            prod_crew = Crew(
                agents=[self.agents["lead_producer"]],
                tasks=[convergence_task],
                process=Process.sequential, verbose=VERBOSE,
            )
            run_crew_with_timeout(prod_crew, "recon", console)

            # ── Parse convergence result ──
            conv_result = {"converged": False, "blockers": [], "warnings": [], "summary": "No result"}
            conv_path = Path(out, "02_design", f"convergence_result_{loop_num}.json")
            if conv_path.exists():
                try:
                    conv_result = json.loads(conv_path.read_text())
                except (json.JSONDecodeError, ValueError):
                    # Try to extract JSON from task output
                    conv_text = conv_path.read_text()
                    json_match = re.search(r'\{.*\}', conv_text, re.DOTALL)
                    if json_match:
                        try:
                            conv_result = json.loads(json_match.group())
                        except (json.JSONDecodeError, ValueError):
                            pass

            self.state.convergence_history.append({
                "loop": loop_num,
                "converged": conv_result.get("converged", False),
                "blockers": len(conv_result.get("blockers", [])),
                "warnings": len(conv_result.get("warnings", [])),
                "summary": conv_result.get("summary", ""),
            })

            console.print(f"  Converged: {conv_result.get('converged', False)}")
            console.print(f"  Blockers: {len(conv_result.get('blockers', []))}")
            console.print(f"  Warnings: {len(conv_result.get('warnings', []))}")
            console.print(f"  Summary: {conv_result.get('summary', 'N/A')}")

            if conv_result.get("converged", False):
                console.print(f"[green]✅ CONVERGED after {loop_num} loop(s)[/green]")
                emit("ooda_result", converged=True, loop=loop_num, blockers=0, warnings=len(conv_result.get("warnings", [])))
                converged = True
                break

            if loop_num >= MAX_CONVERGENCE_LOOPS:
                console.print(f"[yellow]⚠️ Max {MAX_CONVERGENCE_LOOPS} loops reached — continuing with best available[/yellow]")
                emit("ooda_result", converged=False, loop=loop_num, blockers=len(conv_result.get("blockers", [])), warnings=len(conv_result.get("warnings", [])))
                break

            # ── ACT: Targeted revisions based on blockers ──
            _update_stage_db(self.state.job_id, f"OODA revision loop {loop_num + 1}/{MAX_CONVERGENCE_LOOPS}")
            console.print(f"\n[bold yellow]🔄 OODA — Revision Iteration {loop_num + 1}[/bold yellow]")
            emit("ooda_start", loop=loop_num + 1, max=MAX_CONVERGENCE_LOOPS, phase="revision")

            blockers = conv_result.get("blockers", [])
            designer_issues = [b for b in blockers if b.get("agent") == "game_designer"]
            math_issues = [b for b in blockers if b.get("agent") == "mathematician"]

            revision_tasks = []

            if designer_issues:
                issues_text = "\n".join(f"  - {b['issue']}: {b['instruction']}" for b in designer_issues)
                revision_tasks.append(Task(
                    description=(
                        f"REVISION REQUIRED — The convergence check found these issues with the GDD:\n\n"
                        f"{issues_text}\n\n"
                        f"Use the patch_gdd_section tool to update ONLY the affected sections.\n"
                        f"GDD path: {out}/02_design/gdd.md\n\n"
                        f"Read the current GDD first using read_file, then patch the specific "
                        f"sections that need changes. Do NOT rewrite the entire document.\n"
                        f"After patching, use calculate_rtp_budget to verify your RTP breakdown still sums correctly.\n"
                        f"Finally, run audit_gdd_quality on {out}/02_design/gdd.md to confirm grade ≥ B."
                    ),
                    expected_output="GDD sections patched and quality audit confirms grade B or above",
                    agent=self.agents["game_designer"],
                ))

            if math_issues or designer_issues:
                issues_text = "\n".join(
                    f"  - {b['issue']}: {b['instruction']}"
                    for b in (math_issues + designer_issues)
                )
                revision_tasks.append(Task(
                    description=(
                        f"MATH REVISION REQUIRED — The convergence check found these issues:\n\n"
                        f"{issues_text}\n\n"
                        f"Read the current GDD using read_file: {out}/02_design/gdd.md\n"
                        f"Read previous simulation: {out}/03_math/simulation_results.json\n\n"
                        f"Adjust reel strips and/or paytable to resolve the issues.\n"
                        f"Re-run the Monte Carlo simulation ({PipelineConfig.SIMULATION_SPINS:,} spins).\n"
                        f"Use optimize_paytable to re-converge to {idea.target_rtp}% RTP.\n"
                        f"Check jurisdiction caps using search_regulations.\n\n"
                        f"After saving, run check_paytable_sanity with output_dir: {out}\n"
                        f"Fix any FAIL/NEEDS_FIXES issues before completing.\n\n"
                        f"Save updated files to the SAME paths:\n"
                        f"  {out}/03_math/BaseReels.csv\n"
                        f"  {out}/03_math/paytable.csv\n"
                        f"  {out}/03_math/simulation_results.json"
                    ),
                    expected_output="Updated math model files saved and paytable sanity check passes",
                    agent=self.agents["mathematician"],
                ))

            if revision_tasks:
                # ══════════════════════════════════════════════════════════════
                # PHASE 12: PARALLEL REVISIONS
                # Designer patches GDD ∥ Mathematician fixes math model
                # Sequential: ~10 min. Parallel: ~6 min (limited by slowest)
                # Safe because: designer patches GDD sections, mathematician
                # reads GDD for context but fixes math files independently.
                # ══════════════════════════════════════════════════════════════
                if len(revision_tasks) >= 2:
                    console.print("[cyan]⚡ Running designer + math revisions in PARALLEL...[/cyan]")
                    emit("parallel_start", tasks=["GDD Revision", "Math Revision"])
                    rev_errors = [None, None]

                    def _run_designer_rev():
                        try:
                            d_crew = Crew(
                                agents=[self.agents["game_designer"]],
                                tasks=[revision_tasks[0]],
                                process=Process.sequential, verbose=VERBOSE,
                            )
                            run_crew_with_timeout(d_crew, "design", console)
                        except Exception as e:
                            rev_errors[0] = str(e)

                    def _run_math_rev():
                        try:
                            m_crew = Crew(
                                agents=[self.agents["mathematician"]],
                                tasks=[revision_tasks[1]],
                                process=Process.sequential, verbose=VERBOSE,
                            )
                            run_crew_with_timeout(m_crew, "design", console)
                        except Exception as e:
                            rev_errors[1] = str(e)

                    with ThreadPoolExecutor(max_workers=2) as executor:
                        futures = [
                            executor.submit(_run_designer_rev),
                            executor.submit(_run_math_rev),
                        ]
                        for future in as_completed(futures):
                            try:
                                future.result()
                            except Exception as e:
                                console.print(f"[yellow]⚠️ Parallel revision error: {e}[/yellow]")

                    for i, label in enumerate(["GDD", "Math"]):
                        if rev_errors[i]:
                            console.print(f"[yellow]⚠️ {label} revision error: {rev_errors[i]}[/yellow]")
                        else:
                            console.print(f"[green]✅ {label} revision complete[/green]")
                else:
                    # Single revision task — run normally
                    rev_agents = list({t.agent for t in revision_tasks})
                    rev_crew = Crew(
                        agents=rev_agents,
                        tasks=revision_tasks,
                        process=Process.sequential, verbose=VERBOSE,
                    )
                    run_crew_with_timeout(rev_crew, "design", console)

        self.state.convergence_loops_run = loop_num if 'loop_num' in dir() else 0

        # ── Read final outputs ──
        gdd_path = Path(out, "02_design", "gdd.md")
        gdd_file_text = ""
        if gdd_path.exists():
            gdd_file_text = gdd_path.read_text(encoding="utf-8", errors="replace")
            console.print(f"[green]✅ GDD file: {len(gdd_file_text)} chars[/green]")
        else:
            console.print("[yellow]⚠️ GDD file not found, using task output[/yellow]")
            gdd_file_text = str(gdd_task.output) if gdd_task else ""

        self.state.gdd = {"output": gdd_file_text}
        self.state.math_model = {"output": ""}

        sim_path = Path(out, "03_math", "simulation_results.json")
        if sim_path.exists():
            try:
                self.state.math_model["results"] = json.loads(sim_path.read_text())
                self.state.math_model["output"] = sim_path.read_text()
            except json.JSONDecodeError:
                pass

        # Save convergence history
        conv_log_path = Path(out, "02_design", "convergence_history.json")
        conv_log_path.write_text(json.dumps({
            "loops_run": self.state.convergence_loops_run,
            "converged": converged,
            "history": self.state.convergence_history,
        }, indent=2), encoding="utf-8")

        console.print(f"[green]✅ GDD + Math complete — {self.state.convergence_loops_run} OODA loop(s), converged={converged}[/green]")
        emit("stage_done", name="Design & Math", num=2, loops=self.state.convergence_loops_run, converged=converged)
        self._stage_exit("design_and_math")

    @listen(run_design_and_math)
    def checkpoint_design(self):
        self._stage_enter("checkpoint_design")
        # Phase 3A: Auto-approve in iterate mode
        if self.state.iterate_mode:
            self.state.design_math_approved = True
            console.print("[dim]⏭️ Design review auto-approved (iterate mode)[/dim]")
            self._stage_exit("checkpoint_design")
            return

        # Phase 5: Auto-approve under time pressure
        decision = self._budget_decision("checkpoint_design")
        if decision == "skip":
            self._skip_stage("checkpoint_design", "auto-approving to preserve budget")
            self.state.design_math_approved = True
            self._stage_exit("checkpoint_design")
            return

        _update_stage_db(self.state.job_id, "Design review")
        if not self.state.research_approved:
            self._stage_exit("checkpoint_design")
            return
        # Adversarial review of GDD + Math
        self._run_adversarial_review("post_design_math",
            f"Theme: {self.state.game_idea.theme}\n"
            f"Markets: {self.state.game_idea.target_markets}\n"
            f"GDD: {str(self.state.gdd.get('output',''))[:2000]}\n"
            f"Math: {str(self.state.math_model.get('output',''))[:2000]}")

        self.state.design_math_approved = hitl_checkpoint(
            "post_design_math",
            f"GDD + Math complete. This is the CRITICAL checkpoint.\n"
            f"GDD: {self.state.output_dir}/02_design/\nMath: {self.state.output_dir}/03_math/\n"
            f"Adversarial review: {self.state.output_dir}/adversarial_review_post_design_math.md",
            self.state, auto=self.auto_mode,
        )
        self._stage_exit("checkpoint_design")

    # ---- Stage 4: Mood Boards ----

    @listen(checkpoint_design)
    def run_mood_boards(self):
        self._stage_enter("mood_boards")
        # ACP: Check feature flag
        if not self._flag("enable_mood_boards", True):
            self.state.mood_board = {"output": "Mood boards disabled via ACP."}
            self.state.mood_board_approved = True
            console.print("[dim]⏭️ Mood boards skipped (enable_mood_boards=false)[/dim]")
            self._stage_exit("mood_boards")
            return
        # Phase 3A: Skip mood boards unless art is being re-run
        if self.state.iterate_mode:
            rerun = set(self.state.iterate_config.get("rerun_stages", []))
            if "art" not in rerun:
                self.state.mood_board_approved = True
                console.print("[dim]⏭️ Skipping mood boards (iterate mode — art not selected)[/dim]")
                self._stage_exit("mood_boards")
                return

        # Phase 5: Smart skip/compress under time pressure
        decision = self._budget_decision("mood_boards")
        if decision == "skip":
            self._skip_stage("mood_boards", "time pressure — production will use default art direction")
            self.state.mood_board = {"output": "Mood boards skipped due to time pressure. Production will use theme defaults."}
            self.state.mood_board_approved = True  # auto-approve so production proceeds
            self._stage_exit("mood_boards")
            return
        is_lite = (decision == "lite")
        if is_lite:
            self._skip_stage("mood_boards", "generating 1 quick concept instead of full exploration", decision="lite")

        _update_stage_db(self.state.job_id, f"Mood boards{' (lite)' if is_lite else ''}")
        if not self.state.design_math_approved:
            self._stage_exit("mood_boards")
            return
        mode_label = " [bold yellow]⚡ LITE[/bold yellow]" if is_lite else ""
        console.print(f"\n[bold magenta]🎨 Stage 3a: Mood Boards{mode_label}[/bold magenta]\n")
        desc = "Quick single concept generation" if is_lite else "Generating visual style and mood board concepts"
        emit("stage_start", name="Mood Boards", num="3a", icon="🎨", desc=desc)
        emit("agent_start", agent="Design Agent", task="Mood board generation")
        idea = self.state.game_idea
        try:
            art_examples = get_art_director_examples()
        except Exception:
            art_examples = ""

        # Phase 5: Lite mode — 1 mood board, no vision QA re-checks
        variants = 1 if is_lite else PipelineConfig.MOOD_BOARD_VARIANTS
        qa_instruction = (
            "Skip vision_qa for speed — focus on getting one strong concept down."
            if is_lite else
            "CRITICAL: After EACH image, use vision_qa to check quality:\n"
            "  - Theme adherence, distinctiveness, scalability, emotional impact\n"
            "  - If vision_qa returns FAIL, adjust the prompt and regenerate"
        )

        if variants <= 1 or is_lite:
            # Single variant — run normally
            mood_task = Task(
                description=(
                    f"Create 1 mood board variant for '{idea.theme}'.\n"
                    f"Style: {idea.art_style}\n\n"
                    f"{art_examples}\n\n"
                    f"Define style direction, color palette (6-8 hex codes), mood keywords.\n"
                    f"Use the generate_image tool to create a concept image.\n"
                    f"{qa_instruction}\n"
                    f"Save images to: {self.state.output_dir}/04_art/mood_boards/\n"
                    f"Recommend the best direction for production."
                ),
                expected_output="Mood board variant with image saved",
                agent=self.agents["art_director"],
            )
            crew = Crew(agents=[self.agents["art_director"]], tasks=[mood_task], process=Process.sequential, verbose=VERBOSE)
            result = run_crew_with_timeout(crew, "mood_board", console)
        else:
            # ══════════════════════════════════════════════════════════════
            # PHASE 12A: PARALLEL MOOD BOARD VARIANTS
            # Before: 4 variants sequential by 1 agent (~10 min)
            # After:  4 variants parallel (~3 min)
            # Each variant gets a distinct style direction to explore
            # ══════════════════════════════════════════════════════════════
            console.print(f"[cyan]⚡ Generating {variants} mood board variants in PARALLEL...[/cyan]")
            emit("parallel_start", tasks=[f"Variant {i+1}" for i in range(variants)])

            style_directions = [
                "Bold and vivid — saturated colors, high contrast, maximum visual impact",
                "Elegant and refined — muted tones, gold accents, premium feel",
                "Dark and atmospheric — deep shadows, neon highlights, cinematic mood",
                "Whimsical and playful — bright pastels, rounded shapes, fun energy",
            ][:variants]

            mood_results = [None] * variants
            mood_errors = [None] * variants

            def _gen_variant(idx):
                try:
                    variant_task = Task(
                        description=(
                            f"Create mood board VARIANT {idx + 1} of {variants} for '{idea.theme}'.\n"
                            f"Style: {idea.art_style}\n"
                            f"Direction: {style_directions[idx]}\n\n"
                            f"{art_examples}\n\n"
                            f"Define style direction, color palette (6-8 hex codes), mood keywords.\n"
                            f"Use the generate_image tool to create a concept image.\n"
                            f"{qa_instruction}\n"
                            f"Save image to: {self.state.output_dir}/04_art/mood_boards/variant_{idx + 1}.png\n"
                            f"Save description to: {self.state.output_dir}/04_art/mood_boards/variant_{idx + 1}.md\n"
                            f"Include: style direction, color palette hex codes, mood keywords, visual treatment."
                        ),
                        expected_output=f"Variant {idx + 1} mood board with image saved",
                        agent=self.agents["art_director"],
                    )
                    v_crew = Crew(
                        agents=[self.agents["art_director"]],
                        tasks=[variant_task],
                        process=Process.sequential, verbose=VERBOSE,
                    )
                    mood_results[idx] = run_crew_with_timeout(v_crew, "mood_board", console)
                except Exception as e:
                    mood_errors[idx] = str(e)

            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = [executor.submit(_gen_variant, i) for i in range(variants)]
                for f in as_completed(futures):
                    try: f.result()
                    except Exception as e:
                        console.print(f"[yellow]⚠️ Mood board parallel error: {e}[/yellow]")

            for i in range(variants):
                if mood_errors[i]:
                    console.print(f"[yellow]⚠️ Variant {i+1} error: {mood_errors[i]}[/yellow]")
                else:
                    console.print(f"[green]✅ Variant {i+1} complete[/green]")

            result = "; ".join(str(r) for r in mood_results if r)

        self.state.mood_board = {"output": str(result), "mode": "lite" if is_lite else "full"}
        console.print("[green]✅ Mood boards generated[/green]")
        emit("stage_done", name="Mood Boards", num="3a")
        self._stage_exit("mood_boards")

    @listen(run_mood_boards)
    def checkpoint_art(self):
        self._stage_enter("checkpoint_art")
        # Phase 3A: Auto-approve in iterate mode
        if self.state.iterate_mode:
            self.state.mood_board_approved = True
            console.print("[dim]⏭️ Art review auto-approved (iterate mode)[/dim]")
            self._stage_exit("checkpoint_art")
            return

        # Phase 5: Auto-approve under time pressure
        decision = self._budget_decision("checkpoint_art")
        if decision == "skip":
            self._skip_stage("checkpoint_art", "auto-approving to preserve budget")
            self.state.mood_board_approved = True
            self._stage_exit("checkpoint_art")
            return

        _update_stage_db(self.state.job_id, "Art direction review")
        if not self.state.design_math_approved:
            self._stage_exit("checkpoint_art")
            return
        # Adversarial review of art
        self._run_adversarial_review("post_art_review",
            f"Theme: {self.state.game_idea.theme}\n"
            f"Art Style: {self.state.game_idea.art_style}\n"
            f"Mood Board Output: {str(self.state.mood_board.get('output',''))[:2000]}")

        self.state.mood_board_approved = hitl_checkpoint(
            "post_art_review",
            f"Mood boards in: {self.state.output_dir}/04_art/mood_boards/\n"
            f"Adversarial review: {self.state.output_dir}/adversarial_review_post_art_review.md\n"
            f"Select preferred direction.",
            self.state, auto=self.auto_mode,
        )
        self._stage_exit("checkpoint_art")

    # ---- Stage 5: Full Production ----

    @listen(checkpoint_art)
    def run_production(self):
        self._stage_enter("production")
        _update_stage_db(self.state.job_id, "Art ∥ Audio ∥ Anim ∥ Compliance ∥ Patent (5-way parallel)")
        if not self.state.mood_board_approved:
            self._stage_exit("production")
            return

        # Phase 3A: In iterate mode, only run selected production stages
        if self.state.iterate_mode:
            rerun = set(self.state.iterate_config.get("rerun_stages", []))
            if not rerun.intersection({"art", "compliance"}):
                console.print("[dim]⏭️ Skipping production (iterate mode — art/compliance not selected)[/dim]")
                self._stage_exit("production")
                return

        console.print("\n[bold magenta]🎨⚖️ Stage 3b: Production + Compliance (5-WAY PARALLEL)[/bold magenta]\n")
        emit("stage_start", name="Production + Compliance", num="3b", icon="⚡", desc="5-way parallel: Art, Audio, Animation, Compliance, Patent/IP")
        idea = self.state.game_idea
        gdd_ctx = str(self.state.gdd.get("output", ""))[:5000]
        math_ctx = str(self.state.math_model.get("output", ""))[:3000]

        # ══════════════════════════════════════════════════════════════
        # SYMBOL-AWARE ART GENERATION (DETERMINISTIC)
        # The LLM agent defines art direction. Then we call generate_image
        # directly in a Python loop — guaranteeing every symbol is created.
        # ══════════════════════════════════════════════════════════════
        symbol_names = _extract_symbols_from_gdd(gdd_ctx, idea.theme)
        symbols_dir = f"{self.state.output_dir}/04_art/symbols"
        bg_dir = f"{self.state.output_dir}/04_art/backgrounds"
        ui_dir = f"{self.state.output_dir}/04_art/ui"

        # Lightweight agent task: define color palette and style direction only
        art_direction_task = Task(
            description=(
                f"Define the visual art direction for '{idea.theme}'.\n\n"
                f"GDD context:\n{gdd_ctx[:3000]}\n\n"
                f"INSTRUCTIONS:\n"
                f"1. Define the color palette: 6-8 hex codes with emotional intent\n"
                f"2. Describe the visual treatment for high-pay vs low-pay symbols\n"
                f"3. Describe the background atmosphere and lighting direction\n"
                f"4. Write a 1-sentence DALL-E style modifier for this theme\n"
                f"   (e.g. 'cinematic golden lighting, rich jewel tones, epic fantasy realism')\n\n"
                f"Save the art direction brief to: {self.state.output_dir}/04_art/art_direction.md\n"
                f"Use the file_writer tool to save it.\n\n"
                f"DO NOT generate any images — just define the direction.\n"
                f"The pipeline will auto-generate all {len(symbol_names)} symbols + backgrounds + logo."
            ),
            expected_output="Art direction brief saved to 04_art/art_direction.md",
            agent=self.agents["art_director"],
        )

        # The actual art generation happens deterministically in _run_art below
        _art_symbol_names = symbol_names
        _art_symbols_dir = symbols_dir
        _art_bg_dir = bg_dir
        _art_ui_dir = ui_dir
        _art_theme = idea.theme
        _art_style = idea.art_style

        art_task = art_direction_task

        audio_task = Task(
            description=(
                f"Generate the COMPLETE audio package for '{idea.theme}'.\n\n"
                f"GDD context:\n{gdd_ctx[:1500]}\n\n"
                f"═══ SINGLE STEP — Generate ALL Audio ═══\n"
                f"Call sound_design ONCE with:\n"
                f"  action='full'\n"
                f"  theme='{idea.theme}'\n"
                f"  output_dir='{self.state.output_dir}/04_audio/'\n\n"
                f"This single call will:\n"
                f"  1. Create a comprehensive audio design brief document\n"
                f"  2. Generate ALL 13 core sound effects via ElevenLabs:\n"
                f"     spin_start, reel_tick, spin_stop, win_small, win_medium,\n"
                f"     win_big, win_mega, scatter_land, bonus_trigger,\n"
                f"     free_spin_start, anticipation, button_click, ambient\n\n"
                f"IMPORTANT: You MUST call the sound_design tool with action='full'.\n"
                f"Do NOT call generate_sfx individually — the 'full' action handles everything.\n"
                f"Do NOT just describe the sounds — actually call the tool.\n\n"
                f"Save directory: {self.state.output_dir}/04_audio/"
            ),
            expected_output="Audio design brief + 13 sound effects generated and saved to 04_audio/",
            agent=self.agents["art_director"],
        )

        compliance_task = Task(
            description=(
                f"Review game package for full regulatory compliance.\n\n"
                f"Target jurisdictions: {idea.target_markets}\n"
                f"GDD:\n{gdd_ctx}\nMath:\n{math_ctx}\n\n"
                f"═══ STEP 1 — STRUCTURED COMPLIANCE CHECK ═══\n"
                f"Run check_jurisdiction_compliance with:\n"
                f"  markets: {idea.target_markets}\n"
                f"  proposed_rtp: {idea.target_rtp}\n"
                f"  proposed_max_win: {idea.max_win_multiplier}\n"
                f"  proposed_features: {[f.value for f in idea.requested_features]}\n"
                f"  game_theme: '{idea.theme}'\n"
                f"This gives you PASS/FAIL per requirement per market.\n\n"
                f"═══ STEP 2 — DEEP DIVE ON FLAGGED MARKETS ═══\n"
                f"For any BLOCKED or CONDITIONAL_PASS markets, run get_jurisdiction_profile\n"
                f"to get full requirements. Cross-reference with the GDD and math model.\n"
                f"Use search_regulations for any additional checks.\n"
                f"Use patent_ip_scan to check ALL proposed mechanics for IP conflicts.\n\n"
                f"═══ STEP 3 — CERTIFICATION PATH ═══\n"
                f"Use certification_planner to map the full cert journey:\n"
                f"  - Recommended test lab, applicable standards (GLI-11, etc.)\n"
                f"  - Timeline and cost estimate per market\n"
                f"  - Submission documentation checklist\n\n"
                f"═══ STEP 4 — SAVE STRUCTURED JSON ═══\n"
                f"Save to: {self.state.output_dir}/05_legal/compliance_report.json\n"
                f"Format:\n"
                f"{{\n"
                f'  "overall_status": "green|yellow|red",\n'
                f'  "flags": [\n'
                f'    {{"jurisdiction": "...", "category": "...", "risk_level": "low|medium|high",\n'
                f'     "finding": "...", "recommendation": "..."}}\n'
                f"  ],\n"
                f'  "ip_assessment": {{\n'
                f'    "theme_clear": true|false,\n'
                f'    "potential_conflicts": ["..."],\n'
                f'    "trademarked_terms_to_avoid": ["..."],\n'
                f'    "recommendation": "..."\n'
                f"  }},\n"
                f'  "patent_risks": [\n'
                f'    {{"mechanic": "...", "risk_level": "...", "details": "..."}}\n'
                f"  ],\n"
                f'  "jurisdiction_summary": {{\n'
                f'    "Georgia": {{"status": "...", "min_rtp": ..., "max_win_limit": ..., "notes": "..."}},\n'
                f'    "Texas": {{"status": "...", "min_rtp": ..., "max_win_limit": ..., "notes": "..."}}\n'
                f"  }},\n"
                f'  "certification_path": [\n'
                f'    "Step 1: ...", "Step 2: ..."\n'
                f"  ]\n"
                f"}}\n\n"
                f"CRITICAL: The JSON file MUST be valid JSON with the keys above.\n"
                f"Also save cert plan to: {self.state.output_dir}/05_legal/certification_plan.json"
            ),
            expected_output="Compliance report + certification plan saved as structured JSON",
            agent=self.agents["compliance_officer"],
        )

        # ══════════════════════════════════════════════════════════════
        # Phase 12: 5-WAY PARALLEL PRODUCTION
        # Art ∥ Audio ∥ Animation ∥ Compliance ∥ Patent/IP
        # Phase 7A was 2-way: art+audio ∥ compliance (~20 min)
        # Phase 12 is 5-way: art ∥ audio ∥ anim ∥ compliance ∥ patent (~12 min)
        # ══════════════════════════════════════════════════════════════
        # Phase 3A: Selectively run branches in iterate mode
        iter_rerun = set(self.state.iterate_config.get("rerun_stages", [])) if self.state.iterate_mode else {"art", "compliance"}
        run_art = "art" in iter_rerun
        run_comp = "compliance" in iter_rerun

        _update_stage_db(self.state.job_id, "Art ∥ Audio ∥ Anim ∥ Compliance ∥ Patent (5-way parallel)")
        stages_running = []
        if run_art: stages_running.extend(["Art", "Audio", "Animation"])
        if run_comp: stages_running.extend(["Compliance", "Patent/IP"])
        console.print(f"[cyan]⚡ Running {' ∥ '.join(stages_running)} in 5-WAY PARALLEL...[/cyan]")
        emit("parallel_start", tasks=stages_running)

        art_crew_result = [None]
        audio_crew_result = [None]
        anim_crew_result = [None]
        compliance_crew_result = [None]
        patent_crew_result = [None]
        art_crew_error = [None]
        audio_crew_error = [None]
        anim_crew_error = [None]
        compliance_crew_error = [None]
        patent_crew_error = [None]

        def _run_art():
            """Branch 1: Art direction brief (agent) + deterministic image generation (loop)."""
            try:
                # Phase A: Let agent define art direction (palette, style modifiers)
                art_only_crew = Crew(
                    agents=[self.agents["art_director"]],
                    tasks=[art_task],
                    process=Process.sequential, verbose=VERBOSE,
                )
                art_crew_result[0] = run_crew_with_timeout(art_only_crew, "production", console)

                # Phase B: Read art direction brief for style modifier
                style_modifier = f"{_art_style} style"
                brief_path = Path(self.state.output_dir, "04_art", "art_direction.md")
                if brief_path.exists():
                    brief_text = brief_path.read_text(encoding="utf-8")[:1000]
                    # Extract style modifier line if present
                    for line in brief_text.split("\n"):
                        if "dall-e" in line.lower() or "modifier" in line.lower() or "style:" in line.lower():
                            cleaned = line.split(":", 1)[-1].strip().strip("'\"`)>")
                            if len(cleaned) > 10:
                                style_modifier = cleaned
                                break

                # Phase C: Deterministic image generation — every symbol guaranteed
                from tools.custom_tools import ImageGenerationTool
                img_gen = ImageGenerationTool()
                manifest = {"symbols": [], "backgrounds": [], "ui": [], "style_modifier": style_modifier}

                console.print(f"\n[bold green]🎨 Generating {len(_art_symbol_names)} symbols deterministically...[/bold green]")
                emit("step_log", msg=f"Generating {len(_art_symbol_names)} symbol images...")

                for i, sym in enumerate(_art_symbol_names):
                    slug = "".join(c if c.isalnum() else "_" for c in sym.lower()).strip("_")
                    prompt = (
                        f"Slot game symbol: {sym}. {_art_theme} theme. "
                        f"{style_modifier}. Isolated on transparent dark background, "
                        f"highly detailed game-ready icon, centered composition, "
                        f"rich colors, no text or letters in the image."
                    )
                    console.print(f"  [{i+1}/{len(_art_symbol_names)}] {sym}...")
                    emit("step_log", msg=f"Symbol {i+1}/{len(_art_symbol_names)}: {sym}")

                    try:
                        result = json.loads(img_gen._run(
                            prompt=prompt,
                            size="1024x1024",
                            asset_name=f"symbol_{slug}",
                            output_dir=_art_symbols_dir,
                        ))
                        manifest["symbols"].append({
                            "name": sym, "slug": slug,
                            "file": result.get("file_path", ""),
                            "status": result.get("status", "unknown"),
                        })
                        if result.get("status") == "success":
                            console.print(f"    [green]✓[/green] {result.get('file_path', '')}")
                        else:
                            console.print(f"    [yellow]⚠ {result.get('reason', 'skipped')}[/yellow]")
                    except Exception as sym_err:
                        console.print(f"    [red]✗ {sym_err}[/red]")
                        manifest["symbols"].append({
                            "name": sym, "slug": slug, "file": "", "status": "error",
                            "error": str(sym_err),
                        })

                # Phase D: Backgrounds
                console.print("\n[bold green]🖼️ Generating backgrounds...[/bold green]")
                emit("step_log", msg="Generating background images...")

                bg_specs = [
                    ("background_base", f"Slot game background, {_art_theme}, {style_modifier}, "
                     f"dark atmospheric environment, reel area center, wide cinematic, game UI ready"),
                    ("background_freespin", f"Slot game bonus free spin background, {_art_theme}, "
                     f"{style_modifier}, enhanced magical version, dramatic lighting, energy effects, wide cinematic"),
                ]
                for bg_name, bg_prompt in bg_specs:
                    try:
                        result = json.loads(img_gen._run(
                            prompt=bg_prompt, size="1792x1024",
                            asset_name=bg_name, output_dir=_art_bg_dir,
                        ))
                        manifest["backgrounds"].append({
                            "name": bg_name, "file": result.get("file_path", ""),
                            "status": result.get("status", "unknown"),
                        })
                        console.print(f"  [green]✓[/green] {bg_name}")
                    except Exception as bg_err:
                        console.print(f"  [red]✗ {bg_name}: {bg_err}[/red]")
                        manifest["backgrounds"].append({"name": bg_name, "status": "error", "error": str(bg_err)})

                # Phase E: Logo
                console.print("\n[bold green]✨ Generating game logo...[/bold green]")
                emit("step_log", msg="Generating game logo...")
                try:
                    result = json.loads(img_gen._run(
                        prompt=(f"Game logo: {_art_theme}, {style_modifier}, metallic embossed "
                                f"lettering, transparent background, slot game branding, premium quality"),
                        size="1024x1024",
                        asset_name="game_logo",
                        output_dir=_art_ui_dir,
                    ))
                    manifest["ui"].append({
                        "name": "game_logo", "file": result.get("file_path", ""),
                        "status": result.get("status", "unknown"),
                    })
                    console.print(f"  [green]✓[/green] game_logo")
                except Exception as logo_err:
                    console.print(f"  [red]✗ logo: {logo_err}[/red]")
                    manifest["ui"].append({"name": "game_logo", "status": "error", "error": str(logo_err)})

                # Phase F: Save manifest
                manifest_path = Path(self.state.output_dir, "04_art", "manifest.json")
                manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

                success_count = sum(1 for s in manifest["symbols"] if s["status"] == "success")
                total = len(manifest["symbols"]) + len(manifest["backgrounds"]) + len(manifest["ui"])
                total_ok = success_count + sum(1 for b in manifest["backgrounds"] if b["status"] == "success") + sum(1 for u in manifest["ui"] if u["status"] == "success")
                console.print(f"\n[bold]📊 Art generation complete: {total_ok}/{total} assets generated[/bold]")
                emit("step_log", msg=f"Art complete: {total_ok}/{total} assets")

            except Exception as e:
                art_crew_error[0] = str(e)

        def _run_audio():
            """Branch 2: Audio Engineer — dedicated audio production."""
            try:
                # Re-assign audio task to dedicated audio engineer agent
                audio_task_dedicated = Task(
                    description=audio_task.description,
                    expected_output=audio_task.expected_output,
                    agent=self.agents["audio_engineer"],
                )
                audio_only_crew = Crew(
                    agents=[self.agents["audio_engineer"]],
                    tasks=[audio_task_dedicated],
                    process=Process.sequential, verbose=VERBOSE,
                )
                audio_crew_result[0] = run_crew_with_timeout(audio_only_crew, "production", console)
            except Exception as e:
                audio_crew_error[0] = str(e)

        def _run_animation():
            """Branch 3: Animation Director — full animation spec + keyframes."""
            try:
                anim_task = Task(
                    description=(
                        f"Create the COMPLETE animation package for '{idea.theme}'.\n\n"
                        f"GDD context (read Section 11 — Gameplay Animations + symbol hierarchy):\n"
                        f"{gdd_ctx}\n\n"
                        f"GAME SPECS:\n"
                        f"  Grid: {idea.grid_cols}x{idea.grid_rows} | {idea.ways_or_lines}\n"
                        f"  Volatility: {idea.volatility.value} | Max Win: {idea.max_win_multiplier}x\n"
                        f"  Features: {[f.value for f in idea.requested_features]}\n\n"

                        f"═══ DELIVERABLE 1: ANIMATION SPEC (JSON) ═══\n"
                        f"Save to: {self.state.output_dir}/04_animation/animation_spec.json\n\n"
                        f"Structure:\n"
                        f"{{\n"
                        f'  "meta": {{"game": "{idea.theme}", "target_fps": 60, "gpu_only": true}},\n'
                        f'  "reel_mechanics": {{\n'
                        f'    "spin_start": {{"duration_ms": ..., "easing": "cubic-bezier(...)", "keyframes": [...]}},\n'
                        f'    "reel_stop_bounce": {{"duration_ms": ..., "easing": "...", "bounce_height_px": ..., "keyframes": [...]}},\n'
                        f'    "scatter_slowdown": {{"duration_ms": ..., "speed_multiplier": 0.3, "wobble_amplitude_deg": ...}},\n'
                        f'    "anticipation_wobble": {{"duration_ms": ..., "easing": "...", "keyframes": [...]}}\n'
                        f"  }},\n"
                        f'  "symbol_animations": {{\n'
                        f'    "idle_breathe": {{"duration_ms": ..., "scale_range": [0.98, 1.02], "easing": "...", "loop": true}},\n'
                        f'    "land_impact": {{"duration_ms": ..., "keyframes": [...]}},\n'
                        f'    "win_small": {{"duration_ms": ..., "glow_color": "#...", "keyframes": [...]}},\n'
                        f'    "win_medium": {{"duration_ms": ..., "particles": {{"type": "...", "count": ..., "spread_deg": ...}}, "keyframes": [...]}},\n'
                        f'    "win_big": {{"duration_ms": ..., "screen_shake": true, "particles": {{...}}, "keyframes": [...]}},\n'
                        f'    "win_mega": {{"duration_ms": ..., "full_screen_takeover": true, "buildup_ms": ..., "keyframes": [...]}},\n'
                        f'    "scatter_activate": {{"duration_ms": ..., "keyframes": [...]}},\n'
                        f'    "wild_expand": {{"duration_ms": ..., "expand_direction": "...", "keyframes": [...]}}\n'
                        f"  }},\n"
                        f'  "win_celebrations": {{\n'
                        f'    "win_line_trace": {{"duration_ms": ..., "line_width_px": ..., "glow_radius_px": ...}},\n'
                        f'    "win_amount_countup": {{"duration_ms": ..., "easing": "ease-out", "number_format": "..."}},\n'
                        f'    "multiplier_reveal": {{"duration_ms": ..., "keyframes": [...]}},\n'
                        f'    "big_win_banner": {{"duration_ms": ..., "keyframes": [...]}},\n'
                        f'    "coin_shower": {{"duration_ms": ..., "particle_count": ..., "gravity": ..., "spread_deg": ...}},\n'
                        f'    "mega_win_screen_shake": {{"duration_ms": ..., "amplitude_px": ..., "frequency_hz": ...}}\n'
                        f"  }},\n"
                        f'  "feature_transitions": {{\n'
                        f'    "bonus_trigger_flash": {{"duration_ms": ..., "keyframes": [...]}},\n'
                        f'    "transition_wipe": {{"duration_ms": ..., "direction": "...", "easing": "..."}},\n'
                        f'    "free_spin_intro": {{"duration_ms": ..., "keyframes": [...]}},\n'
                        f'    "free_spin_counter_tick": {{"duration_ms": ..., "keyframes": [...]}},\n'
                        f'    "return_to_base": {{"duration_ms": ..., "keyframes": [...]}}\n'
                        f"  }},\n"
                        f'  "ui_animations": {{\n'
                        f'    "button_press": {{"duration_ms": ..., "scale": ..., "easing": "..."}},\n'
                        f'    "bet_change_tick": {{"duration_ms": ..., "keyframes": [...]}},\n'
                        f'    "balance_update_flash": {{"duration_ms": ..., "color": "#..."}},\n'
                        f'    "autoplay_indicator": {{"duration_ms": ..., "easing": "...", "loop": true}}\n'
                        f"  }},\n"
                        f'  "audio_sync_map": {{\n'
                        f'    "spin_start": {{"sound": "spin_start", "trigger_at_ms": 0}},\n'
                        f'    "reel_stop_bounce": {{"sound": "spin_stop", "trigger_at_ms": ...}},\n'
                        f'    "win_small": {{"sound": "win_small", "trigger_at_ms": ...}},\n'
                        f'    "win_big": {{"sound": "win_big", "trigger_at_ms": ...}},\n'
                        f'    "scatter_activate": {{"sound": "scatter_land", "trigger_at_ms": ...}},\n'
                        f'    "bonus_trigger_flash": {{"sound": "bonus_trigger", "trigger_at_ms": ...}}\n'
                        f"  }}\n"
                        f"}}\n\n"

                        f"ALL values must be EXACT numbers — no placeholders or '...' in the final output.\n"
                        f"Every keyframe must specify percentage, transform, and opacity.\n"
                        f"Every easing must be a valid CSS cubic-bezier or named curve.\n\n"

                        f"═══ DELIVERABLE 2: CSS KEYFRAMES LIBRARY ═══\n"
                        f"Save to: {self.state.output_dir}/04_animation/animation_keyframes.css\n\n"
                        f"Generate production-ready CSS @keyframes for ALL animations above.\n"
                        f"Use only GPU-composited properties: transform, opacity, filter.\n"
                        f"Include CSS custom properties for easy theme customization.\n"
                        f"Include @media (prefers-reduced-motion) fallbacks.\n"
                        f"Add utility classes: .anim-reel-spin, .anim-sym-win-small, etc.\n\n"

                        f"═══ DELIVERABLE 3: ANIMATION BRIEF (MARKDOWN) ═══\n"
                        f"Save to: {self.state.output_dir}/04_animation/animation_brief.md\n\n"
                        f"A human-readable animation direction document covering:\n"
                        f"- Animation philosophy and emotional arc for this specific theme\n"
                        f"- Per-symbol animation descriptions with creative direction\n"
                        f"- Timing diagram: full spin cycle from button press to result display\n"
                        f"- Win tier escalation chart (small → medium → big → mega)\n"
                        f"- Feature transition storyboards with frame-by-frame descriptions\n"
                        f"- Near-miss anticipation choreography (MOST IMPORTANT)\n"
                        f"- Performance budget: target frame times, max concurrent particles\n"
                        f"- Mobile vs cabinet differences\n"
                        f"- Audio-animation sync guide\n\n"

                        f"CRITICAL: The near-miss anticipation sequence (2/3 scatters landed, "
                        f"last reel slowing) is the single most important animation in the game. "
                        f"Give it detailed treatment: exact reel deceleration curve, wobble parameters, "
                        f"tension-building particle effects, and audio sync points."
                    ),
                    expected_output="Animation spec JSON + CSS keyframes + animation brief saved to 04_animation/",
                    agent=self.agents["animation_director"],
                )
                anim_crew = Crew(
                    agents=[self.agents["animation_director"]],
                    tasks=[anim_task],
                    process=Process.sequential, verbose=VERBOSE,
                )
                anim_crew_result[0] = run_crew_with_timeout(anim_crew, "production", console)
            except Exception as e:
                anim_crew_error[0] = str(e)

        def _run_compliance():
            """Branch 4: Compliance Officer — regulatory review."""
            try:
                comp_crew = Crew(
                    agents=[self.agents["compliance_officer"]],
                    tasks=[compliance_task],
                    process=Process.sequential, verbose=VERBOSE,
                )
                compliance_crew_result[0] = run_crew_with_timeout(comp_crew, "production", console)
            except Exception as e:
                compliance_crew_error[0] = str(e)

        def _run_patent_check():
            """Branch 5: Patent Specialist — parallel IP risk assessment."""
            try:
                patent_task = Task(
                    description=(
                        f"Run a comprehensive patent and IP risk assessment for '{idea.theme}'.\n\n"
                        f"Features: {[f.value for f in idea.requested_features]}\n"
                        f"GDD context:\n{gdd_ctx[:2000]}\n\n"
                        f"Use patent_ip_scan to check ALL proposed mechanics for IP conflicts.\n"
                        f"Use deep_research to verify trademark availability for the theme name.\n\n"
                        f"Save structured report to: {self.state.output_dir}/05_legal/patent_ip_report.json\n"
                        f"Format: {{'overall_ip_risk': 'LOW|MEDIUM|HIGH', "
                        f"'patent_conflicts': [...], 'trademark_risks': [...], "
                        f"'recommendations': [...]}}"
                    ),
                    expected_output="Patent/IP risk report saved as JSON",
                    agent=self.agents["patent_specialist"],
                )
                patent_crew = Crew(
                    agents=[self.agents["patent_specialist"]],
                    tasks=[patent_task],
                    process=Process.sequential, verbose=VERBOSE,
                )
                patent_crew_result[0] = run_crew_with_timeout(patent_crew, "recon", console)
            except Exception as e:
                patent_crew_error[0] = str(e)

        futures_to_run = []
        with ThreadPoolExecutor(max_workers=5) as executor:
            if run_art:
                futures_to_run.append(executor.submit(_run_art))
                if self._flag("enable_sound_design", True):
                    futures_to_run.append(executor.submit(_run_audio))
                else:
                    console.print("[dim]⏭️ Audio skipped (enable_sound_design=false)[/dim]")
                if self._flag("enable_animation", True):
                    futures_to_run.append(executor.submit(_run_animation))
                else:
                    console.print("[dim]⏭️ Animation skipped (enable_animation=false)[/dim]")
            if run_comp:
                futures_to_run.append(executor.submit(_run_compliance))
                if self._flag("enable_patent_scan", True):
                    futures_to_run.append(executor.submit(_run_patent_check))
                else:
                    console.print("[dim]⏭️ Patent check skipped (enable_patent_scan=false)[/dim]")
            for future in as_completed(futures_to_run):
                try:
                    future.result()
                except Exception as e:
                    console.print(f"[yellow]⚠️ Parallel task error: {e}[/yellow]")

        # ── Report results ──
        for label, err in [("Art", art_crew_error[0]), ("Audio", audio_crew_error[0]),
                           ("Animation", anim_crew_error[0]),
                           ("Compliance", compliance_crew_error[0]), ("Patent/IP", patent_crew_error[0])]:
            if err:
                console.print(f"[yellow]⚠️ {label} branch error: {err}[/yellow]")
            else:
                console.print(f"[green]✅ {label} branch complete[/green]")

        self.state.art_assets = {"output": str(art_task.output) if art_crew_result[0] else ""}

        # Read compliance output from files (prefer structured JSON, fallback to text)
        comp_text = str(compliance_task.output)
        for comp_md in [
            Path(self.state.output_dir, "05_legal", "compliance_review.md"),
            Path(self.state.output_dir, "05_legal", "compliance_report.md"),
        ]:
            if comp_md.exists():
                comp_text = comp_md.read_text(encoding="utf-8", errors="replace")
                break
        self.state.compliance = {"output": comp_text}

        # Read audio brief
        audio_brief_path = Path(self.state.output_dir, "04_audio", "audio_design_brief.md")
        if audio_brief_path.exists():
            self.state.sound_design = {
                "brief": audio_brief_path.read_text(encoding="utf-8", errors="replace"),
                "brief_path": str(audio_brief_path),
            }
            console.print("[green]🔊 Audio design brief generated[/green]")

        # Try to load structured compliance results
        comp_path = Path(self.state.output_dir, "05_legal", "compliance_report.json")
        if comp_path.exists():
            try:
                self.state.compliance["results"] = json.loads(comp_path.read_text())
            except json.JSONDecodeError:
                pass

        # Merge patent/IP results into compliance data
        patent_path = Path(self.state.output_dir, "05_legal", "patent_ip_report.json")
        if patent_path.exists():
            try:
                patent_data = json.loads(patent_path.read_text())
                self.state.compliance["patent_ip"] = patent_data
                console.print(f"[green]✅ Patent/IP report: risk={patent_data.get('overall_ip_risk', 'UNKNOWN')}[/green]")
            except json.JSONDecodeError:
                pass

        # Read animation outputs
        anim_dir = Path(self.state.output_dir, "04_animation")
        anim_spec_path = anim_dir / "animation_spec.json"
        anim_brief_path = anim_dir / "animation_brief.md"
        anim_css_path = anim_dir / "animation_keyframes.css"
        anim_data = {}
        if anim_spec_path.exists():
            try:
                anim_data["spec"] = json.loads(anim_spec_path.read_text())
                console.print(f"[green]🎬 Animation spec: {len(anim_data['spec'].get('symbol_animations', {}))} symbol anims[/green]")
            except (json.JSONDecodeError, ValueError):
                anim_data["spec_raw"] = anim_spec_path.read_text(encoding="utf-8", errors="replace")
        if anim_brief_path.exists():
            anim_data["brief"] = anim_brief_path.read_text(encoding="utf-8", errors="replace")
            console.print("[green]🎬 Animation brief generated[/green]")
        if anim_css_path.exists():
            anim_data["css_keyframes"] = anim_css_path.read_text(encoding="utf-8", errors="replace")
            console.print(f"[green]🎬 CSS keyframes: {len(anim_data['css_keyframes'])} chars[/green]")
        if anim_data:
            self.state.animation_package = anim_data
        else:
            console.print("[yellow]⚠️ No animation outputs generated[/yellow]")

        # Try to load cert plan
        cert_path = Path(self.state.output_dir, "05_legal", "certification_plan.json")
        if cert_path.exists():
            try:
                self.state.certification_plan = json.loads(cert_path.read_text())
            except json.JSONDecodeError:
                pass

        # Check for generated audio files
        audio_dir = Path(self.state.output_dir, "04_audio")
        audio_dir.mkdir(parents=True, exist_ok=True)
        audio_files = list(audio_dir.glob("*.mp3")) + list(audio_dir.glob("*.wav"))
        if audio_files:
            if not self.state.sound_design:
                self.state.sound_design = {}
            self.state.sound_design["files_count"] = len(audio_files)
            self.state.sound_design["path"] = str(audio_dir)
            console.print(f"[green]🔊 {len(audio_files)} audio files generated[/green]")

        # Fallback: if audio engineer didn't generate audio files, call the tool directly
        audio_brief_path = audio_dir / "audio_design_brief.md"
        if not audio_files:
            try:
                console.print("[cyan]🔊 Agent didn't generate audio — running sound_design(full) directly...[/cyan]")
                from tools.tier2_upgrades import SoundDesignTool
                sdt = SoundDesignTool()
                gdd_text = str(self.state.gdd.get("output", ""))[:2000] if self.state.gdd else ""
                full_result = json.loads(sdt._run(
                    action="full",
                    theme=idea.theme,
                    gdd_context=gdd_text,
                    output_dir=str(audio_dir),
                ))
                if not self.state.sound_design:
                    self.state.sound_design = {}
                sounds_gen = full_result.get("sounds_generated", 0)
                self.state.sound_design["files_count"] = sounds_gen
                self.state.sound_design["path"] = str(audio_dir)
                if audio_brief_path.exists():
                    self.state.sound_design["brief"] = audio_brief_path.read_text(encoding="utf-8", errors="replace")
                    self.state.sound_design["brief_path"] = str(audio_brief_path)
                console.print(f"[green]🔊 Audio fallback: brief + {sounds_gen} sound effects generated[/green]")
            except Exception as e:
                console.print(f"[yellow]⚠️ Audio fallback failed: {e}[/yellow]")
                # Last resort: at least generate the brief
                if not audio_brief_path.exists():
                    try:
                        from tools.tier2_upgrades import SoundDesignTool
                        SoundDesignTool()._generate_brief(idea.theme, "", str(audio_dir))
                        console.print("[green]🔊 Audio design brief generated (brief-only fallback)[/green]")
                    except Exception:
                        pass
        elif not audio_brief_path.exists():
            # Sounds exist but no brief — generate the brief
            try:
                from tools.tier2_upgrades import SoundDesignTool
                gdd_text = str(self.state.gdd.get("output", ""))[:2000] if self.state.gdd else ""
                SoundDesignTool()._generate_brief(idea.theme, gdd_text, str(audio_dir))
                if not self.state.sound_design:
                    self.state.sound_design = {}
                self.state.sound_design["brief"] = audio_brief_path.read_text(encoding="utf-8", errors="replace")
                console.print("[green]🔊 Audio design brief generated (supplement)[/green]")
            except Exception:
                pass

        console.print("[green]✅ Production + Compliance complete[/green]")
        self._stage_exit("production")

    # ---- Stage 6: Assembly + PDF Generation ----

    @listen(run_production)
    def assemble_package(self):
        self._stage_enter("assemble_package")
        _update_stage_db(self.state.job_id, "Assembling final package")
        if not self.state.mood_board_approved:
            self._stage_exit("assemble_package")
            return
        console.print("\n[bold green]📦 Stage 4: Assembly + PDF Generation[/bold green]\n")
        emit("stage_start", name="Assembly & PDF", num=4, icon="📦", desc="Generating professional documents, prototype, and export package")

        output_path = Path(self.state.output_dir)
        pdf_dir = output_path / "06_pdf"

        # ══════════════════════════════════════════════════════════════
        # PHASE 12A: PARALLEL ASSEMBLY
        # Before: Prototype → Revenue → Exports → PDFs (sequential, ~5 min)
        # After:  [Prototype ∥ Revenue+Exports] → PDFs (~2.5 min)
        #         PDFs still need all data collected, so they run last.
        #         But prototype + revenue + exports are independent.
        # ══════════════════════════════════════════════════════════════

        # ---- Branch 1: HTML5 Prototype (independent) ----
        proto_result_holder = [None]
        proto_error_holder = [None]

        def _gen_prototype():
            try:
                console.print("[cyan]🎮 Generating AI-themed HTML5 prototype...[/cyan]")
                proto = HTML5PrototypeTool()
                idea = self.state.game_idea
                symbols = ["👑", "💎", "🏆", "🌟", "A", "K", "Q", "J", "10"]
                features = [f.value.replace("_", " ").title() for f in idea.requested_features]
                gdd_ctx = str(self.state.gdd.get("output", ""))[:3000] if self.state.gdd else ""
                math_ctx = str(self.state.math_model.get("output", ""))[:2000] if self.state.math_model else ""
                art_dir = str(output_path / "04_art")
                audio_dir = str(output_path / "04_audio")

                proto_result = json.loads(proto._run(
                    game_title=idea.theme,
                    theme=idea.theme,
                    grid_cols=idea.grid_cols,
                    grid_rows=idea.grid_rows,
                    symbols=symbols,
                    features=features,
                    target_rtp=idea.target_rtp,
                    output_dir=str(output_path / "07_prototype"),
                    paytable_summary=f"Target RTP: {idea.target_rtp}% | Volatility: {idea.volatility.value} | Max Win: {idea.max_win_multiplier}x",
                    art_dir=art_dir,
                    audio_dir=audio_dir,
                    gdd_context=gdd_ctx,
                    math_context=math_ctx,
                    volatility=idea.volatility.value,
                    max_win_multiplier=idea.max_win_multiplier,
                ))
                proto_result_holder[0] = proto_result
                self.state.prototype_path = proto_result.get("file_path", "")
                sym_imgs = proto_result.get("symbols_with_images", 0)
                bonus = proto_result.get("bonus_name", "")
                console.print(f"[green]✅ Prototype generated: {proto_result.get('file_path', '')}[/green]")
                console.print(f"    Symbols with DALL-E art: {sym_imgs} | Bonus: {bonus}")
            except Exception as e:
                proto_error_holder[0] = str(e)
                console.print(f"[yellow]⚠️ Prototype generation failed (non-fatal): {e}[/yellow]")

        # ---- Branch 2: Revenue projection + Engine exports (independent) ----
        revenue_error_holder = [None]
        export_error_holder = [None]

        def _gen_revenue_and_exports():
            idea = self.state.game_idea
            # Revenue projection
            try:
                _update_stage_db(self.state.job_id, "Generating revenue projections")
                from tools.revenue_engine import run_revenue_projection

                od = Path(self.state.output_dir)
                math_data = None
                sim_path = od / "03_math" / "simulation_results.json"
                if sim_path.exists():
                    try:
                        math_data = json.loads(sim_path.read_text())
                    except (json.JSONDecodeError, ValueError):
                        pass
                if not math_data and self.state.math_model:
                    math_data = self.state.math_model.get("results", {})

                rev_params = {
                    "theme": idea.theme,
                    "volatility": idea.volatility.value,
                    "markets": ", ".join(idea.target_markets),
                    "max_win": idea.max_win_multiplier,
                    "features": [f.value for f in idea.requested_features],
                }
                rev_result = run_revenue_projection(
                    sim_results=math_data if math_data else {},
                    game_params=rev_params,
                )
                self.state.revenue_projection = rev_result
                rev_dir = output_path / "08_revenue"
                rev_dir.mkdir(parents=True, exist_ok=True)
                (rev_dir / "revenue_projection.json").write_text(
                    json.dumps(rev_result, indent=2, default=str), encoding="utf-8"
                )
                console.print(f"[green]💰 Revenue projection: GGR 365d = ${rev_result.get('ggr_365d', 0):,.0f} | "
                             f"Break-even: {rev_result.get('break_even_days', '?')} days | "
                             f"ROI: {rev_result.get('roi_365d', '?')}%[/green]")
            except Exception as e:
                revenue_error_holder[0] = str(e)
                console.print(f"[yellow]⚠️ Revenue projection failed (non-fatal): {e}[/yellow]")

            # Engine export packages
            try:
                _update_stage_db(self.state.job_id, "Generating engine export packages")
                from tools.export_engine import generate_export_package
                gdd_text = str(self.state.gdd.get("output", ""))[:5000] if self.state.gdd else ""

                export_params = {
                    "grid_cols": idea.grid_cols,
                    "grid_rows": idea.grid_rows,
                    "ways_or_lines": idea.ways_or_lines,
                    "target_rtp": idea.target_rtp,
                    "max_win": idea.max_win_multiplier,
                    "volatility": idea.volatility.value,
                    "art_style": idea.art_style,
                    "markets": ", ".join(idea.target_markets),
                    "features": [f.value for f in idea.requested_features],
                }
                for fmt in ("unity", "godot", "generic"):
                    try:
                        zp = generate_export_package(
                            output_dir=str(output_path),
                            format=fmt,
                            game_title=idea.theme,
                            game_params=export_params,
                            gdd_text=gdd_text,
                        )
                        console.print(f"[green]🎮 {fmt.title()} export: {Path(zp).name}[/green]")
                    except Exception as ex:
                        console.print(f"[yellow]⚠️ {fmt} export failed: {ex}[/yellow]")
            except Exception as e:
                export_error_holder[0] = str(e)
                console.print(f"[yellow]⚠️ Engine export failed (non-fatal): {e}[/yellow]")

        # Run prototype ∥ revenue+exports in parallel
        console.print("[cyan]⚡ Running Prototype ∥ Revenue+Exports in PARALLEL...[/cyan]")
        emit("parallel_start", tasks=["Prototype", "Revenue+Exports"])
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(_gen_prototype), executor.submit(_gen_revenue_and_exports)]
            for f in as_completed(futures):
                try: f.result()
                except Exception as e:
                    console.print(f"[yellow]⚠️ Assembly parallel error: {e}[/yellow]")

        # ---- Generate PDFs (needs all data collected, runs after parallel branches) ----
        try:
            from tools.pdf_generator import generate_full_package

            # Build params dict for PDF generator
            game_params = {
                "theme": self.state.game_idea.theme,
                "volatility": self.state.game_idea.volatility.value,
                "target_rtp": self.state.game_idea.target_rtp,
                "grid": f"{self.state.game_idea.grid_cols}x{self.state.game_idea.grid_rows}",
                "ways": self.state.game_idea.ways_or_lines,
                "max_win": self.state.game_idea.max_win_multiplier,
                "markets": ", ".join(self.state.game_idea.target_markets),
                "art_style": self.state.game_idea.art_style,
                "features": [f.value for f in self.state.game_idea.requested_features],
            }

            # ── Collect ALL data from disk files (agents save here) ──
            od = Path(self.state.output_dir)

            # GDD: read the actual markdown file the agent wrote
            gdd_data = None
            gdd_text = ""
            for gdd_path in [od / "02_design" / "gdd.md", od / "02_design" / "gdd.txt"]:
                if gdd_path.exists():
                    gdd_text = gdd_path.read_text(encoding="utf-8", errors="replace")
                    break
            if not gdd_text and self.state.gdd:
                gdd_text = str(self.state.gdd.get("output", ""))
            if gdd_text and len(gdd_text) > 100:
                gdd_data = {"_raw_text": gdd_text}
                # Also try structured JSON version
                gdd_json_path = od / "02_design" / "gdd.json"
                if gdd_json_path.exists():
                    try:
                        gdd_data.update(json.loads(gdd_json_path.read_text()))
                    except (json.JSONDecodeError, ValueError):
                        pass

            # Math: read structured simulation results + player behavior
            math_data = None
            sim_path = od / "03_math" / "simulation_results.json"
            if sim_path.exists():
                try:
                    math_data = json.loads(sim_path.read_text())
                except (json.JSONDecodeError, ValueError):
                    pass
            # Merge player behavior data
            behavior_path = od / "03_math" / "player_behavior.json"
            if behavior_path.exists():
                try:
                    behavior = json.loads(behavior_path.read_text())
                    if math_data:
                        math_data["player_behavior"] = behavior
                    else:
                        math_data = {"player_behavior": behavior}
                except (json.JSONDecodeError, ValueError):
                    pass
            # Fallback: use state data
            if not math_data and self.state.math_model:
                math_data = self.state.math_model.get("results", None)
            # Also read any raw math text for prose sections
            math_text = ""
            for math_md in [od / "03_math" / "math_report.md", od / "03_math" / "math_model.md"]:
                if math_md.exists():
                    math_text = math_md.read_text(encoding="utf-8", errors="replace")
                    break
            if not math_text and self.state.math_model:
                math_text = str(self.state.math_model.get("output", ""))
            if math_data is None:
                math_data = {}
            math_data["_raw_text"] = math_text

            # Read all math CSV files for PDF rendering
            math_csvs = {}
            for csv_name in ["BaseReels.csv", "FreeReels.csv", "FeatureReelStrips.csv",
                             "paytable.csv", "reel_strips.csv"]:
                csv_path = od / "03_math" / csv_name
                if csv_path.exists():
                    math_csvs[csv_name] = csv_path.read_text(encoding="utf-8", errors="replace")
            math_data["_csv_files"] = math_csvs

            # Compliance: read structured JSON + cert plan
            compliance_data = None
            comp_path = od / "05_legal" / "compliance_report.json"
            if comp_path.exists():
                try:
                    compliance_data = json.loads(comp_path.read_text())
                except (json.JSONDecodeError, ValueError):
                    pass
            cert_path = od / "05_legal" / "certification_plan.json"
            if cert_path.exists():
                try:
                    cert = json.loads(cert_path.read_text())
                    if compliance_data:
                        compliance_data["certification_plan"] = cert
                    else:
                        compliance_data = {"certification_plan": cert}
                except (json.JSONDecodeError, ValueError):
                    pass
            if not compliance_data and self.state.compliance:
                compliance_data = self.state.compliance.get("results", None)
            # Raw compliance text fallback
            comp_text = ""
            for comp_md in [od / "05_legal" / "compliance_report.md", od / "05_legal" / "compliance_review.md"]:
                if comp_md.exists():
                    comp_text = comp_md.read_text(encoding="utf-8", errors="replace")
                    break
            if not comp_text and self.state.compliance:
                comp_text = str(self.state.compliance.get("output", ""))
            if compliance_data is None:
                compliance_data = {}
            compliance_data["_raw_text"] = comp_text

            # Research: already structured in state
            research_data = self.state.market_research or {}
            # Also try reading from file
            research_path = od / "01_research" / "market_research.json"
            if research_path.exists() and not research_data:
                try:
                    research_data = json.loads(research_path.read_text())
                except (json.JSONDecodeError, ValueError):
                    pass
            # Read full market report markdown
            report_path = od / "01_research" / "market_report.md"
            if report_path.exists():
                report_text = report_path.read_text(encoding="utf-8", errors="replace")
                if report_text and len(report_text) > 100:
                    research_data["report"] = report_text

            # Adversarial reviews: read all review files
            reviews = {}
            review_dir = od / "01_research"
            for rev_file in review_dir.glob("adversarial_review_*.md"):
                reviews[rev_file.stem] = rev_file.read_text(encoding="utf-8", errors="replace")

            console.print(f"    Data collected — GDD: {len(gdd_text)} chars, "
                         f"Math: {'JSON' if math_data.get('simulation') or math_data.get('results') else 'text'}, "
                         f"Compliance: {'JSON' if compliance_data.get('overall_status') else 'text'}")

            # Revenue + exports already ran in parallel branch above

            # Collect art and audio data for their PDFs
            art_data_for_pdf = {
                "output": str(self.state.art_assets.get("output", "")) if self.state.art_assets else "",
                "path": str(od / "04_art"),
            }
            audio_data_for_pdf = self.state.sound_design if self.state.sound_design else {}
            if not audio_data_for_pdf.get("path"):
                audio_data_for_pdf["path"] = str(od / "04_audio")

            pdf_files = generate_full_package(
                output_dir=str(pdf_dir),
                game_title=self.state.game_idea.theme,
                game_params=game_params,
                research_data=research_data,
                gdd_data=gdd_data,
                math_data=math_data,
                compliance_data=compliance_data,
                reviews=reviews,
                audio_data=audio_data_for_pdf,
                art_data=art_data_for_pdf,
            )
            self.state.pdf_files = pdf_files
            console.print(f"[green]📄 Generated {len(pdf_files)} PDFs[/green]")

        except Exception as e:
            console.print(f"[yellow]⚠️ PDF generation error: {e}[/yellow]")
            self.state.errors.append(f"PDF generation failed: {e}")

        # ---- Build Manifest ----
        all_files = [str(f.relative_to(output_path)) for f in output_path.rglob("*") if f.is_file()]
        image_count = len([f for f in output_path.rglob("*") if f.suffix in (".png", ".jpg", ".webp")])

        cost_summary = self.cost_tracker.summary()

        manifest = {
            "game_title": self.state.game_idea.theme,
            "game_slug": self.state.game_slug,
            "generated_at": datetime.now().isoformat(),
            "pipeline_version": "5.0.0",  # Phase 12A: Full Send
            "llm_routing": {
                "premium_model": LLMConfig.PREMIUM,
                "heavy_model": LLMConfig.HEAVY,
                "light_model": LLMConfig.LIGHT,
            },
            "preflight": {
                "trend_radar": bool(self.state.trend_radar),
                "jurisdiction_constraints": bool(self.state.jurisdiction_constraints),
                "blockers": self.state.jurisdiction_constraints.get("intersection", {}).get("blockers", []) if self.state.jurisdiction_constraints else [],
            },
            "math_quality": {
                "optimized_rtp": self.state.optimized_rtp,
                "player_behavior": bool(self.state.player_behavior),
                "vision_qa_checks": len(self.state.vision_qa_results),
            },
            "tier2": {
                "patent_scan": bool(self.state.patent_scan),
                "sound_design": bool(self.state.sound_design),
                "animation_package": bool(self.state.animation_package),
                "prototype": bool(self.state.prototype_path),
                "certification_plan": bool(self.state.certification_plan),
                "revenue_projection": bool(self.state.revenue_projection),
            },
            "cost": cost_summary,
            "input_parameters": self.state.game_idea.model_dump(),
            "files_generated": all_files,
            "pdf_files": self.state.pdf_files,
            "total_files": len(all_files),
            "total_images": image_count,
            "hitl_approvals": self.state.hitl_approvals,
            "errors": self.state.errors,
            "started_at": self.state.started_at,
            "completed_at": datetime.now().isoformat(),
            "checkpoint": {
                "resume_count": self.state.resume_count,
                "stage_timings": self.state.stage_timings,
                "skipped_stages": self.state.skipped_stages,
                "budget": self.budget.snapshot(),
                "smart_skip_summary": {
                    "stages_skipped": len(self.state.skipped_stages),
                    "skipped_list": self.state.skipped_stages,
                    "priority_map_used": {s: f"P{STAGE_PRIORITY.get(s, '?')}" for s in PIPELINE_STAGES},
                    "budget_pressure": (
                        "none" if not self.state.skipped_stages else
                        "moderate" if len(self.state.skipped_stages) <= 3 else
                        "severe"
                    ),
                },
            },
        }

        (output_path / "PACKAGE_MANIFEST.json").write_text(
            json.dumps(manifest, indent=2, default=str), encoding="utf-8"
        )

        self.state.completed_at = datetime.now().isoformat()
        self.state.total_tokens_used = cost_summary["total_tokens"]
        self.state.estimated_cost_usd = cost_summary["estimated_cost_usd"]

        audio_count = len([f for f in output_path.rglob("*") if f.suffix in (".mp3", ".wav")])

        budget_snap = self.budget.snapshot()
        console.print(Panel(
            f"[bold green]✅ Pipeline Complete[/bold green]\n\n"
            f"📁 Output: {self.state.output_dir}\n"
            f"📄 PDFs: {len(self.state.pdf_files)}\n"
            f"🖼️ Images: {image_count}\n"
            f"🔊 Audio: {audio_count}\n"
            f"🎮 Prototype: {'Yes' if self.state.prototype_path else 'No'}\n"
            f"📊 Files: {len(all_files)}\n"
            f"💰 Est. Cost: ${cost_summary['estimated_cost_usd']:.2f}\n"
            f"⏱️ {self.state.started_at} → {self.state.completed_at}\n"
            f"⏱️ Budget: {budget_snap['total_elapsed_s']:.0f}s / {budget_snap['total_budget_s']}s "
            f"({budget_snap['pct_used']:.1f}% used)"
            + (f" | resumed {self.state.resume_count}x" if self.state.resume_count else "")
            + (f"\n⚡ Skipped: {', '.join(self.state.skipped_stages)}" if self.state.skipped_stages else ""),
            title="🎰 Package Complete", border_style="green",
        ))
        emit("stage_done", name="Assembly & PDF", num=4)
        emit("metric", key="pdfs", value=len(self.state.pdf_files), label="PDFs Generated")
        emit("metric", key="images", value=image_count, label="Images")
        emit("metric", key="audio", value=audio_count, label="Audio Files")
        emit("metric", key="files", value=len(all_files), label="Total Files")
        emit("metric", key="cost", value=round(cost_summary["estimated_cost_usd"], 2), label="Est. Cost")
        emit("metric", key="budget_pct", value=budget_snap["pct_used"], label="Budget Used %")
        emit("metric", key="budget_elapsed", value=budget_snap["total_elapsed_s"], label="Total Time (s)")
        if self.state.skipped_stages:
            emit("metric", key="skipped", value=len(self.state.skipped_stages), label="Stages Skipped")
        emit("info", msg="Pipeline complete", icon="🎰")

        # ---- Save to Knowledge Base (UPGRADE 4) ----
        try:
            from tools.advanced_research import KnowledgeBaseTool
            kb = KnowledgeBaseTool()
            game_data = {
                "theme": self.state.game_idea.theme,
                "target_markets": self.state.game_idea.target_markets,
                "volatility": self.state.game_idea.volatility.value,
                "target_rtp": self.state.game_idea.target_rtp,
                "grid": f"{self.state.game_idea.grid_cols}x{self.state.game_idea.grid_rows}",
                "ways_or_lines": self.state.game_idea.ways_or_lines,
                "max_win": self.state.game_idea.max_win_multiplier,
                "art_style": self.state.game_idea.art_style,
                "features": [f.value for f in self.state.game_idea.requested_features],
                "gdd_summary": str(self.state.gdd.get("output", ""))[:2000] if self.state.gdd else "",
                "math_summary": str(self.state.math_model.get("output", ""))[:1000] if self.state.math_model else "",
                "compliance_summary": str(self.state.compliance.get("output", ""))[:1000] if self.state.compliance else "",
                "cost_usd": cost_summary['estimated_cost_usd'],
                "completed_at": self.state.completed_at,
            }
            kb._run(action="save", game_slug=self.state.game_slug, game_data=json.dumps(game_data))
            console.print("[green]🧠 Saved to knowledge base for future reference[/green]")
        except Exception as e:
            console.print(f"[yellow]⚠️ Knowledge base save failed (non-fatal): {e}[/yellow]")

        self._stage_exit("assemble_package")
        return self.state

    # ============================================================
    # Adversarial Review Helper (UPGRADE 5)
    # ============================================================

    def _run_adversarial_review(self, stage: str, context_summary: str):
        """Run the adversarial reviewer agent on the current stage's output."""
        # ACP: Check feature flag
        if not self._flag("enable_adversarial_review", True):
            console.print(f"[dim]⏭️ Adversarial review skipped for {stage} (enable_adversarial_review=false)[/dim]")
            return
        try:
            from agents.adversarial_reviewer import build_review_task_description
            console.print(f"\n[bold red]🔴 Adversarial Review: {stage}[/bold red]\n")

            review_desc = build_review_task_description(
                stage=stage,
                context_summary=context_summary,
                output_dir=self.state.output_dir,
            )

            review_task = Task(
                description=review_desc,
                expected_output=f"Structured adversarial critique saved to {self.state.output_dir}/adversarial_review_{stage}.md",
                agent=self.agents["adversarial_reviewer"],
            )

            crew = Crew(
                agents=[self.agents["adversarial_reviewer"]],
                tasks=[review_task],
                process=Process.sequential, verbose=VERBOSE,
            )
            result = run_crew_with_timeout(crew, "recon", console)

            # Ensure the review is saved
            review_path = Path(self.state.output_dir, f"adversarial_review_{stage}.md")
            if not review_path.exists():
                review_path.write_text(str(result), encoding="utf-8")

            console.print(f"[green]✅ Adversarial review complete: {review_path.name}[/green]")
        except Exception as e:
            console.print(f"[yellow]⚠️ Adversarial review failed (non-fatal): {e}[/yellow]")
