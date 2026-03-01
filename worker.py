#!/usr/bin/env python3
"""
ARKAINBRAIN — Subprocess Worker

Runs pipeline/recon jobs in a separate process to avoid:
- Import deadlocks (crewai module locks)
- Thread-safety issues with OpenAI clients
- GIL contention on CPU-bound simulation

Usage (called by web_app.py, not directly):
    python worker.py pipeline <job_id> '<json_params>'
    python worker.py recon <job_id> <state_name>
"""

import json
import os
import sqlite3
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

# ── Suppress CrewAI tracing prompts in subprocess ──
os.environ["CREWAI_TELEMETRY_OPT_OUT"] = "true"
os.environ["OTEL_SDK_DISABLED"] = "true"
os.environ["CREWAI_TRACING_ENABLED"] = "false"
os.environ["DO_NOT_TRACK"] = "1"
os.environ["LITELLM_LOG"] = "ERROR"  # Suppress litellm info/debug logs

# ── Suppress noisy library loggers that bypass stdout capture ──
import logging
for _noisy in ("litellm", "httpx", "httpcore", "openai", "urllib3", "crewai"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# ── Pre-create CrewAI config to prevent tracing prompt entirely ──
# CrewAI checks ~/.crewai/ for stored preferences. If missing, it asks.
_crewai_dirs = [
    Path.home() / ".crewai",
    Path("/tmp/crewai_storage"),
]
for _d in _crewai_dirs:
    _d.mkdir(parents=True, exist_ok=True)
    _cfg = _d / "config.json"
    if not _cfg.exists():
        _cfg.write_text(json.dumps({"tracing_enabled": False, "tracing_disabled": True}))
    # Also write the db3 format some versions use
    _db = _d / "crewai_config.db"
    if not _db.exists():
        try:
            import sqlite3 as _sq
            _c = _sq.connect(str(_db))
            _c.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)")
            _c.execute("INSERT OR REPLACE INTO config VALUES ('tracing_enabled', 'false')")
            _c.commit()
            _c.close()
        except Exception:
            pass

os.environ["CREWAI_STORAGE_DIR"] = "/tmp/crewai_storage"

# ── Redirect stdin to prevent any interactive prompts ──
sys.stdin = open(os.devnull, "r")

# ── OpenAI SDK retry: exponential backoff on 429s ──
os.environ.setdefault("OPENAI_MAX_RETRIES", "5")
os.environ.setdefault("OPENAI_TIMEOUT", "120")

from dotenv import load_dotenv
load_dotenv()

DB_PATH = os.getenv("DB_PATH", "arkainbrain.db")
LOG_DIR = Path(os.getenv("LOG_DIR", "./logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ── Structured logger for worker process ──
logger = logging.getLogger("arkainbrain.worker")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)

# ── Global pipeline timeout (seconds) ──
# If the entire pipeline exceeds this, the worker self-terminates.
# Override: PIPELINE_TIMEOUT_SECONDS=5400 (default: 1 hour)
PIPELINE_TIMEOUT = int(os.getenv("PIPELINE_TIMEOUT_SECONDS", "5400"))  # 90 min (convergence loops add time)


def _start_watchdog(job_id: str, timeout: int):
    """Watchdog thread that kills the process if the pipeline exceeds timeout.

    Phase 3 upgrade: If a checkpoint.json exists in the output_dir,
    the job is marked 'partial' (resumable) instead of 'failed'.
    """
    import threading, signal

    def _timeout_handler():
        status = "failed"
        error_msg = f"Pipeline timed out after {timeout}s"
        last_stage = ""

        try:
            # Query DB for output_dir (written early in pipeline.initialize)
            db = sqlite3.connect(DB_PATH, timeout=5)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA busy_timeout=3000")
            row = db.execute(
                "SELECT output_dir FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            output_dir = row[0] if row and row[0] else None

            if output_dir:
                ckpt_path = Path(output_dir) / "checkpoint.json"
                if ckpt_path.exists():
                    try:
                        ckpt = json.loads(ckpt_path.read_text(encoding="utf-8"))
                        last_stage = ckpt.get("stage", "unknown")
                        stage_idx = ckpt.get("stage_index", -1)
                        stages_total = len(ckpt.get("stages_completed", [])) + len(ckpt.get("stages_remaining", []))
                        elapsed = ckpt.get("pipeline_elapsed_s", 0)

                        # Has checkpoint → mark partial (resumable)
                        status = "partial"
                        error_msg = (
                            f"Timed out after {timeout}s — completed through "
                            f"'{last_stage}' ({stage_idx + 1}/{stages_total} stages, "
                            f"{elapsed:.0f}s elapsed). Resume available."
                        )
                    except (json.JSONDecodeError, KeyError, TypeError):
                        # Corrupted checkpoint — treat as failed
                        error_msg = f"Pipeline timed out after {timeout}s (checkpoint unreadable)"
                else:
                    error_msg = f"Pipeline timed out after {timeout}s — no stages completed"

            db.execute(
                "UPDATE jobs SET status=?, error=? WHERE id=? AND status='running'",
                (status, error_msg, job_id),
            )
            db.commit()
            db.close()
        except Exception:
            # Last-resort: try to mark failed even if checkpoint detection blew up
            try:
                db2 = sqlite3.connect(DB_PATH, timeout=5)
                db2.execute(
                    "UPDATE jobs SET status='failed', error=? WHERE id=? AND status='running'",
                    (f"Pipeline timed out after {timeout}s", job_id),
                )
                db2.commit()
                db2.close()
            except Exception:
                pass

        logger.error(
            f"Pipeline {job_id} exceeded {timeout}s — status={status}"
            + (f" (last stage: {last_stage})" if last_stage else "")
        )
        os._exit(1)  # Hard exit — no cleanup, no deadlock

    timer = threading.Timer(timeout, _timeout_handler)
    timer.daemon = True
    timer.start()
    return timer


class JobLogger:
    """Per-job logging: explicit log() calls go to Railway stdout + log file.
    All captured stdout/stderr (CrewAI, litellm, etc.) goes ONLY to log file.
    This prevents Railway's 500 logs/sec rate limit."""

    def __init__(self, job_id: str):
        self.job_id = job_id
        self.log_path = LOG_DIR / f"{job_id}.log"
        self.log_file = open(self.log_path, "w", buffering=1)  # line-buffered
        self._original_stdout = sys.stdout
        self._original_stderr = sys.stderr

    def log(self, msg: str):
        """Write a status line to BOTH Railway logs and the job log file."""
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        self._original_stdout.write(line + "\n")
        self._original_stdout.flush()
        self.log_file.write(line + "\n")

    def capture_output(self):
        """Redirect stdout/stderr to log file ONLY (not Railway stdout).
        Live log viewer reads the file via SSE — users still see everything."""
        sys.stdout = _LogFileWriter(self.log_file)
        sys.stderr = _LogFileWriter(self.log_file)

    def close(self):
        sys.stdout = self._original_stdout
        sys.stderr = self._original_stderr
        self.log_file.close()


class _LogFileWriter:
    """File-like writer that sends ALL output to the log file only.
    Railway stdout is never touched — prevents 500 logs/sec rate limit."""

    def __init__(self, log_file):
        self._log = log_file

    def write(self, data):
        if data:
            try:
                self._log.write(data)
                self._log.flush()
            except (ValueError, IOError):
                pass

    def flush(self):
        try:
            self._log.flush()
        except (ValueError, IOError):
            pass

    def isatty(self):
        return False

    def fileno(self):
        raise OSError("log-only stream has no fileno")

    @property
    def encoding(self):
        return 'utf-8'

    @property
    def errors(self):
        return 'strict'

    def readable(self):
        return False

    def writable(self):
        return True

    def seekable(self):
        return False


_ALLOWED_JOB_COLUMNS = frozenset({
    "status", "current_stage", "output_dir", "error", "completed_at",
    "params", "parent_job_id", "version",
    "selected_profile_id", "selected_workflow_id", "resolved_config_json", "config_version_ref",
})

def _get_db():
    """Get a SQLite connection with Row factory (for ACP engine)."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def update_db(job_id: str, **kw):
    """Update job in SQLite (concurrency-safe with WAL mode)."""
    # Whitelist column names to prevent SQL injection via kwargs
    bad = set(kw.keys()) - _ALLOWED_JOB_COLUMNS
    if bad:
        raise ValueError(f"Disallowed column(s): {bad}")
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    sets = ",".join(f"{k}=?" for k in kw)
    conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", list(kw.values()) + [job_id])
    conn.commit()
    conn.close()


def _check_variant_parent_completion(job_id: str):
    """Phase 4A: If this job is a variant, check if all siblings are done.
    If so, mark the parent variant_parent job as complete."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job or job["job_type"] != "variant" or not job["parent_job_id"]:
            conn.close()
            return

        parent_id = job["parent_job_id"]
        siblings = conn.execute(
            "SELECT status FROM jobs WHERE parent_job_id=? AND job_type='variant'",
            (parent_id,)
        ).fetchall()

        if all(s["status"] in ("complete", "failed") for s in siblings):
            completed = sum(1 for s in siblings if s["status"] == "complete")
            failed = sum(1 for s in siblings if s["status"] == "failed")
            status = "complete" if completed > 0 else "failed"
            conn.execute(
                "UPDATE jobs SET status=?, current_stage=?, completed_at=? WHERE id=?",
                (status, f"{completed} complete, {failed} failed", datetime.now().isoformat(), parent_id)
            )
            conn.commit()
            logger.info(f"Variant parent {parent_id}: {completed} complete, {failed} failed → {status}")
        conn.close()
    except Exception as e:
        logger.warning(f"Variant parent check error: {e}")


# ============================================================
# Email Notification Helpers
# ============================================================

def _get_user_email(job_id: str) -> str:
    """Look up the email of the user who launched a job.
    Returns empty string if user has disabled email notifications."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT u.email, u.email_notify FROM jobs j JOIN users u ON j.user_id = u.id WHERE j.id = ?",
            (job_id,)
        ).fetchone()
        conn.close()
        if not row or not row["email"]:
            return ""
        # Check if user has opted out of emails
        notify = row["email_notify"] if "email_notify" in row.keys() else 1
        if not notify:
            return ""
        return row["email"]
    except Exception:
        return ""


def _send_completion_email(job_id: str, game_title: str, output_dir: str):
    """Send pipeline/iterate completion email (best-effort, never raises)."""
    try:
        from tools.email_notify import notify_pipeline_complete, is_enabled
        if not is_enabled():
            return
        email = _get_user_email(job_id)
        if not email:
            return
        stats = {}
        if output_dir:
            from pathlib import Path as P
            sim_path = P(output_dir) / "03_math" / "simulation_results.json"
            if sim_path.exists():
                import json as _j
                sim = _j.loads(sim_path.read_text())
                r = sim.get("results", sim)
                stats["measured_rtp"] = r.get("measured_rtp", "—")
            pdf_count = len(list(P(output_dir).glob("06_pdfs/*.pdf"))) if (P(output_dir) / "06_pdfs").exists() else 0
            stats["pdf_count"] = pdf_count or "—"
        notify_pipeline_complete(email, job_id, game_title, output_dir, stats=stats)
    except Exception as e:
        logger.debug(f"Email non-fatal: {e}")


def _send_failure_email(job_id: str, game_title: str, error: str):
    """Send pipeline failure email (best-effort, never raises)."""
    try:
        from tools.email_notify import notify_pipeline_failed, is_enabled
        if not is_enabled():
            return
        email = _get_user_email(job_id)
        if not email:
            return
        notify_pipeline_failed(email, job_id, game_title, error)
    except Exception as e:
        logger.debug(f"Email non-fatal: {e}")


def _send_recon_email(job_id: str, state_name: str):
    """Send recon completion email (best-effort, never raises)."""
    try:
        from tools.email_notify import notify_recon_complete, is_enabled
        if not is_enabled():
            return
        email = _get_user_email(job_id)
        if not email:
            return
        notify_recon_complete(email, job_id, state_name)
    except Exception as e:
        logger.debug(f"Email non-fatal: {e}")


def setup_openai_retry():
    """Configure OpenAI SDK and litellm for rate-limit retries with backoff."""
    # CrewAI uses the OpenAI SDK directly (not litellm).
    # The SDK reads OPENAI_MAX_RETRIES env var for auto-retry on 429s.
    os.environ.setdefault("OPENAI_MAX_RETRIES", "5")
    os.environ.setdefault("OPENAI_TIMEOUT", "120")

    # Also configure litellm if present (used by some tool calls)
    try:
        import litellm
        litellm.num_retries = 5
        litellm.request_timeout = 120
        litellm.set_verbose = False       # Suppress print()-based debug output
        litellm.suppress_debug_info = True
    except ImportError:
        pass

    # Monkey-patch OpenAI client defaults for maximum resilience
    try:
        import openai
        openai.default_headers = {**(openai.default_headers or {})}
        # Increase default max_retries from 2 to 5
        if hasattr(openai, '_default_max_retries'):
            openai._default_max_retries = 5
    except (ImportError, AttributeError):
        pass


def _load_acp_config(job_id: str, job_type: str, logger) -> dict:
    """Load ACP config for a job. Returns resolved config dict (empty on failure)."""
    try:
        from config.acp_engine import ACP
        from config.settings import LLMConfig
        db = _get_db()
        acp = ACP(db)
        acp.seed_defaults()

        # Check kill switches
        if acp.flag("emergency_stop"):
            update_db(job_id, status="failed", error="Emergency stop active — job blocked by admin")
            logger.log("BLOCKED: Emergency stop is active")
            db.close()
            return {"_blocked": True}
        if acp.flag("queue_paused"):
            update_db(job_id, status="failed", error="Queue paused — job blocked by admin")
            logger.log("BLOCKED: Queue is paused")
            db.close()
            return {"_blocked": True}

        resolved = acp.resolve_config(job_type)
        LLMConfig.load_from_acp(resolved)

        # Stamp resolved config on job
        resolved_json = json.dumps(resolved, default=str)
        try:
            db.execute(
                "UPDATE jobs SET resolved_config_json=?, selected_profile_id=?, selected_workflow_id=? WHERE id=?",
                (resolved_json[:50000], resolved.get("profile_id", ""), resolved.get("workflow_id", ""), job_id)
            )
            db.commit()
        except Exception:
            pass

        logger.log(f"ACP loaded: profile={resolved.get('profile_name','?')}, "
                    f"agents={len(resolved.get('agents',{}))}")
        db.close()
        return resolved
    except Exception as e:
        logger.log(f"ACP init warning (using defaults): {e}")
        return {}


def run_pipeline(job_id: str, params_json: str):
    """Run the full slot pipeline."""
    logger = JobLogger(job_id)
    logger.capture_output()  # Route all print output (including CrewAI) to log file
    setup_openai_retry()
    update_db(job_id, status="running", current_stage="Initializing")
    logger.log(f"Pipeline {job_id} starting (timeout: {PIPELINE_TIMEOUT}s)")

    # ── ACP: Resolve runtime config and stamp the job ──
    acp_config = _load_acp_config(job_id, "slot_pipeline", logger)
    if acp_config.get("_blocked"):
        logger.close()
        return

    # Start watchdog — kills process if pipeline exceeds timeout
    watchdog = _start_watchdog(job_id, PIPELINE_TIMEOUT)

    try:
        p = json.loads(params_json)

        # ── Auto State Recon for unknown US states ──
        KNOWN_JURISDICTIONS = {
            "uk", "malta", "ontario", "new jersey", "curacao", "curaçao",
            "sweden", "spain", "gibraltar", "isle of man", "alderney",
            "denmark", "italy", "portugal", "france", "germany",
            "michigan", "pennsylvania", "west virginia", "connecticut",
        }
        US_STATES = {
            "alabama","alaska","arizona","arkansas","california","colorado",
            "connecticut","delaware","florida","georgia","hawaii","idaho",
            "illinois","indiana","iowa","kansas","kentucky","louisiana","maine",
            "maryland","massachusetts","michigan","minnesota","mississippi",
            "missouri","montana","nebraska","nevada","new hampshire","new jersey",
            "new mexico","new york","north carolina","north dakota","ohio",
            "oklahoma","oregon","pennsylvania","rhode island","south carolina",
            "south dakota","tennessee","texas","utah","vermont","virginia",
            "washington","west virginia","wisconsin","wyoming",
        }

        if p.get("enable_recon", False):
            states_needing_recon = [
                m for m in p["target_markets"]
                if m.strip().lower() in US_STATES and m.strip().lower() not in KNOWN_JURISDICTIONS
            ]
            for state in states_needing_recon:
                try:
                    update_db(job_id, current_stage=f"State Recon: {state}")
                    logger.log(f"Running recon for {state}")
                    from flows.state_recon import run_recon
                    run_recon(state, auto=True, job_id=job_id)
                    logger.log(f"Recon complete for {state}")
                except Exception as e:
                    logger.log(f"WARN: State recon failed for {state}: {e}")

        # ── Build game input ──
        from models.schemas import GameIdeaInput, Volatility, FeatureType

        feats = []
        for f in p.get("requested_features", []):
            try:
                feats.append(FeatureType(f))
            except ValueError:
                pass

        gi = GameIdeaInput(
            theme=p["theme"],
            target_markets=p["target_markets"],
            volatility=Volatility(p["volatility"]),
            target_rtp=p["target_rtp"],
            grid_cols=p["grid_cols"],
            grid_rows=p["grid_rows"],
            ways_or_lines=str(p["ways_or_lines"]),
            max_win_multiplier=p["max_win_multiplier"],
            art_style=p["art_style"],
            requested_features=feats,
            competitor_references=p.get("competitor_references", []),
            special_requirements=p.get("special_requirements", ""),
        )

        # ── Run pipeline ──
        update_db(job_id, current_stage="Pipeline executing")
        logger.log("Pipeline executing — agents starting")
        interactive = p.get("interactive", False)
        if interactive:
            os.environ["HITL_ENABLED"] = "true"

        from flows.pipeline import SlotStudioFlow, PipelineState
        initial_state = PipelineState(game_idea=gi, job_id=job_id)
        flow = SlotStudioFlow(auto_mode=not interactive, initial_state=initial_state)
        fs = flow.kickoff()

        od = getattr(fs, "output_dir", None) if hasattr(fs, "output_dir") else None
        watchdog.cancel()  # Pipeline completed — cancel the watchdog
        update_db(
            job_id,
            status="complete",
            output_dir=str(od) if od else None,
            completed_at=datetime.now().isoformat(),
        )
        logger.log(f"Pipeline {job_id} COMPLETE → {od}")

        # ── Email notification ──
        _send_completion_email(job_id, p.get("theme", "Game"), str(od) if od else "")

        # Phase 4A: Check if this is a variant — if all siblings complete, mark parent done
        _check_variant_parent_completion(job_id)

    except Exception as e:
        update_db(job_id, status="failed", error=str(e)[:500])
        logger.log(f"Pipeline {job_id} FAILED: {e}")
        traceback.print_exc()
        _send_failure_email(job_id, p.get("theme", "Game") if 'p' in dir() else "Game", str(e)[:500])
        _check_variant_parent_completion(job_id)  # Check even on failure
    finally:
        watchdog.cancel()
        logger.close()


def run_recon_job(job_id: str, state_name: str):
    """Run state recon."""
    logger = JobLogger(job_id)
    logger.capture_output()  # Route all print output (including CrewAI) to log file
    setup_openai_retry()
    update_db(job_id, status="running", current_stage=f"Researching {state_name}...")
    logger.log(f"Recon {job_id} starting for {state_name}")

    try:
        from flows.state_recon import run_recon
        result = run_recon(state_name, auto=True, job_id=job_id)
        od = getattr(result, "output_dir", None) if result else None
        update_db(
            job_id,
            status="complete",
            output_dir=str(od) if od else None,
            completed_at=datetime.now().isoformat(),
        )
        logger.log(f"Recon {job_id} COMPLETE → {od}")

        # ── Email notification ──
        _send_recon_email(job_id, state_name)

    except Exception as e:
        update_db(job_id, status="failed", error=str(e)[:500])
        logger.log(f"Recon {job_id} FAILED: {e}")
        traceback.print_exc()  # Goes to captured stderr → log file
    finally:
        logger.close()


def run_iterate(job_id: str, params_json: str):
    """Run a selective re-run iteration of an existing pipeline."""
    logger = JobLogger(job_id)
    logger.capture_output()
    setup_openai_retry()
    update_db(job_id, status="running", current_stage="Initializing iteration")
    logger.log(f"Iterate {job_id} starting")

    watchdog = _start_watchdog(job_id, PIPELINE_TIMEOUT)

    try:
        p = json.loads(params_json)
        iterate_config = p.pop("_iterate", {})
        source_output = iterate_config.get("source_output_dir", "")
        rerun_stages = iterate_config.get("rerun_stages", ["math"])
        version = iterate_config.get("version", 2)

        logger.log(f"Source: {source_output}")
        logger.log(f"Re-run stages: {rerun_stages}")
        logger.log(f"Version: v{version}")

        # ── Build game input ──
        from models.schemas import GameIdeaInput, Volatility, FeatureType

        feats = []
        for f in p.get("requested_features", []):
            try:
                feats.append(FeatureType(f))
            except ValueError:
                pass

        gi = GameIdeaInput(
            theme=p["theme"],
            target_markets=p["target_markets"],
            volatility=Volatility(p["volatility"]),
            target_rtp=p["target_rtp"],
            grid_cols=p["grid_cols"],
            grid_rows=p["grid_rows"],
            ways_or_lines=str(p["ways_or_lines"]),
            max_win_multiplier=p["max_win_multiplier"],
            art_style=p["art_style"],
            requested_features=feats,
            competitor_references=p.get("competitor_references", []),
            special_requirements=p.get("special_requirements", ""),
        )

        # ── Run iterate flow ──
        update_db(job_id, current_stage=f"Iterating (v{version})")
        logger.log("Iterate flow starting")

        from flows.pipeline import SlotStudioFlow, PipelineState
        initial_state = PipelineState(
            game_idea=gi,
            job_id=job_id,
            iterate_mode=True,
            iterate_config={
                "source_output_dir": source_output,
                "rerun_stages": rerun_stages,
                "version": version,
            },
        )
        flow = SlotStudioFlow(auto_mode=True, initial_state=initial_state)

        fs = flow.kickoff()

        od = getattr(fs, "output_dir", None) if hasattr(fs, "output_dir") else None
        watchdog.cancel()
        update_db(
            job_id,
            status="complete",
            output_dir=str(od) if od else None,
            completed_at=datetime.now().isoformat(),
        )
        logger.log(f"Iterate {job_id} COMPLETE → {od}")

        # ── Email notification ──
        _send_completion_email(job_id, p.get("theme", "Game") if 'p' in dir() else "Game", str(od) if od else "")

    except Exception as e:
        update_db(job_id, status="failed", error=str(e)[:500])
        logger.log(f"Iterate {job_id} FAILED: {e}")
        traceback.print_exc()
        _send_failure_email(job_id, p.get("theme", "Game") if 'p' in dir() else "Game", str(e)[:500])
    finally:
        watchdog.cancel()
        logger.close()


def run_mini_rmg_job(job_id: str, params_json: str):
    """Run a Mini-RMG pipeline job (mini-game generation)."""
    logger = JobLogger(job_id)
    logger.capture_output()
    setup_openai_retry()
    p = json.loads(params_json) if params_json else {}
    theme = p.get("theme", "Mini Game")
    game_type = p.get("game_type", "crash")
    update_db(job_id, status="running", current_stage=f"Starting {game_type} pipeline")
    logger.log(f"Mini-RMG {job_id} starting: {game_type} / {theme}")

    # ── ACP: Resolve runtime config ──
    _load_acp_config(job_id, "mini_rmg", logger)

    try:
        from flows.mini_rmg_pipeline import run_mini_rmg
        from pathlib import Path as _Path

        od = _Path(os.environ.get("OUTPUT_ROOT", "output")) / f"minigame_{job_id}"
        od.mkdir(parents=True, exist_ok=True)

        run_mini_rmg(job_id, p, str(od))

        update_db(
            job_id,
            status="complete",
            output_dir=str(od),
            completed_at=datetime.now().isoformat(),
        )
        logger.log(f"Mini-RMG {job_id} COMPLETE → {od}")

    except Exception as e:
        update_db(job_id, status="failed", error=str(e)[:500])
        logger.log(f"Mini-RMG {job_id} FAILED: {e}")
        traceback.print_exc()
    finally:
        logger.close()


def run_resume(job_id: str):
    """Resume a partial pipeline from its last checkpoint.

    Reads checkpoint.json from the job's output_dir, hydrates PipelineState,
    and calls flow.resume_from_stage() to continue execution.

    The output_dir is read from the DB (written during initialize stage).
    """
    logger = JobLogger(job_id)
    logger.capture_output()
    setup_openai_retry()

    logger.log(f"Resume {job_id} starting")

    # ── Read output_dir and current state from DB ──
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()

    if not job:
        logger.log(f"Resume FAILED: job {job_id} not found in DB")
        return

    if job["status"] != "partial":
        logger.log(f"Resume FAILED: job status is '{job['status']}', expected 'partial'")
        update_db(job_id, error=f"Cannot resume: status is '{job['status']}', expected 'partial'")
        return

    output_dir = job["output_dir"]
    if not output_dir or not Path(output_dir).exists():
        logger.log(f"Resume FAILED: output_dir missing or doesn't exist: {output_dir}")
        update_db(job_id, status="failed", error="Cannot resume: output directory missing")
        return

    ckpt_path = Path(output_dir) / "checkpoint.json"
    if not ckpt_path.exists():
        logger.log(f"Resume FAILED: checkpoint.json not found in {output_dir}")
        update_db(job_id, status="failed", error="Cannot resume: checkpoint.json missing")
        return

    # ── Load checkpoint ──
    try:
        ckpt = json.loads(ckpt_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, IOError) as e:
        logger.log(f"Resume FAILED: cannot read checkpoint: {e}")
        update_db(job_id, status="failed", error=f"Cannot resume: checkpoint unreadable: {e}")
        return

    last_stage = ckpt.get("stage", "")
    resume_count = ckpt.get("resume_count", 0)
    prior_elapsed = ckpt.get("pipeline_elapsed_s", 0)

    # ── Validate resume limit ──
    from flows.pipeline import MAX_RESUMES
    if resume_count >= MAX_RESUMES:
        logger.log(f"Resume FAILED: max resumes ({MAX_RESUMES}) reached")
        update_db(
            job_id, status="failed",
            error=f"Maximum resumes ({MAX_RESUMES}) exceeded — manual intervention required"
        )
        return

    logger.log(
        f"Checkpoint loaded: last_stage='{last_stage}', "
        f"resume #{resume_count + 1}/{MAX_RESUMES}, "
        f"prior_elapsed={prior_elapsed:.0f}s"
    )

    # ── Mark job as running ──
    update_db(job_id, status="running", current_stage=f"Resuming from {last_stage}")

    # Start watchdog for the resume run
    watchdog = _start_watchdog(job_id, PIPELINE_TIMEOUT)

    try:
        # ── Hydrate PipelineState from checkpoint ──
        from flows.pipeline import SlotStudioFlow, PipelineState, TimeBudget, PIPELINE_TIMEOUT as PT

        state_data = ckpt.get("state", {})
        state = PipelineState.model_validate(state_data)
        state.resume_count = resume_count + 1

        # ── Create flow with restored state via constructor (avoids property setter issue) ──
        flow = SlotStudioFlow(auto_mode=True, initial_state=state)
        flow.budget = TimeBudget(PT, already_elapsed=prior_elapsed)

        # ── Execute remaining stages ──
        logger.log(f"Executing resume_from_stage('{last_stage}')")
        fs = flow.resume_from_stage(last_stage)

        # ── Success ──
        od = getattr(fs, "output_dir", None) if hasattr(fs, "output_dir") else output_dir
        watchdog.cancel()
        update_db(
            job_id,
            status="complete",
            output_dir=str(od) if od else output_dir,
            completed_at=datetime.now().isoformat(),
        )
        logger.log(f"Resume {job_id} COMPLETE → {od}")

        # ── Email notification ──
        title = state.game_idea.theme if state.game_idea else "Game"
        _send_completion_email(job_id, title, str(od) if od else output_dir)

    except Exception as e:
        update_db(job_id, status="failed", error=f"Resume failed: {str(e)[:500]}")
        logger.log(f"Resume {job_id} FAILED: {e}")
        traceback.print_exc()
        title = "Game"
        try:
            title = ckpt.get("state", {}).get("game_idea", {}).get("theme", "Game")
        except Exception:
            pass
        _send_failure_email(job_id, title, f"Resume failed: {str(e)[:300]}")
    finally:
        watchdog.cancel()
        logger.close()


if __name__ == "__main__":
    if len(sys.argv) < 3:
        logger.error("Usage: python worker.py [pipeline|recon|iterate|resume] <job_id> <params>")
        sys.exit(1)

    job_type = sys.argv[1]
    job_id = sys.argv[2]

    if job_type == "pipeline":
        params_json = sys.argv[3] if len(sys.argv) > 3 else "{}"
        run_pipeline(job_id, params_json)
    elif job_type == "recon":
        state_name = sys.argv[3] if len(sys.argv) > 3 else "unknown"
        run_recon_job(job_id, state_name)
    elif job_type == "iterate":
        params_json = sys.argv[3] if len(sys.argv) > 3 else "{}"
        run_iterate(job_id, params_json)
    elif job_type == "resume":
        run_resume(job_id)
    elif job_type == "mini_rmg":
        params_json = sys.argv[3] if len(sys.argv) > 3 else "{}"
        run_mini_rmg_job(job_id, params_json)
    else:
        logger.error(f"Unknown job type: {job_type}")
        sys.exit(1)
