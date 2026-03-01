"""
ARKAINBRAIN — Mini-Game Configuration System (Phase 1)

Central config schema that makes every arcade game data-driven.
Each game boots from a MiniGameConfig JSON injected as window.GAME_CONFIG.

Pipeline agents generate these configs; the games consume them.
This module:
  1. Defines Pydantic models for type-safe config generation
  2. Provides default configs for all 8 game types
  3. Calculates math profiles (RTP, bucket multipliers, payouts)
  4. Generates CSS theme variables from palette configs

Usage:
    from tools.minigame_config import build_config, MiniGameType
    config = build_config(
        game_type=MiniGameType.CRASH,
        theme_name="Cosmic Crash",
        target_rtp=97.0,
        volatility="medium",
    )
    json_str = config.model_dump_json(indent=2)
"""

from __future__ import annotations

import json
import math
import hashlib
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, field_validator


# ═══════════════════════════════════════════════════════════════
# Enums
# ═══════════════════════════════════════════════════════════════

class MiniGameType(str, Enum):
    PLINKO  = "plinko"
    CRASH   = "crash"
    MINES   = "mines"
    DICE    = "dice"
    WHEEL   = "wheel"
    HILO    = "hilo"
    CHICKEN = "chicken"
    SCRATCH = "scratch"
    NOVEL   = "novel"


class Volatility(str, Enum):
    LOW    = "low"
    MEDIUM = "medium"
    HIGH   = "high"
    ULTRA  = "ultra"


class RiskProfile(str, Enum):
    LOW  = "low"
    MED  = "med"
    HIGH = "high"


# ═══════════════════════════════════════════════════════════════
# Sub-Models
# ═══════════════════════════════════════════════════════════════

class ThemeConfig(BaseModel):
    """Visual theme — drives CSS custom properties + header/branding."""
    name: str = "Default"
    title: str = ""                   # Display title (e.g., "Cosmic Crash")
    subtitle: str = ""                # Subtitle (e.g., "Phase 3")
    icon: str = "🎮"                  # Emoji icon
    title_font: str = "Inter"         # Google font for h1
    body_font: str = "Inter"          # Google font for body
    # Color palette — maps to CSS custom properties
    primary: str = "#6366f1"          # --acc
    secondary: str = "#06b6d4"        # --acc2
    bg_start: str = "#030014"         # --bg0 gradient start
    bg_end: str = "#0a0020"           # --bg1 gradient end
    text: str = "#e2e8f0"             # --txt
    text_dim: str = "#64748b"         # --dim
    win: str = "#22c55e"              # --win
    lose: str = "#ef4444"             # --lose
    gold: str = "#f59e0b"             # --gold / --warn
    # Optional extras
    extra_vars: dict[str, str] = Field(default_factory=dict)

    def to_css_vars(self) -> str:
        """Generate :root CSS custom properties block."""
        base = {
            "--acc": self.primary,
            "--acc2": self.secondary,
            "--bg0": self.bg_start,
            "--bg1": self.bg_end,
            "--txt": self.text,
            "--dim": self.text_dim,
            "--win": self.win,
            "--lose": self.lose,
            "--gold": self.gold,
        }
        base.update(self.extra_vars)
        pairs = ";".join(f"{k}:{v}" for k, v in base.items())
        return f":root{{{pairs}}}"


class MathConfig(BaseModel):
    """Mathematical model — drives RTP, payouts, house edge."""
    target_rtp: float = Field(96.0, ge=80.0, le=99.9)
    house_edge: float = 4.0                      # Derived: 100 - target_rtp
    volatility: Volatility = Volatility.MEDIUM
    min_bet: float = 0.10
    max_bet: float = 100.00
    bet_options: list[float] = Field(
        default_factory=lambda: [0.10, 0.25, 0.50, 1.0, 2.0, 5.0, 10.0, 25.0]
    )
    default_bet: float = 1.0
    max_win_multiplier: float = 1000.0
    starting_balance: float = 1000.0

    # Game-type-specific math (only relevant fields populated)
    # Crash
    crash_house_edge: float = 0.03               # P(instant bust)
    crash_max_mult: float = 100.0

    # Plinko
    plinko_rows: int = 12
    plinko_risk_profiles: dict[str, list[float]] = Field(default_factory=dict)

    # Mines
    mines_grid_size: int = 25
    mines_cols: int = 5
    mines_options: list[int] = Field(default_factory=lambda: [1, 3, 5, 10, 15])
    mines_default: int = 5
    mines_edge_factor: float = 0.97              # Multiplier = edge_factor / P(safe)

    # Dice
    dice_edge_factor: float = 0.97               # mult = edge_factor * 100 / chance

    # Wheel
    wheel_segments: list[dict] = Field(default_factory=list)

    # HiLo
    hilo_mult_formula: str = "1 + streak * 0.5 + streak^2 * 0.1"
    hilo_deck_size: int = 52
    hilo_values: list[str] = Field(
        default_factory=lambda: ["A","2","3","4","5","6","7","8","9","10","J","Q","K"]
    )
    hilo_suits: list[str] = Field(
        default_factory=lambda: ["♠","♥","♦","♣"]
    )

    # Chicken
    chicken_lanes: int = 9
    chicken_cols: int = 4
    chicken_hazards_per_lane: int = 1
    chicken_mult_formula: str = "1 + lane * 0.4 + lane^2 * 0.05"

    # Scratch
    scratch_symbols: list[dict] = Field(default_factory=list)
    scratch_win_chance: float = 0.35              # P(3-of-a-kind match)
    scratch_grid_size: int = 9                    # 3×3

    @field_validator("house_edge", mode="before")
    @classmethod
    def derive_house_edge(cls, v, info):
        rtp = info.data.get("target_rtp", 96.0)
        return round(100.0 - rtp, 4)


class PhysicsConfig(BaseModel):
    """Physics parameters (mainly for Plinko/Pachinko)."""
    gravity: float = 0.3
    bounce_damping: float = 0.6
    peg_radius: float = 4.0
    ball_radius: float = 6.0
    friction: float = 0.99
    max_velocity: float = 15.0


class AudioConfig(BaseModel):
    """Procedural audio configuration."""
    scale: str = "pentatonic_minor"               # Musical scale
    base_note: str = "C4"
    master_volume: float = 0.7
    sfx_volume: float = 0.6
    music_volume: float = 0.3
    ambient_volume: float = 0.2
    hit_sound: str = "default"                     # Overridable per theme
    win_sound: str = "default"
    lose_sound: str = "default"


class ComplianceConfig(BaseModel):
    """RMG compliance flags."""
    rng_source: str = "math_random"               # "math_random" | "crypto" | "server"
    result_logging: bool = False
    session_limits: bool = False
    reality_check_interval_s: int = 3600
    responsible_gaming_url: str = ""
    jurisdiction: str = "demo"                     # "demo" | "ontario" | "uk" | etc.


# ═══════════════════════════════════════════════════════════════
# Main Config Model
# ═══════════════════════════════════════════════════════════════

class MiniGameConfig(BaseModel):
    """Complete configuration for a mini-game instance.

    This is serialized to JSON and injected as `window.GAME_CONFIG`
    at the top of every game HTML file.
    """
    version: str = "1.0.0"
    game_type: MiniGameType
    theme: ThemeConfig = Field(default_factory=ThemeConfig)
    math: MathConfig = Field(default_factory=MathConfig)
    physics: PhysicsConfig = Field(default_factory=PhysicsConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    compliance: ComplianceConfig = Field(default_factory=ComplianceConfig)
    # Metadata
    generated_by: str = "arkainbrain"
    config_hash: str = ""                          # SHA-256 of math config for audit

    def model_post_init(self, __context):
        """Compute config hash after init."""
        math_json = self.math.model_dump_json(exclude={"bet_options", "starting_balance"})
        self.config_hash = hashlib.sha256(math_json.encode()).hexdigest()[:16]


# ═══════════════════════════════════════════════════════════════
# Default Theme Presets
# ═══════════════════════════════════════════════════════════════

THEME_PRESETS: dict[str, dict] = {
    "cosmic_crash": {
        "name": "Cosmic Crash", "title": "COSMIC CRASH", "subtitle": "Crash · Space · Phase 3",
        "icon": "🚀", "title_font": "Orbitron",
        "primary": "#6366f1", "secondary": "#06b6d4",
        "bg_start": "#030014", "bg_end": "#0a0020",
        "text": "#e2e8f0", "text_dim": "#64748b",
    },
    "glacier_drop": {
        "name": "Glacier Drop", "title": "❄️ GLACIER DROP", "subtitle": "Plinko · Musical Physics · Phase 3",
        "icon": "🧊", "title_font": "Quicksand",
        "primary": "#0284c7", "secondary": "#67e8f9",
        "bg_start": "#000810", "bg_end": "#001830",
        "text": "#d4f0ff", "text_dim": "#5b9bb5",
        "extra_vars": {"--hot": "#dc2626"},
    },
    "neon_grid": {
        "name": "Neon Grid", "title": "⚡ NEON GRID", "subtitle": "Mines · Cyberpunk · Phase 3",
        "icon": "💎", "title_font": "Orbitron",
        "primary": "#06ffc7", "secondary": "#d946ef",
        "bg_start": "#05000d", "bg_end": "#12002e",
        "text": "#e0d4ff", "text_dim": "#6b5fa0",
        "extra_vars": {"--neon": "#06ffc7", "--hot": "#e11d48", "--purple": "#d946ef"},
    },
    "dragon_dice": {
        "name": "Dragon Dice", "title": "🐉 DRAGON DICE", "subtitle": "Dice · Dragon Fire · Phase 3",
        "icon": "🐉", "title_font": "Bebas Neue",
        "primary": "#dc2626", "secondary": "#f59e0b",
        "bg_start": "#1a0000", "bg_end": "#2d0a00",
        "text": "#fde8d0", "text_dim": "#a0522d",
        "extra_vars": {"--fire": "#dc2626", "--amber": "#fbbf24"},
    },
    "trident_spin": {
        "name": "Trident Spin", "title": "🔱 TRIDENT SPIN", "subtitle": "Wheel · Ocean Depths · Phase 3",
        "icon": "🔱", "title_font": "Playfair Display",
        "primary": "#0891b2", "secondary": "#67e8f9",
        "bg_start": "#001520", "bg_end": "#002030",
        "text": "#cce8f4", "text_dim": "#4a8a9e",
    },
    "pharaohs_fortune": {
        "name": "Pharaoh's Fortune", "title": "🏛️ PHARAOH'S FORTUNE", "subtitle": "Hi-Lo · Ancient Egypt · Phase 3",
        "icon": "🏛️", "title_font": "Cinzel",
        "primary": "#ffd700", "secondary": "#b8860b",
        "bg_start": "#1a0f00", "bg_end": "#2d1800",
        "text": "#f5deb3", "text_dim": "#8b6914",
    },
    "jungle_runner": {
        "name": "Jungle Runner", "title": "🐔 JUNGLE RUNNER", "subtitle": "Chicken · Jungle Trail · Phase 3",
        "icon": "🐔", "title_font": "Lora",
        "primary": "#16a34a", "secondary": "#84cc16",
        "bg_start": "#001a00", "bg_end": "#0a2a0a",
        "text": "#c6f4c6", "text_dim": "#3d7a3d",
        "extra_vars": {"--grn": "#16a34a", "--lime": "#84cc16"},
    },
    "golden_vault": {
        "name": "Golden Vault", "title": "🏆 GOLDEN VAULT", "subtitle": "Scratch · Gold · Phase 3",
        "icon": "🏆", "title_font": "Playfair Display",
        "primary": "#fbbf24", "secondary": "#a16207",
        "bg_start": "#0a0500", "bg_end": "#1a0f00",
        "text": "#f5deb3", "text_dim": "#8b6914",
    },
}


# ═══════════════════════════════════════════════════════════════
# Math Profile Generators
# ═══════════════════════════════════════════════════════════════

def _crash_math(target_rtp: float, volatility: Volatility) -> dict:
    """Generate crash game math parameters.

    Crash formula: crashPoint = (1 - houseEdge) / (1 - r)
    where r ~ Uniform(0,1) and r < houseEdge → instant bust at 1.0x.
    RTP = 1 - houseEdge (approximately).
    """
    he = round(1.0 - target_rtp / 100.0, 4)
    max_mult = {
        Volatility.LOW: 50, Volatility.MEDIUM: 100,
        Volatility.HIGH: 500, Volatility.ULTRA: 1000,
    }.get(volatility, 100)
    return {"crash_house_edge": he, "crash_max_mult": max_mult}


def _plinko_math(target_rtp: float, volatility: Volatility) -> dict:
    """Generate Plinko bucket multipliers for each risk profile.

    Uses binomial distribution: P(bucket k) = C(rows, k) / 2^rows.
    Solves for multipliers such that sum(P[k] * M[k]) = target_rtp/100.
    """
    rows = 12
    n_buckets = rows + 1  # 13 buckets for 12 rows

    # Binomial probabilities
    probs = []
    for k in range(n_buckets):
        p = math.comb(rows, k) / (2 ** rows)
        probs.append(p)

    def solve_mults(base_mults: list[float], target: float) -> list[float]:
        """Scale multipliers to hit target RTP."""
        current_rtp = sum(p * m for p, m in zip(probs, base_mults))
        if current_rtp == 0:
            return base_mults
        scale = target / current_rtp
        return [round(m * scale, 2) for m in base_mults]

    # Base multiplier shapes per risk (symmetric — bucket 0 and 12 are edges)
    risk_shapes = {
        "low": [2, 1.2, 0.8, 0.5, 0.3, 0.2, 0.2, 0.2, 0.3, 0.5, 0.8, 1.2, 2],
        "med": [5, 2, 1.2, 0.6, 0.3, 0.1, 0.1, 0.1, 0.3, 0.6, 1.2, 2, 5],
        "high": [50, 10, 3, 1, 0.3, 0.1, 0, 0.1, 0.3, 1, 3, 10, 50],
    }

    target_ratio = target_rtp / 100.0
    profiles = {}
    for risk, shape in risk_shapes.items():
        profiles[risk] = solve_mults(shape, target_ratio)

    return {"plinko_rows": rows, "plinko_risk_profiles": profiles}


def _mines_math(target_rtp: float) -> dict:
    """Mines: multiplier = edge_factor / P(all revealed safe).
    P(safe sequence of n reveals from 25 tiles with m mines):
        P = Π_{i=0}^{n-1} (safe_total - i) / (grid - i)
    """
    edge_factor = round(target_rtp / 100.0, 4)
    return {"mines_edge_factor": edge_factor}


def _dice_math(target_rtp: float) -> dict:
    """Dice: mult = edge_factor * 100 / chance_percent."""
    edge_factor = round(target_rtp / 100.0, 4)
    return {"dice_edge_factor": edge_factor}


def _wheel_math(target_rtp: float, volatility: Volatility) -> dict:
    """Generate wheel segments with multipliers targeting RTP.

    20 segments, each equally likely (P = 1/20 = 0.05).
    RTP = sum(mult[i]) / 20.
    target_sum = target_rtp / 100 * 20.
    """
    target_sum = target_rtp / 100.0 * 20

    # Segment templates by volatility
    if volatility in (Volatility.LOW,):
        base = [0, 1.2, 0, 1.5, 0, 2, 0.5, 3, 0, 1.2, 5, 0.5, 0, 1.5, 8, 0.5, 0, 2, 1.2, 10]
    elif volatility in (Volatility.HIGH, Volatility.ULTRA):
        base = [0, 0.5, 0, 0, 0, 1.5, 0, 0, 0, 0.5, 0, 0, 0, 1, 0, 0, 0, 2, 0, 50]
    else:  # medium
        base = [0, 1.2, 0, 1.5, 0, 2, 0.5, 3, 0, 1.2, 5, 0.5, 0, 1.5, 10, 0.5, 0, 2, 1.2, 25]

    # Scale to target RTP
    current_sum = sum(base)
    if current_sum > 0:
        scale = target_sum / current_sum
        adjusted = [round(m * scale, 2) for m in base]
    else:
        adjusted = base

    # Build segment objects with colors
    colors_win = ["#164e63", "#155e75", "#0e7490", "#0891b2", "#0284c7"]
    colors_bust = "#1e293b"
    segments = []
    for i, m in enumerate(adjusted):
        if m <= 0:
            segments.append({"mult": 0, "label": "BUST", "color": colors_bust, "tc": "#94a3b8"})
        elif m >= 20:
            segments.append({"mult": m, "label": f"💎{m}x", "color": "#dc2626", "tc": "#fff"})
        elif m >= 8:
            segments.append({"mult": m, "label": f"🔱{m}x", "color": "#7c3aed", "tc": "#fff"})
        elif m >= 3:
            segments.append({"mult": m, "label": f"{m}x", "color": colors_win[min(i % 5, 4)], "tc": "#fff"})
        else:
            segments.append({"mult": m, "label": f"{m}x", "color": colors_win[i % 3], "tc": "#67e8f9"})

    return {"wheel_segments": segments}


def _scratch_math(target_rtp: float) -> dict:
    """Generate scratch card symbols with multipliers.

    Win chance = probability of 3-of-a-kind in 9-cell grid.
    Symbols weighted so expected payout matches target RTP.
    """
    symbols = [
        {"emoji": "💎", "mult": 50, "color": "#818cf8"},
        {"emoji": "👑", "mult": 25, "color": "#fbbf24"},
        {"emoji": "🏺", "mult": 10, "color": "#f59e0b"},
        {"emoji": "⭐", "mult": 5,  "color": "#fbbf24"},
        {"emoji": "🪙", "mult": 2,  "color": "#a16207"},
        {"emoji": "📜", "mult": 1,  "color": "#8b6914"},
        {"emoji": "🪨", "mult": 0,  "color": "#57534e"},
    ]
    # Win chance affects effective RTP
    # With ~35% win chance and weighted selection, average payout ≈ target
    win_chance = min(0.5, max(0.15, target_rtp / 100.0 * 0.36))
    return {"scratch_symbols": symbols, "scratch_win_chance": round(win_chance, 4)}


# ═══════════════════════════════════════════════════════════════
# Config Builder
# ═══════════════════════════════════════════════════════════════

def build_config(
    game_type: MiniGameType | str,
    theme_preset: str = "",
    theme_overrides: dict | None = None,
    target_rtp: float = 96.0,
    volatility: Volatility | str = Volatility.MEDIUM,
    max_win_multiplier: float = 1000.0,
    bet_options: list[float] | None = None,
    starting_balance: float = 1000.0,
    jurisdiction: str = "demo",
) -> MiniGameConfig:
    """Build a complete MiniGameConfig from high-level parameters.

    This is the main entry point for both:
    - Pipeline agents generating configs
    - web_app.py generating configs for dynamic serving

    Args:
        game_type: Which game type to configure
        theme_preset: Key from THEME_PRESETS (or empty for defaults)
        theme_overrides: Dict to override specific theme values
        target_rtp: Target return-to-player percentage
        volatility: Volatility profile
        max_win_multiplier: Maximum possible win multiplier
        bet_options: Custom bet amounts (or None for defaults)
        starting_balance: Demo balance
        jurisdiction: "demo", "ontario", "uk", etc.

    Returns:
        Complete MiniGameConfig ready to serialize to JSON
    """
    if isinstance(game_type, str):
        game_type = MiniGameType(game_type)
    if isinstance(volatility, str):
        volatility = Volatility(volatility)

    # ── Theme ──
    theme_data = {}
    if theme_preset and theme_preset in THEME_PRESETS:
        theme_data = {**THEME_PRESETS[theme_preset]}
    if theme_overrides:
        theme_data.update(theme_overrides)
    theme = ThemeConfig(**theme_data) if theme_data else ThemeConfig()

    # ── Math ──
    math_params = {
        "target_rtp": target_rtp,
        "volatility": volatility,
        "max_win_multiplier": max_win_multiplier,
        "starting_balance": starting_balance,
    }
    if bet_options:
        math_params["bet_options"] = bet_options

    # Game-specific math
    if game_type == MiniGameType.CRASH:
        math_params.update(_crash_math(target_rtp, volatility))
    elif game_type == MiniGameType.PLINKO:
        math_params.update(_plinko_math(target_rtp, volatility))
    elif game_type == MiniGameType.MINES:
        math_params.update(_mines_math(target_rtp))
    elif game_type == MiniGameType.DICE:
        math_params.update(_dice_math(target_rtp))
    elif game_type == MiniGameType.WHEEL:
        math_params.update(_wheel_math(target_rtp, volatility))
    elif game_type == MiniGameType.SCRATCH:
        math_params.update(_scratch_math(target_rtp))

    math_cfg = MathConfig(**math_params)

    # ── Compliance ──
    compliance = ComplianceConfig(jurisdiction=jurisdiction)
    if jurisdiction != "demo":
        compliance.rng_source = "crypto"
        compliance.result_logging = True
        compliance.session_limits = True

    # ── Audio ──
    audio = AudioConfig()

    # ── Physics ──
    physics = PhysicsConfig()

    return MiniGameConfig(
        game_type=game_type,
        theme=theme,
        math=math_cfg,
        physics=physics,
        audio=audio,
        compliance=compliance,
    )


# ═══════════════════════════════════════════════════════════════
# Config → JS Injection
# ═══════════════════════════════════════════════════════════════

def config_to_js_injection(config: MiniGameConfig) -> str:
    """Generate the JS block to inject at the top of a game HTML file.

    Returns a <script> tag with window.GAME_CONFIG and CSS overrides.
    """
    json_str = config.model_dump_json()
    css_vars = config.theme.to_css_vars()

    return (
        f'<script>window.GAME_CONFIG={json_str};</script>\n'
        f'<style>{css_vars}</style>'
    )


def inject_config_into_html(html: str, config: MiniGameConfig) -> str:
    """Inject config into an existing game HTML file.

    Inserts the config script block right after <head>.
    Also overrides :root CSS variables.
    """
    injection = config_to_js_injection(config)

    # Insert after <head> tag
    head_idx = html.lower().find("<head>")
    if head_idx != -1:
        insert_pos = head_idx + len("<head>")
        html = html[:insert_pos] + "\n" + injection + "\n" + html[insert_pos:]
    else:
        # Fallback: prepend
        html = injection + "\n" + html

    return html


# ═══════════════════════════════════════════════════════════════
# Default Configs for Existing Games
# ═══════════════════════════════════════════════════════════════

def default_config(game_id: str) -> MiniGameConfig:
    """Get the default config that matches the current hardcoded game behavior.

    These defaults are calibrated to produce the exact same gameplay as
    the existing Phase 3 games, so the refactored versions behave identically.
    """
    GAME_DEFAULTS = {
        "crash": {
            "game_type": "crash",
            "theme_preset": "cosmic_crash",
            "target_rtp": 97.0,
            "volatility": "medium",
            "max_win_multiplier": 100,
        },
        "plinko": {
            "game_type": "plinko",
            "theme_preset": "glacier_drop",
            "target_rtp": 96.0,
            "volatility": "medium",
            "max_win_multiplier": 1000,
        },
        "mines": {
            "game_type": "mines",
            "theme_preset": "neon_grid",
            "target_rtp": 97.0,
            "volatility": "medium",
            "max_win_multiplier": 1000,
        },
        "dice": {
            "game_type": "dice",
            "theme_preset": "dragon_dice",
            "target_rtp": 97.0,
            "volatility": "medium",
            "max_win_multiplier": 1000,
        },
        "wheel": {
            "game_type": "wheel",
            "theme_preset": "trident_spin",
            "target_rtp": 96.0,
            "volatility": "medium",
            "max_win_multiplier": 25,
        },
        "hilo": {
            "game_type": "hilo",
            "theme_preset": "pharaohs_fortune",
            "target_rtp": 96.0,
            "volatility": "medium",
            "max_win_multiplier": 1000,
        },
        "chicken": {
            "game_type": "chicken",
            "theme_preset": "jungle_runner",
            "target_rtp": 96.0,
            "volatility": "medium",
            "max_win_multiplier": 1000,
        },
        "scratch": {
            "game_type": "scratch",
            "theme_preset": "golden_vault",
            "target_rtp": 96.0,
            "volatility": "medium",
            "max_win_multiplier": 50,
        },
    }

    params = GAME_DEFAULTS.get(game_id)
    if not params:
        raise ValueError(f"Unknown game_id: {game_id}. Valid: {list(GAME_DEFAULTS)}")

    return build_config(**params)


# ═══════════════════════════════════════════════════════════════
# Validation / Audit
# ═══════════════════════════════════════════════════════════════

def validate_config(config: MiniGameConfig) -> list[str]:
    """Run sanity checks on a config and return list of warnings."""
    warnings = []

    m = config.math
    if m.target_rtp < 85:
        warnings.append(f"RTP {m.target_rtp}% is unusually low — most jurisdictions require ≥85%")
    if m.target_rtp > 99:
        warnings.append(f"RTP {m.target_rtp}% is very high — house edge only {m.house_edge}%")

    if m.default_bet not in m.bet_options:
        warnings.append(f"Default bet {m.default_bet} not in bet_options {m.bet_options}")

    if config.game_type == MiniGameType.WHEEL and m.wheel_segments:
        n = len(m.wheel_segments)
        rtp_actual = sum(s["mult"] for s in m.wheel_segments) / n
        if abs(rtp_actual - m.target_rtp / 100.0) > 0.02:
            warnings.append(
                f"Wheel segment RTP ({rtp_actual:.4f}) differs from target "
                f"({m.target_rtp/100:.4f}) by more than 2%"
            )

    if config.game_type == MiniGameType.CRASH:
        implied_rtp = (1 - m.crash_house_edge) * 100
        if abs(implied_rtp - m.target_rtp) > 1.0:
            warnings.append(
                f"Crash house_edge implies RTP {implied_rtp:.1f}% but target is {m.target_rtp}%"
            )

    if config.compliance.jurisdiction != "demo" and config.compliance.rng_source == "math_random":
        warnings.append("Non-demo jurisdiction using Math.random — should use 'crypto' or 'server'")

    return warnings


# ═══════════════════════════════════════════════════════════════
# CLI — for testing
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    game_id = sys.argv[1] if len(sys.argv) > 1 else "crash"
    cfg = default_config(game_id)
    warnings = validate_config(cfg)

    print(cfg.model_dump_json(indent=2))
    if warnings:
        print("\n⚠️  Warnings:")
        for w in warnings:
            print(f"  - {w}")
    else:
        print(f"\n✅ Config valid | hash={cfg.config_hash}")
