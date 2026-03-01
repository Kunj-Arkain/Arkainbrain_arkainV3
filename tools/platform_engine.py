"""
ARKAINBRAIN — Platform Integration Engine (Phase 6)

Server-side game engine that moves outcome determination server-side
for real-money play. Provides:

  1. Game Sessions: create → play rounds → verify → close
  2. Server-Side RNG: outcomes computed server-side, hash-committed
  3. Bet/Win Tracking: balance management with transaction log
  4. Analytics: per-game and per-player statistics
  5. Operator Dashboard: revenue, RTP monitoring, player stats
  6. Game Library: searchable catalog with metadata
  7. Progressive Jackpots: cross-game contribution pool
  8. A/B Deployment: variant serving per user segment

Usage:
    from tools.platform_engine import PlatformEngine
    engine = PlatformEngine(db_path="platform.db")
    session = engine.create_session(user_id="u1", game_type="crash", balance=100)
    result = engine.play_round(session.id, bet_amount=1.0)
    engine.close_session(session.id)
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Optional


# ═══════════════════════════════════════════════════════════════
# Data Models
# ═══════════════════════════════════════════════════════════════

@dataclass
class GameSession:
    id: str
    user_id: str
    game_type: str
    game_id: str            # specific game variant
    balance: float
    initial_balance: float
    server_seed: str
    server_seed_hash: str
    client_seed: str
    nonce: int = 0
    rounds_played: int = 0
    total_wagered: float = 0.0
    total_won: float = 0.0
    status: str = "active"  # active / closed / expired
    created_at: str = ""
    closed_at: str = ""
    ab_variant: str = ""    # A/B test variant ID

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.utcnow().isoformat()


@dataclass
class RoundResult:
    session_id: str
    round_id: str
    nonce: int
    game_type: str
    bet_amount: float
    outcome: dict           # game-specific outcome
    multiplier: float
    payout: float
    balance_after: float
    combined_hash: str
    jackpot_contribution: float = 0.0
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.utcnow().isoformat()


@dataclass
class JackpotPool:
    id: str
    name: str
    current_amount: float
    contribution_rate: float  # fraction of each bet
    seed_amount: float
    max_amount: float
    last_won_at: str = ""
    last_won_by: str = ""
    total_contributions: float = 0.0
    total_payouts: float = 0.0


@dataclass
class GameLibraryEntry:
    id: str
    game_type: str
    title: str
    theme: str
    filename: str
    rtp: float
    house_edge: float
    volatility: str
    max_win: float
    tags: list[str]
    thumbnail: str = ""
    play_count: int = 0
    total_wagered: float = 0.0
    avg_session_minutes: float = 0.0
    rating: float = 0.0
    status: str = "active"   # active / paused / retired
    ab_variants: list[str] = field(default_factory=list)
    created_at: str = ""
    config_json: str = ""


# ═══════════════════════════════════════════════════════════════
# Database Schema
# ═══════════════════════════════════════════════════════════════

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS game_sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    game_type TEXT NOT NULL,
    game_id TEXT NOT NULL DEFAULT '',
    balance REAL NOT NULL,
    initial_balance REAL NOT NULL,
    server_seed TEXT NOT NULL,
    server_seed_hash TEXT NOT NULL,
    client_seed TEXT NOT NULL,
    nonce INTEGER DEFAULT 0,
    rounds_played INTEGER DEFAULT 0,
    total_wagered REAL DEFAULT 0,
    total_won REAL DEFAULT 0,
    status TEXT DEFAULT 'active',
    ab_variant TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS game_rounds (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    nonce INTEGER NOT NULL,
    game_type TEXT NOT NULL,
    bet_amount REAL NOT NULL,
    outcome_json TEXT NOT NULL,
    multiplier REAL NOT NULL,
    payout REAL NOT NULL,
    balance_after REAL NOT NULL,
    combined_hash TEXT NOT NULL,
    jackpot_contribution REAL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (session_id) REFERENCES game_sessions(id)
);

CREATE TABLE IF NOT EXISTS game_library (
    id TEXT PRIMARY KEY,
    game_type TEXT NOT NULL,
    title TEXT NOT NULL,
    theme TEXT NOT NULL,
    filename TEXT NOT NULL,
    rtp REAL NOT NULL,
    house_edge REAL NOT NULL,
    volatility TEXT DEFAULT 'medium',
    max_win REAL DEFAULT 1000,
    tags_json TEXT DEFAULT '[]',
    thumbnail TEXT DEFAULT '',
    play_count INTEGER DEFAULT 0,
    total_wagered REAL DEFAULT 0,
    avg_session_minutes REAL DEFAULT 0,
    rating REAL DEFAULT 0,
    status TEXT DEFAULT 'active',
    ab_variants_json TEXT DEFAULT '[]',
    config_json TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS jackpot_pools (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    current_amount REAL NOT NULL DEFAULT 0,
    contribution_rate REAL NOT NULL DEFAULT 0.005,
    seed_amount REAL NOT NULL DEFAULT 100,
    max_amount REAL NOT NULL DEFAULT 10000,
    last_won_at TEXT,
    last_won_by TEXT,
    total_contributions REAL DEFAULT 0,
    total_payouts REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS analytics_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    user_id TEXT,
    session_id TEXT,
    game_type TEXT,
    game_id TEXT,
    data_json TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_rounds_session ON game_rounds(session_id);
CREATE INDEX IF NOT EXISTS idx_rounds_game ON game_rounds(game_type);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON game_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_game ON game_sessions(game_type);
CREATE INDEX IF NOT EXISTS idx_analytics_type ON analytics_events(event_type);
CREATE INDEX IF NOT EXISTS idx_analytics_game ON analytics_events(game_type);
CREATE INDEX IF NOT EXISTS idx_library_type ON game_library(game_type);
CREATE INDEX IF NOT EXISTS idx_library_status ON game_library(status);
"""


# ═══════════════════════════════════════════════════════════════
# RNG Integration
# ═══════════════════════════════════════════════════════════════

def _generate_server_seed() -> tuple[str, str]:
    """Generate a server seed and its hash commitment."""
    seed = hashlib.sha256(os.urandom(32)).hexdigest()
    seed_hash = hashlib.sha256(seed.encode()).hexdigest()
    return seed, seed_hash


def _derive_hash(server_seed: str, client_seed: str, nonce: int) -> str:
    """HMAC-SHA256 derivation for provably-fair outcomes."""
    import hmac
    msg = f"{client_seed}:{nonce}".encode()
    key = server_seed.encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def _hash_to_float(hex_hash: str, offset: int = 0) -> float:
    """Convert 4 bytes of hash to float in [0, 1)."""
    h = int(hex_hash[offset:offset+8], 16)
    return h / 0xFFFFFFFF


def _hash_to_int(hex_hash: str, max_val: int, offset: int = 0) -> int:
    """Convert hash bytes to integer in [0, max_val)."""
    return int(_hash_to_float(hex_hash, offset) * max_val)


# ═══════════════════════════════════════════════════════════════
# Server-Side Outcome Generators
# ═══════════════════════════════════════════════════════════════

def _resolve_crash(combined: str, config: dict) -> dict:
    he = config.get("house_edge", 0.03)
    max_mult = config.get("max_multiplier", 100)
    r = _hash_to_float(combined)
    if r < he:
        return {"crash_point": 1.0, "bust": True, "multiplier": 0.0}
    cp = min(math.floor(((1 - he) / (1 - r)) * 100) / 100, max_mult)
    return {"crash_point": cp, "bust": False, "multiplier": cp}


def _resolve_plinko(combined: str, config: dict) -> dict:
    rows = config.get("rows", 12)
    path = []
    for i in range(rows):
        bit = int(combined[i * 2:(i * 2) + 2], 16) % 2
        path.append("R" if bit else "L")
    bucket = sum(1 for p in path if p == "R")
    mults = config.get("multipliers", [])
    mult = mults[bucket] if bucket < len(mults) else 1.0
    return {"bucket": bucket, "path": "".join(path), "multiplier": mult}


def _resolve_mines(combined: str, config: dict) -> dict:
    grid = config.get("grid_size", 25)
    mines = config.get("mine_count", 5)
    positions = []
    available = list(range(grid))
    for i in range(mines):
        idx = _hash_to_int(combined, len(available), offset=i * 8)
        positions.append(available.pop(idx))
    return {"mine_positions": sorted(positions), "grid_size": grid}


def _resolve_dice(combined: str, config: dict) -> dict:
    r = _hash_to_float(combined)
    roll = round(r * 100, 2)
    return {"roll": roll}


def _resolve_wheel(combined: str, config: dict) -> dict:
    segments = config.get("segments", [])
    n = len(segments) if segments else 20
    idx = _hash_to_int(combined, n)
    mult = segments[idx].get("mult", 0) if idx < len(segments) else 0
    label = segments[idx].get("label", f"{mult}x") if idx < len(segments) else "?"
    return {"segment": idx, "label": label, "multiplier": mult}


def _resolve_hilo(combined: str, config: dict) -> dict:
    r = _hash_to_float(combined)
    value = int(r * 13) + 1
    suits = ["♠", "♥", "♦", "♣"]
    suit = suits[_hash_to_int(combined, 4, offset=8)]
    return {"value": value, "suit": suit, "card": f"{value}{suit}"}


def _resolve_chicken(combined: str, config: dict) -> dict:
    cols = config.get("columns", 4)
    hazards = config.get("hazards_per_lane", 1)
    hazard_cols = []
    available = list(range(cols))
    for h in range(hazards):
        idx = _hash_to_int(combined, len(available), offset=h * 8)
        hazard_cols.append(available.pop(idx))
    safe = sorted(set(range(cols)) - set(hazard_cols))
    return {"hazard_columns": sorted(hazard_cols), "safe_columns": safe}


def _resolve_scratch(combined: str, config: dict) -> dict:
    win_prob = config.get("win_probability", 0.3)
    r = _hash_to_float(combined)
    is_win = r < win_prob
    symbols = config.get("symbols", [])
    if is_win and symbols:
        sym_idx = _hash_to_int(combined, len(symbols), offset=8)
        mult = symbols[sym_idx].get("mult", 1)
    else:
        sym_idx = -1
        mult = 0
    return {"is_win": is_win, "symbol_index": sym_idx, "multiplier": mult}


RESOLVERS = {
    "crash": _resolve_crash,
    "plinko": _resolve_plinko,
    "mines": _resolve_mines,
    "dice": _resolve_dice,
    "wheel": _resolve_wheel,
    "hilo": _resolve_hilo,
    "chicken": _resolve_chicken,
    "scratch": _resolve_scratch,
}


# ═══════════════════════════════════════════════════════════════
# Platform Engine
# ═══════════════════════════════════════════════════════════════

class PlatformEngine:
    """Server-side game engine for real-money play."""

    def __init__(self, db_path: str = "platform.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        db = sqlite3.connect(self.db_path)
        db.executescript(SCHEMA_SQL)
        db.commit()
        db.close()

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        return db

    # ─── Session Management ───────────────────────────────────

    def create_session(
        self,
        user_id: str,
        game_type: str,
        balance: float,
        game_id: str = "",
        client_seed: str = "",
        ab_variant: str = "",
    ) -> GameSession:
        """Create a new provably-fair game session."""
        session_id = str(uuid.uuid4())[:12]
        server_seed, seed_hash = _generate_server_seed()
        if not client_seed:
            client_seed = hashlib.sha256(os.urandom(16)).hexdigest()[:16]

        session = GameSession(
            id=session_id,
            user_id=user_id,
            game_type=game_type,
            game_id=game_id,
            balance=balance,
            initial_balance=balance,
            server_seed=server_seed,
            server_seed_hash=seed_hash,
            client_seed=client_seed,
            ab_variant=ab_variant,
        )

        db = self._db()
        db.execute(
            """INSERT INTO game_sessions
               (id, user_id, game_type, game_id, balance, initial_balance,
                server_seed, server_seed_hash, client_seed, ab_variant)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session.id, user_id, game_type, game_id, balance, balance,
             server_seed, seed_hash, client_seed, ab_variant),
        )
        db.commit()
        db.close()

        self._track_event("session_start", user_id=user_id,
                          session_id=session_id, game_type=game_type,
                          data={"balance": balance, "game_id": game_id})
        return session

    def play_round(
        self,
        session_id: str,
        bet_amount: float,
        game_config: dict = None,
        player_action: dict = None,
    ) -> RoundResult:
        """Execute a single round with server-side RNG.

        Args:
            session_id: Active session ID
            bet_amount: Wager amount
            game_config: Game-specific config (house_edge, segments, etc.)
            player_action: Player's choice (e.g. cashout target, threshold)

        Returns:
            RoundResult with outcome, payout, and new balance
        """
        db = self._db()
        row = db.execute("SELECT * FROM game_sessions WHERE id=?",
                         (session_id,)).fetchone()
        if not row:
            db.close()
            raise ValueError(f"Session not found: {session_id}")
        if row["status"] != "active":
            db.close()
            raise ValueError(f"Session {session_id} is {row['status']}")
        if bet_amount > row["balance"]:
            db.close()
            raise ValueError(f"Insufficient balance: {row['balance']:.2f} < {bet_amount:.2f}")
        if bet_amount <= 0:
            db.close()
            raise ValueError("Bet must be positive")

        game_type = row["game_type"]
        nonce = row["nonce"]
        server_seed = row["server_seed"]
        client_seed = row["client_seed"]

        # Generate outcome
        combined = _derive_hash(server_seed, client_seed, nonce)
        resolver = RESOLVERS.get(game_type)
        if not resolver:
            db.close()
            raise ValueError(f"Unknown game type: {game_type}")

        config = game_config or {}
        outcome = resolver(combined, config)

        # Calculate payout
        multiplier = outcome.get("multiplier", 0.0)

        # Apply player action for games that need it
        if player_action and game_type == "crash":
            cashout_at = player_action.get("cashout_at", 0)
            if outcome["crash_point"] >= cashout_at > 0:
                multiplier = cashout_at
            else:
                multiplier = 0.0
        elif player_action and game_type == "dice":
            threshold = player_action.get("threshold", 50)
            direction = player_action.get("direction", "over")
            roll = outcome["roll"]
            edge_pct = config.get("edge_pct", 97)
            if direction == "over":
                chance = (100 - threshold) / 100
            else:
                chance = threshold / 100
            win = (roll > threshold) if direction == "over" else (roll < threshold)
            mult = math.floor((edge_pct / (chance * 100)) * 100) / 100 if chance > 0 else 0
            multiplier = mult if win else 0.0
            outcome["player_wins"] = win
            outcome["multiplier"] = multiplier

        payout = round(bet_amount * multiplier, 2)
        new_balance = round(row["balance"] - bet_amount + payout, 2)

        # Jackpot contribution
        jp_contrib = self._process_jackpot(db, bet_amount, game_type)

        # Create round record
        round_id = str(uuid.uuid4())[:10]
        result = RoundResult(
            session_id=session_id,
            round_id=round_id,
            nonce=nonce,
            game_type=game_type,
            bet_amount=bet_amount,
            outcome=outcome,
            multiplier=multiplier,
            payout=payout,
            balance_after=new_balance,
            combined_hash=combined,
            jackpot_contribution=jp_contrib,
        )

        # Update DB
        db.execute(
            """INSERT INTO game_rounds
               (id, session_id, nonce, game_type, bet_amount, outcome_json,
                multiplier, payout, balance_after, combined_hash, jackpot_contribution)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (round_id, session_id, nonce, game_type, bet_amount,
             json.dumps(outcome), multiplier, payout, new_balance,
             combined, jp_contrib),
        )
        db.execute(
            """UPDATE game_sessions
               SET balance=?, nonce=nonce+1, rounds_played=rounds_played+1,
                   total_wagered=total_wagered+?, total_won=total_won+?
               WHERE id=?""",
            (new_balance, bet_amount, payout, session_id),
        )
        # Update game library stats
        if row["game_id"]:
            db.execute(
                """UPDATE game_library
                   SET play_count=play_count+1, total_wagered=total_wagered+?
                   WHERE id=?""",
                (bet_amount, row["game_id"]),
            )
        db.commit()
        db.close()

        self._track_event("round_played", user_id=row["user_id"],
                          session_id=session_id, game_type=game_type,
                          data={"bet": bet_amount, "mult": multiplier,
                                "payout": payout, "balance": new_balance})
        return result

    def close_session(self, session_id: str) -> dict:
        """Close a session and reveal the server seed for verification."""
        db = self._db()
        row = db.execute("SELECT * FROM game_sessions WHERE id=?",
                         (session_id,)).fetchone()
        if not row:
            db.close()
            raise ValueError(f"Session not found: {session_id}")

        now = datetime.utcnow().isoformat()
        db.execute(
            "UPDATE game_sessions SET status='closed', closed_at=? WHERE id=?",
            (now, session_id),
        )
        db.commit()
        db.close()

        profit = row["total_won"] - row["total_wagered"]
        self._track_event("session_close", user_id=row["user_id"],
                          session_id=session_id, game_type=row["game_type"],
                          data={"rounds": row["rounds_played"],
                                "wagered": row["total_wagered"],
                                "won": row["total_won"],
                                "profit": profit})
        return {
            "session_id": session_id,
            "server_seed": row["server_seed"],
            "server_seed_hash": row["server_seed_hash"],
            "client_seed": row["client_seed"],
            "rounds_played": row["rounds_played"],
            "total_wagered": row["total_wagered"],
            "total_won": row["total_won"],
            "profit": round(profit, 2),
        }

    def verify_round(self, session_id: str, nonce: int) -> dict:
        """Verify a specific round after session is closed."""
        db = self._db()
        sess = db.execute("SELECT * FROM game_sessions WHERE id=?",
                          (session_id,)).fetchone()
        if not sess:
            db.close()
            raise ValueError(f"Session not found: {session_id}")
        if sess["status"] != "closed":
            db.close()
            raise ValueError("Session must be closed to verify rounds")

        rnd = db.execute(
            "SELECT * FROM game_rounds WHERE session_id=? AND nonce=?",
            (session_id, nonce),
        ).fetchone()
        db.close()

        if not rnd:
            raise ValueError(f"Round {nonce} not found in session {session_id}")

        expected = _derive_hash(sess["server_seed"], sess["client_seed"], nonce)
        matches = expected == rnd["combined_hash"]

        return {
            "verified": matches,
            "server_seed": sess["server_seed"],
            "client_seed": sess["client_seed"],
            "nonce": nonce,
            "expected_hash": expected,
            "stored_hash": rnd["combined_hash"],
            "outcome": json.loads(rnd["outcome_json"]),
            "multiplier": rnd["multiplier"],
            "payout": rnd["payout"],
        }

    # ─── Jackpot System ───────────────────────────────────────

    def create_jackpot(self, name: str, seed: float = 100,
                       rate: float = 0.005, max_amt: float = 10000) -> str:
        """Create a progressive jackpot pool."""
        jp_id = str(uuid.uuid4())[:8]
        db = self._db()
        db.execute(
            """INSERT INTO jackpot_pools
               (id, name, current_amount, contribution_rate, seed_amount, max_amount)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (jp_id, name, seed, rate, seed, max_amt),
        )
        db.commit()
        db.close()
        return jp_id

    def _process_jackpot(self, db: sqlite3.Connection,
                         bet_amount: float, game_type: str) -> float:
        """Add jackpot contribution from a bet. Returns contribution amount."""
        pools = db.execute(
            "SELECT * FROM jackpot_pools WHERE current_amount < max_amount"
        ).fetchall()
        total_contrib = 0.0
        for pool in pools:
            contrib = round(bet_amount * pool["contribution_rate"], 4)
            new_amount = min(pool["current_amount"] + contrib, pool["max_amount"])
            db.execute(
                """UPDATE jackpot_pools
                   SET current_amount=?, total_contributions=total_contributions+?
                   WHERE id=?""",
                (new_amount, contrib, pool["id"]),
            )
            total_contrib += contrib
        return total_contrib

    def get_jackpots(self) -> list[dict]:
        """Get all jackpot pools with current amounts."""
        db = self._db()
        rows = db.execute("SELECT * FROM jackpot_pools").fetchall()
        db.close()
        return [dict(r) for r in rows]

    def award_jackpot(self, pool_id: str, user_id: str,
                      session_id: str) -> float:
        """Award a jackpot to a user. Returns amount won."""
        db = self._db()
        pool = db.execute("SELECT * FROM jackpot_pools WHERE id=?",
                          (pool_id,)).fetchone()
        if not pool:
            db.close()
            raise ValueError(f"Jackpot pool not found: {pool_id}")

        amount = pool["current_amount"]
        now = datetime.utcnow().isoformat()
        db.execute(
            """UPDATE jackpot_pools
               SET current_amount=seed_amount, last_won_at=?,
                   last_won_by=?, total_payouts=total_payouts+?
               WHERE id=?""",
            (now, user_id, amount, pool_id),
        )
        db.commit()
        db.close()

        self._track_event("jackpot_won", user_id=user_id,
                          session_id=session_id, game_type="jackpot",
                          data={"pool_id": pool_id, "amount": amount})
        return amount

    # ─── Game Library ─────────────────────────────────────────

    def register_game(self, entry: dict) -> str:
        """Register a game in the library."""
        game_id = entry.get("id") or str(uuid.uuid4())[:8]
        db = self._db()
        db.execute(
            """INSERT OR REPLACE INTO game_library
               (id, game_type, title, theme, filename, rtp, house_edge,
                volatility, max_win, tags_json, thumbnail, status, config_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (game_id, entry["game_type"], entry["title"], entry.get("theme", ""),
             entry.get("filename", ""), entry.get("rtp", 96),
             entry.get("house_edge", 4), entry.get("volatility", "medium"),
             entry.get("max_win", 1000), json.dumps(entry.get("tags", [])),
             entry.get("thumbnail", ""), entry.get("status", "active"),
             json.dumps(entry.get("config", {}))),
        )
        db.commit()
        db.close()
        return game_id

    def search_games(self, query: str = "", game_type: str = "",
                     volatility: str = "", status: str = "active",
                     sort_by: str = "play_count",
                     limit: int = 50) -> list[dict]:
        """Search the game library."""
        db = self._db()
        sql = "SELECT * FROM game_library WHERE 1=1"
        params = []

        if status:
            sql += " AND status=?"
            params.append(status)
        if game_type:
            sql += " AND game_type=?"
            params.append(game_type)
        if volatility:
            sql += " AND volatility=?"
            params.append(volatility)
        if query:
            sql += " AND (title LIKE ? OR theme LIKE ? OR tags_json LIKE ?)"
            q = f"%{query}%"
            params.extend([q, q, q])

        valid_sorts = {"play_count": "play_count DESC",
                       "newest": "created_at DESC",
                       "rating": "rating DESC",
                       "wagered": "total_wagered DESC",
                       "rtp": "rtp DESC"}
        sql += f" ORDER BY {valid_sorts.get(sort_by, 'play_count DESC')}"
        sql += f" LIMIT {limit}"

        rows = db.execute(sql, params).fetchall()
        db.close()

        results = []
        for r in rows:
            d = dict(r)
            d["tags"] = json.loads(d.pop("tags_json", "[]"))
            d["ab_variants"] = json.loads(d.pop("ab_variants_json", "[]"))
            d["config"] = json.loads(d.pop("config_json", "{}"))
            results.append(d)
        return results

    def get_game(self, game_id: str) -> Optional[dict]:
        """Get a single game by ID."""
        db = self._db()
        row = db.execute("SELECT * FROM game_library WHERE id=?",
                         (game_id,)).fetchone()
        db.close()
        if not row:
            return None
        d = dict(row)
        d["tags"] = json.loads(d.pop("tags_json", "[]"))
        d["ab_variants"] = json.loads(d.pop("ab_variants_json", "[]"))
        d["config"] = json.loads(d.pop("config_json", "{}"))
        return d

    # ─── Analytics ────────────────────────────────────────────

    def _track_event(self, event_type: str, user_id: str = "",
                     session_id: str = "", game_type: str = "",
                     game_id: str = "", data: dict = None):
        """Track an analytics event."""
        try:
            db = self._db()
            db.execute(
                """INSERT INTO analytics_events
                   (event_type, user_id, session_id, game_type, game_id, data_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (event_type, user_id, session_id, game_type, game_id,
                 json.dumps(data or {})),
            )
            db.commit()
            db.close()
        except Exception:
            pass  # analytics should never block gameplay

    def get_player_stats(self, user_id: str) -> dict:
        """Get aggregated stats for a player."""
        db = self._db()
        sessions = db.execute(
            """SELECT COUNT(*) as n, SUM(rounds_played) as rounds,
                      SUM(total_wagered) as wagered, SUM(total_won) as won,
                      AVG(total_won/NULLIF(total_wagered,0)) as avg_rtp
               FROM game_sessions WHERE user_id=?""",
            (user_id,),
        ).fetchone()

        favorites = db.execute(
            """SELECT game_type, COUNT(*) as cnt
               FROM game_sessions WHERE user_id=?
               GROUP BY game_type ORDER BY cnt DESC LIMIT 3""",
            (user_id,),
        ).fetchall()

        recent = db.execute(
            """SELECT id, game_type, rounds_played, total_wagered, total_won,
                      status, created_at
               FROM game_sessions WHERE user_id=?
               ORDER BY created_at DESC LIMIT 10""",
            (user_id,),
        ).fetchall()
        db.close()

        return {
            "user_id": user_id,
            "total_sessions": sessions["n"] or 0,
            "total_rounds": sessions["rounds"] or 0,
            "total_wagered": round(sessions["wagered"] or 0, 2),
            "total_won": round(sessions["won"] or 0, 2),
            "profit": round((sessions["won"] or 0) - (sessions["wagered"] or 0), 2),
            "avg_rtp": round((sessions["avg_rtp"] or 0) * 100, 2),
            "favorite_games": [dict(f) for f in favorites],
            "recent_sessions": [dict(r) for r in recent],
        }

    def get_game_stats(self, game_type: str = "",
                       days: int = 30) -> dict:
        """Get operator-level game statistics."""
        db = self._db()
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()

        where = "WHERE created_at >= ?"
        params = [cutoff]
        if game_type:
            where += " AND game_type=?"
            params.append(game_type)

        agg = db.execute(
            f"""SELECT game_type,
                       COUNT(DISTINCT session_id) as sessions,
                       COUNT(*) as rounds,
                       SUM(bet_amount) as wagered,
                       SUM(payout) as won,
                       AVG(multiplier) as avg_mult,
                       MAX(multiplier) as max_mult,
                       SUM(jackpot_contribution) as jp_contrib
                FROM game_rounds {where}
                GROUP BY game_type""",
            params,
        ).fetchall()

        results = []
        for r in agg:
            wagered = r["wagered"] or 0
            won = r["won"] or 0
            ggr = wagered - won
            rtp = (won / wagered * 100) if wagered > 0 else 0
            results.append({
                "game_type": r["game_type"],
                "sessions": r["sessions"],
                "rounds": r["rounds"],
                "wagered": round(wagered, 2),
                "won": round(won, 2),
                "ggr": round(ggr, 2),
                "rtp_measured": round(rtp, 2),
                "avg_multiplier": round(r["avg_mult"] or 0, 2),
                "max_multiplier": round(r["max_mult"] or 0, 2),
                "jackpot_contributions": round(r["jp_contrib"] or 0, 2),
            })

        # Totals
        total_wagered = sum(r["wagered"] for r in results)
        total_won = sum(r["won"] for r in results)

        db.close()
        return {
            "period_days": days,
            "game_filter": game_type or "all",
            "games": results,
            "totals": {
                "wagered": round(total_wagered, 2),
                "won": round(total_won, 2),
                "ggr": round(total_wagered - total_won, 2),
                "overall_rtp": round(total_won / total_wagered * 100, 2) if total_wagered > 0 else 0,
            },
        }

    def get_realtime_dashboard(self) -> dict:
        """Real-time operator dashboard data."""
        db = self._db()

        active = db.execute(
            "SELECT COUNT(*) as n FROM game_sessions WHERE status='active'"
        ).fetchone()["n"]

        today = datetime.utcnow().strftime("%Y-%m-%d")
        today_stats = db.execute(
            """SELECT COUNT(*) as rounds, COALESCE(SUM(bet_amount),0) as wagered,
                      COALESCE(SUM(payout),0) as won
               FROM game_rounds WHERE created_at >= ?""",
            (today,),
        ).fetchone()

        jackpots = db.execute(
            "SELECT name, current_amount FROM jackpot_pools"
        ).fetchall()

        # Top games today
        top = db.execute(
            """SELECT game_type, COUNT(*) as rounds, SUM(bet_amount) as wagered
               FROM game_rounds WHERE created_at >= ?
               GROUP BY game_type ORDER BY wagered DESC LIMIT 5""",
            (today,),
        ).fetchall()

        db.close()

        wagered = today_stats["wagered"]
        won = today_stats["won"]

        return {
            "active_sessions": active,
            "today": {
                "rounds": today_stats["rounds"],
                "wagered": round(wagered, 2),
                "won": round(won, 2),
                "ggr": round(wagered - won, 2),
                "rtp": round(won / wagered * 100, 2) if wagered > 0 else 0,
            },
            "jackpots": [{"name": j["name"], "amount": j["current_amount"]}
                         for j in jackpots],
            "top_games": [dict(t) for t in top],
        }

    # ─── A/B Variant Serving ──────────────────────────────────

    def assign_variant(self, user_id: str, game_id: str) -> str:
        """Assign a user to an A/B variant for a game."""
        game = self.get_game(game_id)
        if not game or not game.get("ab_variants"):
            return ""
        variants = game["ab_variants"]
        # Deterministic assignment based on user_id hash
        h = hashlib.md5(f"{user_id}:{game_id}".encode()).hexdigest()
        idx = int(h[:8], 16) % len(variants)
        return variants[idx]
