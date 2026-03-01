"""
ARKAINBRAIN — AI-Powered Gaming Intelligence Platform
by ArkainGames.com
"""
import html, json, logging, os, secrets, sqlite3, subprocess, time, uuid
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

# ── Structured logging (replaces print statements) ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("arkainbrain")

os.environ["CREWAI_TELEMETRY_OPT_OUT"] = "true"
os.environ["OTEL_SDK_DISABLED"] = "true"
os.environ["CREWAI_TRACING_ENABLED"] = "false"  # Disable tracing prompt
os.environ["DO_NOT_TRACK"] = "1"
os.environ["CREWAI_STORAGE_DIR"] = "/tmp/crewai_storage"

# ── Pre-create CrewAI config to prevent interactive tracing prompt ──
for _d in [Path.home() / ".crewai", Path("/tmp/crewai_storage")]:
    _d.mkdir(parents=True, exist_ok=True)
    _cfg = _d / "config.json"
    if not _cfg.exists():
        _cfg.write_text(json.dumps({"tracing_enabled": False, "tracing_disabled": True}))

from flask import Flask, redirect, url_for, session, request, jsonify, send_from_directory, Response, g, has_app_context
from werkzeug.middleware.proxy_fix import ProxyFix
from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # Trust Railway's reverse proxy

# ── Register ACP Admin Blueprint ──
try:
    from admin import admin_bp
    app.register_blueprint(admin_bp)
    logger.info("Registered ACP admin blueprint at /admin/")
except Exception as _admin_err:
    import traceback
    logger.error(f"Admin blueprint registration FAILED: {_admin_err}\n{traceback.format_exc()}")
    # Also add a fallback route so /admin/ shows the error instead of 404
    @app.route("/admin/")
    def admin_error_page():
        return f"<pre>Admin blueprint failed to load:\n\n{html.escape(str(_admin_err))}\n\n{html.escape(traceback.format_exc())}</pre>", 500

# XSS protection — escape user-supplied content before rendering in HTML
_esc = html.escape

# sqlite3.Row does not support .get() — use this helper everywhere
def _rget(row, key, default=None):
    """Safe .get() for sqlite3.Row objects."""
    try:
        val = row[key]
        return val if val is not None else default
    except (IndexError, KeyError):
        return default

# ── Stable SECRET_KEY — survives process restarts, gunicorn recycling, deploys ──
# Priority: env var → persisted file → generate-and-save
# Without this, every gunicorn --max-requests restart invalidates ALL sessions.
def _get_or_create_secret_key():
    # 1. Explicit env var — always wins
    env_key = os.getenv("FLASK_SECRET_KEY") or os.getenv("SECRET_KEY")
    if env_key:
        return env_key
    # 2. Persisted to file — survives process restarts within same container
    key_file = Path(os.getenv("DB_PATH", "arkainbrain.db")).parent / ".flask_secret_key"
    try:
        if key_file.exists():
            stored = key_file.read_text().strip()
            if len(stored) >= 32:
                return stored
    except Exception:
        pass
    # 3. Generate once and save
    new_key = secrets.token_hex(32)
    try:
        key_file.write_text(new_key)
    except Exception:
        pass  # In-memory only if filesystem is truly read-only
    return new_key

app.secret_key = _get_or_create_secret_key()
if not (os.getenv("FLASK_SECRET_KEY") or os.getenv("SECRET_KEY")):
    logger.warning("FLASK_SECRET_KEY not set — sessions may not survive Railway redeploys. "
                    "Set it in Railway env vars for permanent session persistence.")

# ── Session configuration — persist across browser restarts + devices ──
app.config["PREFERRED_URL_SCHEME"] = "https"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB — reject oversized uploads/posts
# Only set Secure=True in production (HTTPS)
if os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RENDER") or os.getenv("FLY_APP_NAME"):
    app.config["SESSION_COOKIE_SECURE"] = True

LOG_DIR = Path(os.getenv("LOG_DIR", "./logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)

oauth = OAuth(app)
google = oauth.register(
    name="google",
    client_id=os.getenv("GOOGLE_CLIENT_ID", ""),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET", ""),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "./output"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = os.getenv("DB_PATH", "arkainbrain.db")

def _open_db():
    """Open a raw SQLite connection (caller manages lifecycle)."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")     # Concurrent reads + writes
    conn.execute("PRAGMA busy_timeout=5000")     # Wait up to 5s for lock
    return conn

def get_db():
    """Get DB connection. In request context, caches in g and auto-closes on teardown.
    Outside request context (startup, tests), returns a standalone connection — caller must close."""
    try:
        if has_app_context():
            if "_database" not in g:
                g._database = _open_db()
            return g._database
    except RuntimeError:
        pass
    return _open_db()

@app.teardown_appcontext
def _close_db(exc):
    """Auto-close the per-request DB connection. Prevents connection leaks on exceptions."""
    db = g.pop("_database", None)
    if db is not None:
        db.close()

def init_db():
    """Initialize ALL database tables using the comprehensive schema from config.database.
    
    IMPORTANT: This delegates to config.database.init_db() which has the FULL schema
    (users, jobs, reviews, file_tags, run_records, etc.). Previously this function only
    created users+jobs, causing 'no such table: reviews' errors.
    
    We also run a local fallback for the core tables in case config.database 
    has import issues (defensive — production resilience).
    """
    # Primary: use comprehensive schema from config.database
    try:
        from config.database import init_db as _full_init_db, migrate_db as _full_migrate_db
        _full_init_db()
        _full_migrate_db()
        return
    except Exception as e:
        logger.warning(f"config.database.init_db() failed, using fallback: {e}")
    
    # Fallback: create at minimum the tables web_app needs
    db = _open_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT, 
            picture TEXT, created_at TEXT DEFAULT (datetime('now')),
            email_notify INTEGER DEFAULT 1, is_admin INTEGER DEFAULT 0,
            api_key TEXT, provider TEXT DEFAULT 'google'
        );
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, 
            job_type TEXT NOT NULL DEFAULT 'slot_pipeline',
            title TEXT NOT NULL, params TEXT, status TEXT DEFAULT 'queued', 
            current_stage TEXT DEFAULT 'Initializing', output_dir TEXT, 
            error TEXT, created_at TEXT DEFAULT (datetime('now')), 
            completed_at TEXT, parent_job_id TEXT, version INTEGER DEFAULT 1,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS reviews (
            id TEXT PRIMARY KEY, job_id TEXT NOT NULL,
            stage TEXT NOT NULL, title TEXT NOT NULL,
            summary TEXT, files TEXT,
            status TEXT DEFAULT 'pending', approved INTEGER,
            feedback TEXT, created_at TEXT DEFAULT (datetime('now')),
            resolved_at TEXT,
            FOREIGN KEY (job_id) REFERENCES jobs(id)
        );
        CREATE TABLE IF NOT EXISTS review_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, review_id TEXT NOT NULL,
            user_id TEXT, comment TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (review_id) REFERENCES reviews(id)
        );
        CREATE TABLE IF NOT EXISTS section_approvals (
            id INTEGER PRIMARY KEY AUTOINCREMENT, review_id TEXT NOT NULL,
            section_name TEXT NOT NULL, approved INTEGER DEFAULT 0,
            notes TEXT, updated_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (review_id) REFERENCES reviews(id)
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id);
        CREATE INDEX IF NOT EXISTS idx_reviews_job ON reviews(job_id);
        CREATE INDEX IF NOT EXISTS idx_reviews_status ON reviews(status);
        CREATE TABLE IF NOT EXISTS generated_games (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
            game_type TEXT NOT NULL, theme_name TEXT NOT NULL,
            filename TEXT NOT NULL, config_json TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS competitor_games (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, provider TEXT NOT NULL,
            theme TEXT, theme_tags TEXT, rtp REAL, volatility TEXT, grid TEXT,
            max_win REAL, features TEXT, release_date TEXT, source TEXT,
            source_url TEXT, metadata TEXT, created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS market_snapshots (
            id TEXT PRIMARY KEY, snapshot_type TEXT NOT NULL, scan_date TEXT NOT NULL,
            data TEXT NOT NULL, sources_count INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS opportunity_scores (
            id TEXT PRIMARY KEY, theme TEXT NOT NULL, mechanic TEXT NOT NULL,
            opportunity_score REAL NOT NULL, demand_signal REAL,
            supply_saturation REAL, trend_momentum REAL, reasoning TEXT,
            scan_date TEXT NOT NULL, created_at TEXT DEFAULT (datetime('now'))
        );
    """)
    db.close()

# ── Migrate existing databases: add new columns if missing ──
def _migrate_db():
    """Run migrations. Delegates to config.database.migrate_db() for comprehensive coverage.
    Falls back to local migration for core tables if import fails."""
    try:
        # config.database.migrate_db handles all tables comprehensively
        from config.database import migrate_db as _full_migrate
        _full_migrate()
        return
    except Exception:
        pass
    # Fallback: minimal local migration
    try:
        db = _open_db()
        cols = [r["name"] for r in db.execute("PRAGMA table_info(jobs)").fetchall()]
        if "parent_job_id" not in cols:
            db.execute("ALTER TABLE jobs ADD COLUMN parent_job_id TEXT")
        if "version" not in cols:
            db.execute("ALTER TABLE jobs ADD COLUMN version INTEGER DEFAULT 1")
        user_cols = [r["name"] for r in db.execute("PRAGMA table_info(users)").fetchall()]
        if "email_notify" not in user_cols:
            db.execute("ALTER TABLE users ADD COLUMN email_notify INTEGER DEFAULT 1")
        if "is_admin" not in user_cols:
            db.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0")
        db.commit()
        db.close()
    except Exception as e:
        logger.warning(f"DB migration check: {e}")
init_db()
_migrate_db()

# ── Recover from crashes: check for orphaned "running" jobs from before restart ──
def _recover_stale_jobs():
    """On startup, check for jobs stuck in 'running'/'queued' from a previous crash.
    With start_new_session=True, workers may still be alive — only mark truly stale ones.
    Pipeline timeout is 90 min, so anything > 100 min is definitely stale."""
    try:
        db = _open_db()
        stale = db.execute(
            "SELECT id, title FROM jobs WHERE status IN ('running','queued') "
            "AND created_at < datetime('now', '-100 minutes')"
        ).fetchall()
        for job in stale:
            db.execute(
                "UPDATE jobs SET status='failed', error='Timed out — exceeded maximum pipeline duration' WHERE id=?",
                (job["id"],)
            )
        if stale:
            db.commit()
            logger.info(f"[RECOVERY] Marked {len(stale)} stale jobs as failed")
        db.close()
    except Exception as e:
        logger.warning(f"Stale job recovery: {e}")

_recover_stale_jobs()

# ── Concurrent job limit — prevent runaway subprocess spawning ──
MAX_CONCURRENT_JOBS = 3

def _check_job_limit(user_id):
    """Return error response if user has too many running jobs, else None."""
    db = get_db()
    running = db.execute(
        "SELECT COUNT(*) as c FROM jobs WHERE user_id=? AND status IN ('queued','running')",
        (user_id,)
    ).fetchone()["c"]
    if running >= MAX_CONCURRENT_JOBS:
        return f"You have {running} jobs in progress (max {MAX_CONCURRENT_JOBS}). Please wait for one to finish.", 429
    return None

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session: return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated

@app.before_request
def _refresh_session():
    """Keep sessions alive for 30 days from last activity.
    This runs on every request and resets the 30-day expiry timer."""
    session.permanent = True

@app.before_request
def _csrf_origin_check():
    """Reject cross-origin POST/PUT/DELETE requests (poor-man's CSRF protection).
    Combined with SameSite=Lax cookies, this blocks most CSRF vectors."""
    if request.method in ("POST", "PUT", "DELETE"):
        origin = request.headers.get("Origin") or request.headers.get("Referer", "")
        if origin:
            from urllib.parse import urlparse
            allowed = request.host_url.rstrip("/")
            incoming = f"{urlparse(origin).scheme}://{urlparse(origin).netloc}"
            if incoming and incoming != allowed:
                return "Cross-origin request blocked", 403

def current_user(): return session.get("user", {})

BRAND_CSS = r"""
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=Geist+Mono:wght@400;500&display=swap');
:root{
  --bg-void:#000000;--bg-surface:#0a0a0a;--bg-card:#111111;--bg-card-hover:#1a1a1a;--bg-input:#0a0a0a;
  --border:rgba(255,255,255,0.06);--border-hover:rgba(255,255,255,0.12);--border-focus:rgba(255,255,255,0.20);
  --text:#d4d4d4;--text-bright:#ffffff;--text-muted:#888888;--text-dim:#555555;
  --accent:#ffffff;--accent-soft:rgba(255,255,255,0.06);--accent-mid:rgba(255,255,255,0.10);--accent-bright:#ffffff;
  --success:#22c55e;--success-soft:rgba(34,197,94,0.08);--warning:#eab308;--warning-soft:rgba(234,179,8,0.08);--danger:#ef4444;--danger-soft:rgba(239,68,68,0.08);
  --radius:10px;--radius-lg:14px;--radius-xl:20px;
  --transition:all 0.15s ease;
}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg-void);color:var(--text);min-height:100vh;-webkit-font-smoothing:antialiased;font-size:14px;line-height:1.6}
::selection{background:rgba(255,255,255,0.15);color:#fff}
::-webkit-scrollbar{width:4px}::-webkit-scrollbar-track{background:transparent}::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.08);border-radius:10px}::-webkit-scrollbar-thumb:hover{background:rgba(255,255,255,0.14)}
a{color:var(--text-bright);text-decoration:none;transition:var(--transition)}a:hover{color:var(--text-muted)}

/* ── Layout Shell ── */
.topbar{position:sticky;top:0;z-index:100;display:flex;align-items:center;justify-content:space-between;padding:0 24px;height:52px;background:rgba(0,0,0,0.9);border-bottom:1px solid var(--border);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px)}
.logo{display:flex;align-items:center;gap:10px;font-weight:700;font-size:14px;letter-spacing:-0.03em;color:var(--text-bright);text-decoration:none}
.logo-mark{width:28px;height:28px;border-radius:8px;background:#fff;display:grid;place-items:center;font-size:13px;font-weight:800;color:#000}
.version-tag{font-size:10px;font-weight:500;color:var(--text-dim);font-family:'Geist Mono',monospace}
.user-pill{display:flex;align-items:center;gap:8px;padding:5px 14px 5px 5px;border-radius:24px;border:1px solid var(--border);font-size:12px;color:var(--text-muted);text-decoration:none;transition:var(--transition)}
.user-pill img{width:24px;height:24px;border-radius:50%}
.user-pill:hover{border-color:var(--border-hover);color:var(--text-bright)}

.shell{display:grid;grid-template-columns:220px 1fr;min-height:calc(100vh - 52px)}
.sidebar{padding:12px 0;border-right:1px solid var(--border);background:var(--bg-void);display:flex;flex-direction:column;gap:1px}
.sidebar a{display:flex;align-items:center;gap:10px;padding:9px 20px;font-size:13px;font-weight:400;color:var(--text-muted);text-decoration:none;transition:var(--transition);margin:0 8px;border-radius:8px}
.sidebar a:hover{color:var(--text-bright);background:var(--accent-soft)}
.sidebar a.active{color:var(--text-bright);background:var(--accent-soft);font-weight:500}
.sidebar a svg{width:16px;height:16px;opacity:0.4;flex-shrink:0}
.sidebar a:hover svg{opacity:0.65}.sidebar a.active svg{opacity:0.8}
.sidebar .section-label{font-size:10px;font-weight:500;letter-spacing:1.2px;color:var(--text-dim);padding:20px 20px 8px;text-transform:uppercase}

.main{padding:32px 48px;max-width:780px;width:100%;animation:fadeIn 0.15s ease}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
.page-title{font-size:22px;font-weight:700;color:var(--text-bright);margin-bottom:4px;letter-spacing:-0.03em}
.page-subtitle{color:var(--text-muted);font-size:13px;margin-bottom:28px;font-weight:400}

/* ── Cards — nearly invisible borders, float on black ── */
.card{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius-lg);padding:24px;margin-bottom:14px;transition:var(--transition)}
.card:hover{border-color:var(--border-hover)}
.card h2{font-size:11px;font-weight:500;color:var(--text-muted);margin-bottom:16px;display:flex;align-items:center;gap:8px;letter-spacing:0.6px;text-transform:uppercase}

/* ── Forms ── */
label{display:block;font-size:12px;font-weight:500;color:var(--text-muted);margin-bottom:6px;letter-spacing:0.2px}
input,select,textarea{width:100%;padding:10px 14px;border-radius:var(--radius);border:1px solid var(--border);background:var(--bg-input);color:var(--text-bright);font-family:'Inter',sans-serif;font-size:13px;margin-bottom:16px;outline:none;transition:var(--transition)}
input:focus,select:focus,textarea:focus{border-color:var(--border-focus)}
input::placeholder,textarea::placeholder{color:var(--text-dim)}
textarea{min-height:70px;resize:vertical}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.row3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px}

/* ── Buttons — flat, no gradients ── */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;padding:9px 20px;border-radius:var(--radius);border:none;font-family:'Inter',sans-serif;font-size:13px;font-weight:500;cursor:pointer;transition:var(--transition);text-decoration:none}
.btn-primary{background:var(--text-bright);color:#000;font-weight:600}
.btn-primary:hover{opacity:0.85;color:#000}
.btn-primary:active{transform:scale(0.98)}
.btn-ghost{background:transparent;color:var(--text);border:1px solid var(--border)}
.btn-ghost:hover{border-color:var(--border-hover);color:var(--text-bright)}
.btn-sm{padding:6px 14px;font-size:12px;border-radius:8px}
.btn-full{width:100%}

/* ── Badges — minimal ── */
.badge{display:inline-flex;align-items:center;gap:5px;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:500;letter-spacing:0.1px}
.badge-running{color:var(--text-bright);background:var(--accent-soft)}
.badge-complete{color:var(--success)}
.badge-failed{color:var(--danger)}
.badge-queued{color:var(--warning)}
.badge-partial{color:#f59e0b;background:rgba(245,158,11,0.10)}
.badge-running::before{content:'';width:5px;height:5px;border-radius:50%;background:var(--text-bright);animation:pulse 1.8s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.15}}

/* ── History / List Items ── */
.history-item{display:grid;grid-template-columns:1fr 120px 140px 100px;align-items:center;padding:13px 20px;border-bottom:1px solid var(--border);font-size:13px;transition:var(--transition)}
.history-item:hover{background:var(--accent-soft)}
.history-title{font-weight:500;color:var(--text-bright)}
.history-type{color:var(--text-muted);font-size:12px;margin-top:2px}
.history-date{color:var(--text-dim);font-size:12px;font-family:'Geist Mono',monospace}
.history-actions{display:flex;gap:6px;justify-content:flex-end}

/* ── File Rows ── */
.file-row{display:flex;align-items:center;justify-content:space-between;padding:11px 20px;border-bottom:1px solid var(--border);font-size:13px;transition:var(--transition)}
.file-row:hover{background:var(--accent-soft)}
.file-row a{color:var(--text-bright);text-decoration:none;font-family:'Geist Mono',monospace;font-size:12px}
.file-row a:hover{color:var(--text-muted)}
.file-size{color:var(--text-dim);font-size:11px;font-family:'Geist Mono',monospace}

/* ── Stat Cards ── */
.stat-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:24px}
.stat-card{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);padding:16px;text-align:center;transition:var(--transition)}
.stat-card:hover{border-color:var(--border-hover)}
.stat-card .stat-icon{font-size:18px;margin-bottom:6px}
.stat-card .stat-val{font-size:16px;font-weight:600;color:var(--text-bright)}
.stat-card .stat-label{font-size:10px;color:var(--text-dim);text-transform:uppercase;letter-spacing:0.6px;margin-top:4px;font-weight:500}
.stat-card.online .stat-val{color:var(--success)}
.stat-card.offline{opacity:0.35}

/* ── Feature Grid ── */
.feature-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:4px 10px}
.feature-grid label{display:flex;align-items:center;gap:8px;font-size:12px;font-weight:400;color:var(--text);text-transform:none;margin-bottom:0;cursor:pointer;padding:8px 10px;border-radius:8px;transition:var(--transition);border:1px solid transparent}
.feature-grid label:hover{background:var(--accent-soft);border-color:var(--border)}
.feature-grid label input{width:auto;margin:0;accent-color:#fff}
.feature-grid .feat-tag{font-size:9px;padding:2px 6px;border-radius:5px;font-weight:600;margin-left:auto;letter-spacing:0.2px}
.feat-tag.ip-risk{background:var(--danger-soft);color:var(--danger)}
.feat-tag.safe{background:var(--success-soft);color:var(--success)}
.feat-tag.banned{background:var(--warning-soft);color:var(--warning)}

/* ── Toggle / Options ── */
.toggle-section{padding:14px 18px;background:var(--bg-surface);border-radius:var(--radius);margin-top:12px;display:flex;flex-wrap:wrap;gap:18px;border:1px solid var(--border)}
.toggle-item{display:flex;align-items:center;gap:8px}
.toggle-item input{width:auto;margin:0;accent-color:#fff}
.toggle-item label{margin:0;font-size:12px;text-transform:none;color:var(--text-bright);font-weight:500}
.toggle-item .toggle-desc{font-size:11px;color:var(--text-dim)}

/* ── Login ── */
.login-wrap{min-height:100vh;display:grid;place-items:center;background:var(--bg-void)}
.login-box{text-align:center;padding:48px;width:380px;position:relative;z-index:1}
.login-box h1{font-size:24px;font-weight:700;letter-spacing:-0.03em;color:var(--text-bright);margin:24px 0 10px}
.login-box p{color:var(--text-dim);font-size:13px;margin-bottom:40px;line-height:1.7}
.google-btn{display:inline-flex;align-items:center;gap:10px;padding:12px 28px;border-radius:var(--radius);border:1px solid var(--border);background:transparent;color:var(--text-bright);font-family:'Inter',sans-serif;font-size:13px;font-weight:500;cursor:pointer;transition:var(--transition);text-decoration:none}
.google-btn:hover{border-color:var(--border-hover);background:var(--accent-soft)}
.google-btn svg{width:18px;height:18px}

/* ── Special Components ── */
.proto-frame{width:100%;height:600px;border:1px solid var(--border);border-radius:var(--radius);background:#000}
.audio-player{display:flex;align-items:center;gap:12px;padding:10px 20px;border-bottom:1px solid var(--border);font-size:13px}
.audio-player audio{height:32px;flex:1}
.audio-player .audio-name{font-family:'Geist Mono',monospace;font-size:11px;color:var(--text-bright);min-width:140px}

.cert-timeline{display:flex;gap:0;margin:16px 0}
.cert-step{flex:1;text-align:center;padding:12px 8px;position:relative}
.cert-step::after{content:'';position:absolute;top:26px;right:0;width:50%;height:2px;background:var(--border)}
.cert-step::before{content:'';position:absolute;top:26px;left:0;width:50%;height:2px;background:var(--border)}
.cert-step:first-child::before,.cert-step:last-child::after{display:none}
.cert-step .cert-dot{width:8px;height:8px;border-radius:50%;background:var(--text-bright);margin:0 auto 8px;position:relative;z-index:1}
.cert-step .cert-title{font-size:11px;font-weight:500;color:var(--text-bright)}
.cert-step .cert-sub{font-size:10px;color:var(--text-muted)}

.recon-input-group{display:flex;gap:12px;align-items:flex-end}
.recon-input-group input{margin-bottom:0;flex:1}
.recon-input-group .btn{white-space:nowrap;height:42px}
.empty-state{text-align:center;padding:48px 20px;color:var(--text-dim)}
.empty-state h3{font-size:14px;color:var(--text-muted);margin-bottom:6px;font-weight:500}
.empty-state p{font-size:13px}

/* ── Capability Grid ── */
.capability-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.cap-item{display:flex;align-items:center;gap:10px;padding:10px 14px;border-radius:8px;background:transparent;border:1px solid var(--border);font-size:12px;color:var(--text);transition:var(--transition)}
.cap-item:hover{border-color:var(--border-hover);background:var(--accent-soft)}
.cap-item b{color:var(--text-bright);font-weight:500}
.cap-item .cap-tag{font-size:10px;color:var(--text-dim);margin-left:auto;font-family:'Geist Mono',monospace}

/* ── Action Cards ── */
.action-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:24px}
.action-card{display:flex;align-items:center;gap:14px;padding:16px 20px;border-radius:var(--radius-lg);border:1px solid var(--border);background:transparent;text-decoration:none;transition:var(--transition)}
.action-card:hover{border-color:var(--border-hover);background:var(--accent-soft)}
.action-card .action-icon{font-size:20px;width:40px;height:40px;border-radius:10px;display:grid;place-items:center;background:var(--accent-soft);flex-shrink:0}
.action-card .action-text{font-size:13px;font-weight:600;color:var(--text-bright)}
.action-card .action-desc{font-size:12px;color:var(--text-dim);margin-top:2px}

/* ── Greeting ── */
.greeting{margin-bottom:24px}
.greeting h2{font-size:24px;font-weight:700;letter-spacing:-0.03em;color:var(--text-bright);margin-bottom:4px}
.greeting p{font-size:13px;color:var(--text-muted);font-weight:400}
.greeting .engine-tag{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:16px;border:1px solid var(--border);font-size:11px;color:var(--text-muted);font-weight:400;margin-top:8px}
.greeting .engine-tag::before{content:'';width:4px;height:4px;border-radius:50%;background:var(--success);animation:pulse 2s ease-in-out infinite}

/* ── Pipeline Form Sections ── */
.form-section{position:relative;counter-increment:form-step}
.form-section::before{content:counter(form-step);position:absolute;left:-36px;top:24px;width:24px;height:24px;border-radius:50%;background:var(--accent-soft);border:1px solid var(--border);display:grid;place-items:center;font-size:11px;font-weight:600;color:var(--text-muted);font-family:'Geist Mono',monospace}
.form-steps{counter-reset:form-step;padding-left:36px}

/* ── Log Terminal — Grok thinking style ── */
.log-terminal{background:var(--bg-surface);border:1px solid var(--border);border-radius:var(--radius-lg);padding:0;font-family:'Geist Mono',monospace;font-size:11.5px;line-height:1.8;height:calc(100vh - 200px);overflow-y:auto;white-space:pre-wrap;color:var(--text);position:relative}
.log-terminal .log-header{position:sticky;top:0;display:flex;align-items:center;gap:10px;padding:12px 16px;background:rgba(10,10,10,0.95);border-bottom:1px solid var(--border);backdrop-filter:blur(8px);z-index:10;font-size:12px;color:var(--text-dim)}
.log-terminal .log-body{padding:16px}

/* ── Shimmer Thinking Animation ── */
@keyframes shimmer-text{0%{background-position:-200% center}100%{background-position:200% center}}
.stage-shimmer{background:linear-gradient(90deg,var(--text-dim) 25%,var(--text-bright) 50%,var(--text-dim) 75%);background-size:200% auto;-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;animation:shimmer-text 2s linear infinite}

/* ── Progress Timeline ── */
.progress-timeline{display:flex;flex-direction:column;gap:0}
.progress-step{display:flex;align-items:flex-start;gap:12px;padding:8px 0;font-size:12px;color:var(--text-muted)}
.progress-step .step-dot{width:6px;height:6px;border-radius:50%;background:var(--text-dim);margin-top:5px;flex-shrink:0}
.progress-step.active .step-dot{background:var(--text-bright);box-shadow:0 0 6px rgba(255,255,255,0.3);animation:pulse 1.8s ease-in-out infinite}
.progress-step.done .step-dot{background:var(--success)}
.progress-step.done{color:var(--text-dim)}
.progress-step.active{color:var(--text-bright)}
.progress-step .step-time{font-family:'Geist Mono',monospace;color:var(--text-dim);font-size:11px;min-width:42px}

@media(max-width:768px){
  .shell{grid-template-columns:1fr}.sidebar{display:none}.main{padding:20px 16px;max-width:100%}
  .history-item{grid-template-columns:1fr 1fr;gap:8px}.stat-grid{grid-template-columns:repeat(2,1fr)}
  .feature-grid{grid-template-columns:1fr 1fr}.capability-grid{grid-template-columns:1fr}
  .action-grid{grid-template-columns:1fr}.greeting h2{font-size:20px}
  .form-steps{padding-left:0}.form-section::before{display:none}
}
"""

ICON_DASH = '<svg fill="none" stroke="currentColor" stroke-width="1.5" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>'
ICON_PLUS = '<svg fill="none" stroke="currentColor" stroke-width="1.5" viewBox="0 0 24 24"><path d="M12 5v14m7-7H5"/></svg>'
ICON_SEARCH = '<svg fill="none" stroke="currentColor" stroke-width="1.5" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.35-4.35"/></svg>'
ICON_FOLDER = '<svg fill="none" stroke="currentColor" stroke-width="1.5" viewBox="0 0 24 24"><path d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z"/></svg>'
ICON_CLOCK = '<svg fill="none" stroke="currentColor" stroke-width="1.5" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg>'
ICON_GLOBE = '<svg fill="none" stroke="currentColor" stroke-width="1.5" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a15 15 0 014 9 15 15 0 01-4 9 15 15 0 01-4-9 15 15 0 014-9z"/></svg>'
ICON_DB = '<svg fill="none" stroke="currentColor" stroke-width="1.5" viewBox="0 0 24 24"><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.66 3.58 3 8 3s8-1.34 8-3V5"/><path d="M4 12c0 1.66 3.58 3 8 3s8-1.34 8-3"/></svg>'
ICON_REVIEW = '<svg fill="none" stroke="currentColor" stroke-width="1.5" viewBox="0 0 24 24"><path d="M9 12l2 2 4-4"/><circle cx="12" cy="12" r="9"/></svg>'
ICON_SETTINGS = '<svg fill="none" stroke="currentColor" stroke-width="1.5" viewBox="0 0 24 24"><path d="M12 15a3 3 0 100-6 3 3 0 000 6z"/><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 01-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 012.83-2.83l.06.06A1.65 1.65 0 009 4.68a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z"/></svg>'
GOOGLE_SVG = '<svg viewBox="0 0 24 24"><path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 01-2.2 3.32v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.1z"/><path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/><path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z"/><path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"/></svg>'
ICON_ARCADE = '<svg fill="none" stroke="currentColor" stroke-width="1.5" viewBox="0 0 24 24"><path d="M14.5 6.5a2.5 2.5 0 11-5 0 2.5 2.5 0 015 0z"/><path d="M6 20l1.5-9h9L18 20"/><path d="M8 14h8"/><circle cx="9.5" cy="17" r=".5" fill="currentColor"/><circle cx="14.5" cy="17" r=".5" fill="currentColor"/></svg>'
FAVICON_SVG = "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='8' fill='white'/><text x='16' y='22' text-anchor='middle' fill='black' font-size='18' font-weight='800'>A</text></svg>"

def layout(content, page="dashboard"):
    user = current_user()
    items = [("dashboard","Dashboard",ICON_DASH,"/"),("new","New Pipeline",ICON_PLUS,"/new"),("minigame","New Mini-Game","🎮","/new-minigame"),("novel","Novel Game","🧪","/novel"),("arcade","Arcade",ICON_ARCADE,"/arcade"),("trends","Trend Radar","📡","/trends"),("library","Game Library","📚","/game-library"),("recon","State Recon",ICON_GLOBE,"/recon"),("reviews","Reviews",ICON_REVIEW,"/reviews"),("history","History",ICON_CLOCK,"/history"),("files","All Files",ICON_FOLDER,"/files"),("qdrant","Qdrant",ICON_DB,"/qdrant"),("settings","Settings",ICON_SETTINGS,"/settings")]
    # Show Control Plane link only to admin
    try:
        from admin.decorators import _is_admin
        if _is_admin(user):
            items.append(("admin","⚡ Control Plane","⚡","/admin/"))
    except Exception:
        pass
    nav = '<div class="section-label">Platform</div>'
    for k,l,i,h in items:
        nav += f'<a href="{h}" class="{"active" if page==k else ""}">{i} {l}</a>'
    pic = user.get("picture","") or ""
    name = user.get("name","User")
    initial = _esc(name[0].upper()) if name else "U"
    pic_tag = f'<img src="{_esc(pic)}" alt="" onerror="this.style.display=\'none\';this.nextElementSibling.style.display=\'grid\'" style="width:20px;height:20px;border-radius:50%"><span style="display:none;width:20px;height:20px;border-radius:50%;background:var(--bg-card-hover);place-items:center;font-size:10px;font-weight:600;color:var(--text-muted)">{initial}</span>' if pic else f'<span style="display:inline-grid;width:20px;height:20px;border-radius:50%;background:var(--bg-card-hover);place-items:center;font-size:10px;font-weight:600;color:var(--text-muted)">{initial}</span>'
    return f'''<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ARKAINBRAIN</title><link rel="icon" href="{FAVICON_SVG}"><style>{BRAND_CSS}</style></head><body>
<div class="topbar"><a href="/" class="logo"><div class="logo-mark">A</div>ARKAINBRAIN <span class="version-tag">v8</span></a><a href="/logout" class="user-pill">{pic_tag}{name} · Sign Out</a></div>
<div class="shell"><nav class="sidebar">{nav}<div class="section-label" style="margin-top:auto;padding-top:40px"><span style="color:var(--text-dim);font-size:10px;letter-spacing:0.5px">ArkainGames.com</span></div></nav><main class="main">{content}</main></div></body></html>'''

# ─── AUTH ───
@app.route("/login")
def login_page():
    return f'''<!DOCTYPE html><html><head><title>ARKAINBRAIN</title><link rel="icon" href="{FAVICON_SVG}"><style>{BRAND_CSS}</style></head><body>
<div class="login-wrap"><div class="login-box"><div class="logo-mark" style="width:44px;height:44px;font-size:20px;margin:0 auto;border-radius:12px">A</div><h1>ARKAINBRAIN</h1><p>AI-powered gaming intelligence & arcade platform.</p><a href="/auth/google" class="google-btn">{GOOGLE_SVG} Continue with Google</a><div style="margin-top:32px;font-size:11px;color:var(--text-dim)">ArkainGames.com · v8</div></div></div></body></html>'''

@app.route("/auth/google")
def google_login():
    return google.authorize_redirect(url_for("google_callback", _external=True))

@app.route("/auth/callback")
def google_callback():
    try:
        token = google.authorize_access_token()
        info = token.get("userinfo") or google.userinfo()
        db = get_db()
        db.execute("INSERT INTO users (id,email,name,picture) VALUES (?,?,?,?) ON CONFLICT(email) DO UPDATE SET name=excluded.name,picture=excluded.picture",
            (str(uuid.uuid4()), info["email"], info.get("name",""), info.get("picture","")))
        db.commit()
        row = db.execute("SELECT * FROM users WHERE email=?", (info["email"],)).fetchone()
        session.permanent = True  # 30-day session — survives browser close
        session["user"] = {"id":row["id"],"email":row["email"],"name":row["name"],"picture":row["picture"]}
        logger.info(f"Login: {info['email']} → user_id={row['id']}")
        return redirect("/")
    except Exception as e:
        logger.error(f"Auth error: {e}")
        return f"Auth error: {e}", 500

@app.route("/logout")
def logout():
    session.clear(); return redirect("/login")

# ─── DASHBOARD ───
@app.route("/")
@login_required
def dashboard():
    user = current_user()
    db = get_db()
    recent = db.execute("SELECT * FROM jobs WHERE user_id=? ORDER BY created_at DESC LIMIT 8", (user["id"],)).fetchall()
    rows = ""
    running_ids = []
    for job in recent:
        jid = job["id"]
        status = job["status"]
        stage = job["current_stage"] or ""
        bc = {"running":"badge-running","complete":"badge-complete","failed":"badge-failed","partial":"badge-partial"}.get(status,"badge-queued")
        tl = "Slot Pipeline" if job["job_type"]=="slot_pipeline" else ("Mini-Game" if job["job_type"]=="mini_rmg" else "State Recon")
        dt = job["created_at"][:16].replace("T"," ") if job["created_at"] else ""
        stage_html = f'<span class="stage-shimmer" style="font-size:11px;margin-left:4px">{stage}</span>' if status == "running" and stage else ""
        if status == "complete" and job["output_dir"]:
            act = f'<a href="/job/{jid}/files" class="btn btn-ghost btn-sm">Files</a>'
        elif status == "running":
            act = f'<a href="/job/{jid}/logs" class="btn btn-ghost btn-sm" style="border-color:var(--border-hover);color:var(--text-bright)">Watch Live</a>'
        elif status == "partial" and job["output_dir"]:
            act = (f'<button onclick="resumeJob(\'{jid}\')" class="btn btn-ghost btn-sm" '
                   f'style="border-color:#f59e0b;color:#f59e0b" id="resume-{jid}">▶ Resume</button>'
                   f'<a href="/job/{jid}/files" class="btn btn-ghost btn-sm" style="margin-left:4px">Files</a>')
        else:
            act = ""
        rows += f'<div class="history-item" id="job-{jid}"><div><div class="history-title">{_esc(job["title"])}</div><div class="history-type">{tl}</div></div><div><span class="badge {bc}" id="badge-{jid}">{status}</span>{stage_html}</div><div class="history-date">{dt}</div><div class="history-actions" id="act-{jid}">{act}</div></div>'
        if status in ("running", "queued"):
            running_ids.append(jid)
    if not rows:
        rows = '<div class="empty-state"><h3>No pipelines yet</h3><p>Launch a Slot Pipeline or State Recon to get started.</p></div>'
    fname = user.get("name","").split()[0] if user.get("name") else "Operator"
    # Check for pending reviews
    review_banner = ""
    try:
        from tools.web_hitl import get_pending_reviews
        pending = get_pending_reviews()
        if pending:
            review_banner = f'<a href="/reviews" class="card" style="border-color:var(--border-hover);display:flex;align-items:center;gap:14px;text-decoration:none"><span class="badge badge-running" style="font-size:13px;padding:6px 14px">{len(pending)}</span><div><div style="font-weight:500;color:var(--text-bright);font-size:13px">Pipeline waiting for your review</div><div style="font-size:12px;color:var(--text-muted)">Click to approve, reject, or give feedback</div></div></a>'
    except Exception as e:
        logger.debug(f"Review banner: {e}")

    # API status checks
    has_openai = bool(os.getenv("OPENAI_API_KEY"))
    has_serper = bool(os.getenv("SERPER_API_KEY"))
    has_elevenlabs = bool(os.getenv("ELEVENLABS_API_KEY"))
    has_qdrant = bool(os.getenv("QDRANT_URL"))
    has_resend = bool(os.getenv("RESEND_API_KEY"))

    api_cards = f'''<div class="stat-grid">
        <div class="stat-card {'online' if has_openai else 'offline'}"><div class="stat-icon">🧠</div><div class="stat-val">{'●' if has_openai else '○'}</div><div class="stat-label">OpenAI GPT-5</div></div>
        <div class="stat-card {'online' if has_serper else 'offline'}"><div class="stat-icon">🔍</div><div class="stat-val">{'●' if has_serper else '○'}</div><div class="stat-label">Serper Search</div></div>
        <div class="stat-card {'online' if has_elevenlabs else 'offline'}"><div class="stat-icon">🔊</div><div class="stat-val">{'●' if has_elevenlabs else '○'}</div><div class="stat-label">ElevenLabs</div></div>
        <div class="stat-card {'online' if has_qdrant else 'offline'}"><div class="stat-icon">🗃️</div><div class="stat-val">{'●' if has_qdrant else '○'}</div><div class="stat-label">Qdrant DB</div></div>
        <div class="stat-card {'online' if has_resend else 'offline'}"><div class="stat-icon">📧</div><div class="stat-val">{'●' if has_resend else '○'}</div><div class="stat-label">Email Alerts</div></div>
    </div>'''

    # Count totals
    db2 = get_db()
    total_jobs = db2.execute("SELECT COUNT(*) FROM jobs WHERE user_id=?", (user["id"],)).fetchone()[0]
    completed_jobs = db2.execute("SELECT COUNT(*) FROM jobs WHERE user_id=? AND status='complete'", (user["id"],)).fetchone()[0]

    return layout(f'''
    <div class="greeting">
        <h2>Welcome back, {fname}</h2>
        <p>What would you like to build today?</p>
        <div class="engine-tag">GPT-5 · 6 Agents · OODA Convergence · ARCADE Engine</div>
    </div>
    {review_banner}
    {api_cards}
    <div class="action-grid">
        <a href="/new" class="action-card"><div class="action-icon">🎰</div><div><div class="action-text">New Slot Pipeline</div><div class="action-desc">Concept → certified game package</div></div></a>
        <a href="/new-minigame" class="action-card"><div class="action-icon">🎮</div><div><div class="action-text">New Mini-Game</div><div class="action-desc">Pick a game type → themed HTML5 build</div></div></a>
        <a href="/novel" class="action-card"><div class="action-icon">🧪</div><div><div class="action-text">Novel Game</div><div class="action-desc">Describe a game → AI invents it</div></div></a>
        <a href="/arcade" class="action-card"><div class="action-icon">🕹️</div><div><div class="action-text">Arcade Suite</div><div class="action-desc">8 playable HTML5 mini-games</div></div></a>
        <a href="/recon" class="action-card"><div class="action-icon">🌐</div><div><div class="action-text">State Recon</div><div class="action-desc">AI legal research for any jurisdiction</div></div></a>
        <a href="/trends" class="action-card"><div class="action-icon">📡</div><div><div class="action-text">Trend Radar</div><div class="action-desc">Market scan, blue ocean finder, competitor intel</div></div></a>
    </div>
    <div class="card"><h2>Capabilities</h2>
        <div class="capability-grid">
            <div class="cap-item">🛰️ <b>Pre-Flight Intel</b> <span class="cap-tag">trend · jurisdiction</span></div>
            <div class="cap-item">🔬 <b>Vision QA</b> <span class="cap-tag">every image</span></div>
            <div class="cap-item">📐 <b>Math Optimizer</b> <span class="cap-tag">RTP ±0.1%</span></div>
            <div class="cap-item">🎭 <b>Agent Debate</b> <span class="cap-tag">OODA loop</span></div>
            <div class="cap-item">👤 <b>Player Behavior</b> <span class="cap-tag">5K sessions</span></div>
            <div class="cap-item">🔒 <b>Patent Scanner</b> <span class="cap-tag">IP check</span></div>
            <div class="cap-item">🎮 <b>HTML5 Prototype</b> <span class="cap-tag">playable</span></div>
            <div class="cap-item" style="{'opacity:0.35' if not has_elevenlabs else ''}">{'🔊' if has_elevenlabs else '🔇'} <b>Sound Design</b> <span class="cap-tag">{'on' if has_elevenlabs else '<a href=/settings style=color:var(--danger)>setup</a>'}</span></div>
            <div class="cap-item">📋 <b>Cert Planner</b> <span class="cap-tag">lab · cost</span></div>
            <div class="cap-item">⚔️ <b>Adversarial QA</b> <span class="cap-tag">devil's advocate</span></div>
            <div class="cap-item">📍 <b>Geo Research</b> <span class="cap-tag">region scoring</span></div>
            <div class="cap-item" style="{'opacity:0.35' if not has_resend else ''}">{'📧' if has_resend else '📭'} <b>Email Alerts</b> <span class="cap-tag">{'on' if has_resend else '<a href=/settings style=color:var(--danger)>setup</a>'}</span></div>
        </div>
    </div>
    <div class="card" style="padding:0;overflow:hidden"><div style="padding:16px 20px 8px"><h2 style="margin-bottom:0">Recent Activity</h2></div>{rows}</div>
    <script>
    // Auto-refresh running jobs every 4 seconds
    const runningIds = {json.dumps(running_ids)};
    if (runningIds.length > 0) {{
        const poll = setInterval(() => {{
            let remaining = 0;
            runningIds.forEach(jid => {{
                fetch('/api/status/' + jid).then(r => r.json()).then(d => {{
                    const badge = document.getElementById('badge-' + jid);
                    if (!badge) return;
                    if (d.status !== badge.textContent) {{
                        badge.textContent = d.status;
                        badge.className = 'badge badge-' + (d.status === 'complete' ? 'complete' : d.status === 'failed' ? 'failed' : d.status === 'running' ? 'running' : d.status === 'partial' ? 'partial' : 'queued');
                        if (d.status === 'complete' || d.status === 'failed' || d.status === 'partial') {{
                            setTimeout(() => location.reload(), 1000);
                        }}
                    }}
                    if (d.status === 'running' || d.status === 'queued') remaining++;
                }}).catch(() => {{}});
            }});
            if (remaining === 0) clearInterval(poll);
        }}, 4000);
    }}

    function resumeJob(jobId) {{
        const btn = document.getElementById('resume-' + jobId);
        if (btn) {{ btn.disabled = true; btn.textContent = 'Resuming...'; }}
        fetch('/api/resume/' + jobId, {{method: 'POST'}})
            .then(r => r.json())
            .then(d => {{
                if (d.error) {{ alert('Resume failed: ' + d.error); if (btn) {{ btn.disabled = false; btn.textContent = '▶ Resume'; }} }}
                else {{ window.location.href = '/job/' + jobId + '/logs'; }}
            }})
            .catch(e => {{ alert('Resume request failed'); if (btn) {{ btn.disabled = false; btn.textContent = '▶ Resume'; }} }});
    }}
    </script>''', "dashboard")

# ─── NEW PIPELINE ───
@app.route("/new")
@login_required
def new_pipeline():
    has_elevenlabs = bool(os.getenv("ELEVENLABS_API_KEY"))
    el_note = "" if has_elevenlabs else ' <span class="feat-tag ip-risk">No API key</span>'
    return layout(f'''
    <div class="greeting" style="margin-bottom:20px">
        <h2 style="font-size:20px">New Slot Pipeline</h2>
        <p>Describe your concept. Six agents research, design, model, illustrate, and certify it.</p>
    </div>
    <form action="/api/pipeline" method="POST">
    <div class="card"><h2 style="display:flex;align-items:center;justify-content:space-between">🎰 Game Concept
    <button type="button" id="surpriseBtn" onclick="surpriseMe()" style="background:linear-gradient(135deg,#f59e0b,#ef4444,#ec4899);color:#fff;border:none;padding:8px 18px;border-radius:20px;font-size:12px;font-weight:700;cursor:pointer;transition:all .2s;box-shadow:0 2px 12px rgba(239,68,68,.3)">🎲 Surprise Me</button></h2>
    <label>Theme / Concept</label><input name="theme" id="inp_theme" placeholder="e.g. Ancient Egyptian curse with escalating darkness" required style="font-size:15px;padding:14px 16px">
    <div class="row2"><div><label>Target Jurisdictions</label><input name="target_markets" id="inp_markets" placeholder="e.g. Georgia, Texas, UK, Malta" value="Georgia, Texas">
    <p style="font-size:10px;color:var(--text-muted);margin-top:-12px;margin-bottom:12px">US states, countries, or regulated markets. Auto-recon for unknown states.</p>
    </div>
    <div><label>Volatility</label><select name="volatility" id="inp_vol"><option value="low">Low</option><option value="medium" selected>Medium</option><option value="high">High</option><option value="very_high">Very High</option></select></div></div></div>

    <div class="card"><h2>📐 Math & Grid</h2>
    <div class="row3"><div><label>Target RTP %</label><input type="number" name="target_rtp" id="inp_rtp" value="96.0" step="0.1" min="85" max="99"></div><div><label>Grid Cols</label><input type="number" name="grid_cols" id="inp_cols" value="5"></div><div><label>Grid Rows</label><input type="number" name="grid_rows" id="inp_rows" value="3"></div></div>
    <div class="row3"><div><label>Ways / Lines</label><input type="number" name="ways_or_lines" id="inp_ways" value="243"></div><div><label>Max Win Multiplier</label><input type="number" name="max_win_multiplier" id="inp_maxwin" value="5000"></div><div><label>Art Style</label><input name="art_style" id="inp_artstyle" value="Cinematic realism"></div></div></div>

    <div class="card"><h2>⚡ Features & Mechanics</h2>
    <div class="feature-grid">
        <label><input type="checkbox" name="features" value="free_spins" checked> Free Spins <span class="feat-tag safe">✓ Safe</span></label>
        <label><input type="checkbox" name="features" value="multipliers" checked> Multipliers <span class="feat-tag safe">✓ Safe</span></label>
        <label><input type="checkbox" name="features" value="expanding_wilds"> Expanding Wilds <span class="feat-tag safe">✓ Safe</span></label>
        <label><input type="checkbox" name="features" value="cascading_reels"> Cascading Reels <span class="feat-tag safe">Low IP</span></label>
        <label><input type="checkbox" name="features" value="mystery_symbols"> Mystery Symbols <span class="feat-tag safe">✓ Safe</span></label>
        <label><input type="checkbox" name="features" value="walking_wilds"> Walking Wilds <span class="feat-tag safe">Low IP</span></label>
        <label><input type="checkbox" name="features" value="cluster_pays"> Cluster Pays <span class="feat-tag safe">Low IP</span></label>
        <label><input type="checkbox" name="features" value="hold_and_spin"> Hold & Spin <span class="feat-tag ip-risk">Med IP</span></label>
        <label><input type="checkbox" name="features" value="bonus_buy"> Bonus Buy <span class="feat-tag banned">UK/SE ban</span></label>
        <label><input type="checkbox" name="features" value="progressive_jackpot"> Progressive Jackpot <span class="feat-tag ip-risk">+cost</span></label>
        <label><input type="checkbox" name="features" value="megaways"> Megaways™ <span class="feat-tag ip-risk">License req</span></label>
        <label><input type="checkbox" name="features" value="split_symbols"> Split Symbols <span class="feat-tag safe">Low IP</span></label>
    </div>
    <p style="font-size:10px;color:var(--text-muted);margin-top:12px">IP risk tags are pre-flight estimates. Patent Scanner verifies during execution.</p>
    <div style="margin-top:16px"><label>Competitor References</label><input name="competitor_references" id="inp_comps" placeholder="e.g. Book of Dead, Legacy of Dead, Sweet Bonanza">
    <label>Special Requirements</label><textarea name="special_requirements" id="inp_special" placeholder="e.g. Must support mobile portrait mode, needs 5+ free spin retriggers, dark moody atmosphere..."></textarea></div></div>

    <div class="card"><h2>🤖 Pipeline Intelligence</h2>
    <div class="toggle-section">
        <div class="toggle-item"><input type="checkbox" name="enable_recon" value="on" checked id="recon"><label for="recon">🌐 Auto State Recon</label><span class="toggle-desc">Research unknown state laws</span></div>
        <div class="toggle-item"><input type="checkbox" name="enable_prototype" value="on" checked id="proto"><label for="proto">🎮 HTML5 Prototype</label><span class="toggle-desc">Playable demo</span></div>
        <div class="toggle-item"><input type="checkbox" name="enable_sound" value="on" {'checked' if has_elevenlabs else ''} id="snd"><label for="snd">🔊 Sound Design{el_note}</label><span class="toggle-desc">ElevenLabs SFX</span></div>
        <div class="toggle-item"><input type="checkbox" name="enable_cert_plan" value="on" checked id="cert"><label for="cert">📋 Cert Planning</label><span class="toggle-desc">Lab + timeline + cost</span></div>
        <div class="toggle-item"><input type="checkbox" name="enable_patent_scan" value="on" checked id="pat"><label for="pat">🔒 Patent/IP Scan</label><span class="toggle-desc">Mechanic conflicts</span></div>
    </div></div>

    <div class="card"><h2>⚙️ Execution Mode</h2>
    <div style="display:flex;gap:24px;align-items:center;margin-bottom:16px">
        <label style="display:flex;align-items:center;gap:8px;cursor:pointer;margin:0"><input type="radio" name="exec_mode" value="auto" checked style="width:auto;margin:0;accent-color:#fff" onchange="document.getElementById('variantOpts').style.display='none'"> <span style="text-transform:none;font-size:13px;color:var(--text-bright);font-weight:500">Auto</span><span style="font-size:11px;color:var(--text-dim);margin-left:4px">fully autonomous</span></label>
        <label style="display:flex;align-items:center;gap:8px;cursor:pointer;margin:0"><input type="radio" name="exec_mode" value="interactive" style="width:auto;margin:0;accent-color:#fff" onchange="document.getElementById('variantOpts').style.display='none'"> <span style="text-transform:none;font-size:13px;color:var(--text-bright);font-weight:500">Interactive</span><span style="font-size:11px;color:var(--text-dim);margin-left:4px">review at each stage</span></label>
        <label style="display:flex;align-items:center;gap:8px;cursor:pointer;margin:0"><input type="radio" name="exec_mode" value="variants" style="width:auto;margin:0;accent-color:#fff" onchange="document.getElementById('variantOpts').style.display='flex'"> <span style="text-transform:none;font-size:13px;color:var(--text-bright);font-weight:500">A/B Variants</span><span style="font-size:11px;color:var(--text-dim);margin-left:4px">2-5 parallel versions</span></label>
    </div>
    <div id="variantOpts" style="display:none;align-items:center;gap:12px;padding:12px;background:rgba(255,255,255,0.02);border:1px solid var(--border);border-radius:8px">
        <label style="font-size:12px;color:var(--text-muted);margin:0">Variants:</label>
        <select name="variant_count" style="width:60px;font-size:13px;padding:4px 8px;background:var(--bg-card);color:var(--text);border:1px solid var(--border);border-radius:6px">
            <option value="2">2</option><option value="3" selected>3</option><option value="4">4</option><option value="5">5</option></select>
        <span style="font-size:11px;color:var(--text-dim)">Conservative / Aggressive / Hybrid / Premium / Jackpot</span>
    </div></div>
    <button type="submit" id="launchBtn" class="btn btn-primary btn-full" style="padding:14px;font-size:14px;border-radius:var(--radius-lg)">Launch Pipeline &rarr;</button>
    <script>document.getElementById('launchBtn').addEventListener('click',function(e){{var m=document.querySelector('input[name=exec_mode]:checked').value;if(m==='variants'){{this.form.action='/api/variants'}}else{{if(m==='interactive'){{var h=document.createElement('input');h.type='hidden';h.name='interactive';h.value='on';this.form.appendChild(h)}}this.form.action='/api/pipeline'}}}});
    async function surpriseMe(){{
        var btn=document.getElementById('surpriseBtn');
        btn.disabled=true;btn.innerHTML='🎲 Generating...';btn.style.opacity='0.6';
        try{{
            var r=await fetch('/api/surprise');var d=await r.json();
            if(d.theme)document.getElementById('inp_theme').value=d.theme;
            if(d.volatility){{var s=document.getElementById('inp_vol');for(var i=0;i<s.options.length;i++)if(s.options[i].value===d.volatility)s.selectedIndex=i;}}
            if(d.target_rtp)document.getElementById('inp_rtp').value=d.target_rtp;
            if(d.grid_cols)document.getElementById('inp_cols').value=d.grid_cols;
            if(d.grid_rows)document.getElementById('inp_rows').value=d.grid_rows;
            if(d.ways_or_lines)document.getElementById('inp_ways').value=d.ways_or_lines;
            if(d.max_win_multiplier)document.getElementById('inp_maxwin').value=d.max_win_multiplier;
            if(d.art_style)document.getElementById('inp_artstyle').value=d.art_style;
            if(d.target_markets)document.getElementById('inp_markets').value=d.target_markets;
            if(d.competitor_references)document.getElementById('inp_comps').value=d.competitor_references;
            if(d.special_requirements)document.getElementById('inp_special').value=d.special_requirements;
            if(d.features){{document.querySelectorAll('input[name=features]').forEach(function(cb){{cb.checked=d.features.includes(cb.value);}});}}
            document.getElementById('inp_theme').style.background='rgba(245,158,11,0.08)';
            setTimeout(function(){{document.getElementById('inp_theme').style.background='';}},2000);
        }}catch(e){{console.error(e);}}
        btn.disabled=false;btn.innerHTML='🎲 Surprise Me';btn.style.opacity='1';
    }}</script>
    </form>''', "new")

# ─── ARCADE HUB ───

ARCADE_GAMES = [
    {"id": "crash",   "file": "crash_cosmic-crash.html",     "title": "Cosmic Crash",       "icon": "🚀", "theme": "Space",     "desc": "Watch the multiplier climb — cash out before it crashes!", "color": "#6366f1", "color2": "#06b6d4"},
    {"id": "plinko",  "file": "plinko_glacier-drop.html",    "title": "Glacier Drop",       "icon": "🧊", "theme": "Arctic",    "desc": "Drop the puck through icy pegs into prize buckets.", "color": "#0ea5e9", "color2": "#06b6d4"},
    {"id": "mines",   "file": "mines_neon-grid.html",        "title": "Neon Grid",          "icon": "💎", "theme": "Cyberpunk", "desc": "Uncover gems on the grid — avoid the hidden mines.", "color": "#a855f7", "color2": "#ec4899"},
    {"id": "dice",    "file": "dice_dragon-dice.html",       "title": "Dragon Dice",        "icon": "🐉", "theme": "Dragon",    "desc": "Roll the dragon dice and bet on the outcome.", "color": "#ef4444", "color2": "#f97316"},
    {"id": "wheel",   "file": "wheel_trident-spin.html",     "title": "Trident Spin",       "icon": "🔱", "theme": "Ocean",     "desc": "Spin the trident wheel for oceanic multipliers.", "color": "#0891b2", "color2": "#2dd4bf"},
    {"id": "hilo",    "file": "hilo_pharaohs-fortune.html",  "title": "Pharaoh's Fortune",  "icon": "🏛️", "theme": "Egyptian",  "desc": "Predict higher or lower in this Egyptian card game.", "color": "#d97706", "color2": "#fbbf24"},
    {"id": "chicken", "file": "chicken_jungle-runner.html",   "title": "Jungle Runner",      "icon": "🐔", "theme": "Jungle",    "desc": "How far will you run? Cash out before you're caught!", "color": "#16a34a", "color2": "#4ade80"},
    {"id": "scratch", "file": "scratch_golden-vault.html",    "title": "Golden Vault",       "icon": "🏆", "theme": "Gold Vault","desc": "Scratch to reveal symbols and unlock vault prizes.", "color": "#ca8a04", "color2": "#fde047"},
]

@app.route("/arcade")
@login_required
def arcade_hub():
    cards = ""
    for g in ARCADE_GAMES:
        cards += f'''
        <a href="/arcade/play/{g['id']}" class="arcade-card" style="--gc1:{g['color']};--gc2:{g['color2']}">
            <div class="arcade-card-icon">{g['icon']}</div>
            <div class="arcade-card-body">
                <div class="arcade-card-title">{_esc(g['title'])}</div>
                <div class="arcade-card-theme">{_esc(g['theme'])}</div>
                <div class="arcade-card-desc">{_esc(g['desc'])}</div>
            </div>
            <div style="display:flex;flex-direction:column;gap:6px;flex-shrink:0;position:relative;z-index:1">
                <div class="arcade-card-play">PLAY</div>
            </div>
        </a>'''

    # Load generated games from registry
    gen_cards = ""
    gen_count = 0
    try:
        gen_reg = Path(__file__).parent / "static" / "arcade" / "games" / "generated" / "_registry.json"
        if gen_reg.exists():
            gen_games = json.loads(gen_reg.read_text())
            gen_count = len(gen_games)
            gt_icons = {"crash": "🚀", "plinko": "⚡", "mines": "💣", "dice": "🎲",
                        "wheel": "🎡", "hilo": "🃏", "chicken": "🐔", "scratch": "🎟️"}
            gt_colors = {"crash": ("#6366f1","#06b6d4"), "plinko": ("#0ea5e9","#06b6d4"),
                         "mines": ("#a855f7","#ec4899"), "dice": ("#ef4444","#f97316"),
                         "wheel": ("#0891b2","#2dd4bf"), "hilo": ("#d97706","#fbbf24"),
                         "chicken": ("#16a34a","#4ade80"), "scratch": ("#ca8a04","#fde047")}
            for gg in reversed(gen_games[-20:]):  # Show newest first, cap at 20
                gt = gg.get("game_type", "crash")
                c1, c2 = gt_colors.get(gt, ("#6366f1","#06b6d4"))
                icon = gt_icons.get(gt, "🎮")
                rtp_str = f'{gg.get("rtp", 96):.1f}%'
                gen_cards += f'''
                <a href="/arcade/play/gen/{_esc(gg['filename'])}" class="arcade-card" style="--gc1:{c1};--gc2:{c2}">
                    <div class="arcade-card-icon">{icon}</div>
                    <div class="arcade-card-body">
                        <div class="arcade-card-title">{_esc(gg.get('title', gg.get('theme','Game')))}</div>
                        <div class="arcade-card-theme">Generated • {gt.title()} • RTP {rtp_str}</div>
                        <div class="arcade-card-desc">{_esc(gg.get('theme',''))}</div>
                    </div>
                    <div style="display:flex;flex-direction:column;gap:6px;flex-shrink:0;position:relative;z-index:1">
                        <div class="arcade-card-play">PLAY</div>
                    </div>
                </a>'''
    except Exception:
        pass  # Registry missing or corrupt — skip gracefully

    gen_section = ""
    if gen_cards:
        gen_section = f'''
        <div style="margin-top:40px">
            <h3 style="font-size:16px;font-weight:700;color:var(--text-bright);margin-bottom:16px">🎮 Generated Games ({gen_count})</h3>
            <div class="arcade-grid">{gen_cards}</div>
        </div>'''

    content = f'''
    <style>
    .arcade-hero{{text-align:center;margin-bottom:32px}}
    .arcade-hero h2{{font-size:24px;font-weight:800;background:linear-gradient(135deg,#6366f1,#06b6d4);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:4px}}
    .arcade-hero p{{color:var(--text-muted);font-size:13px}}
    .arcade-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px}}
    .arcade-card{{display:flex;align-items:center;gap:16px;padding:20px;background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius-lg);text-decoration:none;color:var(--text);transition:all .2s;position:relative;overflow:hidden}}
    .arcade-card::before{{content:'';position:absolute;inset:0;background:linear-gradient(135deg,var(--gc1),var(--gc2));opacity:0;transition:opacity .2s}}
    .arcade-card:hover{{border-color:var(--gc1);transform:translateY(-2px);box-shadow:0 8px 24px rgba(0,0,0,0.3)}}
    .arcade-card:hover::before{{opacity:0.06}}
    .arcade-card-icon{{font-size:36px;width:56px;height:56px;display:grid;place-items:center;background:rgba(255,255,255,0.03);border-radius:14px;flex-shrink:0;position:relative;z-index:1}}
    .arcade-card-body{{flex:1;min-width:0;position:relative;z-index:1}}
    .arcade-card-title{{font-size:15px;font-weight:700;color:var(--text-bright)}}
    .arcade-card-theme{{font-size:10px;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:var(--gc1);margin:2px 0 6px}}
    .arcade-card-desc{{font-size:12px;color:var(--text-muted);line-height:1.4}}
    .arcade-card-play{{position:relative;z-index:1;padding:6px 14px;border-radius:8px;border:1px solid var(--gc1);font-size:11px;font-weight:700;letter-spacing:1px;color:var(--gc1);flex-shrink:0;transition:all .2s}}
    .arcade-card:hover .arcade-card-play{{background:var(--gc1);color:#fff}}
    .arcade-stats{{display:flex;gap:24px;justify-content:center;margin:20px 0 28px}}
    .arcade-stat{{text-align:center}}
    .arcade-stat .num{{font-size:28px;font-weight:800;color:var(--text-bright)}}
    .arcade-stat .lbl{{font-size:11px;color:var(--text-dim);margin-top:2px}}
    .arcade-links{{display:flex;gap:8px;justify-content:center;margin-top:24px;flex-wrap:wrap}}
    .arcade-links a{{font-size:12px;padding:6px 14px;border-radius:8px;border:1px solid var(--border);color:var(--text-muted);text-decoration:none;transition:all .15s}}
    .arcade-links a:hover{{color:var(--text-bright);border-color:var(--text-dim)}}
    </style>
    <div class="arcade-hero">
        <h2>{ICON_ARCADE} ARCADE Game Suite</h2>
        <p>Production-ready HTML5 mini-games. Create unlimited themed variants with one click.</p>
    </div>
    <div class="arcade-stats">
        <div class="arcade-stat"><div class="num">8</div><div class="lbl">Base Games</div></div>
        <div class="arcade-stat"><div class="num">{gen_count}</div><div class="lbl">Generated</div></div>
        <div class="arcade-stat"><div class="num">∞</div><div class="lbl">Themes</div></div>
    </div>
    <div class="arcade-grid">{cards}</div>
    {gen_section}
    <div class="arcade-links">
        <a href="/arcade/create" style="background:linear-gradient(135deg,#6366f1,#06b6d4);color:#fff;font-weight:700;border-color:transparent;padding:8px 18px">🎨 Create Themed Game</a>
        <a href="/new-minigame" style="background:linear-gradient(135deg,#ec4899,#f97316);color:#fff;font-weight:700;border-color:transparent;padding:8px 18px">🎮 New Pipeline</a>
        <a href="/novel" style="background:linear-gradient(135deg,#f97316,#ef4444);color:#fff;font-weight:700;border-color:transparent;padding:8px 18px">🧪 Novel Game</a>
        <a href="/game-library" style="background:linear-gradient(135deg,#06b6d4,#3b82f6);color:#fff;font-weight:700;border-color:transparent;padding:8px 18px">📚 Library</a>
        <a href="/arcade/lab" style="background:linear-gradient(135deg,#7c3aed,#ec4899);color:#fff;font-weight:700;border-color:transparent;padding:8px 18px">🔬 Math Lab</a>
        <a href="/arcade/showcase">Engine Showcase</a>
        <a href="/arcade/engine-test">Engine Test</a>
        <a href="/arcade/plinko-test">Plinko Physics Test</a>
    </div>'''

    # Check for generated games and append them
    gen_dir = Path("static/arcade/games/generated")
    if gen_dir.exists():
        gen_files = sorted(gen_dir.glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True)
        if gen_files:
            gen_cards = ""
            for gf in gen_files[:12]:  # Show max 12
                name = gf.stem.replace("_", " ").replace("-", " ").title()
                gen_cards += f'''
                <a href="/arcade/play/gen/{_esc(gf.name)}" class="arcade-card" style="--gc1:#8b5cf6;--gc2:#ec4899">
                    <div class="arcade-card-icon">🎨</div>
                    <div class="arcade-card-body">
                        <div class="arcade-card-title">{_esc(name)}</div>
                        <div class="arcade-card-theme">CUSTOM</div>
                        <div class="arcade-card-desc">AI-themed variant</div>
                    </div>
                    <div class="arcade-card-play">PLAY</div>
                </a>'''
            content += f'''
    <div style="margin-top:32px">
        <h3 style="font-size:16px;font-weight:700;color:var(--text-bright);margin-bottom:16px">🎨 Your Themed Games</h3>
        <div class="arcade-grid">{gen_cards}</div>
    </div>'''

    return layout(content, "arcade")


@app.route("/arcade/play/<game_id>")
@login_required
def arcade_play(game_id):
    game = next((g for g in ARCADE_GAMES if g["id"] == game_id), None)
    if not game:
        return redirect("/arcade")
    return f'''<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no"><title>ARCADE — {_esc(game["title"])}</title>
<style>*{{margin:0;padding:0;box-sizing:border-box}}body{{background:#030014;overflow:hidden}}
.back-btn{{position:fixed;top:12px;left:12px;z-index:9999;padding:8px 14px;background:rgba(0,0,0,0.7);backdrop-filter:blur(8px);border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:#94a3b8;font-size:12px;font-weight:600;text-decoration:none;transition:all .15s;display:flex;align-items:center;gap:6px}}
.back-btn:hover{{background:rgba(255,255,255,0.1);color:#e2e8f0}}
.back-btn svg{{width:14px;height:14px}}
iframe{{width:100vw;height:100vh;border:none}}</style></head><body>
<a href="/arcade" class="back-btn"><svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M19 12H5m7-7l-7 7 7 7"/></svg>Back to Arcade</a>
<iframe src="/static/arcade/games/phase3/{game['file']}" allowfullscreen></iframe>
</body></html>'''


@app.route("/arcade/showcase")
@login_required
def arcade_showcase():
    return f'''<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ARCADE — Engine Showcase</title>
<style>*{{margin:0;padding:0;box-sizing:border-box}}body{{background:#030014;overflow:hidden}}
.back-btn{{position:fixed;top:12px;left:12px;z-index:9999;padding:8px 14px;background:rgba(0,0,0,0.7);backdrop-filter:blur(8px);border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:#94a3b8;font-size:12px;font-weight:600;text-decoration:none;transition:all .15s;display:flex;align-items:center;gap:6px}}
.back-btn:hover{{background:rgba(255,255,255,0.1);color:#e2e8f0}}
iframe{{width:100vw;height:100vh;border:none}}</style></head><body>
<a href="/arcade" class="back-btn"><svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M19 12H5m7-7l-7 7 7 7"/></svg>Back to Arcade</a>
<iframe src="/static/arcade/demos/showcase_v3.html" allowfullscreen></iframe>
</body></html>'''


@app.route("/arcade/engine-test")
@login_required
def arcade_engine_test():
    return f'''<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ARCADE — Engine Test</title>
<style>*{{margin:0;padding:0;box-sizing:border-box}}body{{background:#030014;overflow:hidden}}
.back-btn{{position:fixed;top:12px;left:12px;z-index:9999;padding:8px 14px;background:rgba(0,0,0,0.7);backdrop-filter:blur(8px);border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:#94a3b8;font-size:12px;font-weight:600;text-decoration:none;transition:all .15s;display:flex;align-items:center;gap:6px}}
.back-btn:hover{{background:rgba(255,255,255,0.1);color:#e2e8f0}}
iframe{{width:100vw;height:100vh;border:none}}</style></head><body>
<a href="/arcade" class="back-btn"><svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M19 12H5m7-7l-7 7 7 7"/></svg>Back to Arcade</a>
<iframe src="/static/arcade/demos/engine_test.html" allowfullscreen></iframe>
</body></html>'''


@app.route("/arcade/plinko-test")
@login_required
def arcade_plinko_test():
    return f'''<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ARCADE — Plinko Physics</title>
<style>*{{margin:0;padding:0;box-sizing:border-box}}body{{background:#030014;overflow:hidden}}
.back-btn{{position:fixed;top:12px;left:12px;z-index:9999;padding:8px 14px;background:rgba(0,0,0,0.7);backdrop-filter:blur(8px);border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:#94a3b8;font-size:12px;font-weight:600;text-decoration:none;transition:all .15s;display:flex;align-items:center;gap:6px}}
.back-btn:hover{{background:rgba(255,255,255,0.1);color:#e2e8f0}}
iframe{{width:100vw;height:100vh;border:none}}</style></head><body>
<a href="/arcade" class="back-btn"><svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M19 12H5m7-7l-7 7 7 7"/></svg>Back to Arcade</a>
<iframe src="/static/arcade/demos/plinko_test.html" allowfullscreen></iframe>
</body></html>'''


# ─── MINI-GAME GENERATOR (Phase 1) ───

@app.route("/arcade/create")
@login_required
def arcade_create():
    """Form to create a themed mini-game variant."""
    game_opts = "".join(f'<option value="{g["id"]}">{g["title"]} ({g["id"]})</option>' for g in ARCADE_GAMES)
    return layout(f'''
    <style>
    .gen-form{{display:flex;flex-direction:column;gap:16px}}
    .gen-form label{{font-size:12px;font-weight:600;color:var(--text-bright);margin-bottom:4px;display:block}}
    .gen-form input,.gen-form select{{width:100%;padding:10px 14px;border-radius:8px;border:1px solid var(--border);background:var(--bg-card);color:var(--text);font-size:13px}}
    .gen-form input:focus,.gen-form select:focus{{border-color:var(--text-dim);outline:none}}
    .color-row{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
    .color-input{{display:flex;align-items:center;gap:8px}}
    .color-input input[type=color]{{width:36px;height:36px;border:none;border-radius:6px;cursor:pointer;padding:0;background:none}}
    .color-input input[type=text]{{flex:1}}
    .preview-bar{{padding:16px;border-radius:10px;text-align:center;font-weight:700;font-size:18px;margin-top:8px;transition:all .3s}}
    </style>
    <h2 class="page-title">🎨 Create Themed Mini-Game</h2>
    <p class="page-subtitle">Choose a base game type, customize the theme, and generate a new variant.</p>
    <div class="card">
        <h2>Game Configuration</h2>
        <form class="gen-form" action="/api/arcade/generate" method="POST">
            <div>
                <label>Base Game Type</label>
                <select name="game_type" id="gen-type">{game_opts}</select>
            </div>
            <div>
                <label>Theme Name</label>
                <input name="theme_name" placeholder="e.g. Samurai Pachinko, Neon Nights, Ocean Treasures" required>
            </div>
            <div>
                <label>Game Title (shown in header)</label>
                <input name="title" placeholder="e.g. ⚔️ SAMURAI DROP">
            </div>
            <div>
                <label>Subtitle</label>
                <input name="subtitle" placeholder="e.g. Plinko · Samurai · Phase 1" value="">
            </div>
            <div class="color-row">
                <div>
                    <label>Primary Color</label>
                    <div class="color-input">
                        <input type="color" name="accent" value="#6366f1" id="gen-acc" oninput="document.getElementById('gen-acc-t').value=this.value;updatePreview()">
                        <input type="text" name="accent_text" id="gen-acc-t" value="#6366f1" oninput="document.getElementById('gen-acc').value=this.value;updatePreview()">
                    </div>
                </div>
                <div>
                    <label>Secondary Color</label>
                    <div class="color-input">
                        <input type="color" name="accent2" value="#06b6d4" id="gen-acc2" oninput="document.getElementById('gen-acc2-t').value=this.value;updatePreview()">
                        <input type="text" name="accent2_text" id="gen-acc2-t" value="#06b6d4" oninput="document.getElementById('gen-acc2').value=this.value;updatePreview()">
                    </div>
                </div>
            </div>
            <div class="color-row">
                <div>
                    <label>Dark Background</label>
                    <div class="color-input">
                        <input type="color" name="bg_dark" value="#030014" id="gen-bg0">
                        <input type="text" name="bg_dark_text" id="gen-bg0-t" value="#030014">
                    </div>
                </div>
                <div>
                    <label>Mid Background</label>
                    <div class="color-input">
                        <input type="color" name="bg_mid" value="#0a0028" id="gen-bg1">
                        <input type="text" name="bg_mid_text" id="gen-bg1-t" value="#0a0028">
                    </div>
                </div>
            </div>
            <div>
                <label>Display Font</label>
                <select name="font_display">
                    <option value="Inter">Inter (Clean)</option>
                    <option value="Orbitron">Orbitron (Sci-Fi)</option>
                    <option value="Quicksand">Quicksand (Playful)</option>
                    <option value="Playfair Display">Playfair Display (Elegant)</option>
                    <option value="Cinzel">Cinzel (Egyptian/Classic)</option>
                    <option value="Bebas Neue">Bebas Neue (Bold)</option>
                    <option value="Lora">Lora (Natural)</option>
                </select>
            </div>
            <div>
                <label>Starting Balance</label>
                <input name="balance" type="number" value="1000" step="100" min="100" max="100000">
            </div>
            <div class="color-row">
                <div>
                    <label>Target RTP (%)</label>
                    <input name="target_rtp" type="number" value="96.0" step="0.5" min="85" max="99">
                </div>
                <div>
                    <label>Volatility</label>
                    <select name="volatility">
                        <option value="low">Low — frequent small wins</option>
                        <option value="medium" selected>Medium — balanced</option>
                        <option value="high">High — rare big wins</option>
                        <option value="ultra">Ultra — jackpot-heavy</option>
                    </select>
                </div>
            </div>
            <div class="preview-bar" id="gen-preview" style="background:linear-gradient(135deg,#6366f1,#06b6d4);color:#fff">
                Theme Preview
            </div>
            <button type="submit" class="btn btn-primary" style="width:100%;padding:14px;font-size:14px;font-weight:700">🎮 Generate Game</button>
        </form>
    </div>
    <script>
    function updatePreview(){{
        const a=document.getElementById('gen-acc').value;
        const b=document.getElementById('gen-acc2').value;
        document.getElementById('gen-preview').style.background='linear-gradient(135deg,'+a+','+b+')';
    }}
    </script>''', "arcade")


@app.route("/api/arcade/generate", methods=["POST"])
@login_required
def arcade_generate():
    """Generate a themed mini-game variant from form data."""
    import json as _json
    from pathlib import Path as _Path

    game_type = request.form.get("game_type", "plinko")
    theme_name = request.form.get("theme_name", "Custom Theme")
    title = request.form.get("title", "")
    subtitle = request.form.get("subtitle", "")
    accent = request.form.get("accent", "#6366f1")
    accent2 = request.form.get("accent2", "#06b6d4")
    bg_dark = request.form.get("bg_dark", "#030014")
    bg_mid = request.form.get("bg_mid", "#0a0028")
    font_display = request.form.get("font_display", "Inter")
    balance = float(request.form.get("balance", 1000))
    target_rtp = float(request.form.get("target_rtp", 96.0))
    volatility = request.form.get("volatility", "medium")

    try:
        from tools.minigame_config import build_config
        from tools.minigame_injector import save_themed_game

        theme_overrides = {
            "name": theme_name,
            "title": title or theme_name,
            "subtitle": subtitle or f"{game_type.title()} · {theme_name} · Custom",
            "primary": accent,
            "secondary": accent2,
            "bg_start": bg_dark,
            "bg_end": bg_mid,
            "title_font": font_display,
        }
        config = build_config(
            game_type=game_type,
            theme_overrides=theme_overrides,
            target_rtp=target_rtp,
            volatility=volatility,
            starting_balance=balance,
        )

        # Generate the game file
        import re as _re
        safe_name = _re.sub(r'[^a-z0-9]+', '-', theme_name.lower()).strip('-')
        filename = f"{game_type}_{safe_name}.html"

        gen_dir = _Path("static/arcade/games/generated")
        path = save_themed_game(game_type, config, output_name=filename, output_dir=gen_dir)

        # Register in DB so it shows up in arcade
        user = current_user()
        db = get_db()
        game_id = f"gen_{safe_name}"
        try:
            db.execute(
                "INSERT OR REPLACE INTO generated_games "
                "(id, user_id, game_type, theme_name, filename, config_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
                (game_id, user["id"], game_type, theme_name, filename,
                 config.model_dump_json(exclude_none=True))
            )
            db.commit()
        except Exception:
            pass  # Table might not exist yet — game still works via file

        # Redirect to play the generated game
        return redirect(f"/arcade/play/gen/{filename}")

    except Exception as e:
        logger.error(f"Mini-game generation failed: {e}")
        import traceback
        traceback.print_exc()
        return f'''<html><body style="background:#030014;color:#ef4444;padding:40px;font-family:sans-serif">
        <h2>Generation Failed</h2><p>{_esc(str(e))}</p>
        <a href="/arcade/create" style="color:#6366f1">← Try Again</a></body></html>''', 500


@app.route("/arcade/play/gen/<filename>")
@login_required
def arcade_play_generated(filename):
    """Serve a generated mini-game variant."""
    import re as _re
    # Security: only allow safe filenames
    if not _re.match(r'^[a-z0-9_-]+\.html$', filename):
        return redirect("/arcade")
    gen_path = Path(f"static/arcade/games/generated/{filename}")
    if not gen_path.exists():
        return redirect("/arcade")
    theme_name = filename.replace(".html", "").replace("_", " ").replace("-", " ").title()
    return f'''<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no"><title>ARCADE — {_esc(theme_name)}</title>
<style>*{{margin:0;padding:0;box-sizing:border-box}}body{{background:#030014;overflow:hidden}}
.back-btn{{position:fixed;top:12px;left:12px;z-index:9999;padding:8px 14px;background:rgba(0,0,0,0.7);backdrop-filter:blur(8px);border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:#94a3b8;font-size:12px;font-weight:600;text-decoration:none;transition:all .15s;display:flex;align-items:center;gap:6px}}
.back-btn:hover{{background:rgba(255,255,255,0.1);color:#e2e8f0}}
iframe{{width:100vw;height:100vh;border:none}}</style></head><body>
<a href="/arcade" class="back-btn"><svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M19 12H5m7-7l-7 7 7 7"/></svg>Back to Arcade</a>
<iframe src="/static/arcade/games/generated/{_esc(filename)}" allowfullscreen></iframe>
</body></html>'''

# ─── CONFIG-DRIVEN MINI-GAMES (Phase 1) ───

@app.route("/arcade/configured/<game_id>")
@login_required
def arcade_configured(game_id):
    """Serve a game with dynamically injected GAME_CONFIG.

    Reads the original game HTML, injects config JSON + bridge script,
    serves the modified version. The game runs with config-driven parameters.

    Query params:
        rtp=<float>    — target RTP (default: game default)
        vol=<str>      — volatility: low|medium|high|ultra
        theme=<str>    — theme preset name (or custom JSON)
        balance=<float>— starting balance
    """
    game = next((g for g in ARCADE_GAMES if g["id"] == game_id), None)
    if not game:
        return redirect("/arcade")

    from tools.minigame_config import (
        build_config, default_config,
        MiniGameType, Volatility, THEME_PRESETS,
    )
    from tools.minigame_injector import inject_config
    from pathlib import Path

    # Parse query params
    target_rtp = float(request.args.get("rtp", 0)) or None
    volatility = request.args.get("vol", None)
    theme_preset = request.args.get("theme", None)
    balance = float(request.args.get("balance", 0)) or None

    # Build config
    try:
        if target_rtp or volatility or theme_preset or balance:
            # Custom config from params
            params = {"game_type": game_id}
            # Find matching theme preset
            preset_map = {
                "crash": "cosmic_crash", "plinko": "glacier_drop",
                "mines": "neon_grid", "dice": "dragon_dice",
                "wheel": "trident_spin", "hilo": "pharaohs_fortune",
                "chicken": "jungle_runner", "scratch": "golden_vault",
            }
            params["theme_preset"] = theme_preset or preset_map.get(game_id, "")
            if target_rtp:
                params["target_rtp"] = target_rtp
            if volatility:
                params["volatility"] = volatility
            if balance:
                params["starting_balance"] = balance
            config = build_config(**params)
        else:
            config = default_config(game_id)
    except Exception as e:
        logger.warning(f"Config build failed for {game_id}: {e}")
        config = default_config(game_id)

    # Read original game HTML — prefer phase4 (config-native) over phase3
    game_path = Path(app.static_folder) / "arcade" / "games" / "phase4" / game["file"]
    if not game_path.exists():
        game_path = Path(app.static_folder) / "arcade" / "games" / "phase3" / game["file"]
    if not game_path.exists():
        return f"Game file not found: {game['file']}", 404

    html = game_path.read_text(encoding="utf-8")

    # Inject config
    html = inject_config(html, config)

    # Inject config bridge script before </body> (only for phase3 games that need it)
    is_phase4 = "phase4" in str(game_path)
    if not is_phase4:
        bridge_tag = '<script src="/static/arcade/engine/config_bridge.js"></script>'
        body_close = html.rfind("</body>")
        if body_close != -1:
            html = html[:body_close] + bridge_tag + "\n" + html[body_close:]

    return html


@app.route("/api/minigame/config/<game_id>")
@login_required
def api_minigame_config(game_id):
    """API: Generate a MiniGameConfig JSON.

    Query params same as /arcade/configured/<game_id>.
    Returns raw JSON config for inspection/debugging.
    """
    from tools.minigame_config import build_config, default_config, validate_config

    target_rtp = float(request.args.get("rtp", 0)) or None
    volatility = request.args.get("vol", None)
    theme_preset = request.args.get("theme", None)

    try:
        if target_rtp or volatility or theme_preset:
            preset_map = {
                "crash": "cosmic_crash", "plinko": "glacier_drop",
                "mines": "neon_grid", "dice": "dragon_dice",
                "wheel": "trident_spin", "hilo": "pharaohs_fortune",
                "chicken": "jungle_runner", "scratch": "golden_vault",
            }
            config = build_config(
                game_type=game_id,
                theme_preset=theme_preset or preset_map.get(game_id, ""),
                target_rtp=target_rtp or 96.0,
                volatility=volatility or "medium",
            )
        else:
            config = default_config(game_id)

        warnings = validate_config(config)
        result = json.loads(config.model_dump_json())
        result["_warnings"] = warnings
        return json.dumps(result, indent=2), 200, {"Content-Type": "application/json"}

    except Exception as e:
        return json.dumps({"error": str(e)}), 400, {"Content-Type": "application/json"}


@app.route("/api/arcade/math/<game_id>")
@login_required
def api_arcade_math(game_id):
    """API: Get math model certification report for a game type.

    Query params:
        rtp   — target RTP percentage (default: 96)
        vol   — volatility (low/medium/high/ultra)
        mc    — include Monte Carlo validation (0/1, default: 0)
        rounds — Monte Carlo rounds (default: 500000)
    """
    try:
        from tools.minigame_math import MiniGameMathEngine
        from tools.minigame_config import build_config

        target_rtp = float(request.args.get("rtp", 96.0))
        volatility = request.args.get("vol", "medium")
        include_mc = request.args.get("mc", "0") == "1"
        mc_rounds = min(int(request.args.get("rounds", 500000)), 5_000_000)

        engine = MiniGameMathEngine()
        config = build_config(
            game_type=game_id,
            target_rtp=target_rtp,
            volatility=volatility,
        )
        model = engine.model_for_config(config)
        report = model.certification_report(
            include_montecarlo=include_mc,
            mc_rounds=mc_rounds,
        )
        return json.dumps(report, indent=2, default=str), 200, {
            "Content-Type": "application/json"
        }
    except Exception as e:
        return json.dumps({"error": str(e)}), 400, {
            "Content-Type": "application/json"
        }


@app.route("/api/arcade/validate/<game_id>")
@login_required
def api_arcade_validate(game_id):
    """API: Run Monte Carlo validation for a game type.

    Query params:
        rounds — simulation rounds (default: 1000000, max: 10000000)
    """
    try:
        from tools.minigame_montecarlo import MonteCarloValidator

        n_rounds = min(int(request.args.get("rounds", 1_000_000)), 10_000_000)
        mc = MonteCarloValidator(tolerance=0.002)

        validators = {
            "crash": mc.validate_crash,
            "plinko": mc.validate_plinko,
            "mines": mc.validate_mines,
            "dice": mc.validate_dice,
            "wheel": mc.validate_wheel,
            "hilo": mc.validate_hilo,
            "chicken": mc.validate_chicken,
            "scratch": mc.validate_scratch,
        }
        fn = validators.get(game_id)
        if not fn:
            return json.dumps({"error": f"Unknown game: {game_id}"}), 400, {
                "Content-Type": "application/json"
            }

        result = fn(n_rounds=n_rounds)
        return json.dumps(result.to_dict(), indent=2), 200, {
            "Content-Type": "application/json"
        }
    except Exception as e:
        return json.dumps({"error": str(e)}), 400, {
            "Content-Type": "application/json"
        }


@app.route("/api/arcade/validate-all")
@login_required
def api_arcade_validate_all():
    """API: Run Monte Carlo validation across all 8 game types.

    Query params:
        rounds — rounds per game (default: 500000, max: 5000000)
    """
    try:
        from tools.minigame_montecarlo import MonteCarloValidator

        n_rounds = min(int(request.args.get("rounds", 500_000)), 5_000_000)
        mc = MonteCarloValidator(tolerance=0.002)
        report = mc.validate_all(n_rounds=n_rounds)
        return report.to_json(), 200, {"Content-Type": "application/json"}
    except Exception as e:
        return json.dumps({"error": str(e)}), 400, {
            "Content-Type": "application/json"
        }


@app.route("/api/arcade/rng/session", methods=["POST"])
@login_required
def api_arcade_rng_session():
    """API: Create a new provably-fair RNG session.

    POST body (JSON):
        client_seed — optional player-provided seed
    Returns:
        server_seed_hash, client_seed (for player to save)
    """
    try:
        from tools.minigame_rng import ProvablyFairRNG

        data = request.get_json(silent=True) or {}
        client_seed = data.get("client_seed")
        rng = ProvablyFairRNG()
        session = rng.new_session(client_seed=client_seed)
        return json.dumps({
            "server_seed_hash": session.server_seed_hash,
            "client_seed": session.client_seed,
            "session_id": session.server_seed_hash[:16],
        }), 200, {"Content-Type": "application/json"}
    except Exception as e:
        return json.dumps({"error": str(e)}), 400, {
            "Content-Type": "application/json"
        }


@app.route("/arcade/lab")
@login_required
def arcade_lab():
    """Mini-Game Lab — configure and preview games with custom parameters."""
    game_cards = ""
    for g in ARCADE_GAMES:
        game_cards += f'<option value="{g["id"]}">{g["icon"]} {g["title"]}</option>'

    return layout(f'''
    <style>
    .lab-grid{{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-top:20px}}
    @media(max-width:768px){{.lab-grid{{grid-template-columns:1fr}}}}
    .lab-field{{margin-bottom:14px}}
    .lab-field label{{display:block;font-size:11px;font-weight:600;color:var(--text-dim);margin-bottom:4px;text-transform:uppercase;letter-spacing:.5px}}
    .lab-field input,.lab-field select{{width:100%;padding:10px 12px;border-radius:8px;border:1px solid var(--border);background:var(--bg-card);color:var(--text-bright);font-size:13px}}
    .lab-field input:focus,.lab-field select:focus{{border-color:var(--text-dim);outline:none}}
    .lab-preview{{border:none;border-radius:12px;width:100%;height:600px;background:#030014}}
    .lab-btn{{padding:12px 24px;border-radius:8px;border:none;background:linear-gradient(135deg,#6366f1,#06b6d4);color:#fff;font-size:13px;font-weight:700;cursor:pointer;letter-spacing:.5px;width:100%}}
    .lab-btn:hover{{box-shadow:0 4px 16px rgba(99,102,241,0.3)}}
    .lab-json{{font-family:monospace;font-size:11px;background:rgba(0,0,0,0.3);border:1px solid var(--border);border-radius:8px;padding:12px;max-height:300px;overflow-y:auto;color:var(--text-dim);white-space:pre;margin-top:12px}}
    </style>
    <h2 class="page-title">{ICON_ARCADE} Mini-Game Lab</h2>
    <p class="page-subtitle">Configure RTP, volatility, and theme — preview instantly.</p>
    <div class="lab-grid">
        <div class="card">
            <h2>Configuration</h2>
            <div class="lab-field"><label>Game Type</label><select id="lab-game">{game_cards}</select></div>
            <div class="lab-field"><label>Target RTP %</label><input id="lab-rtp" type="number" value="96" min="85" max="99.5" step="0.5"></div>
            <div class="lab-field"><label>Volatility</label><select id="lab-vol"><option value="low">Low</option><option value="medium" selected>Medium</option><option value="high">High</option><option value="ultra">Ultra</option></select></div>
            <div class="lab-field"><label>Starting Balance</label><input id="lab-bal" type="number" value="1000" min="100" max="100000" step="100"></div>
            <button class="lab-btn" onclick="labPreview()">🔬 Generate & Preview</button>
            <div class="lab-json" id="lab-cert" style="display:none;background:rgba(124,58,237,0.08);border-color:rgba(124,58,237,0.2)"></div>
            <div class="lab-json" id="lab-config" style="display:none"></div>
        </div>
        <div class="card" style="padding:0;overflow:hidden">
            <iframe id="lab-frame" class="lab-preview" src="about:blank"></iframe>
        </div>
    </div>
    <script>
    async function labPreview() {{
        const game = document.getElementById('lab-game').value;
        const rtp = document.getElementById('lab-rtp').value;
        const vol = document.getElementById('lab-vol').value;
        const bal = document.getElementById('lab-bal').value;
        const url = `/arcade/configured/${{game}}?rtp=${{rtp}}&vol=${{vol}}&balance=${{bal}}`;
        document.getElementById('lab-frame').src = url;

        // Fetch config JSON for display
        try {{
            const resp = await fetch(`/api/minigame/config/${{game}}?rtp=${{rtp}}&vol=${{vol}}`);
            const data = await resp.json();
            const el = document.getElementById('lab-config');
            el.style.display = 'block';
            el.textContent = JSON.stringify(data, null, 2);
        }} catch(e) {{ console.warn('Config fetch failed', e); }}

        // Fetch math certification
        try {{
            const resp = await fetch(`/api/arcade/math/${{game}}?rtp=${{rtp}}&vol=${{vol}}`);
            const data = await resp.json();
            const el = document.getElementById('lab-cert');
            el.style.display = 'block';
            const proof = data.rtp_proof || {{}};
            const vol_p = data.volatility_profile || {{}};
            el.innerHTML = `<div style="font-size:12px;font-weight:700;color:var(--text-bright);margin-bottom:8px">📜 Math Certification</div>`
              + `<div>RTP: <b>${{(proof.theoretical_rtp_pct||0).toFixed(2)}}%</b> | HE: ${{(100-(proof.theoretical_rtp_pct||0)).toFixed(2)}}%</div>`
              + `<div>P_sum: ${{proof.probability_sum_check}} | RTP_check: ${{proof.rtp_check}}</div>`
              + `<div>σ=${{(vol_p.standard_deviation||0).toFixed(2)}} | hit=${{(vol_p.hit_frequency_pct||0).toFixed(1)}}% | max=${{vol_p.max_win_multiplier||'?'}}×</div>`
              + `<div style="margin-top:6px"><button class="lab-btn" style="padding:6px 12px;font-size:11px;width:auto" onclick="labMC('${{game}}',Number(${{rtp}}))">▶ Run Monte Carlo (500K)</button></div>`
              + `<div id="lab-mc" style="margin-top:8px;font-size:11px;color:var(--text-dim)"></div>`;
        }} catch(e) {{ console.warn('Cert fetch failed', e); }}
    }}

    async function labMC(game, rtp) {{
        const el = document.getElementById('lab-mc');
        el.textContent = '⏳ Running 500K simulations...';
        try {{
            const resp = await fetch(`/api/arcade/validate/${{game}}?rounds=500000`);
            const d = await resp.json();
            const delta = Math.abs((d.measured_rtp||0)*100 - (d.theoretical_rtp||0)*100).toFixed(4);
            const pass_ = d.rtp_pass ? '✅ PASS' : '❌ FAIL';
            el.innerHTML = `${{pass_}} | measured=${{((d.measured_rtp||0)*100).toFixed(3)}}% | Δ=${{delta}}% | ${{(d.rounds_simulated||0).toLocaleString()}} rounds`;
        }} catch(e) {{ el.textContent = '❌ Error: ' + e; }}
    }}
    </script>''', "arcade")


# ─── MINI-GAME PIPELINE LAUNCHER ───
@app.route("/new-minigame")
@login_required
def new_minigame():
    """Form to launch a full Mini-RMG pipeline job."""
    game_options = ""
    for g in ["crash", "plinko", "mines", "dice", "wheel", "hilo", "chicken", "scratch"]:
        icons = {"crash": "🚀", "plinko": "⚡", "mines": "💣", "dice": "🎲",
                 "wheel": "🎡", "hilo": "🃏", "chicken": "🐔", "scratch": "🎟️"}
        game_options += f'<option value="{g}">{icons.get(g,"")} {g.title()}</option>\n'

    return layout(f'''
    <style>
    .mg-form{{max-width:640px;margin:0 auto}}
    .mg-field{{margin-bottom:16px}}
    .mg-field label{{display:block;font-size:11px;font-weight:600;color:var(--text-dim);margin-bottom:5px;text-transform:uppercase;letter-spacing:.5px}}
    .mg-field input,.mg-field select,.mg-field textarea{{width:100%;padding:10px 12px;border-radius:8px;border:1px solid var(--border);background:var(--bg-card);color:var(--text-bright);font-size:13px}}
    .mg-field textarea{{min-height:80px;resize:vertical}}
    .mg-row{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
    .mg-btn{{display:block;width:100%;padding:14px;border-radius:8px;border:none;background:linear-gradient(135deg,#7c3aed,#ec4899);color:#fff;font-size:14px;font-weight:700;cursor:pointer;letter-spacing:.5px;margin-top:8px}}
    .mg-btn:hover{{box-shadow:0 4px 20px rgba(124,58,237,0.4)}}
    .mg-info{{background:rgba(124,58,237,0.08);border:1px solid rgba(124,58,237,0.15);border-radius:8px;padding:12px;font-size:11px;color:var(--text-dim);margin-top:16px}}
    </style>
    <h2 class="page-title">{ICON_ARCADE} New Mini-Game Pipeline</h2>
    <p class="page-subtitle">Generate a production-ready HTML5 mini-game with math certification.</p>
    <div class="card mg-form">
        <form action="/api/launch-minigame" method="POST">
            <div class="mg-field"><label>Game Type</label><select name="game_type">{game_options}</select></div>
            <div class="mg-field"><label>Theme / Title</label><input name="theme" placeholder="e.g. Neon Galaxy Crash" required></div>
            <div class="mg-row">
                <div class="mg-field"><label>Target RTP %</label><input name="target_rtp" type="number" value="96" min="85" max="99.5" step="0.5"></div>
                <div class="mg-field"><label>House Edge %</label><input name="house_edge" type="number" value="4" min="0.5" max="15" step="0.5"></div>
            </div>
            <div class="mg-row">
                <div class="mg-field"><label>Max Multiplier</label><input name="max_multiplier" type="number" value="1000" min="10" max="100000" step="10"></div>
                <div class="mg-field"><label>Starting Balance</label><input name="starting_balance" type="number" value="1000" min="100" max="100000" step="100"></div>
            </div>
            <div class="mg-row">
                <div class="mg-field"><label>Volatility</label>
                    <select name="volatility">
                        <option value="low">Low — Frequent small wins</option>
                        <option value="medium" selected>Medium — Balanced</option>
                        <option value="high">High — Rare big wins</option>
                        <option value="ultra">Ultra — Jackpot-heavy</option>
                    </select>
                </div>
                <div class="mg-field"><label>Web3 Output</label>
                    <select name="web3_mode">
                        <option value="0">No — Standard HTML5 only</option>
                        <option value="1">Yes — Include Solidity contracts</option>
                    </select>
                </div>
                <div class="mg-field"><label>Code Generation</label>
                    <select name="full_codegen">
                        <option value="0">Template — Reliable, themed skin</option>
                        <option value="1">Full Codegen — Unique game code (experimental)</option>
                    </select>
                </div>
            </div>
            <div class="mg-row">
                <div class="mg-field"><label>Language</label>
                    <select name="language">
                        <option value="en" selected>🇬🇧 English</option>
                        <option value="es">🇪🇸 Español</option>
                        <option value="pt">🇧🇷 Português</option>
                        <option value="de">🇩🇪 Deutsch</option>
                        <option value="fr">🇫🇷 Français</option>
                        <option value="ja">🇯🇵 日本語</option>
                        <option value="ko">🇰🇷 한국어</option>
                        <option value="zh">🇨🇳 中文</option>
                        <option value="hi">🇮🇳 हिन्दी</option>
                        <option value="ar">🇸🇦 العربية</option>
                    </select>
                </div>
                <div class="mg-field"><label>Server-Side RNG</label>
                    <select name="server_mode">
                        <option value="0" selected>Demo — Client-side only</option>
                        <option value="1">Real — Server-side wallet bridge</option>
                    </select>
                </div>
            </div>
            <div class="mg-field"><label>Theme Colors (optional)</label>
                <div class="mg-row">
                    <input name="primary_color" type="color" value="#7c3aed" style="height:38px;padding:2px">
                    <input name="secondary_color" type="color" value="#06b6d4" style="height:38px;padding:2px">
                </div>
            </div>
            <div class="mg-field"><label>Special Requirements (optional)</label>
                <textarea name="special_requirements" placeholder="e.g. Egyptian theme, progressive jackpot, mobile-first layout..."></textarea>
            </div>
            <button type="submit" class="mg-btn">🎮 Launch Mini-Game Pipeline</button>
        </form>
        <div class="mg-info">
            <b>Pipeline stages:</b> Math Model → Game Design → HTML5 Build → Compliance → Package<br>
            Output includes: playable game, math certification, Monte Carlo validation, RNG spec, compliance report.
        </div>
    </div>''', "arcade")


@app.route("/api/launch-minigame", methods=["POST"])
@login_required
def api_launch_minigame():
    """Launch a Mini-RMG pipeline job."""
    user = current_user()
    limit_err = _check_job_limit(user["id"])
    if limit_err:
        return limit_err
    job_id = str(uuid.uuid4())[:8]
    params = {
        "game_type": request.form.get("game_type", "crash"),
        "theme": request.form.get("theme", "Mini Game"),
        "target_rtp": float(request.form.get("target_rtp", 96)),
        "house_edge": float(request.form.get("house_edge", 4)) / 100.0,
        "max_multiplier": float(request.form.get("max_multiplier", 1000)),
        "starting_balance": float(request.form.get("starting_balance", 1000)),
        "volatility": request.form.get("volatility", "medium"),
        "web3_mode": request.form.get("web3_mode") == "1",
        "full_codegen": request.form.get("full_codegen") == "1",
        "primary_color": request.form.get("primary_color", "#7c3aed"),
        "secondary_color": request.form.get("secondary_color", "#06b6d4"),
        "special_requirements": request.form.get("special_requirements", ""),
        "language": request.form.get("language", "en"),
        "server_mode": request.form.get("server_mode") == "1",
    }
    db = get_db()
    db.execute(
        "INSERT INTO jobs (id,user_id,job_type,title,params,status) VALUES (?,?,?,?,?,?)",
        (job_id, user["id"], "mini_rmg", f"🎮 {params['theme']}", json.dumps(params), "queued"),
    )
    db.commit()
    _spawn_worker(job_id, "mini_rmg", json.dumps(params))
    return redirect(f"/job/{job_id}/logs")


# ─── NOVEL GAME GENERATOR ───
@app.route("/novel")
@login_required
def novel_game_page():
    """Novel game creation — describe a game in natural language."""
    return layout(f'''
    <style>
    .novel-form{{max-width:640px;margin:0 auto}}
    .novel-field{{margin-bottom:16px}}
    .novel-field label{{display:block;font-size:11px;font-weight:600;color:var(--text-dim);margin-bottom:5px;text-transform:uppercase;letter-spacing:.5px}}
    .novel-field input,.novel-field select,.novel-field textarea{{width:100%;padding:10px 12px;border-radius:8px;border:1px solid var(--border);background:var(--bg-card);color:var(--text-bright);font-size:13px}}
    .novel-field textarea{{min-height:100px;resize:vertical}}
    .novel-row{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
    .novel-btn{{display:block;width:100%;padding:14px;border-radius:8px;border:none;background:linear-gradient(135deg,#f97316,#ef4444);color:#fff;font-size:14px;font-weight:700;cursor:pointer;letter-spacing:.5px;margin-top:8px}}
    .novel-btn:hover{{box-shadow:0 4px 20px rgba(249,115,22,0.4)}}
    .novel-btn:disabled{{opacity:0.5;cursor:wait}}
    .novel-examples{{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}}
    .novel-ex{{padding:4px 10px;border-radius:6px;border:1px solid var(--border);font-size:11px;color:var(--text-dim);cursor:pointer;transition:var(--transition)}}
    .novel-ex:hover{{border-color:var(--text-dim);color:var(--text-bright)}}
    .novel-result{{margin-top:20px;display:none}}
    .novel-result.show{{display:block}}
    .novel-preview iframe{{width:100%;height:500px;border:none;border-radius:12px;background:#030014}}
    .novel-report{{background:rgba(0,0,0,0.2);border:1px solid var(--border);border-radius:8px;padding:12px;font-size:11px;margin-top:12px;font-family:monospace;max-height:300px;overflow-y:auto;color:var(--text-dim)}}
    </style>
    <h2 class="page-title">🧪 Novel Game Inventor</h2>
    <p class="page-subtitle">Describe a game in plain English. AI invents the mechanic, builds it, and playtests it.</p>
    <div class="card novel-form">
        <div class="novel-field">
            <label>Describe your game idea</label>
            <textarea id="novel-desc" placeholder="e.g. A tower stacking game where you build higher for bigger multipliers. Each block has a chance to wobble and topple the tower."></textarea>
            <div class="novel-examples">
                <span class="novel-ex" onclick="document.getElementById('novel-desc').value=this.textContent">Tower stacking game with wobbling blocks</span>
                <span class="novel-ex" onclick="document.getElementById('novel-desc').value=this.textContent">Deep sea diving for treasure</span>
                <span class="novel-ex" onclick="document.getElementById('novel-desc').value=this.textContent">Marble race down a mountain</span>
                <span class="novel-ex" onclick="document.getElementById('novel-desc').value=this.textContent">Gem mining grid with hidden explosives</span>
                <span class="novel-ex" onclick="document.getElementById('novel-desc').value=this.textContent">Space wheel spin with cosmic multipliers</span>
            </div>
        </div>
        <div class="novel-row">
            <div class="novel-field"><label>Target RTP %</label><input id="novel-rtp" type="number" value="96" min="85" max="99" step="0.5"></div>
            <div class="novel-field"><label>Volatility</label><select id="novel-vol"><option value="low">Low</option><option value="medium" selected>Medium</option><option value="high">High</option><option value="ultra">Ultra</option></select></div>
        </div>
        <button class="novel-btn" id="novel-go" onclick="generateNovel()">🧪 Invent & Build Game</button>
        <div class="novel-result" id="novel-result">
            <div class="novel-preview"><iframe id="novel-frame" src="about:blank"></iframe></div>
            <div class="novel-report" id="novel-report"></div>
        </div>
    </div>
    <script>
    async function generateNovel() {{
        const desc = document.getElementById('novel-desc').value.trim();
        if (!desc) return;
        const btn = document.getElementById('novel-go');
        btn.disabled = true; btn.textContent = '⏳ Inventing...';

        try {{
            const resp = await fetch('/api/novel/generate', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{
                    description: desc,
                    target_rtp: parseFloat(document.getElementById('novel-rtp').value),
                    volatility: document.getElementById('novel-vol').value,
                }})
            }});
            const data = await resp.json();
            if (data.error) {{ alert(data.error); return; }}

            // Show result
            const el = document.getElementById('novel-result');
            el.classList.add('show');

            // Load game in iframe
            if (data.game_url) {{
                document.getElementById('novel-frame').src = data.game_url;
            }}

            // Show report
            const rpt = data.playtest || {{}};
            const badges = [];
            if (data.used_fullgen) badges.push('<span style="background:#7c3aed;color:#fff;padding:2px 8px;border-radius:4px;font-size:10px">🧠 FULL CODEGEN</span>');
            if (data.was_refined) badges.push('<span style="background:#f59e0b;color:#000;padding:2px 8px;border-radius:4px;font-size:10px">🔄 LLM REFINED</span>');
            document.getElementById('novel-report').innerHTML =
                '<b>' + (data.mechanic?.name || 'Game') + '</b> ' + badges.join(' ') + ' | '
                + 'Arch: ' + (data.mechanic?.archetype||'?') + ' | Vis: ' + (data.mechanic?.visual_pattern||'?') + '<br>'
                + 'RTP: ' + (rpt.measured_rtp*100||0).toFixed(2) + '% | Hit: ' + (rpt.hit_frequency*100||0).toFixed(1) + '% | '
                + 'Fun: ' + (rpt.fun_score||0).toFixed(1) + '/10 | Max Win: ' + (rpt.max_win_achieved||0).toFixed(1) + 'x<br>'
                + 'Issues: ' + ((rpt.issues||[]).join(', ') || 'None') + '<br>'
                + 'Suggestions: ' + ((rpt.suggestions||[]).join(', ') || 'None');
        }} catch(e) {{
            alert('Error: ' + e.message);
        }} finally {{
            btn.disabled = false; btn.textContent = '🧪 Invent & Build Game';
        }}
    }}
    </script>''', "arcade")


@app.route("/api/novel/generate", methods=["POST"])
@login_required
def api_novel_generate():
    """Generate a novel game from a natural language description."""
    try:
        from tools.minigame_novel import MechanicInventor, NovelGameBuilder, PlaytestSimulator

        data = request.get_json(silent=True) or {}
        desc = data.get("description", "")
        if not desc:
            return jsonify({"error": "Description required"}), 400

        target_rtp = float(data.get("target_rtp", 96))
        volatility = data.get("volatility", "medium")

        # Pass 1: Invent mechanic (LLM-powered by default)
        inventor = MechanicInventor(use_llm=True)
        mechanic = inventor.invent(desc, target_rtp=target_rtp, volatility=volatility)

        # Pass 2: Playtest initial mechanic
        simulator = PlaytestSimulator()
        initial_report = simulator.evaluate(mechanic, n_rounds=50_000)

        # Pass 3: LLM refinement — analyze playtest, fix issues
        refined = inventor.refine_with_llm(mechanic, initial_report)
        was_refined = refined.mechanic_hash != mechanic.mechanic_hash

        # Pass 4: Build HTML — try full codegen first, fall back to template builder
        html = None
        used_fullgen = False
        try:
            from tools.minigame_fullgen import FullGameGenerator
            # Build a design dict for the full codegen engine
            fullgen_design = {
                "title": refined.name,
                "tagline": refined.description[:100],
                "subtitle": f"{refined.archetype.title()} Game",
                "icon": "🧪",
                "description": refined.description,
                "sound_theme": refined.theme_suggestion,
                "ui_theme": {
                    "primary_color": "#a855f7", "secondary_color": "#06b6d4",
                    "bg_start": "#0a0014", "bg_end": "#0a0020",
                    "text_color": "#e2e8f0", "text_dim": "#64748b",
                    "win_color": "#22c55e", "lose_color": "#ef4444",
                    "gold_color": "#f59e0b",
                },
                "flavor_text": {
                    "win_messages": ["Nice win!", "You got it!", "Winner!", "Sweet!", "Boom!"],
                    "loss_messages": ["Try again!", "So close!", "Not this time!", "Almost!", "Next round!"],
                    "big_win_messages": ["MEGA WIN!", "JACKPOT!", "INCREDIBLE!"],
                },
                "logic": {
                    "bet_options": [0.10, 0.25, 0.50, 1.00, 2.00, 5.00, 10.00],
                    "game_labels": {"play_button": "PLAY", "cashout_button": "CASH OUT"},
                    "visual_effects": {"particle_type": "stars"},
                },
            }
            gen = FullGameGenerator(max_fix_attempts=2)
            gen_config = {
                "game_type": refined.archetype,
                "house_edge": (100 - refined.parameters.get("target_rtp", 96)) / 100,
                "target_rtp": refined.parameters.get("target_rtp", 96),
                "max_multiplier": refined.max_multiplier,
                "volatility": volatility,
                "starting_balance": 1000,
            }
            result = gen.generate(
                description=refined.description,
                design=fullgen_design,
                config=gen_config,
            )
            if result.validation.get("passed") or result.validation.get("score", 0) >= 50:
                html = result.html
                used_fullgen = True
        except Exception:
            pass

        # Fallback: use template-based NovelGameBuilder
        if not html:
            builder = NovelGameBuilder()
            html = builder.build(refined)

        # Save to generated games directory
        gen_dir = Path(__file__).parent / "static" / "arcade" / "games" / "generated"
        gen_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r'[^a-z0-9]+', '-', refined.name.lower()).strip('-')
        filename = f"novel_{safe_name}_{refined.mechanic_hash[:8]}.html"
        (gen_dir / filename).write_text(html, encoding="utf-8")

        # Pass 5: Playtest refined version
        final_report = simulator.evaluate(refined, n_rounds=50_000) if was_refined else initial_report

        return jsonify({
            "mechanic": refined.to_dict(),
            "playtest": final_report.to_dict(),
            "game_url": f"/arcade/play/gen/{filename}",
            "filename": filename,
            "was_refined": was_refined,
            "used_fullgen": used_fullgen,
            "initial_playtest": initial_report.to_dict() if was_refined else None,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/novel/variants", methods=["POST"])
@login_required
def api_novel_variants():
    """Generate theme and risk variants of a game description."""
    try:
        from tools.minigame_novel import MechanicInventor, VariantGenerator

        data = request.get_json(silent=True) or {}
        desc = data.get("description", "")
        if not desc:
            return jsonify({"error": "Description required"}), 400

        inventor = MechanicInventor(use_llm=True)
        mechanic = inventor.invent(desc)
        vg = VariantGenerator()
        builds = vg.build_all(mechanic, themes=int(data.get("themes", 4)))

        # Save each variant
        gen_dir = Path(__file__).parent / "static" / "arcade" / "games" / "generated"
        gen_dir.mkdir(parents=True, exist_ok=True)
        results = []
        for b in builds:
            safe_name = re.sub(r'[^a-z0-9]+', '-', b['name'].lower()).strip('-')
            fname = f"novel_{safe_name}.html"
            (gen_dir / fname).write_text(b['html'], encoding="utf-8")
            results.append({
                "name": b['name'],
                "theme": b['theme'],
                "url": f"/arcade/play/gen/{fname}",
                "html_size": b['html_length'],
            })

        return jsonify({"variants": results, "count": len(results)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── PLATFORM ENGINE (Phase 6) ───

def _get_platform():
    """Get or create a PlatformEngine instance."""
    from tools.platform_engine import PlatformEngine
    db_dir = Path(__file__).parent / "data"
    db_dir.mkdir(exist_ok=True)
    return PlatformEngine(str(db_dir / "platform.db"))



@app.route("/game-library")
@login_required
def game_library_page():
    """Searchable game catalog."""
    try:
        pe = _get_platform()
        games = pe.search_games(sort_by="newest", limit=100)
    except Exception:
        games = []

    cards = ""
    for g in games:
        icon = {"crash":"🚀","plinko":"⚡","mines":"💣","dice":"🎲","wheel":"🎡","hilo":"🃏","chicken":"🐔","scratch":"🎟️"}.get(g["game_type"],"🎮")
        vol_badge = {"low":"🟢","medium":"🟡","high":"🟠","ultra":"🔴"}.get(g.get("volatility",""),"")
        cards += (
            f'<div class="lib-card">'
            f'<div class="lib-icon">{icon}</div>'
            f'<div class="lib-info">'
            f'<div class="lib-title">{_esc(g["title"])}</div>'
            f'<div class="lib-meta">{g["game_type"].title()} · RTP {g["rtp"]:.1f}% · {vol_badge} {g.get("volatility","").title()}</div>'
            f'<div class="lib-stats">{g.get("play_count",0):,} plays · ${g.get("total_wagered",0):,.0f} wagered</div>'
            f'</div>'
            f'<div class="lib-actions">'
            f'<a href="/arcade/play/{g.get("filename","")}" class="btn btn-ghost btn-sm">Play</a>'
            f'</div></div>'
        )
    if not cards:
        cards = '<div class="empty-state"><h3>No games in library</h3><p>Generate games via the Mini-Game Pipeline to populate the library.</p></div>'

    return layout(f'''
    <style>
    .lib-card{{display:flex;align-items:center;gap:14px;padding:14px 18px;border-bottom:1px solid var(--border);transition:var(--transition)}}
    .lib-card:hover{{background:rgba(255,255,255,0.02)}}
    .lib-icon{{font-size:28px;width:44px;text-align:center}}
    .lib-info{{flex:1}}
    .lib-title{{font-weight:600;color:var(--text-bright);font-size:14px}}
    .lib-meta{{font-size:11px;color:var(--text-dim);margin-top:2px}}
    .lib-stats{{font-size:11px;color:var(--text-muted);margin-top:2px}}
    </style>
    <h2 class="page-title">📚 Game Library</h2>
    <p class="page-subtitle">{len(games)} games in catalog</p>
    <div class="card" style="padding:0;overflow:hidden">{cards}</div>
    ''', "arcade")


# ─── STATE RECON ───
@app.route("/recon")
@login_required
def recon_page():
    return layout(f'''
    <h2 class="page-title">{ICON_GLOBE} State Recon</h2>
    <p class="page-subtitle">Point at any US state. AI agents research laws, find loopholes, design compliant games.</p>
    <div class="card"><h2>{ICON_SEARCH} Research a State</h2><form action="/api/recon" method="POST"><label>US State Name</label><div class="recon-input-group"><input name="state" placeholder="e.g. North Carolina" required><button type="submit" class="btn btn-primary">Launch Recon</button></div></form></div>
    <div class="card"><h2>Pipeline Stages</h2><div style="display:grid;grid-template-columns:repeat(4,1fr);gap:16px;text-align:center;padding:12px 0">
    <div><div style="font-size:22px;margin-bottom:6px">&#128269;</div><div style="font-size:12px;font-weight:600;color:var(--text-bright)">Legal Research</div><div style="font-size:11px;color:var(--text-dim)">Statutes, case law, AG opinions</div></div>
    <div><div style="font-size:22px;margin-bottom:6px">&#9878;&#65039;</div><div style="font-size:12px;font-weight:600;color:var(--text-bright)">Definition Analysis</div><div style="font-size:11px;color:var(--text-dim)">Element mapping, loophole ID</div></div>
    <div><div style="font-size:22px;margin-bottom:6px">&#127918;</div><div style="font-size:12px;font-weight:600;color:var(--text-bright)">Game Architecture</div><div style="font-size:11px;color:var(--text-dim)">Compliant mechanics design</div></div>
    <div><div style="font-size:22px;margin-bottom:6px">&#128203;</div><div style="font-size:12px;font-weight:600;color:var(--text-bright)">Defense Brief</div><div style="font-size:11px;color:var(--text-dim)">Courtroom-ready mapping</div></div></div></div>''', "recon")

# ─── TREND RADAR DASHBOARD ───

@app.route("/trends")
@login_required
def trends_page():
    from tools.market_intel import get_dashboard_data, seed_baseline_if_empty
    db = get_db()
    seed_baseline_if_empty(db)
    d = get_dashboard_data(db)
    scan = d.get("scan_data")
    scan_date = d.get("scan_date")

    # ── Opportunity Finder cards ──
    opp_cards = ""
    for i, o in enumerate(d.get("opportunities", [])[:12]):
        score = o.get("opportunity_score", 0)
        bar_w = min(score * 500, 100)
        color = "#22c55e" if score > 0.12 else "#f59e0b" if score > 0.05 else "#64748b"
        opp_cards += f'''<div style="padding:10px 12px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:12px">
            <div style="font-size:18px;font-weight:800;color:{color};width:28px;text-align:center">{i+1}</div>
            <div style="flex:1">
                <div style="font-weight:700;font-size:12px">{_esc(o["theme"])} × {_esc(o["mechanic"])}</div>
                <div style="font-size:10px;color:var(--text-dim);margin-top:2px">{_esc(o.get("reasoning","")[:120])}</div>
            </div>
            <div style="width:80px">
                <div style="background:var(--bg-dark,#0a0b10);border-radius:4px;height:6px;overflow:hidden">
                    <div style="width:{bar_w}%;height:100%;background:{color};border-radius:4px"></div>
                </div>
                <div style="font-size:9px;color:var(--text-dim);text-align:right;margin-top:2px">{score:.3f}</div>
            </div>
        </div>'''
    if not opp_cards:
        opp_cards = '<div style="padding:20px;text-align:center;color:var(--text-dim);font-size:12px">No opportunities yet — run a market scan first</div>'

    # ── Theme chart (horizontal bars) ──
    themes_chart = ""
    themes = d.get("themes", [])
    if themes:
        max_val = max((t.get("market_share", 0) for t in themes), default=1) or 1
        for t in themes[:12]:
            w = t.get("market_share", 0) / max_val * 100
            themes_chart += f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px"><div style="width:100px;font-size:11px;text-align:right;color:var(--text-dim);flex-shrink:0">{_esc(t["name"])}</div><div style="flex:1;background:rgba(124,106,239,0.1);border-radius:3px;height:18px;overflow:hidden"><div style="width:{w}%;height:100%;background:linear-gradient(90deg,#7c6aef,#a78bfa);border-radius:3px"></div></div><div style="width:32px;font-size:10px;color:var(--text-dim);text-align:right">{t.get("market_share",0):.0f}</div></div>'
    else:
        themes_chart = '<div style="color:var(--text-dim);font-size:12px;padding:12px">No theme data — run a scan</div>'

    # ── Mechanics chart ──
    mechs_chart = ""
    mechs = d.get("mechanics", [])
    if mechs:
        max_m = max((m.get("market_share", 0) for m in mechs), default=1) or 1
        for m in mechs[:10]:
            w = m.get("market_share", 0) / max_m * 100
            mechs_chart += f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px"><div style="width:100px;font-size:11px;text-align:right;color:var(--text-dim);flex-shrink:0">{_esc(m["name"])}</div><div style="flex:1;background:rgba(34,197,94,0.1);border-radius:3px;height:18px;overflow:hidden"><div style="width:{w}%;height:100%;background:linear-gradient(90deg,#22c55e,#4ade80);border-radius:3px"></div></div><div style="width:32px;font-size:10px;color:var(--text-dim);text-align:right">{m.get("market_share",0):.0f}</div></div>'

    # ── Providers chart ──
    provs_chart = ""
    provs = d.get("providers", [])
    if provs:
        max_p = max((p.get("market_share", 0) for p in provs), default=1) or 1
        for p in provs[:10]:
            w = p.get("market_share", 0) / max_p * 100
            provs_chart += f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px"><div style="width:100px;font-size:11px;text-align:right;color:var(--text-dim);flex-shrink:0">{_esc(p["name"])}</div><div style="flex:1;background:rgba(6,182,212,0.1);border-radius:3px;height:18px;overflow:hidden"><div style="width:{w}%;height:100%;background:linear-gradient(90deg,#06b6d4,#22d3ee);border-radius:3px"></div></div><div style="width:32px;font-size:10px;color:var(--text-dim);text-align:right">{p.get("market_share",0):.0f}</div></div>'

    # ── Recent competitors ──
    comp_rows = ""
    for g in d.get("recent_competitors", []):
        comp_rows += f'<tr><td style="font-size:12px;font-weight:600">{_esc(g.get("title",""))}</td><td style="font-size:11px;color:var(--text-dim)">{_esc(g.get("provider",""))}</td><td style="font-size:11px;color:var(--text-dim)">{_esc(g.get("theme","") or "—")}</td></tr>'

    scan_info = ""
    if scan_date:
        try:
            dt = datetime.fromisoformat(scan_date.split("+")[0])
            scan_info = f'Last scan: {dt.strftime("%b %d, %H:%M")} · {d.get("snapshot_count",0)} snapshots'
        except Exception:
            scan_info = f'{d.get("snapshot_count",0)} snapshots'

    return layout(f'''
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:20px">
        <div><h2 class="page-title" style="margin-bottom:4px">📡 Trend Radar</h2>
        <p class="page-subtitle" style="margin-bottom:0">{scan_info or "Market intelligence for slot game development"}</p></div>
        <div style="display:flex;gap:8px">
            <button onclick="runScan()" id="scanBtn" class="btn btn-primary" style="padding:8px 18px;font-size:12px">🛰️ Run Market Scan</button>
            <button onclick="runScan('with_llm')" class="btn" style="padding:8px 14px;font-size:12px;background:rgba(124,106,239,0.15);color:#a78bfa;border:1px solid rgba(124,106,239,0.3)">🧠 + AI Analysis</button>
        </div>
    </div>

    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:20px">
        <div class="stat-card"><div class="stat-val">{len(d.get("opportunities",[]))}</div><div class="stat-label">Opportunities</div></div>
        <div class="stat-card"><div class="stat-val">{d.get("competitor_count",0)}</div><div class="stat-label">Tracked Games</div></div>
        <div class="stat-card"><div class="stat-val">{len(themes)}</div><div class="stat-label">Theme Signals</div></div>
        <div class="stat-card"><div class="stat-val">{d.get("snapshot_count",0)}</div><div class="stat-label">Historical Scans</div></div>
    </div>

    <div class="card" style="border:1px solid rgba(34,197,94,0.3);background:linear-gradient(135deg,rgba(34,197,94,0.04),rgba(124,106,239,0.04))">
        <h2 style="margin-bottom:4px">🔭 Opportunity Finder</h2>
        <p style="font-size:11px;color:var(--text-dim);margin-bottom:12px">Blue ocean niches: high demand × low supply × rising momentum. Score = demand × (1 − saturation) × momentum.</p>
        <div id="oppContainer">{opp_cards}</div>
    </div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:16px">
        <div class="card"><h2>🎨 Theme Landscape</h2><div style="padding:8px 0">{themes_chart}</div></div>
        <div class="card"><h2>⚙️ Mechanic Heatmap</h2><div style="padding:8px 0">{mechs_chart}</div></div>
    </div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:16px">
        <div class="card"><h2>🏢 Provider Activity</h2><div style="padding:8px 0">{provs_chart}</div></div>
        <div class="card">
            <h2>🎮 Competitor Intel ({d.get("competitor_count",0)})</h2>
            <table style="font-size:12px"><thead><tr><th>Title</th><th>Provider</th><th>Theme</th></tr></thead>
            <tbody>{comp_rows if comp_rows else '<tr><td colspan="3" style="color:var(--text-dim)">No games tracked yet</td></tr>'}</tbody></table>
        </div>
    </div>

    <div id="llmInsight" class="card" style="margin-top:16px;display:none;border:1px solid rgba(124,106,239,0.3)">
        <h2>🧠 AI Market Analysis</h2>
        <div id="llmContent" style="font-size:13px;line-height:1.7;white-space:pre-wrap"></div>
    </div>

    <div class="card" style="margin-top:16px">
        <h2>📊 Concept Positioning</h2>
        <p style="font-size:11px;color:var(--text-dim);margin-bottom:12px">Check where a game concept sits on the market map.</p>
        <div style="display:flex;gap:8px">
            <input id="posTheme" placeholder="Enter a theme, e.g. Norse Viking Saga" style="flex:1;margin:0">
            <button onclick="positionConcept()" class="btn btn-primary" style="white-space:nowrap">Analyze Position</button>
        </div>
        <div id="posResult" style="margin-top:12px;display:none"></div>
    </div>

    <script>
    async function runScan(mode){{
        var btn=document.getElementById('scanBtn');
        btn.disabled=true;btn.textContent='⏳ Scanning...';
        try{{
            var r=await fetch('/api/market-scan'+(mode==='with_llm'?'?llm=1':''),{{method:'POST'}});
            var d=await r.json();
            if(d.ok)location.reload();
            else alert('Scan failed: '+(d.error||'Unknown error'));
        }}catch(e){{alert('Error: '+e);}}
        btn.disabled=false;btn.textContent='🛰️ Run Market Scan';
    }}
    async function positionConcept(){{
        var theme=document.getElementById('posTheme').value.trim();
        if(!theme)return;
        var r=await fetch('/api/market-position?theme='+encodeURIComponent(theme));
        var d=await r.json();
        var html='<div style="padding:12px;background:rgba(124,106,239,0.05);border-radius:8px">';
        html+='<div style="font-size:18px;margin-bottom:8px">'+d.positioning+'</div>';
        html+='<div style="font-size:12px;color:var(--text-dim)">Categories: '+(d.matched_categories||[]).join(', ')+'</div>';
        if(d.opportunity_score)html+='<div style="font-size:12px;margin-top:4px">Opportunity score: <strong>'+d.opportunity_score.toFixed(3)+'</strong></div>';
        html+='<div style="font-size:12px;margin-top:4px">Direct competitors tracked: <strong>'+d.direct_competitors+'</strong></div>';
        html+='<div style="font-size:11px;color:var(--text-dim);margin-top:6px">'+d.opportunity_reasoning+'</div>';
        html+='</div>';
        document.getElementById('posResult').innerHTML=html;
        document.getElementById('posResult').style.display='block';
    }}
    </script>
    ''', "trends")


@app.route("/api/market-scan", methods=["POST"])
@login_required
def api_market_scan():
    """Run a full market scan and optionally LLM analysis."""
    try:
        from tools.market_intel import run_full_scan, find_opportunities, llm_market_analysis
        db = get_db()
        theme = request.args.get("theme", "")
        scan = run_full_scan(db, theme_filter=theme)
        opps = find_opportunities(db, scan)

        result = {"ok": True, "themes": len(scan.get("themes", [])),
                  "mechanics": len(scan.get("mechanics", [])),
                  "opportunities": len(opps),
                  "games_detected": len(scan.get("games_detected", []))}

        if request.args.get("llm") == "1":
            analysis = llm_market_analysis(scan)
            if analysis:
                result["llm_analysis"] = analysis

        return jsonify(result)
    except Exception as e:
        logger.error(f"Market scan failed: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/market-position")
@login_required
def api_market_position():
    """Get concept positioning for a theme."""
    try:
        from tools.market_intel import position_concept
        theme = request.args.get("theme", "")
        if not theme:
            return jsonify({"error": "theme required"}), 400
        db = get_db()
        result = position_concept(db, theme, [])
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── HISTORY ───
@app.route("/history")
@login_required
def history_page():
    user = current_user()
    db = get_db()
    jobs = db.execute("SELECT * FROM jobs WHERE user_id=? ORDER BY created_at DESC LIMIT 50", (user["id"],)).fetchall()
    rows = ""
    for job in jobs:
        jid,status = job["id"], job["status"]
        bc = {"running":"badge-running","complete":"badge-complete","failed":"badge-failed","partial":"badge-partial"}.get(status,"badge-queued")
        tl = "Slot" if job["job_type"]=="slot_pipeline" else ("Recon" if job["job_type"]=="state_recon" else ("Mini-Game" if job["job_type"]=="mini_rmg" else ("Iterate" if job["job_type"]=="iterate" else ("Variants" if job["job_type"]=="variant_parent" else ("Variant" if job["job_type"]=="variant" else job["job_type"])))))
        dt = job["created_at"][:16].replace("T"," ") if job["created_at"] else ""
        if job["job_type"] == "variant_parent":
            act = f'<a href="/job/{jid}/variants" class="btn btn-ghost btn-sm">Compare</a>' if status in ("running","complete") else ""
        elif status=="complete":
            act = f'<a href="/job/{jid}/files" class="btn btn-ghost btn-sm">Files</a>'
        elif status=="running":
            act = f'<a href="/job/{jid}/logs" class="btn btn-ghost btn-sm" style="border-color:var(--border-hover);color:var(--text-bright)">Watch Live</a>'
        elif status=="partial" and job["output_dir"]:
            act = (f'<button onclick="resumeJob(\'{jid}\')" class="btn btn-ghost btn-sm" '
                   f'style="border-color:#f59e0b;color:#f59e0b" id="resume-{jid}">▶ Resume</button>'
                   f'<a href="/job/{jid}/files" class="btn btn-ghost btn-sm" style="margin-left:4px">Files</a>')
        else:
            act = ""
        err = f'<div style="font-size:11px;color:var(--danger);margin-top:2px">{job["error"][:80]}...</div>' if job["error"] else ""
        rows += f'<div class="history-item"><div><div class="history-title">{_esc(job["title"])}</div><div class="history-type">{tl}{err}</div></div><div><span class="badge {bc}">{status}</span></div><div class="history-date">{dt}</div><div class="history-actions">{act}</div></div>'
    if not rows: rows = '<div class="empty-state"><h3>No history yet</h3></div>'
    resume_js = """<script>
function resumeJob(jobId) {
    const btn = document.getElementById('resume-' + jobId);
    if (btn) { btn.disabled = true; btn.textContent = 'Resuming...'; }
    fetch('/api/resume/' + jobId, {method: 'POST'})
        .then(r => r.json())
        .then(d => {
            if (d.error) { alert('Resume failed: ' + d.error); if (btn) { btn.disabled = false; btn.textContent = '▶ Resume'; } }
            else { window.location.href = '/job/' + jobId + '/logs'; }
        })
        .catch(e => { alert('Resume request failed'); if (btn) { btn.disabled = false; btn.textContent = '▶ Resume'; } });
}
</script>"""
    return layout(f'<h2 class="page-title" style="margin-bottom:24px">{ICON_CLOCK} Pipeline History</h2><div class="card" style="padding:0;overflow:hidden">{rows}</div>{resume_js}', "history")

# ─── FILES ───
@app.route("/files")
@login_required
def files_page():
    dirs = []
    if OUTPUT_DIR.exists():
        for d in sorted(OUTPUT_DIR.iterdir(), reverse=True):
            if d.is_dir():
                fc = sum(1 for _ in d.rglob("*") if _.is_file())
                ts = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
                dirs.append({"name":d.name,"files":fc,"size":f"{ts/1024:.0f} KB" if ts<1048576 else f"{ts/1048576:.1f} MB","mtime":datetime.fromtimestamp(d.stat().st_mtime).strftime("%Y-%m-%d %H:%M")})
    rows = "".join(f'<div class="file-row"><a href="/files/{d["name"]}">{ICON_FOLDER} {d["name"]}</a><span class="file-size">{d["files"]} files &middot; {d["size"]}</span></div>' for d in dirs)
    if not rows: rows = '<div class="empty-state"><h3>No output files yet</h3></div>'
    return layout(f'<h2 class="page-title" style="margin-bottom:24px">{ICON_FOLDER} Output Files</h2><div class="card" style="padding:0;overflow:hidden">{rows}</div>', "files")

@app.route("/files/<path:subpath>")
@login_required
def browse_files(subpath):
    target = OUTPUT_DIR / subpath
    if not target.exists(): return "Not found", 404
    if target.is_file(): return send_from_directory(target.parent, target.name)
    files = [{"path":str(f.relative_to(target)),"url":f"/files/{f.relative_to(OUTPUT_DIR)}","size":f"{f.stat().st_size/1024:.1f} KB"} for f in sorted(target.rglob("*")) if f.is_file()]
    rows = "".join(f'<div class="file-row"><a href="{f["url"]}">{f["path"]}</a><span class="file-size">{f["size"]}</span></div>' for f in files)
    return layout(f'<div style="margin-bottom:20px"><a href="/files" style="color:var(--text-dim);font-size:12px;text-decoration:none">&larr; Back</a></div><h2 style="font-size:18px;font-weight:700;color:var(--text-bright);margin-bottom:16px">{subpath}</h2><div class="card" style="padding:0;overflow:hidden">{rows}</div>', "files")

# ─── JOB FILES ───
@app.route("/job/<job_id>/files")
@login_required
def job_files(job_id):
    db = get_db(); job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job or not job["output_dir"]: return "Not found", 404
    op = Path(job["output_dir"])
    if not op.exists(): return layout('<div class="card"><p style="color:var(--text-muted)">Output no longer exists.</p></div>')

    # Collect all files
    all_files = sorted(op.rglob("*"))
    files = [{"path":str(f.relative_to(op)),"url":f"/job/{job_id}/dl/{f.relative_to(op)}","size":f"{f.stat().st_size/1024:.1f} KB","ext":f.suffix.lower()} for f in all_files if f.is_file()]

    # Prototype section
    proto_html = ""
    proto_files = [f for f in files if f["path"].startswith("07_prototype") and f["ext"] == ".html"]
    if proto_files:
        proto_html = f'''<div class="card"><h2>🎮 Playable Prototype</h2>
            <iframe src="{proto_files[0]['url']}" class="proto-frame" title="Game Prototype"></iframe>
            <div style="margin-top:8px;text-align:center"><a href="{proto_files[0]['url']}" target="_blank" class="btn btn-ghost btn-sm">Open in new tab ↗</a></div></div>'''

    # Audio section
    audio_html = ""
    audio_files = [f for f in files if f["path"].startswith("04_audio") and f["ext"] in (".mp3", ".wav")]
    if audio_files:
        audio_rows = ""
        for af in audio_files:
            name = Path(af["path"]).stem
            audio_rows += f'<div class="audio-player"><span class="audio-name">{name}</span><audio controls preload="none" src="{af["url"]}"></audio><span class="file-size">{af["size"]}</span></div>'
        audio_html = f'<div class="card"><h2>🔊 AI Sound Design ({len(audio_files)} sounds)</h2><div style="max-height:400px;overflow-y:auto">{audio_rows}</div></div>'

    # Cert plan section
    cert_html = ""
    cert_file = op / "05_legal" / "certification_plan.json"
    if cert_file.exists():
        try:
            cert = json.loads(cert_file.read_text())
            markets = list(cert.get("per_market", {}).keys())
            timeline = cert.get("total_timeline", {})
            cost = cert.get("total_cost", {})
            lab = cert.get("recommended_lab", {})
            flags = cert.get("critical_flags", [])

            flags_html = "".join(f'<div style="padding:6px 10px;background:#ef444415;border-radius:6px;font-size:12px;color:var(--danger);margin-bottom:4px">⚠️ {fl}</div>' for fl in flags)

            cert_html = f'''<div class="card"><h2>📋 Certification Plan</h2>
                <div class="row3" style="margin-bottom:16px">
                    <div><label>Recommended Lab</label><div style="font-size:16px;font-weight:600;color:var(--text-bright)">{lab.get("name","TBD")}</div><div style="font-size:11px;color:var(--text-muted)">Covers {lab.get("covers_markets",0)}/{len(markets)} markets</div></div>
                    <div><label>Timeline (Parallel)</label><div style="font-size:16px;font-weight:700;color:var(--text-bright)">{timeline.get("parallel_testing_weeks","?")} weeks</div><div style="font-size:11px;color:var(--text-muted)">vs {timeline.get("sequential_testing_weeks","?")}w sequential</div></div>
                    <div><label>Total Cost Estimate</label><div style="font-size:16px;font-weight:700;color:var(--warning)">{cost.get("estimated_range","TBD")}</div></div>
                </div>
                {flags_html}
                <div style="margin-top:12px"><a href="/job/{job_id}/dl/05_legal/certification_plan.json" class="btn btn-ghost btn-sm">Download full plan JSON ↓</a></div></div>'''
        except Exception as e:
            logger.debug(f"Cert plan card: {e}")

    # Patent scan section
    patent_html = ""
    patent_file = op / "00_preflight" / "patent_scan.json"
    if patent_file.exists():
        try:
            pscan = json.loads(patent_file.read_text())
            risk = pscan.get("risk_assessment", {})
            risk_level = risk.get("overall_ip_risk", "UNKNOWN")
            risk_color = {"HIGH":"var(--danger)","MEDIUM":"var(--warning)","LOW":"var(--success)"}.get(risk_level, "var(--text-muted)")
            hits = pscan.get("known_patent_hits", [])
            hits_rows = []
            for h in hits:
                risk_str = h.get("risk", "")
                rc = "var(--danger)" if risk_str.startswith("HIGH") else ("var(--warning)" if "MEDIUM" in risk_str else "var(--text-muted)")
                hits_rows.append(f'<div style="padding:6px 10px;background:var(--bg-input);border-radius:6px;font-size:12px;margin-bottom:4px"><b>{h.get("mechanic","")}</b> — {h.get("holder","")} <span style="color:{rc}">({risk_str})</span></div>')
            hits_html = "".join(hits_rows)

            patent_html = f'''<div class="card"><h2>🔒 Patent/IP Scan</h2>
                <div style="margin-bottom:12px"><span style="font-size:16px;font-weight:700;color:{risk_color}">{risk_level} RISK</span>
                <span style="font-size:12px;color:var(--text-muted);margin-left:8px">{risk.get("patent_conflicts",0)} conflicts, {risk.get("trademark_similar_names",0)} trademark matches</span></div>
                {hits_html if hits_html else '<div style="font-size:12px;color:var(--success)">No known patent conflicts detected.</div>'}
            </div>'''
        except Exception as e:
            logger.debug(f"Patent scan card: {e}")

    # Revenue projection card (Phase 5)
    revenue_html = ""
    rev_file = op / "08_revenue" / "revenue_projection.json"

    # Geographic Market Research card (Phase 3)
    geo_html = ""
    geo_file = op / "01_research" / "geo_research.json"
    if geo_file.exists():
        try:
            geo = json.loads(geo_file.read_text())
            state = geo.get("state", "")
            sp = geo.get("state_profile", {})
            legal = sp.get("legal_status", "unknown").replace("_", " ").title()
            ggr = sp.get("annual_ggr_billions", 0)
            regions = geo.get("ranked_regions", [])
            top = geo.get("top_recommendation")

            reg_rows = ""
            for r in regions[:4]:
                score = r.get("composite_score", 0)
                sc = "var(--success)" if score >= 70 else ("var(--warning)" if score >= 40 else "var(--text-muted)")
                density = r.get("casino_density", "—").replace("_", " ").title()
                reg_rows += f'''<div style="display:flex;align-items:center;justify-content:space-between;padding:8px 10px;background:var(--bg-input);border-radius:6px;margin-bottom:4px;font-size:12px">
                    <div style="display:flex;align-items:center;gap:8px">
                        <span style="font-size:14px;font-weight:700;color:{sc};min-width:28px">{r.get("rank","")}</span>
                        <div><div style="font-weight:600;color:var(--text-bright)">{r.get("region","")}</div>
                        <div style="font-size:10px;color:var(--text-muted)">{r.get("pop",0):,} pop · {density} density</div></div>
                    </div>
                    <span style="font-weight:700;color:{sc}">{score}/100</span>
                </div>'''

            top_reason = _esc(top.get("placement_rationale", "")) if top else ""

            geo_html = f'''<div class="card"><h2>&#128205; Geographic Market Analysis</h2>
                <div class="row3" style="margin-bottom:16px">
                    <div><label>State</label><div style="font-size:16px;font-weight:700;color:var(--text-bright)">{_esc(state)}</div></div>
                    <div><label>Legal Status</label><div style="font-size:14px;font-weight:600;color:var(--text-bright)">{_esc(legal)}</div></div>
                    <div><label>Annual GGR</label><div style="font-size:16px;font-weight:700;color:var(--warning)">${ggr:.1f}B</div></div>
                </div>
                <label style="margin-bottom:6px;display:block;font-size:11px">Top Regions by Composite Score</label>
                {reg_rows}
                {f'<div style="margin-top:10px;font-size:12px;color:var(--text-muted);line-height:1.5"><b>Top pick:</b> {top_reason}</div>' if top_reason else ""}
                <div style="margin-top:12px"><a href="/job/{job_id}/dl/01_research/geo_research.json" class="btn btn-ghost btn-sm">Download full report JSON ↓</a></div></div>'''
        except Exception as e:
            logger.debug(f"Geo research card: {e}")
    # Also check for multiple geo files (multiple states)
    if not geo_html:
        geo_files = list((op / "01_research").glob("geo_*.json")) if (op / "01_research").exists() else []
        if geo_files:
            try:
                cards = ""
                for gf in geo_files[:3]:
                    geo = json.loads(gf.read_text())
                    top = geo.get("top_recommendation")
                    if top:
                        cards += f'''<div style="padding:8px 12px;background:var(--bg-input);border-radius:6px;margin-bottom:4px;font-size:12px">
                            <b>{geo.get("state","")}</b>: {top.get("region","")} — score {top.get("composite_score",0)}/100
                            <span style="color:var(--text-dim)">({top.get("casino_density","").replace("_"," ")})</span></div>'''
                if cards:
                    geo_html = f'<div class="card"><h2>&#128205; Geographic Market Analysis</h2>{cards}</div>'
            except Exception as e:
                logger.debug(f"Multi-geo card: {e}")
    if rev_file.exists():
        try:
            rev = json.loads(rev_file.read_text())
            ggr_365 = rev.get("ggr_365d", 0)
            ggr_90 = rev.get("ggr_90d", 0)
            arpdau = rev.get("arpdau", 0)
            be_days = rev.get("break_even_days", "?")
            roi = rev.get("roi_365d", 0)
            hold = rev.get("hold_pct", 0)
            cannibal = rev.get("cannibalization_risk", "?")
            cannibal_c = {"low":"var(--success)","medium":"var(--warning)","high":"var(--danger)"}.get(cannibal, "var(--text-muted)")
            roi_c = "var(--success)" if roi > 0 else "var(--danger)"

            # Mini monthly chart using CSS bars
            monthly = rev.get("ggr_monthly", [])
            max_ggr = max((m.get("ggr", 0) for m in monthly), default=1) or 1
            bars = ""
            for m in monthly[:12]:
                pct = min(100, int(m.get("ggr", 0) / max_ggr * 100))
                bars += f'<div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:2px"><div style="width:100%;height:{pct}px;max-height:60px;background:linear-gradient(to top,rgba(255,255,255,0.05),rgba(255,255,255,0.15));border-radius:3px 3px 0 0"></div><span style="font-size:9px;color:var(--text-dim)">{m.get("month","")}</span></div>'

            # Market breakdown (top 3)
            mkt_rows = ""
            for mk in rev.get("market_breakdown", [])[:3]:
                mkt_rows += f'<div style="display:flex;justify-content:space-between;padding:4px 0;font-size:12px"><span style="color:var(--text-muted)">{mk.get("market","").upper()}</span><span style="color:var(--text-bright);font-family:var(--mono)">${mk.get("ggr_365d",0):,.0f}</span></div>'

            revenue_html = f'''<div class="card"><h2>&#128176; Revenue Projection</h2>
                <div class="row3" style="margin-bottom:16px">
                    <div><label>Annual GGR (365d)</label><div style="font-size:20px;font-weight:700;color:var(--text-bright)">${ggr_365:,.0f}</div></div>
                    <div><label>ARPDAU</label><div style="font-size:20px;font-weight:700;color:var(--text-bright)">${arpdau:.2f}</div></div>
                    <div><label>Hold %</label><div style="font-size:20px;font-weight:700;color:var(--text-bright)">{hold}%</div></div>
                </div>
                <div class="row3" style="margin-bottom:16px">
                    <div><label>Break-Even</label><div style="font-size:16px;font-weight:600;color:var(--warning)">{be_days} days</div></div>
                    <div><label>1-Year ROI</label><div style="font-size:16px;font-weight:600;color:{roi_c}">{roi:+.1f}%</div></div>
                    <div><label>Cannibalization</label><div style="font-size:16px;font-weight:600;color:{cannibal_c}">{cannibal.upper()}</div></div>
                </div>
                <div style="margin-bottom:16px"><label style="margin-bottom:8px;display:block">Monthly GGR Projection</label>
                    <div style="display:flex;gap:2px;align-items:flex-end;height:75px;padding:8px 0">{bars}</div></div>
                <div style="margin-bottom:12px"><label style="margin-bottom:6px;display:block">Top Markets</label>{mkt_rows}</div>
                <a href="/job/{job_id}/revenue" class="btn btn-ghost btn-sm" style="margin-top:4px">View full dashboard &rarr;</a></div>'''
        except Exception as e:
            logger.debug(f"Revenue card: {e}")

    # Engine export card (Phase 6)
    export_html = ""
    export_dir = op / "09_export" if op else None
    has_exports = export_dir and export_dir.exists() and any(export_dir.glob("*.zip"))
    if job["status"] == "complete":
        existing_zips = ""
        if has_exports:
            for zf in sorted(export_dir.glob("*.zip")):
                size_kb = zf.stat().st_size / 1024
                label = "Unity" if "unity" in zf.name else ("Godot" if "godot" in zf.name else "Generic")
                icon = {"Unity": "&#9898;", "Godot": "&#128430;", "Generic": "&#128230;"}.get(label, "&#128230;")
                existing_zips += f'<a href="/job/{job_id}/dl/09_export/{zf.name}" class="btn btn-ghost btn-sm" style="margin-right:8px;margin-bottom:6px">{icon} {label} ({size_kb:.0f} KB) &darr;</a>'

        export_html = f'''<div class="card"><h2>&#127918; Engine Export</h2>
            <p style="font-size:12px;color:var(--text-muted);margin-bottom:12px">Download engine-ready asset packages with structured data, sprites, audio, and auto-generated code.</p>
            <div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px">
                <a href="/api/job/{job_id}/export?format=unity" class="btn btn-primary" style="font-size:12px;padding:8px 16px">&#9898; Unity Package</a>
                <a href="/api/job/{job_id}/export?format=godot" class="btn btn-primary" style="font-size:12px;padding:8px 16px">&#128430; Godot Package</a>
                <a href="/api/job/{job_id}/export?format=generic" class="btn btn-ghost" style="font-size:12px;padding:8px 16px">&#128230; Generic JSON</a>
            </div>
            {"<div style='margin-top:8px'><label style='font-size:11px;margin-bottom:4px;display:block'>Cached exports:</label>" + existing_zips + "</div>" if existing_zips else ""}
            <p style="font-size:10px;color:var(--text-dim);margin-top:8px">Includes: config.json, paytable.json, reelstrips.json, features.json, sprites, audio, {"SlotConfig.cs" if True else ""} + README</p></div>'''

    # Regular file list
    rows = "".join(f'<div class="file-row"><a href="{f["url"]}">{f["path"]}</a><span class="file-size">{f["size"]}</span></div>' for f in files)

    # Iterate button (only for completed jobs)
    iterate_btn = ""
    if job["status"] == "complete":
        iterate_btn = f'<a href="/job/{job_id}/iterate" class="btn btn-primary" style="font-size:13px;padding:8px 20px;margin-left:12px">🔄 Iterate</a>'
    # Variants button for variant_parent or variant jobs
    variants_btn = ""
    parent_for_variants = _rget(job, "parent_job_id") or job_id
    db_v = get_db()
    has_variants = db_v.execute("SELECT COUNT(*) as c FROM jobs WHERE parent_job_id=? AND job_type='variant'", (parent_for_variants,)).fetchone()["c"]
    if has_variants > 0:
        variants_btn = f'<a href="/job/{parent_for_variants}/variants" class="btn btn-ghost" style="font-size:13px;padding:8px 20px;margin-left:8px">🔀 Variants ({has_variants})</a>'

    # Version history + compare selector
    version_html = ""
    compare_html = ""
    db2 = get_db()
    root_id = _rget(job, "parent_job_id") or job_id
    versions = db2.execute("SELECT id,version,status,created_at FROM jobs WHERE id=? OR parent_job_id=? OR id=? ORDER BY version", (root_id, root_id, job_id)).fetchall()
    if len(versions) > 1:
        vrows = ""
        compare_opts = ""
        for v in versions:
            active = " style='color:var(--text-bright);font-weight:600'" if v["id"] == job_id else ""
            sc = {"complete":"var(--success)","running":"var(--warning)","failed":"var(--danger)"}.get(v["status"],"var(--text-dim)")
            vrows += f'<a href="/job/{v["id"]}/files"{active}>v{v["version"] or 1} <span style="color:{sc};font-size:11px">{v["status"]}</span></a> '
            if v["id"] != job_id and v["status"] == "complete":
                compare_opts += f'<option value="{v["id"]}">v{v["version"] or 1}</option>'
        version_html = f'<div style="margin-bottom:12px;font-size:12px;color:var(--text-muted)">Versions: {vrows}</div>'
        if compare_opts:
            compare_html = f'''<div style="display:inline-flex;align-items:center;gap:6px;margin-left:12px">
                <select id="cmpSel" style="font-size:11px;padding:4px 8px;background:var(--bg-card);color:var(--text);border:1px solid var(--border);border-radius:6px">{compare_opts}</select>
                <button onclick="location.href='/job/{job_id}/diff/'+document.getElementById('cmpSel').value" class="btn btn-ghost" style="font-size:11px;padding:4px 12px">Compare ↔</button></div>'''

    return layout(f'''<div style="margin-bottom:20px"><a href="/history" style="color:var(--text-dim);font-size:12px;text-decoration:none">&larr; Back to History</a></div>
    <div style="display:flex;align-items:center;margin-bottom:4px"><h2 style="font-size:18px;font-weight:700;color:var(--text-bright)">{_esc(job["title"])}</h2>{iterate_btn}{variants_btn}{compare_html}</div>
    <p style="color:var(--text-muted);font-size:12px;margin-bottom:4px">{len(files)} files generated · v{_rget(job, "version") or 1}</p>
    {version_html}
    {proto_html}{audio_html}{patent_html}{cert_html}{geo_html}{revenue_html}{export_html}
    <div class="card" style="padding:0;overflow:hidden"><div style="padding:16px 16px 8px"><h2>📁 All Files</h2></div>{rows}</div>''', "history")

@app.route("/job/<job_id>/dl/<path:fp>")
@login_required
def job_dl(job_id, fp):
    db = get_db(); job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job or not job["output_dir"]: return "Not found", 404
    return send_from_directory(Path(job["output_dir"]), fp)


# ─── ITERATE: Selective Re-Run + Parameter Tweaker (Phase 3A-3B) ───

@app.route("/job/<job_id>/iterate")
@login_required
def job_iterate(job_id):
    user = current_user()
    db = get_db()
    job = db.execute("SELECT * FROM jobs WHERE id=? AND user_id=?", (job_id, user["id"])).fetchone()
    if not job: return "Not found", 404
    if job["status"] != "complete": return redirect(f"/job/{job_id}/logs")

    params = json.loads(job["params"]) if job["params"] else {}
    op = Path(job["output_dir"]) if job["output_dir"] else None

    # Read current simulation results for before/after comparison
    sim_data = {}
    if op:
        sim_path = op / "03_math" / "simulation_results.json"
        if sim_path.exists():
            try: sim_data = json.loads(sim_path.read_text())
            except Exception as e: logger.debug(f"Sim data parse: {e}")

    # Read GDD quality audit if exists
    gdd_grade = "—"
    gdd_path = op / "02_design" / "gdd.md" if op else None
    has_gdd = gdd_path and gdd_path.exists()

    # Read convergence history
    conv_data = {}
    if op:
        conv_path = op / "02_design" / "convergence_history.json"
        if conv_path.exists():
            try: conv_data = json.loads(conv_path.read_text())
            except Exception as e: logger.debug(f"Convergence data parse: {e}")

    # Version info
    root_id = _rget(job, "parent_job_id") or job_id
    db2 = get_db()
    current_version = _rget(job, "version") or 1
    version_count = db2.execute("SELECT COUNT(*) as cnt FROM jobs WHERE id=? OR parent_job_id=?", (root_id, root_id)).fetchone()["cnt"]
    next_version = version_count + 1

    # Current params display
    cur_rtp = params.get("target_rtp", 96.0)
    cur_max_win = params.get("max_win_multiplier", 5000)
    cur_vol = params.get("volatility", "medium")
    cur_markets = params.get("target_markets", [])
    cur_features = params.get("requested_features", [])
    measured_rtp = sim_data.get("measured_rtp", "—")
    max_win_achieved = sim_data.get("max_win_achieved", "—")
    hit_freq = sim_data.get("hit_frequency_pct", sim_data.get("hit_frequency", "—"))
    vol_idx = sim_data.get("volatility_index", "—")

    # Markets available for multi-select
    all_markets = ["UK","Malta","Sweden","Ontario","New Jersey","Michigan","Pennsylvania","Curaçao","Isle of Man","Gibraltar","Georgia","Texas","North Carolina","Florida"]
    market_options = ""
    for m in all_markets:
        checked = "checked" if m.lower() in [x.lower() for x in cur_markets] else ""
        market_options += f'<label class="iter-check"><input type="checkbox" name="target_markets" value="{m}" {checked}><span>{m}</span></label>'

    # Feature options
    all_features = ["free_spins","multipliers","expanding_wilds","cascading_reels","hold_and_spin","bonus_buy","scatter_pays","jackpot_progressive","cluster_pays","megaways"]
    feature_options = ""
    for f in all_features:
        checked = "checked" if f in cur_features else ""
        label = f.replace("_"," ").title()
        feature_options += f'<label class="iter-check"><input type="checkbox" name="features" value="{f}" {checked}><span>{label}</span></label>'

    return layout(f'''
    <div style="margin-bottom:20px"><a href="/job/{job_id}/files" style="color:var(--text-dim);font-size:12px;text-decoration:none">&larr; Back to {_esc(job["title"])}</a></div>
    <h2 class="page-title" style="margin-bottom:4px">🔄 Iterate — {_esc(job["title"])}</h2>
    <p style="color:var(--text-muted);font-size:12px;margin-bottom:24px">v{current_version} → v{next_version} · Re-run selected stages with new parameters</p>

    <form method="POST" action="/api/iterate" id="iterateForm">
    <input type="hidden" name="parent_job_id" value="{root_id}">
    <input type="hidden" name="source_job_id" value="{job_id}">
    <input type="hidden" name="source_output_dir" value="{job['output_dir'] or ''}">
    <input type="hidden" name="theme" value="{params.get('theme','')}">
    <input type="hidden" name="art_style" value="{params.get('art_style','')}">
    <input type="hidden" name="grid_cols" value="{params.get('grid_cols',5)}">
    <input type="hidden" name="grid_rows" value="{params.get('grid_rows',3)}">
    <input type="hidden" name="ways_or_lines" value="{params.get('ways_or_lines','243')}">

    <!-- Current Results -->
    <div class="card" style="margin-bottom:16px">
        <h2 style="font-size:15px;font-weight:600;margin-bottom:12px">Current Results (v{current_version})</h2>
        <div class="row4">
            <div><label style="font-size:11px;color:var(--text-muted)">Measured RTP</label><div style="font-size:20px;font-weight:700;color:var(--text-bright)">{measured_rtp}{'%' if isinstance(measured_rtp,(int,float)) else ''}</div></div>
            <div><label style="font-size:11px;color:var(--text-muted)">Max Win Achieved</label><div style="font-size:20px;font-weight:700;color:var(--text-bright)">{max_win_achieved}{'x' if isinstance(max_win_achieved,(int,float)) else ''}</div></div>
            <div><label style="font-size:11px;color:var(--text-muted)">Hit Frequency</label><div style="font-size:20px;font-weight:700;color:var(--text-bright)">{hit_freq}{'%' if isinstance(hit_freq,(int,float)) else ''}</div></div>
            <div><label style="font-size:11px;color:var(--text-muted)">Volatility Index</label><div style="font-size:20px;font-weight:700;color:var(--text-bright)">{vol_idx}</div></div>
        </div>
    </div>

    <!-- Parameter Tweaker (Phase 3B) -->
    <div class="card" style="margin-bottom:16px">
        <h2 style="font-size:15px;font-weight:600;margin-bottom:16px">⚙️ Parameter Tweaker</h2>

        <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px">
            <div>
                <label style="font-size:12px;color:var(--text-muted);display:block;margin-bottom:4px">Target RTP</label>
                <div style="display:flex;align-items:center;gap:10px">
                    <input type="range" name="target_rtp" min="85" max="99" step="0.1" value="{cur_rtp}"
                           oninput="this.nextElementSibling.textContent=this.value+'%'"
                           style="flex:1;accent-color:var(--text-bright)">
                    <span style="font-family:var(--mono);font-size:14px;font-weight:600;color:var(--text-bright);min-width:50px">{cur_rtp}%</span>
                </div>
            </div>
            <div>
                <label style="font-size:12px;color:var(--text-muted);display:block;margin-bottom:4px">Max Win Multiplier</label>
                <div style="display:flex;align-items:center;gap:10px">
                    <input type="range" name="max_win_multiplier" min="1000" max="50000" step="500" value="{cur_max_win}"
                           oninput="this.nextElementSibling.textContent=this.value+'x'"
                           style="flex:1;accent-color:var(--text-bright)">
                    <span style="font-family:var(--mono);font-size:14px;font-weight:600;color:var(--text-bright);min-width:60px">{cur_max_win}x</span>
                </div>
            </div>
        </div>

        <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
            <div>
                <label style="font-size:12px;color:var(--text-muted);display:block;margin-bottom:4px">Volatility</label>
                <select name="volatility" class="input-field" style="height:38px">
                    <option value="low" {"selected" if cur_vol=="low" else ""}>Low</option>
                    <option value="medium" {"selected" if cur_vol=="medium" else ""}>Medium</option>
                    <option value="medium_high" {"selected" if cur_vol=="medium_high" else ""}>Medium-High</option>
                    <option value="high" {"selected" if cur_vol=="high" else ""}>High</option>
                    <option value="extreme" {"selected" if cur_vol=="extreme" else ""}>Extreme</option>
                </select>
            </div>
            <div>
                <label style="font-size:12px;color:var(--text-muted);display:block;margin-bottom:4px">Special Requirements</label>
                <input type="text" name="special_requirements" value="{params.get('special_requirements','')}" class="input-field" placeholder="e.g. reduce free spin frequency">
            </div>
        </div>
    </div>

    <!-- Target Markets -->
    <div class="card" style="margin-bottom:16px">
        <h2 style="font-size:15px;font-weight:600;margin-bottom:12px">🌍 Target Markets</h2>
        <div style="display:flex;flex-wrap:wrap;gap:6px">{market_options}</div>
    </div>

    <!-- Features -->
    <div class="card" style="margin-bottom:16px">
        <h2 style="font-size:15px;font-weight:600;margin-bottom:12px">🎰 Features</h2>
        <div style="display:flex;flex-wrap:wrap;gap:6px">{feature_options}</div>
    </div>

    <!-- Selective Re-Run (Phase 3A) -->
    <div class="card" style="margin-bottom:16px">
        <h2 style="font-size:15px;font-weight:600;margin-bottom:12px">🔄 What to Re-Run</h2>
        <p style="font-size:12px;color:var(--text-muted);margin-bottom:12px">Select which stages to regenerate. Unselected stages keep their current output.</p>
        <div style="display:grid;gap:8px">
            <label class="iter-stage"><input type="checkbox" name="rerun_stages" value="math" checked><div><span style="font-weight:600">Math Model</span><span style="font-size:11px;color:var(--text-muted);display:block">Re-run Monte Carlo simulation with new parameters. Generates new reel strips, paytable, and sim results.</span></div></label>
            <label class="iter-stage"><input type="checkbox" name="rerun_stages" value="gdd"><div><span style="font-weight:600">GDD Patch</span><span style="font-size:11px;color:var(--text-muted);display:block">Update affected GDD sections to match new parameters (RTP budget, feature specs, volatility description).</span></div></label>
            <label class="iter-stage"><input type="checkbox" name="rerun_stages" value="art"><div><span style="font-weight:600">Art Assets</span><span style="font-size:11px;color:var(--text-muted);display:block">Regenerate all symbol images, backgrounds, and logo. Keep everything else.</span></div></label>
            <label class="iter-stage"><input type="checkbox" name="rerun_stages" value="compliance"><div><span style="font-weight:600">Compliance Review</span><span style="font-size:11px;color:var(--text-muted);display:block">Re-check regulations for changed markets or parameters. Generates new compliance report.</span></div></label>
            <label class="iter-stage"><input type="checkbox" name="rerun_stages" value="convergence"><div><span style="font-weight:600">Convergence Loop</span><span style="font-size:11px;color:var(--text-muted);display:block">Run full OODA convergence check to validate GDD ↔ Math ↔ Compliance alignment.</span></div></label>
        </div>
    </div>

    <!-- Submit -->
    <div style="display:flex;justify-content:flex-end;gap:12px;margin-bottom:40px">
        <a href="/job/{job_id}/files" class="btn btn-ghost">Cancel</a>
        <button type="submit" class="btn btn-primary" style="padding:10px 32px">🚀 Launch v{next_version}</button>
    </div>
    </form>

    <style>
        .row4 {{ display:grid; grid-template-columns:repeat(4,1fr); gap:16px }}
        .iter-check {{ display:inline-flex; align-items:center; gap:4px; padding:4px 10px; border:1px solid var(--border); border-radius:6px; cursor:pointer; font-size:12px; transition:border-color .15s }}
        .iter-check:has(input:checked) {{ border-color:var(--text-bright); background:rgba(255,255,255,0.04) }}
        .iter-check input {{ accent-color:var(--text-bright) }}
        .iter-stage {{ display:flex; align-items:flex-start; gap:10px; padding:10px 14px; border:1px solid var(--border); border-radius:8px; cursor:pointer; transition:border-color .15s }}
        .iter-stage:has(input:checked) {{ border-color:var(--text-bright); background:rgba(255,255,255,0.03) }}
        .iter-stage input {{ margin-top:3px; accent-color:var(--text-bright) }}
        input[type="range"] {{ height:4px; background:var(--border); border-radius:2px; -webkit-appearance:none; appearance:none }}
        input[type="range"]::-webkit-slider-thumb {{ -webkit-appearance:none; width:16px; height:16px; border-radius:50%; background:var(--text-bright); cursor:pointer }}
        @media(max-width:768px) {{ .row4 {{ grid-template-columns:repeat(2,1fr) }} }}
    </style>
    ''', "history")


@app.route("/api/iterate", methods=["POST"])
@login_required
def api_iterate():
    user = current_user()
    parent_id = request.form["parent_job_id"]
    source_id = request.form["source_job_id"]
    source_output = request.form["source_output_dir"]

    # Get next version number
    db = get_db()
    version_count = db.execute("SELECT COUNT(*) as cnt FROM jobs WHERE id=? OR parent_job_id=?", (parent_id, parent_id)).fetchone()["cnt"]
    next_version = version_count + 1

    # Build iteration params
    params = {
        "theme": request.form["theme"],
        "target_markets": request.form.getlist("target_markets"),
        "volatility": request.form.get("volatility", "medium"),
        "target_rtp": float(request.form.get("target_rtp", 96)),
        "grid_cols": int(request.form.get("grid_cols", 5)),
        "grid_rows": int(request.form.get("grid_rows", 3)),
        "ways_or_lines": request.form.get("ways_or_lines", "243"),
        "max_win_multiplier": int(request.form.get("max_win_multiplier", 5000)),
        "art_style": request.form.get("art_style", "Cinematic realism"),
        "requested_features": request.form.getlist("features"),
        "special_requirements": request.form.get("special_requirements", ""),
    }

    iterate_config = {
        "source_job_id": source_id,
        "source_output_dir": source_output,
        "rerun_stages": request.form.getlist("rerun_stages"),
        "parent_job_id": parent_id,
        "version": next_version,
    }

    job_id = str(uuid.uuid4())[:8]
    db.execute(
        "INSERT INTO jobs (id, user_id, job_type, title, params, status, parent_job_id, version) VALUES (?,?,?,?,?,?,?,?)",
        (job_id, user["id"], "iterate", f"{params['theme']} v{next_version}",
         json.dumps({**params, "_iterate": iterate_config}), "queued", parent_id, next_version)
    )
    db.commit()

    _spawn_worker(job_id, "iterate", json.dumps({**params, "_iterate": iterate_config}))
    return redirect(f"/job/{job_id}/logs")


def _load_job_metrics(output_dir):
    """Load key metrics from a job output dir for comparison."""
    od = Path(output_dir) if output_dir else None
    data = {"rtp":"—","max_win":"—","hit_freq":"—","vol_idx":"—","gdd_words":0,"symbols":0,"compliance":"—","gdd_sections":[],"rtp_breakdown":{},"ggr_365d":"—","arpdau":"—","roi_365d":"—","break_even_days":"—"}
    if not od or not od.exists(): return data
    sim_path = od / "03_math" / "simulation_results.json"
    if sim_path.exists():
        try:
            sim = json.loads(sim_path.read_text())
            data["rtp"]=sim.get("measured_rtp","—"); data["max_win"]=sim.get("max_win_achieved","—")
            data["hit_freq"]=sim.get("hit_frequency_pct",sim.get("hit_frequency","—")); data["vol_idx"]=sim.get("volatility_index","—")
            data["rtp_breakdown"]=sim.get("rtp_breakdown",{})
        except Exception as e: logger.debug(f"Sim metrics: {e}")
    pt_path = od / "03_math" / "paytable.csv"
    if pt_path.exists():
        try:
            import csv as _csv, io as _io
            data["symbols"] = max(0, sum(1 for _ in _csv.reader(_io.StringIO(pt_path.read_text()))) - 1)
        except Exception as e: logger.debug(f"Paytable parse: {e}")
    gdd_path = od / "02_design" / "gdd.md"
    if gdd_path.exists():
        try:
            gdd_text = gdd_path.read_text(encoding="utf-8", errors="replace")
            data["gdd_words"] = len(gdd_text.split())
            import re as _re; data["gdd_sections"] = _re.findall(r'^## .+', gdd_text, _re.MULTILINE)
        except Exception as e: logger.debug(f"GDD parse: {e}")
    comp_path = od / "05_legal" / "compliance_report.json"
    if comp_path.exists():
        try: data["compliance"] = json.loads(comp_path.read_text()).get("overall_status","—")
        except Exception as e: logger.debug(f"Compliance parse: {e}")
    rev_path = od / "08_revenue" / "revenue_projection.json"
    if rev_path.exists():
        try:
            rv = json.loads(rev_path.read_text())
            data["ggr_365d"] = rv.get("ggr_365d", "—")
            data["arpdau"] = rv.get("arpdau", "—")
            data["roi_365d"] = rv.get("roi_365d", "—")
            data["break_even_days"] = rv.get("break_even_days", "—")
        except Exception as e: logger.debug(f"Revenue parse: {e}")
    return data


@app.route("/job/<job_id>/diff/<other_id>")
@login_required
def job_diff(job_id, other_id):
    user = current_user(); db = get_db()
    job_a = db.execute("SELECT * FROM jobs WHERE id=? AND user_id=?", (job_id, user["id"])).fetchone()
    job_b = db.execute("SELECT * FROM jobs WHERE id=? AND user_id=?", (other_id, user["id"])).fetchone()
    if not job_a or not job_b: return "Not found", 404
    a = _load_job_metrics(job_a["output_dir"]); b = _load_job_metrics(job_b["output_dir"])
    va = _rget(job_a, "version") or 1; vb = _rget(job_b, "version") or 1

    def _dc(label, val_a, val_b, fmt="", hib=None):
        sa = f"{val_a}{fmt}" if isinstance(val_a,(int,float)) else str(val_a)
        sb = f"{val_b}{fmt}" if isinstance(val_b,(int,float)) else str(val_b)
        delta = ""
        if isinstance(val_a,(int,float)) and isinstance(val_b,(int,float)):
            d = val_b - val_a; sign = "+" if d > 0 else ""
            color = "var(--text-muted)"
            if hib is True: color = "var(--success)" if d > 0 else ("var(--danger)" if d < 0 else color)
            elif hib is False: color = "var(--danger)" if d > 0 else ("var(--success)" if d < 0 else color)
            delta = f'<span style="font-size:11px;color:{color};margin-left:4px">{sign}{d:.2f}{fmt}</span>' if d != 0 else ""
        return f'<tr><td style="font-size:12px;color:var(--text-muted);padding:6px 0">{label}</td><td style="font-family:var(--mono);font-size:13px;padding:6px 12px">{sa}</td><td style="font-family:var(--mono);font-size:13px;font-weight:600;padding:6px 12px">{sb}{delta}</td></tr>'

    rows = _dc("Measured RTP",a["rtp"],b["rtp"],"%") + _dc("Max Win",a["max_win"],b["max_win"],"x") + _dc("Hit Frequency",a["hit_freq"],b["hit_freq"],"%",True) + _dc("Volatility Index",a["vol_idx"],b["vol_idx"],"") + _dc("Symbols",a["symbols"],b["symbols"],"") + _dc("GDD Words",a["gdd_words"],b["gdd_words"],"",True) + _dc("Compliance",a["compliance"],b["compliance"],"") + _dc("Annual GGR",a.get("ggr_365d","—"),b.get("ggr_365d","—"),"",True) + _dc("ARPDAU",a.get("arpdau","—"),b.get("arpdau","—"),"",True) + _dc("1Y ROI",a.get("roi_365d","—"),b.get("roi_365d","—"),"%",True) + _dc("Break-Even",a.get("break_even_days","—"),b.get("break_even_days","—")," days",False)

    rtp_a = a.get("rtp_breakdown",{}); rtp_b = b.get("rtp_breakdown",{})
    rtp_rows = "".join(_dc(k.replace("_"," ").title(), rtp_a.get(k,0), rtp_b.get(k,0), "%") for k in sorted(set(list(rtp_a)+list(rtp_b))) if isinstance(rtp_a.get(k,0),(int,float)) and isinstance(rtp_b.get(k,0),(int,float)))
    rtp_sec = f'<div class="card" style="margin-top:16px"><h2 style="font-size:15px;font-weight:600;margin-bottom:12px">RTP Breakdown</h2><table style="width:100%;border-collapse:collapse"><tr><th></th><th style="font-size:11px;color:var(--text-muted);text-align:left;padding:4px 12px">v{va}</th><th style="font-size:11px;color:var(--text-muted);text-align:left;padding:4px 12px">v{vb}</th></tr>{rtp_rows}</table></div>' if rtp_rows else ""

    secs_a = set(a.get("gdd_sections",[])); secs_b = set(b.get("gdd_sections",[]))
    gdd_diff = ""
    if (secs_b - secs_a) or (secs_a - secs_b):
        items = "".join(f'<div style="color:var(--success);font-size:12px">+ {s}</div>' for s in secs_b - secs_a)
        items += "".join(f'<div style="color:var(--danger);font-size:12px">- {s}</div>' for s in secs_a - secs_b)
        gdd_diff = f'<div class="card" style="margin-top:16px"><h2 style="font-size:15px;font-weight:600;margin-bottom:8px">GDD Section Changes</h2>{items}</div>'

    return layout(f'''<div style="margin-bottom:20px"><a href="/job/{job_id}/files" style="color:var(--text-dim);font-size:12px;text-decoration:none">&larr; Back to v{va}</a></div>
    <h2 class="page-title" style="margin-bottom:4px">&#8596; Version Diff</h2>
    <p style="color:var(--text-muted);font-size:12px;margin-bottom:24px">{_esc(job_a["title"])} — v{va} vs v{vb}</p>
    <div class="card"><h2 style="font-size:15px;font-weight:600;margin-bottom:12px">Key Metrics</h2>
        <table style="width:100%;border-collapse:collapse"><tr><th></th><th style="font-size:11px;color:var(--text-muted);text-align:left;padding:4px 12px">v{va}</th><th style="font-size:11px;color:var(--text-muted);text-align:left;padding:4px 12px">v{vb}</th></tr>{rows}</table></div>
    {rtp_sec}{gdd_diff}
    <div style="display:flex;gap:12px;margin-top:24px;margin-bottom:40px">
        <a href="/job/{job_id}/files" class="btn btn-ghost" style="flex:1;text-align:center">View v{va}</a>
        <a href="/job/{other_id}/files" class="btn btn-ghost" style="flex:1;text-align:center">View v{vb}</a></div>''', "history")


@app.route("/job/<job_id>/variants")
@login_required
def job_variants(job_id):
    user = current_user(); db = get_db()
    parent = db.execute("SELECT * FROM jobs WHERE id=? AND user_id=?", (job_id, user["id"])).fetchone()
    if not parent: return "Not found", 404
    variants = db.execute("SELECT * FROM jobs WHERE parent_job_id=? AND job_type='variant' ORDER BY version", (job_id,)).fetchall()
    if not variants:
        return layout(f'<div class="card"><p style="color:var(--text-muted)">No variants yet.</p><a href="/history" class="btn btn-ghost" style="margin-top:12px">Back</a></div>', "history")

    variant_data = []
    for v in variants:
        m = _load_job_metrics(v["output_dir"]); params = json.loads(v["params"]) if v["params"] else {}
        vc = params.get("_variant", {})
        variant_data.append({"id":v["id"],"status":v["status"],"label":vc.get("label",f"V{_rget(v, 'version','?')}"),"strategy":vc.get("strategy",""),"metrics":m})

    header = '<th style="font-size:11px;color:var(--text-muted);padding:6px 12px;text-align:left">Metric</th>'
    for vd in variant_data:
        sc = {"complete":"var(--success)","running":"var(--warning)","failed":"var(--danger)"}.get(vd["status"],"var(--text-dim)")
        header += f'<th style="font-size:12px;padding:6px 12px;text-align:left"><span style="color:var(--text-bright);font-weight:600">{vd["label"]}</span><br><span style="font-size:10px;color:{sc}">{vd["status"]}</span></th>'

    def _vr(label,key,fmt=""):
        c = f'<td style="font-size:12px;color:var(--text-muted);padding:6px 12px">{label}</td>'
        for vd in variant_data:
            val = vd["metrics"].get(key,"—")
            c += f'<td style="font-family:var(--mono);font-size:13px;padding:6px 12px">{val}{fmt if isinstance(val,(int,float)) else ""}</td>'
        return f"<tr>{c}</tr>"
    trows = _vr("RTP","rtp","%")+_vr("Max Win","max_win","x")+_vr("Hit Freq","hit_freq","%")+_vr("Volatility","vol_idx","")+_vr("Symbols","symbols","")+_vr("GDD Words","gdd_words","")+_vr("Compliance","compliance","")+_vr("Annual GGR","ggr_365d","")+_vr("ARPDAU","arpdau","")+_vr("1Y ROI","roi_365d","%")+_vr("Break-Even","break_even_days"," days")

    strat = "".join(f'<div class="card" style="margin-bottom:8px"><div style="display:flex;justify-content:space-between;align-items:center"><div><h3 style="font-size:14px;font-weight:600;color:var(--text-bright);margin:0">{vd["label"]}</h3><p style="font-size:12px;color:var(--text-muted);margin:4px 0 0">{vd["strategy"]}</p></div><a href="/job/{vd["id"]}/files" class="btn btn-ghost" style="font-size:11px;padding:4px 12px">View &rarr;</a></div></div>' for vd in variant_data)

    return layout(f'''<div style="margin-bottom:20px"><a href="/history" style="color:var(--text-dim);font-size:12px;text-decoration:none">&larr; History</a></div>
    <h2 class="page-title" style="margin-bottom:4px">&#128256; Variant Comparison</h2>
    <p style="color:var(--text-muted);font-size:12px;margin-bottom:24px">{_esc(parent["title"])} — {len(variant_data)} variants</p>
    <div class="card" style="overflow-x:auto"><h2 style="font-size:15px;font-weight:600;margin-bottom:12px">Side-by-Side</h2>
        <table style="width:100%;border-collapse:collapse"><tr>{header}</tr>{trows}</table></div>
    <div style="margin-top:16px">{strat}</div>
    <div style="margin-top:24px;margin-bottom:40px"><a href="/history" class="btn btn-ghost">Back</a></div>''', "history")


@app.route("/api/variants", methods=["POST"])
@login_required
def api_launch_variants():
    user = current_user()
    limit_err = _check_job_limit(user["id"])
    if limit_err: return limit_err
    variant_count = max(2, min(int(request.form.get("variant_count", 3)), 5))
    base_params = {"theme":request.form["theme"],"target_markets":[m.strip() for m in request.form.get("target_markets","Georgia, Texas").split(",")],"volatility":request.form.get("volatility","medium"),"target_rtp":float(request.form.get("target_rtp",96)),"grid_cols":int(request.form.get("grid_cols",5)),"grid_rows":int(request.form.get("grid_rows",3)),"ways_or_lines":request.form.get("ways_or_lines","243"),"max_win_multiplier":int(request.form.get("max_win_multiplier",5000)),"art_style":request.form.get("art_style","Cinematic realism"),"requested_features":request.form.getlist("features"),"competitor_references":[r.strip() for r in request.form.get("competitor_references","").split(",") if r.strip()],"special_requirements":request.form.get("special_requirements",""),"enable_recon":request.form.get("enable_recon")=="on"}

    parent_id = str(uuid.uuid4())[:8]; db = get_db()
    db.execute("INSERT INTO jobs (id,user_id,job_type,title,params,status,current_stage) VALUES (?,?,?,?,?,?,?)",
        (parent_id,user["id"],"variant_parent",f"{base_params['theme']} (variants)",json.dumps(base_params),"running",f"Spawning {variant_count} variants"))
    db.commit()

    STRATEGIES = [
        {"label":"Conservative","strategy":"Lower volatility, proven features, safe theme. High hit freq, steady wins.","vol_adj":-1,"rtp_adj":0.5,"max_win_adj":-0.3},
        {"label":"Aggressive","strategy":"Higher volatility, novel mechanics, bold theme. Max win and feature drama.","vol_adj":1,"rtp_adj":-0.3,"max_win_adj":0.5},
        {"label":"Hybrid","strategy":"Mid-volatility with one novel mechanic. Balance excitement and session length.","vol_adj":0,"rtp_adj":0,"max_win_adj":0},
        {"label":"Premium","strategy":"High RTP, moderate volatility, polished. Target experienced fairness-seekers.","vol_adj":0,"rtp_adj":1.0,"max_win_adj":-0.2},
        {"label":"Jackpot Focus","strategy":"Low base RTP, high jackpot contribution. Dream-big psychology.","vol_adj":1,"rtp_adj":-0.5,"max_win_adj":1.0},
    ]
    VOL_LEVELS = ["low","medium","medium_high","high","extreme"]
    variant_ids = []
    for i in range(variant_count):
        s = STRATEGIES[i % len(STRATEGIES)]; vid = str(uuid.uuid4())[:8]; variant_ids.append(vid)
        vp = {**base_params}
        ci = VOL_LEVELS.index(vp["volatility"]) if vp["volatility"] in VOL_LEVELS else 1
        vp["volatility"] = VOL_LEVELS[max(0,min(len(VOL_LEVELS)-1,ci+s["vol_adj"]))]
        vp["target_rtp"] = round(max(85,min(99,vp["target_rtp"]+s["rtp_adj"])),1)
        vp["max_win_multiplier"] = max(1000,int(vp["max_win_multiplier"]*(1+s["max_win_adj"])))
        vp["special_requirements"] = f"VARIANT STRATEGY: {s['strategy']}\n{vp.get('special_requirements','')}"
        vp["_variant"] = {"label":s["label"],"strategy":s["strategy"],"variant_index":i+1}
        db2 = get_db()
        db2.execute("INSERT INTO jobs (id,user_id,job_type,title,params,status,parent_job_id,version) VALUES (?,?,?,?,?,?,?,?)",
            (vid,user["id"],"variant",f"{base_params['theme']} — {s['label']}",json.dumps(vp),"queued",parent_id,i+1))
        db2.commit()
        _spawn_worker(vid, "pipeline", json.dumps(vp))

    db3 = get_db()
    db3.execute("UPDATE jobs SET params=?,current_stage=? WHERE id=?",
        (json.dumps({**base_params,"_variant_ids":variant_ids}),f"{variant_count} variants running",parent_id))
    db3.commit()
    return redirect(f"/job/{parent_id}/variants")


# ─── REVENUE DASHBOARD (Phase 5B) ───

@app.route("/job/<job_id>/revenue")
@login_required
def job_revenue(job_id):
    user = current_user(); db = get_db()
    job = db.execute("SELECT * FROM jobs WHERE id=? AND user_id=?", (job_id, user["id"])).fetchone()
    if not job: return "Not found", 404
    op = Path(job["output_dir"]) if job["output_dir"] else None
    rev_file = op / "08_revenue" / "revenue_projection.json" if op else None
    if not rev_file or not rev_file.exists():
        return layout(f'<div class="card"><p style="color:var(--text-muted)">No revenue projection available for this job.</p><a href="/job/{job_id}/files" class="btn btn-ghost" style="margin-top:12px">Back</a></div>', "history")

    try:
        rev = json.loads(rev_file.read_text())
    except (json.JSONDecodeError, ValueError, OSError):
        return layout(f'<div class="card"><p style="color:var(--text-muted)">Revenue data is corrupted. Re-run the pipeline to regenerate.</p><a href="/job/{job_id}/files" class="btn btn-ghost" style="margin-top:12px">Back</a></div>', "history")

    # ── Hero metrics ──
    hero = f'''<div class="row3" style="margin-bottom:24px">
        <div class="stat-card"><div class="stat-val" style="font-size:24px">${rev.get("ggr_365d",0):,.0f}</div><div class="stat-label">Annual GGR (365d)</div></div>
        <div class="stat-card"><div class="stat-val" style="font-size:24px">${rev.get("arpdau",0):.2f}</div><div class="stat-label">ARPDAU</div></div>
        <div class="stat-card"><div class="stat-val" style="font-size:24px">{rev.get("hold_pct",0)}%</div><div class="stat-label">Effective Hold</div></div>
    </div>
    <div class="row3" style="margin-bottom:24px">
        <div class="stat-card"><div class="stat-val" style="font-size:20px">{rev.get("break_even_days","?")} days</div><div class="stat-label">Break-Even</div></div>
        <div class="stat-card"><div class="stat-val" style="font-size:20px;color:{"var(--success)" if rev.get("roi_365d",0)>0 else "var(--danger)"}">{rev.get("roi_365d",0):+.1f}%</div><div class="stat-label">1-Year ROI</div></div>
        <div class="stat-card"><div class="stat-val" style="font-size:20px">{rev.get("daily_active_users",0):,}</div><div class="stat-label">Projected DAU</div></div>
    </div>'''

    # ── GGR Period Cards ──
    periods = f'''<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:24px">
        <div style="padding:16px;background:var(--bg-card);border:1px solid var(--border);border-radius:8px;text-align:center">
            <div style="font-size:11px;color:var(--text-muted);margin-bottom:4px">30-Day</div>
            <div style="font-size:18px;font-weight:700;color:var(--text-bright)">${rev.get("ggr_30d",0):,.0f}</div></div>
        <div style="padding:16px;background:var(--bg-card);border:1px solid var(--border);border-radius:8px;text-align:center">
            <div style="font-size:11px;color:var(--text-muted);margin-bottom:4px">90-Day</div>
            <div style="font-size:18px;font-weight:700;color:var(--text-bright)">${rev.get("ggr_90d",0):,.0f}</div></div>
        <div style="padding:16px;background:var(--bg-card);border:1px solid var(--border);border-radius:8px;text-align:center">
            <div style="font-size:11px;color:var(--text-muted);margin-bottom:4px">180-Day</div>
            <div style="font-size:18px;font-weight:700;color:var(--text-bright)">${rev.get("ggr_180d",0):,.0f}</div></div>
        <div style="padding:16px;background:var(--bg-card);border:1px solid var(--border);border-radius:8px;text-align:center">
            <div style="font-size:11px;color:var(--text-muted);margin-bottom:4px">365-Day</div>
            <div style="font-size:18px;font-weight:700;color:var(--text-bright)">${rev.get("ggr_365d",0):,.0f}</div></div>
    </div>'''

    # ── Monthly GGR Chart (CSS bar chart) ──
    monthly = rev.get("ggr_monthly", [])
    max_ggr = max((m.get("ggr", 0) for m in monthly), default=1) or 1
    bars = ""
    for m in monthly[:12]:
        pct = min(100, int(m.get("ggr", 0) / max_ggr * 100))
        ggr_val = m.get("ggr", 0)
        bars += f'''<div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:4px">
            <span style="font-size:9px;color:var(--text-dim);font-family:var(--mono)">${ggr_val:,.0f}</span>
            <div style="width:100%;height:{max(4, pct)}px;max-height:80px;background:linear-gradient(to top,rgba(255,255,255,0.08),rgba(255,255,255,0.2));border-radius:4px 4px 0 0"></div>
            <span style="font-size:10px;color:var(--text-muted)">M{m.get("month","")}</span>
            <span style="font-size:9px;color:var(--text-dim)">{m.get("dau",0):,} DAU</span></div>'''
    chart = f'''<div class="card"><h2 style="font-size:15px;font-weight:600;margin-bottom:16px">Monthly GGR Projection</h2>
        <div style="display:flex;gap:4px;align-items:flex-end;height:120px;padding:24px 0 0">{bars}</div></div>'''

    # ── Market Breakdown ──
    mkt_rows = ""
    for mk in rev.get("market_breakdown", []):
        cap = mk.get("captured_players", 0)
        annual = mk.get("ggr_365d", 0)
        pct = mk.get("pct_of_total", 0)
        bar_w = max(2, int(pct))
        mkt_rows += f'''<div style="display:flex;align-items:center;gap:12px;padding:8px 0;border-bottom:1px solid var(--border)">
            <div style="width:60px;font-size:12px;font-weight:600;color:var(--text-bright)">{mk.get("market","").upper()}</div>
            <div style="flex:1;height:6px;background:var(--bg-input);border-radius:3px;overflow:hidden"><div style="width:{bar_w}%;height:100%;background:rgba(255,255,255,0.2);border-radius:3px"></div></div>
            <div style="width:90px;text-align:right;font-family:var(--mono);font-size:12px;color:var(--text-bright)">${annual:,.0f}</div>
            <div style="width:50px;text-align:right;font-size:11px;color:var(--text-muted)">{pct}%</div>
            <div style="width:80px;text-align:right;font-size:11px;color:var(--text-dim)">{cap:,} players</div></div>'''
    markets_card = f'<div class="card"><h2 style="font-size:15px;font-weight:600;margin-bottom:12px">Market Breakdown</h2>{mkt_rows}</div>'

    # ── Sensitivity Analysis ──
    sens_rows = ""
    for s in rev.get("sensitivity", []):
        is_current = s.get("delta_pct", 0) == 0
        bg = "background:rgba(255,255,255,0.03)" if is_current else ""
        fw = "font-weight:700" if is_current else ""
        dc = "var(--success)" if s.get("delta_pct", 0) > 0 else ("var(--danger)" if s.get("delta_pct", 0) < 0 else "var(--text-muted)")
        marker = " ← current" if is_current else ""
        sens_rows += f'<tr style="{bg}"><td style="padding:6px 12px;font-family:var(--mono);font-size:12px;{fw}">{s.get("rtp",0)}%{marker}</td><td style="padding:6px 12px;font-family:var(--mono);font-size:12px">{s.get("hold_pct",0)}%</td><td style="padding:6px 12px;font-family:var(--mono);font-size:12px">${s.get("ggr_365d",0):,.0f}</td><td style="padding:6px 12px;font-size:12px;color:{dc}">{s.get("delta_pct",0):+.1f}%</td></tr>'
    sensitivity_card = f'''<div class="card"><h2 style="font-size:15px;font-weight:600;margin-bottom:12px">Sensitivity Analysis — What if RTP changes?</h2>
        <table style="width:100%;border-collapse:collapse"><tr><th style="font-size:11px;color:var(--text-muted);padding:6px 12px;text-align:left">RTP</th><th style="font-size:11px;color:var(--text-muted);padding:6px 12px;text-align:left">Hold %</th><th style="font-size:11px;color:var(--text-muted);padding:6px 12px;text-align:left">Annual GGR</th><th style="font-size:11px;color:var(--text-muted);padding:6px 12px;text-align:left">Delta</th></tr>{sens_rows}</table></div>'''

    # ── Benchmark Comparison ──
    bench_rows = ""
    for b in rev.get("benchmarks", []):
        sim_bar = max(2, int(b.get("similarity_pct", 0)))
        bench_rows += f'''<div style="display:flex;align-items:center;gap:12px;padding:8px 0;border-bottom:1px solid var(--border)">
            <div style="width:120px;font-size:12px;font-weight:500;color:var(--text-bright)">{b.get("title","")}</div>
            <div style="width:60px;font-size:11px;color:var(--text-dim)">{b.get("volatility","")}</div>
            <div style="width:60px;font-size:11px;color:var(--text-muted)">{b.get("rtp",0)}%</div>
            <div style="flex:1;height:4px;background:var(--bg-input);border-radius:2px"><div style="width:{sim_bar}%;height:100%;background:rgba(255,255,255,0.2);border-radius:2px"></div></div>
            <div style="width:50px;text-align:right;font-size:11px;color:var(--text-muted)">{b.get("similarity_pct",0)}%</div>
            <div style="width:50px;text-align:right;font-size:11px;color:var(--text-dim)">{b.get("performance_vs_ours","")}</div></div>'''
    benchmark_card = f'<div class="card"><h2 style="font-size:15px;font-weight:600;margin-bottom:12px">Benchmark Comparison</h2>{bench_rows}</div>'

    # ── Investment Breakdown ──
    dev_cost = rev.get("total_dev_cost", 0)
    cert_cost = rev.get("cert_cost", 0)
    feature_cost = dev_cost - 45000 - 12000 - 5000 - cert_cost  # Reverse-calculate feature cost
    invest_card = f'''<div class="card"><h2 style="font-size:15px;font-weight:600;margin-bottom:12px">Investment Analysis</h2>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
            <div style="padding:8px;border:1px solid var(--border);border-radius:6px"><div style="font-size:11px;color:var(--text-muted)">Base Dev</div><div style="font-size:14px;font-weight:600;color:var(--text-bright)">$45,000</div></div>
            <div style="padding:8px;border:1px solid var(--border);border-radius:6px"><div style="font-size:11px;color:var(--text-muted)">Features</div><div style="font-size:14px;font-weight:600;color:var(--text-bright)">${max(0,feature_cost):,.0f}</div></div>
            <div style="padding:8px;border:1px solid var(--border);border-radius:6px"><div style="font-size:11px;color:var(--text-muted)">Art + Audio</div><div style="font-size:14px;font-weight:600;color:var(--text-bright)">$17,000</div></div>
            <div style="padding:8px;border:1px solid var(--border);border-radius:6px"><div style="font-size:11px;color:var(--text-muted)">Certification</div><div style="font-size:14px;font-weight:600;color:var(--text-bright)">${cert_cost:,.0f}</div></div>
        </div>
        <div style="margin-top:12px;padding:12px;background:rgba(255,255,255,0.03);border-radius:8px;display:flex;justify-content:space-between">
            <div><div style="font-size:11px;color:var(--text-muted)">Total Investment</div><div style="font-size:18px;font-weight:700;color:var(--text-bright)">${dev_cost:,.0f}</div></div>
            <div style="text-align:right"><div style="font-size:11px;color:var(--text-muted)">Net Profit (Year 1)</div><div style="font-size:18px;font-weight:700;color:{"var(--success)" if rev.get("ggr_365d",0)-dev_cost>0 else "var(--danger)"}">${rev.get("ggr_365d",0)-dev_cost:,.0f}</div></div>
        </div></div>'''

    # ── Operator Scenarios ──
    op_rows = ""
    for ops in rev.get("operator_scenarios", []):
        op_rows += f'''<div style="display:flex;align-items:center;gap:12px;padding:8px 0;border-bottom:1px solid var(--border)">
            <div style="width:80px;font-size:12px;font-weight:600;color:var(--text-bright)">{ops.get("type","").replace("_"," ").title()}</div>
            <div style="flex:1;font-family:var(--mono);font-size:13px;color:var(--text-bright)">${ops.get("ggr_365d",0):,.0f}</div>
            <div style="font-size:11px;color:var(--text-muted)">Margin: {ops.get("margin_pct",0)}%</div></div>'''
    ops_card = f'<div class="card"><h2 style="font-size:15px;font-weight:600;margin-bottom:12px">Operator Type Scenarios</h2>{op_rows}</div>'

    # ── Risk + Vol Profile ──
    cannibal = rev.get("cannibalization_risk", "?")
    cannibal_c = {"low":"var(--success)","medium":"var(--warning)","high":"var(--danger)"}.get(cannibal, "var(--text-muted)")
    risk_card = f'''<div class="card"><h2 style="font-size:15px;font-weight:600;margin-bottom:12px">Risk Profile</h2>
        <div style="margin-bottom:12px"><label style="font-size:11px">Cannibalization Risk</label><div style="font-size:16px;font-weight:600;color:{cannibal_c}">{cannibal.upper()}</div></div>
        <div style="margin-bottom:12px"><label style="font-size:11px">Theme Appeal</label><div style="font-size:16px;font-weight:600;color:var(--text-bright)">{rev.get("theme_appeal",1.0)}x</div></div>
        <div><label style="font-size:11px">Volatility Profile</label><p style="font-size:12px;color:var(--text-muted);margin-top:4px">{rev.get("volatility_profile","")}</p></div></div>'''

    return layout(f'''
    <div style="margin-bottom:20px"><a href="/job/{job_id}/files" style="color:var(--text-dim);font-size:12px;text-decoration:none">&larr; Back to {_esc(job["title"])}</a></div>
    <h2 class="page-title" style="margin-bottom:4px">&#128176; Revenue Dashboard</h2>
    <p style="color:var(--text-muted);font-size:12px;margin-bottom:24px">{_esc(job["title"])} — Financial Projections</p>
    {hero}{periods}{chart}
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
        <div>{markets_card}{invest_card}{ops_card}</div>
        <div>{sensitivity_card}{benchmark_card}{risk_card}</div>
    </div>
    <div style="margin:24px 0 40px"><a href="/job/{job_id}/files" class="btn btn-ghost">Back to files</a></div>''', "history")


# ─── ENGINE EXPORT (Phase 6B) ───

@app.route("/api/job/<job_id>/export")
@login_required
def api_export(job_id):
    """Generate and download engine export package (Unity/Godot/Generic)."""
    user = current_user(); db = get_db()
    job = db.execute("SELECT * FROM jobs WHERE id=? AND user_id=?", (job_id, user["id"])).fetchone()
    if not job or not job["output_dir"]:
        return "Not found", 404

    fmt = request.args.get("format", "unity").lower()
    if fmt not in ("unity", "godot", "generic"):
        return "Invalid format. Use: unity, godot, generic", 400

    od = Path(job["output_dir"])

    # Check if pre-generated ZIP exists
    slug = job["title"].lower().replace(" ", "_").replace("'", "")[:30]
    zip_name = f"{slug}_{fmt}_export.zip"
    cached = od / "09_export" / zip_name
    if cached.exists():
        return send_from_directory(cached.parent, cached.name, as_attachment=True,
                                    download_name=zip_name)

    # Generate on the fly
    try:
        from tools.export_engine import generate_export_package
        params = json.loads(job["params"]) if job["params"] else {}
        export_params = {
            "grid_cols": params.get("grid_cols", 5),
            "grid_rows": params.get("grid_rows", 3),
            "ways_or_lines": params.get("ways_or_lines", 243),
            "target_rtp": params.get("target_rtp", 96.0),
            "max_win": params.get("max_win_multiplier", 5000),
            "volatility": params.get("volatility", "medium"),
            "art_style": params.get("art_style", "Cinematic realism"),
            "markets": ", ".join(params.get("target_markets", [])) if isinstance(params.get("target_markets"), list) else params.get("target_markets", ""),
            "features": params.get("requested_features", []),
        }
        zip_path = generate_export_package(
            output_dir=str(od), format=fmt,
            game_title=job["title"], game_params=export_params,
        )
        zp = Path(zip_path)
        return send_from_directory(zp.parent, zp.name, as_attachment=True,
                                    download_name=zp.name)
    except Exception as e:
        return f"Export failed: {e}", 500


@app.route("/qdrant")
@login_required
def qdrant_status():
    try:
        from tools.qdrant_store import JurisdictionStore
        status = JurisdictionStore().get_status()
    except Exception as e:
        status = {"status":"ERROR","message":str(e),"jurisdictions":[],"total_vectors":0}
    bc = "badge-complete" if status["status"]=="ONLINE" else "badge-failed"
    jhtml = "".join(f'<div style="padding:8px 0;border-bottom:1px solid var(--border);font-size:13px">{j}</div>' for j in status.get("jurisdictions",[])) or '<div style="color:var(--text-muted);font-size:13px;padding:12px 0">No jurisdictions yet — hit <b>Seed Jurisdictions</b> below.</div>'
    configured = bool(os.getenv("QDRANT_URL"))
    seed_btn = ""

    # Import jurisdiction counts for buttons
    try:
        from tools.qdrant_bootstrap import INTERNATIONAL_JURISDICTIONS, US_REGULATED_JURISDICTIONS, US_GRAY_AREA_JURISDICTIONS
        n_int = len(INTERNATIONAL_JURISDICTIONS)
        n_us_reg = len(US_REGULATED_JURISDICTIONS)
        n_us_gray = len(US_GRAY_AREA_JURISDICTIONS)
    except Exception:
        n_int, n_us_reg, n_us_gray = 11, 6, 8

    if configured:
        seed_btn = f'''
    <div class="card"><h2>⚡ Bootstrap / Seed</h2>
    <p style="font-size:13px;color:var(--text-muted);margin-bottom:16px">
        Generate regulatory intelligence for jurisdictions via GPT and embed into Qdrant.
        Each jurisdiction takes ~20s. Existing jurisdictions are skipped automatically.</p>
    <div id="seedControls" style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px">
        <button onclick="seedQdrant('all')" class="btn btn-primary">🌍 Seed All ({n_int + n_us_reg + n_us_gray} jurisdictions)</button>
        <button onclick="seedQdrant('international')" class="btn btn-ghost">🌐 International Only ({n_int})</button>
        <button onclick="seedQdrant('us_regulated')" class="btn btn-ghost">🇺🇸 US Regulated ({n_us_reg})</button>
        <button onclick="seedQdrant('us_gray')" class="btn btn-ghost">⚠️ US Gray Area ({n_us_gray})</button>
    </div>
    <div id="seedStatus" style="display:none;padding:12px;border-radius:8px;background:var(--bg-void);font-size:13px;font-family:monospace;max-height:300px;overflow-y:auto"></div>
    </div>
    <script>
    async function seedQdrant(scope) {{
        const st = document.getElementById('seedStatus');
        st.style.display = 'block';
        st.innerHTML = '⏳ Starting seed (' + scope + ')... this may take a few minutes.\\n';
        document.querySelectorAll('#seedControls button').forEach(b => b.disabled = true);
        try {{
            const r = await fetch('/api/qdrant/seed', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{scope: scope}})
            }});
            const data = await r.json();
            if (data.status === 'complete') {{
                st.innerHTML += '\\n✅ Done! Seeded: ' + data.seeded + ', Failed: ' + data.failed +
                    ', Skipped: ' + data.skipped + ', Vectors: ' + data.total_vectors_added + '\\n';
                if (data.results) {{
                    data.results.forEach(r => {{
                        const icon = r.status === 'ok' ? '✅' : '❌';
                        st.innerHTML += icon + ' ' + r.jurisdiction + ': ' + (r.vectors_added || 0) + ' vectors' +
                            (r.error ? ' — ' + r.error : '') + '\\n';
                    }});
                }}
                setTimeout(() => location.reload(), 2000);
            }} else {{
                st.innerHTML += '\\n❌ ' + (data.message || data.error || JSON.stringify(data));
            }}
        }} catch(e) {{
            st.innerHTML += '\\n❌ Error: ' + e.message;
        }}
        document.querySelectorAll('#seedControls button').forEach(b => b.disabled = false);
    }}
    </script>'''
    else:
        seed_btn = '''
    <div class="card" style="border-left:3px solid var(--danger)">
    <h2>⚠️ Qdrant Not Configured</h2>
    <p style="font-size:13px;color:var(--text-muted)">Set <code>QDRANT_URL</code> and <code>QDRANT_API_KEY</code> in
    <a href="/settings" style="color:var(--accent)">Settings → API Keys</a> to enable vector search.</p></div>'''

    return layout(f'''
    <h2 class="page-title" style="margin-bottom:24px">{ICON_DB} Qdrant Vector Database</h2>
    <div class="card"><h2>Connection <span class="badge {bc}" style="margin-left:8px">{status["status"]}</span></h2>
    <div class="row2" style="margin-top:12px"><div><label>Total Vectors</label><div style="font-size:20px;font-weight:600;color:var(--text-bright)">{status.get("total_vectors",0)}</div></div>
    <div><label>Jurisdictions</label><div style="font-size:20px;font-weight:600;color:var(--text-bright)">{len(status.get("jurisdictions",[]))}</div></div></div></div>
    {seed_btn}
    <div class="card"><h2>Researched Jurisdictions</h2>{jhtml}</div>''', "qdrant")


@app.route("/api/qdrant/seed", methods=["POST"])
@login_required
def api_qdrant_seed():
    """API endpoint to trigger Qdrant bootstrap seeding."""
    try:
        from tools.qdrant_bootstrap import seed_qdrant
        data = request.get_json() or {}
        scope = data.get("scope", "all")

        kwargs = {"skip_existing": True}
        if scope == "international":
            kwargs.update(include_international=True, include_us_regulated=False, include_us_gray_area=False)
        elif scope == "us_regulated":
            kwargs.update(include_international=False, include_us_regulated=True, include_us_gray_area=False)
        elif scope == "us_gray":
            kwargs.update(include_international=False, include_us_regulated=False, include_us_gray_area=True)
        # else: all=True (defaults)

        result = seed_qdrant(**kwargs)
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

# ─── REVIEWS (Web HITL) ───
@app.route("/reviews")
@login_required
def reviews_page():
    from tools.web_hitl import get_pending_reviews
    pending = get_pending_reviews()
    # Also get resolved reviews
    resolved = []
    try:
        db = get_db()
        resolved = db.execute(
            "SELECT r.*, j.title as job_title FROM reviews r JOIN jobs j ON r.job_id=j.id "
            "WHERE r.status!='pending' ORDER BY r.resolved_at DESC LIMIT 20"
        ).fetchall()
    except Exception as e:
        logger.warning(f"Reviews query failed: {e}")

    pending_html = ""
    for r in pending:
        pending_html += f'''<div class="history-item" style="grid-template-columns:1fr 140px 100px">
            <div><div class="history-title">{r["title"]}</div><div class="history-type">{r["job_title"]} &middot; {r["stage"]}</div></div>
            <div class="history-date">{r["created_at"][:16] if r["created_at"] else ""}</div>
            <div class="history-actions"><a href="/review/{r["id"]}" class="btn btn-primary btn-sm">Review</a></div>
        </div>'''
    if not pending_html:
        pending_html = '<div class="empty-state"><h3>No pending reviews</h3><p>Launch a pipeline in Interactive Mode to see checkpoints here.</p></div>'

    resolved_html = ""
    for r in resolved:
        r = dict(r)
        status = "Approved" if r.get("approved") else "Rejected"
        bc = "badge-complete" if r.get("approved") else "badge-failed"
        resolved_html += f'''<div class="history-item" style="grid-template-columns:1fr 100px 140px">
            <div><div class="history-title">{r["title"]}</div><div class="history-type">{r.get("job_title","")} &middot; {r.get("feedback","")[:50]}</div></div>
            <div><span class="badge {bc}">{status}</span></div>
            <div class="history-date">{r.get("resolved_at","")[:16]}</div>
        </div>'''

    return layout(f'''
    <h2 class="page-title" style="margin-bottom:24px">{ICON_REVIEW} Pipeline Reviews</h2>
    <div class="card"><h2 style="color:var(--text-bright)">Pending Reviews <span class="badge badge-running" style="margin-left:8px">{len(pending)}</span></h2>{pending_html}</div>
    {"<div class='card'><h2>Resolved</h2>" + resolved_html + "</div>" if resolved_html else ""}''', "reviews")


@app.route("/review/<review_id>")
@login_required
def review_detail(review_id):
    from tools.web_hitl import get_review
    import json as _json
    review = get_review(review_id)
    if not review:
        return "Review not found", 404

    files = _json.loads(_rget(review, "files","[]")) if _rget(review, "files") else []
    output_dir = _rget(review, "output_dir","")

    # Build file list with download links
    files_html = ""
    if files and output_dir:
        for f in files:
            fpath = Path(output_dir) / f
            if fpath.exists():
                ext = fpath.suffix.lower()
                # Show image previews inline
                if ext in (".png",".jpg",".jpeg",".webp"):
                    files_html += f'<div style="margin:8px 0"><div style="font-size:11px;color:var(--text-muted);margin-bottom:4px;font-family:Geist Mono,monospace">{f}</div><img src="/review/{review_id}/file/{f}" style="max-width:100%;border-radius:8px;border:1px solid var(--border)"></div>'
                else:
                    files_html += f'<div class="file-row"><a href="/review/{review_id}/file/{f}">{f}</a><span class="file-size">{fpath.stat().st_size/1024:.1f} KB</span></div>'

    if not files_html:
        files_html = '<div style="color:var(--text-muted);font-size:13px;padding:12px 0">No files to preview.</div>'

    already_resolved = review["status"] != "pending"
    form_html = ""
    if already_resolved:
        result = "Approved" if _rget(review, "approved") else "Rejected"
        form_html = f'<div class="card" style="border-color:var(--success) !important"><h2>Already {result}</h2><p style="color:var(--text-muted)">{_rget(review, "feedback","")}</p></div>'
    else:
        form_html = f'''<div class="card">
        <h2>Your Decision</h2>
        <form action="/api/review/{review_id}" method="POST">
            <label>Feedback / Art Changes / Notes</label>
            <textarea name="feedback" placeholder="e.g. Make the symbols darker, increase contrast on the wild symbol, add more gold accents..." rows="4"></textarea>
            <div style="display:flex;gap:12px;margin-top:8px">
                <button type="submit" name="action" value="approve" class="btn btn-primary" style="flex:1;padding:14px">Approve &amp; Continue</button>
                <button type="submit" name="action" value="reject" class="btn btn-ghost" style="flex:1;padding:14px;border-color:var(--danger);color:var(--danger)">Reject &amp; Revise</button>
            </div>
        </form></div>'''

    return layout(f'''
    <div style="margin-bottom:20px"><a href="/reviews" style="color:var(--text-dim);font-size:12px;text-decoration:none">&larr; Back to Reviews</a></div>
    <h2 class="page-title">{review["title"]}</h2>
    <p style="color:var(--text-muted);font-size:12px;margin-bottom:24px">{_rget(review, "job_title","")} &middot; Stage: {review["stage"]}</p>

    <div class="card"><h2>Summary</h2><div style="font-size:13px;line-height:1.7;white-space:pre-wrap">{review["summary"]}</div></div>
    <div class="card" style="padding:0;overflow:hidden"><div style="padding:16px 16px 8px"><h2 style="margin-bottom:8px">Generated Files</h2></div>{files_html}</div>
    {form_html}''', "reviews")


@app.route("/review/<review_id>/file/<path:fp>")
@login_required
def review_file(review_id, fp):
    from tools.web_hitl import get_review
    review = get_review(review_id)
    if not review or not _rget(review, "output_dir"):
        return "Not found", 404
    return send_from_directory(Path(review["output_dir"]), fp)


@app.route("/api/review/<review_id>", methods=["POST"])
@login_required
def api_submit_review(review_id):
    from tools.web_hitl import submit_review
    action = request.form.get("action","approve")
    feedback = request.form.get("feedback","")
    approved = (action == "approve")
    submit_review(review_id, approved=approved, feedback=feedback)
    return redirect("/reviews")


# ─── SETTINGS ───
@app.route("/settings")
@login_required
def settings_page():
    keys = {
        "OPENAI_API_KEY": {"label": "OpenAI API Key", "icon": "🧠", "desc": "GPT-5 reasoning agents, DALL-E 3 images, Vision QA", "required": True},
        "SERPER_API_KEY": {"label": "Serper API Key", "icon": "🔍", "desc": "Web search, patent search, trend radar, competitor teardown", "required": True},
        "ELEVENLABS_API_KEY": {"label": "ElevenLabs API Key", "icon": "🔊", "desc": "AI sound effect generation (13 core game sounds)", "required": False},
        "QDRANT_URL": {"label": "Qdrant URL", "icon": "🗃️", "desc": "Vector DB for regulation storage + knowledge base", "required": False},
        "QDRANT_API_KEY": {"label": "Qdrant API Key", "icon": "🔑", "desc": "Auth for Qdrant Cloud", "required": False},
        "ADMIN_EMAIL": {"label": "Admin Email", "icon": "🔒", "desc": "Email that can access the Control Plane (⚡)", "required": False},
        "GOOGLE_CLIENT_ID": {"label": "Google OAuth Client ID", "icon": "🔐", "desc": "Google sign-in", "required": True},
        "GOOGLE_CLIENT_SECRET": {"label": "Google OAuth Secret", "icon": "🔐", "desc": "Google sign-in", "required": True},
        "RESEND_API_KEY": {"label": "Resend API Key", "icon": "📧", "desc": "Email notifications when pipelines complete (resend.com)", "required": False},
        "APP_BASE_URL": {"label": "App Base URL", "icon": "🌐", "desc": "Your deployment URL for email links (e.g. https://arkainbrain.up.railway.app)", "required": False},
    }

    rows = ""
    for env_key, info in keys.items():
        val = os.getenv(env_key, "")
        is_set = bool(val) and val not in ("your-openai-key", "your-serper-key", "your-elevenlabs-key", "your-qdrant-key", "your-qdrant-url", "your-google-client-id", "your-google-client-secret")
        masked = val[:8] + "..." + val[-4:] if is_set and len(val) > 12 else ("Set" if is_set else "Not configured")
        bc = "badge-complete" if is_set else ("badge-failed" if info["required"] else "badge-queued")
        status = "Connected" if is_set else ("Required" if info["required"] else "Optional")
        rows += f'''<div class="file-row" style="padding:14px 16px;gap:16px">
            <div style="display:flex;align-items:center;gap:12px;flex:1">
                <span style="font-size:20px">{info["icon"]}</span>
                <div><div style="font-weight:600;color:var(--text-bright);font-size:13px">{info["label"]}</div>
                <div style="font-size:11px;color:var(--text-muted)">{info["desc"]}</div></div>
            </div>
            <div style="font-family:'Geist Mono',monospace;font-size:11px;color:var(--text-muted);min-width:120px">{masked}</div>
            <span class="badge {bc}">{status}</span>
        </div>'''

    return layout(f'''
    <h2 class="page-title">{ICON_SETTINGS} Settings</h2>
    <p style="color:var(--text-muted);font-size:13px;margin-bottom:24px">API keys and integrations. Configure in <code style="font-family:'Geist Mono',monospace;background:var(--bg-input);padding:2px 6px;border-radius:4px">.env</code> file.</p>
    <div class="card" style="padding:0;overflow:hidden"><div style="padding:16px 16px 8px"><h2>🔗 API Integrations</h2></div>{rows}</div>
    <div class="card"><h2>📋 Quick Setup</h2>
    <pre style="background:var(--bg-input);padding:16px;border-radius:8px;font-family:'Geist Mono',monospace;font-size:11px;color:var(--text);overflow-x:auto;line-height:1.8">
# Copy .env.example to .env and fill in your keys:
cp .env.example .env

# Required:
OPENAI_API_KEY=sk-...          # OpenAI (GPT-5 + DALL-E 3)
SERPER_API_KEY=...              # serper.dev (free tier: 2500 searches)

# Optional (Tier 2):
ELEVENLABS_API_KEY=...          # elevenlabs.io ($5/mo starter for SFX)

# Optional (Email Alerts):
RESEND_API_KEY=...              # resend.com (free: 100 emails/day)
APP_BASE_URL=...                # Your app URL for email links

# Optional (State Recon):
QDRANT_URL=...                  # Qdrant Cloud or self-hosted
QDRANT_API_KEY=...
</pre></div>

    <div class="card"><h2>🏗️ Pipeline Version</h2>
    <div class="row2">
        <div><label>Version</label><div style="font-size:16px;font-weight:600;color:var(--text-bright)">v8.0</div></div>
        <div><label>Model</label><div style="font-size:16px;font-weight:700;color:var(--text-bright)">GPT-5 + GPT-5-mini</div></div>
    </div>
    <div style="margin-top:12px;font-size:12px;color:var(--text-dim);line-height:1.7">
        6 reasoning agents · 8 PDF deliverables · HTML5 prototype · AI sound design · Patent scanner · Cert planner
    </div></div>{_settings_notify_section()}''', "settings")


def _settings_notify_section():
    """Build the email notification toggle section for settings page."""
    try:
        user = current_user()
        db = get_db()
        user_row = db.execute("SELECT email_notify FROM users WHERE id=?", (user["id"],)).fetchone()
        email_notify = user_row["email_notify"] if user_row and "email_notify" in user_row.keys() else 1
        resend_ok = bool(os.getenv("RESEND_API_KEY", "")) and os.getenv("RESEND_API_KEY") not in ("your-resend-key", "")
        status_text = "Active" if resend_ok and email_notify else ("Disabled by you" if resend_ok else "Not configured")
        badge = "badge-complete" if resend_ok and email_notify else "badge-queued"
        checked = "checked" if email_notify else ""
        bg = "var(--success)" if email_notify else "rgba(255,255,255,0.1)"
        left = "22px" if email_notify else "3px"
        email = _esc(user.get("email", ""))
        setup_note = (
            "<div style='font-size:11px;color:var(--text-dim)'>Email service: Resend &middot; Configured via RESEND_API_KEY</div>"
            if resend_ok else
            "<div style='font-size:11px;color:var(--warning)'>&#9888;&#65039; Set RESEND_API_KEY in .env to enable. "
            "<a href='https://resend.com' target='_blank' style='color:var(--accent)'>Get a free key &rarr;</a></div>"
        )
        return f'''<div class="card"><h2>&#128276; Email Notifications</h2>
        <p style="font-size:12px;color:var(--text-muted);margin-bottom:16px">Get notified when pipelines complete or fail.</p>
        <div style="display:flex;align-items:center;justify-content:space-between;padding:14px 16px;background:var(--bg-input);border-radius:8px;margin-bottom:12px">
            <div style="display:flex;align-items:center;gap:12px">
                <span style="font-size:20px">&#128231;</span>
                <div><div style="font-weight:600;color:var(--text-bright);font-size:13px">Pipeline completion emails</div>
                <div style="font-size:11px;color:var(--text-muted)">Sent to {email}</div></div>
            </div>
            <div style="display:flex;align-items:center;gap:10px">
                <span class="badge {badge}">{status_text}</span>
                <label style="position:relative;display:inline-block;width:44px;height:24px;cursor:pointer">
                    <input type="checkbox" id="email-toggle" {checked} onchange="fetch('/api/settings/email-notify',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{enabled:this.checked}})}}).then(r=>r.ok?location.reload():alert('Failed'))" style="opacity:0;width:0;height:0">
                    <span style="position:absolute;inset:0;background:{bg};border-radius:24px;transition:0.3s"></span>
                    <span style="position:absolute;height:18px;width:18px;left:{left};bottom:3px;background:#fff;border-radius:50%;transition:0.3s"></span>
                </label>
            </div>
        </div>
        {setup_note}</div>'''
    except Exception as e:
        logger.debug(f"Notify section: {e}")
        return ""


# ─── API ───

@app.route("/api/settings/email-notify", methods=["POST"])
@login_required
def api_toggle_email_notify():
    """Toggle email notification preference for current user."""
    user = current_user()
    data = request.get_json(silent=True) or {}
    enabled = 1 if data.get("enabled", True) else 0
    db = get_db()
    db.execute("UPDATE users SET email_notify=? WHERE id=?", (enabled, user["id"]))
    db.commit()
    logger.info(f"Email notify {'enabled' if enabled else 'disabled'} for {user['email']}")
    return jsonify({"ok": True, "email_notify": enabled})


# ═══════════════════════════════════════════════════════════
# SURPRISE ME — Novel Slot Concept Generator
# ═══════════════════════════════════════════════════════════

@app.route("/api/surprise")
@login_required
def api_surprise_concept():
    """Generate a random but novel slot game concept.

    Strategy: Mashup two unrelated worlds + a twist mechanic + a mood.
    Tries LLM first (if key present), falls back to rich combinatorial generator.
    """
    import random

    # ── Try LLM-powered generation first ──
    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
    if api_key and os.getenv("OPENAI_API_KEY"):
        try:
            from openai import OpenAI
            client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
            resp = client.chat.completions.create(
                model="gpt-4.1-nano-2025-04-14",
                max_tokens=500,
                temperature=1.2,
                response_format={"type": "json_object"},
                messages=[{
                    "role": "system",
                    "content": (
                        "You are a wildly creative slot game concept inventor. "
                        "Generate ONE novel, never-before-seen slot game concept that mashes up "
                        "two unexpected worlds/themes. Be specific and vivid — not generic. "
                        "Respond ONLY with this JSON (no markdown):\n"
                        '{"theme":"<vivid 8-15 word concept, e.g. Bioluminescent deep-sea casino where jellyfish are the dealers>",'
                        '"volatility":"<low|medium|high|very_high>",'
                        '"target_rtp":<float 92-97>,'
                        '"grid_cols":<int 5-7>,"grid_rows":<int 3-5>,'
                        '"ways_or_lines":"<int>",'
                        '"max_win_multiplier":<int 2000-25000>,'
                        '"art_style":"<specific style, e.g. Art Nouveau watercolor, Pixel art noir, Ukiyo-e woodblock>",'
                        '"features":["<3-5 from: free_spins,multipliers,expanding_wilds,cascading_reels,'
                        'mystery_symbols,walking_wilds,cluster_pays,hold_and_spin,split_symbols>"],'
                        '"target_markets":"Georgia, Texas",'
                        '"competitor_references":"<2-3 real slot games this is inspired by>",'
                        '"special_requirements":"<1-2 sentence unique mechanic or narrative hook>"}'
                    )
                }, {
                    "role": "user",
                    "content": f"Invent concept #{random.randint(1,99999)}. Make it surprising."
                }]
            )
            data = json.loads(resp.choices[0].message.content)
            # Ensure required fields
            data.setdefault("target_markets", "Georgia, Texas")
            data.setdefault("volatility", "high")
            return jsonify(data)
        except Exception as e:
            logger.warning(f"LLM surprise failed, using fallback: {e}")

    # ── Rich combinatorial fallback ──
    # World A × World B × Twist × Mood = novel concept
    worlds_a = [
        "Ancient Sumerian ziggurat", "Bioluminescent deep-sea trench",
        "Victorian clockwork factory", "Neon-soaked Tokyo alleyway at 3am",
        "Abandoned Soviet space station", "Venetian masquerade ball 1742",
        "Aztec obsidian mirror temple", "Prohibition-era speakeasy",
        "Floating sky islands above storm clouds", "Frozen Viking longship graveyard",
        "Cyberpunk night market in Mumbai 2087", "Art Deco Manhattan penthouse 1929",
        "Saharan sandstorm with buried pharaoh city", "Polynesian volcano forge of the gods",
        "Medieval alchemist's tower during eclipse", "Bamboo forest samurai dojo at dawn",
        "Sunken pirate galleon in bioluminescent cave", "Noir detective office in eternal rain",
        "Enchanted mushroom kingdom underground", "Crystal cavern beneath Northern Lights",
        "Steampunk airship racing through thunderstorm", "Voodoo bayou mansion at midnight",
        "Greek Olympus during a divine war", "Retro-futuristic 1960s moon colony",
    ]

    worlds_b = [
        "where shadows have their own economy",
        "where fire and ice coexist in unstable harmony",
        "run by sentient mechanical animals",
        "where gravity reverses every full moon",
        "haunted by musical ghosts who gamble for memories",
        "controlled by rival elemental factions",
        "where time flows backwards during wins",
        "where dreams become physical currency",
        "with portals to parallel timelines",
        "where colors are alive and compete for dominance",
        "guarded by mythological shapeshifters",
        "where luck itself is a living creature",
        "where every symbol tells part of an ancient prophecy",
        "powered by captured starlight and moonbeams",
        "where the reels are dimensions colliding",
        "where alchemy turns losses into golden transformations",
    ]

    art_styles = [
        "Art Nouveau watercolor with gold leaf accents",
        "Ukiyo-e woodblock meets neon cyberpunk",
        "Dark oil painting with chiaroscuro lighting",
        "Cel-shaded anime with metallic textures",
        "Fractured stained glass mosaic style",
        "Retro pixel art with modern depth effects",
        "Luminous digital painting with particle effects",
        "Surrealist dreamscape à la Dalí",
        "Ink wash painting with splashes of crimson",
        "Baroque excess meets minimalist geometry",
        "Hand-carved woodcut with rich earth tones",
        "Holographic chrome with iridescent surfaces",
        "Botanical illustration meets dark fantasy",
        "Art Deco poster style with metallic gradients",
        "Glitch art with corrupted beauty aesthetic",
        "Watercolor bleeds on ancient parchment",
    ]

    special_hooks = [
        "Symbols evolve visually as win streaks build — low-pay royals transform into high-pay themed icons after 5 consecutive wins",
        "The background dynamically changes between day/night cycles affecting wild multiplier values",
        "A narrative choice system where the player picks paths that alter the bonus round mechanics",
        "Symbols have elemental affinities — adjacent matching elements trigger chain reactions beyond paylines",
        "A decay mechanic where unused symbol positions crack and reveal hidden multipliers underneath",
        "Free spins feature a rogue-like progression where each spin adds a permanent modifier",
        "Mystery symbols resolve based on the dominant color already visible on the reels",
        "Win celebrations trigger a mini-narrative cutscene that advances an overarching story",
        "A gravity mechanic where symbols fall in different directions based on which reel triggered the feature",
        "The scatter symbol is a puzzle piece — collecting 4 unique pieces across spins unlocks the bonus",
        "Ambient soundtrack shifts key and tempo based on win/loss streaks creating emotional arc",
        "Symbol stacks on each reel represent different factions vying for control of the game world",
    ]

    comp_refs = [
        "Book of Dead, Immortal Romance", "Reactoonz 2, Sweet Bonanza",
        "Gonzo's Quest, Jammin' Jars", "Dead or Alive 2, Tombstone",
        "Bonanza Megaways, Extra Chilli", "Moon Princess, Starlight Princess",
        "Money Train 3, Hacksaw Gaming style", "Rise of Olympus, Forge of Olympus",
        "Fire Joker, Lightning Roulette", "Rasputin Megaways, Viking Runecraft",
        "Legacy of Dead, Rich Wilde", "Starburst, Twin Spin",
    ]

    grids = [
        (5, 3, "243"), (5, 4, "1024"), (6, 4, "4096"),
        (5, 3, "20"), (5, 3, "50"), (7, 4, "16807"),
        (5, 5, "3125"), (6, 3, "729"),
    ]

    vols = ["medium", "high", "high", "very_high"]
    rtps = [95.5, 96.0, 96.2, 96.5, 96.8, 97.0]
    max_wins = [3000, 5000, 5000, 8000, 10000, 15000, 20000, 25000]

    all_features = [
        "free_spins", "multipliers", "expanding_wilds", "cascading_reels",
        "mystery_symbols", "walking_wilds", "cluster_pays", "hold_and_spin",
        "split_symbols"
    ]

    wa = random.choice(worlds_a)
    wb = random.choice(worlds_b)
    theme = f"{wa} {wb}"
    cols, rows, ways = random.choice(grids)
    vol = random.choice(vols)
    feats = random.sample(all_features, k=random.randint(3, 5))
    # Always include free_spins
    if "free_spins" not in feats:
        feats[0] = "free_spins"

    return jsonify({
        "theme": theme,
        "volatility": vol,
        "target_rtp": random.choice(rtps),
        "grid_cols": cols,
        "grid_rows": rows,
        "ways_or_lines": ways,
        "max_win_multiplier": random.choice(max_wins),
        "art_style": random.choice(art_styles),
        "features": feats,
        "target_markets": "Georgia, Texas",
        "competitor_references": random.choice(comp_refs),
        "special_requirements": random.choice(special_hooks),
    })


@app.route("/api/pipeline", methods=["POST"])
@login_required
def api_launch_pipeline():
    user = current_user()
    limit_err = _check_job_limit(user["id"])
    if limit_err: return limit_err
    job_id = str(uuid.uuid4())[:8]
    params = {"theme":request.form["theme"],"target_markets":[m.strip() for m in request.form.get("target_markets","Georgia, Texas").split(",")],"volatility":request.form.get("volatility","medium"),"target_rtp":float(request.form.get("target_rtp",96)),"grid_cols":int(request.form.get("grid_cols",5)),"grid_rows":int(request.form.get("grid_rows",3)),"ways_or_lines":request.form.get("ways_or_lines","243"),"max_win_multiplier":int(request.form.get("max_win_multiplier",5000)),"art_style":request.form.get("art_style","Cinematic realism"),"requested_features":request.form.getlist("features"),"competitor_references":[r.strip() for r in request.form.get("competitor_references","").split(",") if r.strip()],"special_requirements":request.form.get("special_requirements",""),"enable_recon":request.form.get("enable_recon")=="on"}
    db = get_db(); db.execute("INSERT INTO jobs (id,user_id,job_type,title,params,status) VALUES (?,?,?,?,?,?)", (job_id,user["id"],"slot_pipeline",params["theme"],json.dumps(params),"queued")); db.commit()
    params["interactive"] = request.form.get("interactive") == "on"
    _spawn_worker(job_id, "pipeline", json.dumps(params))
    return redirect(f"/job/{job_id}/logs")

@app.route("/api/recon", methods=["POST"])
@login_required
def api_launch_recon():
    user = current_user()
    limit_err = _check_job_limit(user["id"])
    if limit_err: return limit_err
    sn = request.form["state"].strip(); job_id = str(uuid.uuid4())[:8]
    db = get_db(); db.execute("INSERT INTO jobs (id,user_id,job_type,title,params,status) VALUES (?,?,?,?,?,?)", (job_id,user["id"],"state_recon",f"Recon: {sn}",json.dumps({"state":sn}),"queued")); db.commit()
    _spawn_worker(job_id, "recon", sn)
    return redirect(f"/job/{job_id}/logs")

@app.route("/api/status/<job_id>")
@login_required
def api_job_status(job_id):
    # DB is the source of truth (shared across gunicorn workers + subprocesses)
    db = get_db()
    job = db.execute("SELECT status,current_stage,error FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job:
        return jsonify({"error": "Not found"}), 404
    return jsonify(dict(job))


@app.route("/api/resume/<job_id>", methods=["POST"])
@login_required
def api_resume_job(job_id):
    """Resume a partial pipeline from its last checkpoint.

    Validates:
    - Job exists and belongs to the current user
    - Job status is 'partial'
    - Output dir and checkpoint.json exist
    - Job limit not exceeded
    Then spawns a worker subprocess in 'resume' mode.
    """
    user = current_user()
    db = get_db()
    job = db.execute("SELECT * FROM jobs WHERE id=? AND user_id=?", (job_id, user["id"])).fetchone()

    if not job:
        return jsonify({"error": "Job not found"}), 404

    if job["status"] != "partial":
        return jsonify({"error": f"Cannot resume: status is '{job['status']}', expected 'partial'"}), 400

    if not job["output_dir"] or not Path(job["output_dir"]).exists():
        return jsonify({"error": "Cannot resume: output directory missing"}), 400

    ckpt_path = Path(job["output_dir"]) / "checkpoint.json"
    if not ckpt_path.exists():
        return jsonify({"error": "Cannot resume: checkpoint.json missing"}), 400

    # Check job limit (resume counts as a running job)
    limit_err = _check_job_limit(user["id"])
    if limit_err:
        return jsonify({"error": "Too many jobs running — wait for one to finish"}), 429

    # Spawn resume worker
    _spawn_worker(job_id, "resume")
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/logs/<job_id>")
@login_required
def api_log_stream(job_id):
    """Polling log endpoint — returns new lines since ?after=N.

    Replaces the old SSE generator that held a gunicorn thread for up to 90 min.
    Client polls every 2s with the line cursor. Thread is released after each response.

    Query params:
      after (int): Line number to start from (0 = beginning). Default 0.

    Returns JSON:
      {"lines": [...], "cursor": <next_after>, "done": bool, "status": "running"|"complete"|"failed"}
    """
    after = request.args.get("after", 0, type=int)
    log_path = LOG_DIR / f"{job_id}.log"

    lines = []
    done = False
    status = "running"

    # Read log file from the cursor position
    if log_path.exists():
        try:
            with open(log_path, "r", errors="replace") as f:
                all_lines = f.readlines()
                lines = [l.rstrip() for l in all_lines[after:]]
        except (IOError, OSError):
            pass
    elif after == 0:
        lines = ["Waiting for worker to start..."]

    # Check job status
    _polldb = _open_db()
    job = _polldb.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
    _polldb.close()

    if job:
        status = job["status"]
        if status in ("complete", "failed", "partial"):
            done = True

    cursor = after + len(lines)
    return jsonify({"lines": lines, "cursor": cursor, "done": done, "status": status})


@app.route("/job/<job_id>/logs")
@login_required
def job_logs_page(job_id):
    db = get_db(); job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job: return "Not found", 404
    status = job["status"]
    badge_class = {"running":"badge-running","complete":"badge-complete","failed":"badge-failed","partial":"badge-partial"}.get(status,"badge-queued")
    if status == "complete":
        files_btn = f'<a href="/job/{job_id}/files" class="btn btn-primary btn-sm">View Files</a>'
    elif status == "partial":
        files_btn = (f'<button onclick="resumeJob(\'{job_id}\')" class="btn btn-primary btn-sm" '
                     f'id="resume-{job_id}" style="background:#f59e0b;border-color:#f59e0b">▶ Resume Pipeline</button>'
                     f'<a href="/job/{job_id}/files" class="btn btn-ghost btn-sm" style="margin-left:8px">Browse Files</a>')
    else:
        files_btn = ""
    stage_text = _esc(job["current_stage"] or "")
    created = job["created_at"] or ""
    shimmer_cls = "stage-shimmer" if status == "running" else ""
    timer_cls = "" if status == "running" else "stopped"

    # ── CSS (plain string) ──
    feed_css = '''<style>
    .pl-timeline{display:flex;gap:0;margin-bottom:16px;padding:10px 14px;background:var(--bg-surface);border:1px solid var(--border);border-radius:var(--radius-lg);overflow-x:auto}
    .pl-stage{flex:1;display:flex;align-items:center;gap:5px;padding:5px 6px;font-size:10px;color:var(--text-dim);white-space:nowrap;transition:all 0.4s}
    .pl-dot{width:7px;height:7px;border-radius:50%;background:var(--border);flex-shrink:0;transition:all 0.5s}
    .pl-stage.done .pl-dot{background:#22c55e;box-shadow:0 0 6px #22c55e44}
    .pl-stage.active .pl-dot{background:#7c6aef;box-shadow:0 0 10px #7c6aef88;animation:plp 1.5s ease infinite}
    .pl-stage.active{color:var(--text-bright);font-weight:600}
    .pl-stage.done{color:#22c55e}
    .pl-stage.skipped .pl-dot{background:#f59e0b;box-shadow:0 0 6px #f59e0b44}
    .pl-stage.skipped{color:#f59e0b;text-decoration:line-through;opacity:0.7}
    .pl-stage::after{content:'';flex:1;height:1px;background:var(--border);margin:0 4px}
    .pl-stage:last-child::after{display:none}
    .pl-stage.done::after{background:rgba(34,197,94,0.3)}
    @keyframes plp{0%,100%{transform:scale(1)}50%{transform:scale(1.5)}}
    .feed-timer{font-family:'Geist Mono',monospace;font-size:11px;color:var(--text-dim);display:flex;align-items:center;gap:5px}
    .tdot{width:6px;height:6px;border-radius:50%;background:#22c55e;animation:tblink 1s step-end infinite}
    .tdot.stopped{animation:none;background:var(--text-dim)}
    @keyframes tblink{50%{opacity:0.3}}
    .mbar{display:flex;gap:14px;padding:8px 14px;background:var(--bg-surface);border:1px solid var(--border);border-radius:var(--radius-lg);margin-bottom:14px;font-size:10.5px;flex-wrap:wrap}
    .mi{display:flex;align-items:center;gap:4px;color:var(--text-dim)}.mi .mv{color:var(--text-bright);font-weight:700;font-family:'Geist Mono',monospace;transition:all 0.3s}
    .mi.ok .mv{color:#22c55e}.mi.wr .mv{color:#eab308}.mi.er .mv{color:#ef4444}
    .fwrap{overflow-y:auto;height:calc(100vh - 300px);scroll-behavior:smooth;padding-right:4px}
    .fwrap::-webkit-scrollbar{width:4px}
    .fwrap::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
    .tfeed{display:flex;flex-direction:column;gap:0;padding:0 0 32px}
    .ev{padding:7px 14px;animation:evin 0.35s ease-out;font-size:12px;line-height:1.65;color:var(--text)}
    @keyframes evin{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
    .ev-stage{padding:12px 16px;margin:14px 0 6px;background:linear-gradient(135deg,rgba(124,106,239,0.08),rgba(79,70,229,0.02));border:1px solid rgba(124,106,239,0.12);border-radius:10px;border-left:3px solid #7c6aef;display:flex;align-items:center;gap:10px;font-weight:600;color:var(--text-bright);flex-wrap:wrap}
    .ev-stage .sn{font-size:9px;padding:2px 7px;border-radius:4px;background:rgba(124,106,239,0.15);color:#a78bfa;font-weight:700;letter-spacing:0.5px}
    .ev-stage .sd{font-size:11px;color:var(--text-muted);font-weight:400;margin-left:auto;max-width:55%}
    .ev-ok{padding:6px 14px;border-left:2px solid rgba(34,197,94,0.3);color:#22c55e;font-weight:500;font-size:12px}
    .ev-wr{padding:6px 14px;border-left:2px solid rgba(234,179,8,0.3);color:#eab308;font-size:12px}
    .ev-er{padding:8px 14px;background:rgba(239,68,68,0.05);border:1px solid rgba(239,68,68,0.12);border-radius:8px;border-left:3px solid #ef4444;color:#ef4444;font-weight:500;margin:4px 0;font-size:12px}
    .ev-agent{display:flex;align-items:flex-start;gap:10px;padding:8px 14px;margin:3px 0}
    .aav{width:28px;height:28px;border-radius:7px;display:flex;align-items:center;justify-content:center;font-size:13px;flex-shrink:0}
    .aav-r{background:rgba(59,130,246,0.12)}.aav-m{background:rgba(249,115,22,0.12)}.aav-d{background:rgba(236,72,153,0.12)}.aav-l{background:rgba(168,85,247,0.12)}.aav-p{background:rgba(20,184,166,0.12)}.aav-q{background:rgba(239,68,68,0.12)}
    .abody{flex:1;min-width:0}
    .aname{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:2px}
    .an-r{color:#60a5fa}.an-m{color:#f97316}.an-d{color:#ec4899}.an-l{color:#a855f7}.an-p{color:#14b8a6}.an-q{color:#ef4444}
    .atask{font-size:11.5px;color:var(--text-muted)}
    .atyp{display:inline-flex;gap:3px;margin-left:6px;vertical-align:middle}
    .atyp span{width:3px;height:3px;border-radius:50%;background:currentColor;opacity:0.4;animation:atya 1.2s ease-in-out infinite}
    .atyp span:nth-child(2){animation-delay:0.15s}
    .atyp span:nth-child(3){animation-delay:0.3s}
    @keyframes atya{0%,60%,100%{opacity:0.3;transform:translateY(0)}30%{opacity:1;transform:translateY(-3px)}}
    .ev-ooda{padding:10px 14px;margin:6px 0;background:linear-gradient(135deg,rgba(249,115,22,0.06),transparent);border:1px solid rgba(249,115,22,0.1);border-radius:8px;display:flex;align-items:center;gap:10px;font-size:12px}
    .ooda-b{font-size:9px;padding:3px 7px;border-radius:4px;background:rgba(249,115,22,0.15);color:#f97316;font-weight:700;letter-spacing:0.5px}
    .ooda-s{width:12px;height:12px;border:2px solid rgba(249,115,22,0.2);border-top-color:#f97316;border-radius:50%;animation:ospin 0.8s linear infinite;flex-shrink:0}
    @keyframes ospin{to{transform:rotate(360deg)}}
    .ev-or{padding:10px 14px;margin:4px 0;border-radius:8px;display:flex;align-items:center;gap:8px;font-weight:500;font-size:12px}
    .ev-or.pass{background:rgba(34,197,94,0.06);border:1px solid rgba(34,197,94,0.12);color:#22c55e}
    .ev-or.fail{background:rgba(249,115,22,0.06);border:1px solid rgba(249,115,22,0.12);color:#f97316}
    .ev-par{padding:10px 14px;margin:4px 0;background:rgba(79,70,229,0.03);border:1px solid rgba(79,70,229,0.08);border-radius:8px;font-size:12px}
    .ptracks{display:flex;gap:6px;margin-top:5px;flex-wrap:wrap}
    .ptrack{font-size:10px;padding:3px 8px;border-radius:4px;background:rgba(124,106,239,0.1);color:#a78bfa;display:flex;align-items:center;gap:4px}
    .pdot{width:4px;height:4px;border-radius:50%;background:#7c6aef;animation:plp 1.5s ease infinite}
    .ev-met{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;margin:2px 4px;background:rgba(124,106,239,0.06);border:1px solid rgba(124,106,239,0.1);border-radius:6px;font-size:11px}
    .ev-met .mv{color:var(--text-bright);font-weight:700;font-family:'Geist Mono',monospace}
    .ev-done{padding:18px;margin:16px 0 0;border-radius:12px;text-align:center;font-size:15px;font-weight:700}
    .ev-done.success{background:linear-gradient(135deg,rgba(34,197,94,0.1),rgba(34,197,94,0.02));border:1px solid rgba(34,197,94,0.2);color:#22c55e}
    .ev-done.fail{background:linear-gradient(135deg,rgba(239,68,68,0.1),rgba(239,68,68,0.02));border:1px solid rgba(239,68,68,0.2);color:#ef4444}
    .ev-dim{color:var(--text-dim);font-size:10.5px;padding:2px 14px}
    .ev-th{padding:8px 12px;margin:2px 14px;background:rgba(255,255,255,0.015);border-radius:6px;border:1px solid rgba(255,255,255,0.04);font-size:11px;color:var(--text-dim);font-family:'Geist Mono',monospace;line-height:1.55;max-height:80px;overflow:hidden;cursor:pointer;transition:max-height 0.3s;position:relative}
    .ev-th.exp{max-height:none}
    .ev-th::after{content:'click to expand';position:absolute;bottom:0;left:0;right:0;height:22px;background:linear-gradient(transparent,var(--bg));font-size:9px;display:flex;align-items:flex-end;justify-content:center;color:var(--text-dim);padding-bottom:2px}
    .ev-th.exp::after{display:none}
    .ev-th.short{max-height:none;cursor:default}.ev-th.short::after{display:none}
    .ev-skip{padding:10px 14px;margin:6px 0;background:linear-gradient(135deg,rgba(245,158,11,0.08),rgba(245,158,11,0.02));border:1px solid rgba(245,158,11,0.15);border-radius:8px;border-left:3px solid #f59e0b;font-size:12px}
    .ev-skip .skip-icon{font-size:14px;margin-right:2px}
    .ev-skip .skip-label{font-weight:700;color:#f59e0b;font-size:10px;padding:1px 6px;border-radius:3px;background:rgba(245,158,11,0.15);letter-spacing:0.5px}
    .ev-skip .skip-name{font-weight:600;color:var(--text-bright);margin-left:4px}
    .ev-skip .skip-tag{font-size:9px;padding:1px 5px;border-radius:3px;background:rgba(245,158,11,0.12);color:#fbbf24;margin-left:6px;font-weight:600}
    .ev-skip .skip-reason{color:var(--text-muted);font-size:11px;margin-top:4px}
    .ev-skip .skip-budget{color:var(--text-dim);font-size:10px;margin-top:2px;font-family:'Geist Mono',monospace}
    .ev-compress{padding:10px 14px;margin:6px 0;background:linear-gradient(135deg,rgba(6,182,212,0.08),rgba(6,182,212,0.02));border:1px solid rgba(6,182,212,0.15);border-radius:8px;border-left:3px solid #06b6d4;font-size:12px}
    .ev-compress .compress-icon{font-size:14px;margin-right:2px}
    .ev-compress .compress-label{font-weight:700;color:#06b6d4;font-size:10px;padding:1px 6px;border-radius:3px;background:rgba(6,182,212,0.15);letter-spacing:0.5px}
    .ev-compress .compress-name{font-weight:600;color:var(--text-bright);margin-left:4px}
    .ev-compress .skip-tag{font-size:9px;padding:1px 5px;border-radius:3px;background:rgba(6,182,212,0.12);color:#67e8f9;margin-left:6px;font-weight:600}
    .ev-compress .skip-reason{color:var(--text-muted);font-size:11px;margin-top:4px}
    .ev-compress .skip-budget{color:var(--text-dim);font-size:10px;margin-top:2px;font-family:'Geist Mono',monospace}
    #rawLog{font-family:'Geist Mono',monospace;font-size:10.5px;line-height:1.7;color:var(--text-dim);background:var(--bg-surface);border:1px solid var(--border);border-radius:var(--radius-lg);padding:14px;overflow:auto;height:calc(100vh - 300px);white-space:pre-wrap}
    </style>'''

    # ── HTML (f-string — simple interpolations only) ──
    html = f'''{feed_css}
    <div style="margin-bottom:10px"><a href="/history" style="color:var(--text-dim);font-size:12px;text-decoration:none" onmouseover="this.style.color='var(--text-bright)'" onmouseout="this.style.color='var(--text-dim)'">&larr; Back</a></div>
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
        <div>
            <h2 style="font-size:18px;font-weight:600;color:var(--text-bright);margin-bottom:3px;letter-spacing:-0.02em">{_esc(job["title"])}</h2>
            <div style="font-size:12px;color:var(--text-muted);display:flex;align-items:center;gap:8px">
                <span id="jobStatus" class="badge {badge_class}">{status}</span>
                <span id="jobStage" class="{shimmer_cls}" style="font-size:12px">{stage_text}</span>
                <span class="feed-timer"><span class="tdot {timer_cls}" id="timerDot"></span> <span id="elapsed">0:00</span></span>
            </div>
        </div>
        <div style="display:flex;gap:6px" id="actionBtns">
            <button onclick="window._tRaw()" class="btn btn-ghost btn-sm" id="rawBtn" title="Toggle raw log view">raw</button>
            <button onclick="window._sBot()" class="btn btn-ghost btn-sm">&#8595;</button>
            {files_btn}
        </div>
    </div>
    <div id="jobData" data-job-id="{job_id}" data-status="{status}" data-created="{created}" style="display:none"></div>
    <div id="stageTimeline" class="pl-timeline">
        <div class="pl-stage" data-s="preflight"><span class="pl-dot"></span>Pre-flight</div>
        <div class="pl-stage" data-s="research"><span class="pl-dot"></span>Research</div>
        <div class="pl-stage" data-s="design"><span class="pl-dot"></span>Design</div>
        <div class="pl-stage" data-s="art"><span class="pl-dot"></span>Art</div>
        <div class="pl-stage" data-s="production"><span class="pl-dot"></span>Production</div>
        <div class="pl-stage" data-s="package"><span class="pl-dot"></span>Package</div>
    </div>
    <div class="mbar" id="mbar">
        <div class="mi ok">&check; <span class="mv" id="mOk">0</span> pass</div>
        <div class="mi wr">&#9888; <span class="mv" id="mWr">0</span> warn</div>
        <div class="mi er">&times; <span class="mv" id="mEr">0</span> err</div>
        <div class="mi">&#8634; <span class="mv" id="mOo">0</span> OODA</div>
        <div class="mi" style="margin-left:auto">&#9881; <span class="mv" id="mLn">0</span></div>
    </div>
    <div class="fwrap" id="fw"><div class="tfeed" id="tf"></div></div>
    <pre id="rawLog" style="display:none"></pre>'''

    # ── JS (plain string — no f-string) ──
    js = '<script src="/static/thought-feed.js"></script>'
    resume_js = """<script>
function resumeJob(jobId) {
    var btn = document.getElementById('resume-' + jobId);
    if (btn) { btn.disabled = true; btn.textContent = 'Resuming...'; }
    fetch('/api/resume/' + jobId, {method: 'POST'})
        .then(function(r) { return r.json(); })
        .then(function(d) {
            if (d.error) { alert('Resume failed: ' + d.error); if (btn) { btn.disabled = false; btn.textContent = '▶ Resume Pipeline'; } }
            else { location.reload(); }
        })
        .catch(function() { alert('Resume request failed'); if (btn) { btn.disabled = false; btn.textContent = '▶ Resume Pipeline'; } });
}
</script>"""

    return layout(html + js + resume_js, "history")




# ─── BACKGROUND WORKERS (subprocess-based) ───

# Track running subprocesses for status polling
_running_procs = {}  # job_id → Popen

def _cleanup_finished():
    """Remove completed subprocesses from tracking dict."""
    for jid in list(_running_procs):
        proc = _running_procs[jid]
        if proc.poll() is not None:
            _running_procs.pop(jid, None)

def _spawn_worker(job_id, job_type, *args):
    """Spawn a worker subprocess. No import locks, no deadlocks."""
    _cleanup_finished()
    worker_path = Path(__file__).parent / "worker.py"
    cmd = ["python3", "-u", str(worker_path), job_type, job_id] + list(args)
    env = {
        **os.environ,
        "DB_PATH": DB_PATH,
        "LOG_DIR": str(LOG_DIR),
        # ── Kill CrewAI tracing prompt ──
        "CREWAI_TELEMETRY_OPT_OUT": "true",
        "OTEL_SDK_DISABLED": "true",
        "CREWAI_TRACING_ENABLED": "false",
        "DO_NOT_TRACK": "1",
        # ── OpenAI SDK retry config ──
        "OPENAI_MAX_RETRIES": "5",
        "OPENAI_TIMEOUT": "120",
    }
    proc = subprocess.Popen(
        cmd, env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(Path(__file__).parent),
        start_new_session=True,     # Detach from gunicorn — survives web server restarts
    )
    _running_procs[job_id] = proc


# ─── HEALTH CHECK (for Railway / load balancer monitoring) ───

@app.route("/health")
def health_check():
    """Health check — verifies web server + database are responsive."""
    try:
        db = get_db()
        db.execute("SELECT 1").fetchone()
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 503


# ─── CUSTOM ERROR PAGES ───

@app.errorhandler(404)
def error_404(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Not found"}), 404
    return layout(
        '<div class="card" style="text-align:center;padding:48px">'
        '<h2 style="font-size:48px;font-weight:800;color:var(--text-bright);margin-bottom:8px">404</h2>'
        '<p style="color:var(--text-muted);margin-bottom:24px">The page you\'re looking for doesn\'t exist.</p>'
        '<a href="/" class="btn btn-primary">Go Home</a></div>'
    ), 404

@app.errorhandler(500)
def error_500(e):
    logger.error(f"500 error: {e}", exc_info=True)
    if request.path.startswith("/api/"):
        return jsonify({"error": "Internal server error"}), 500
    return layout(
        '<div class="card" style="text-align:center;padding:48px">'
        '<h2 style="font-size:48px;font-weight:800;color:var(--text-bright);margin-bottom:8px">500</h2>'
        '<p style="color:var(--text-muted);margin-bottom:24px">Something went wrong. Try again or check your pipeline logs.</p>'
        '<div style="display:flex;gap:8px;justify-content:center">'
        '<a href="/history" class="btn btn-primary">View History</a>'
        '<a href="/" class="btn btn-ghost">Go Home</a></div></div>'
    ), 500

@app.errorhandler(429)
def error_429(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": str(e)}), 429
    return layout(
        '<div class="card" style="text-align:center;padding:48px">'
        '<h2 style="font-size:24px;font-weight:700;color:var(--text-bright);margin-bottom:8px">Too Many Jobs</h2>'
        f'<p style="color:var(--text-muted);margin-bottom:24px">You have {MAX_CONCURRENT_JOBS} jobs in progress. Please wait for one to finish.</p>'
        '<a href="/history" class="btn btn-primary">View History</a></div>'
    ), 429


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    logger.info(f"ARKAINBRAIN — http://localhost:{port}")
    app.run(debug=os.getenv("FLASK_DEBUG","false").lower()=="true", host="0.0.0.0", port=port)
