"""
ARKAINBRAIN — Mini-Game Configuration Schema (Phase 1)

Universal config schema that every arcade game reads at boot.
Pipeline agents populate this config → gets injected as `window.GAME_CONFIG`
into the HTML5 game file → game reads all constants from config instead
of hardcoded values.

Usage:
    from config.minigame_schema import MiniGameConfig, PlinkoConfig, CrashConfig, ...
    config = MiniGameConfig(game_type="plinko", theme=ThemeConfig(...), plinko=PlinkoConfig(...))
    json_str = config.model_dump_json(indent=2)
"""

from __future__ import annotations
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


# ═══════════════════════════════════════════════════════════════
# Enums
# ═══════════════════════════════════════════════════════════════

class MiniGameType(str, Enum):
    PLINKO = "plinko"
    CRASH = "crash"
    MINES = "mines"
    DICE = "dice"
    WHEEL = "wheel"
    HILO = "hilo"
    CHICKEN = "chicken"
    SCRATCH = "scratch"
    NOVEL = "novel"


class Volatility(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RiskProfile(str, Enum):
    LOW = "low"
    MEDIUM = "med"
    HIGH = "high"


# ═══════════════════════════════════════════════════════════════
# Theme Configuration
# ═══════════════════════════════════════════════════════════════

class ThemeColors(BaseModel):
    """CSS custom property values injected into :root"""
    accent: str = "#6366f1"         # --acc: primary brand color
    accent2: str = "#06b6d4"        # --acc2: secondary/gradient end
    bg_dark: str = "#030014"        # --bg0: darkest background
    bg_mid: str = "#0a0028"         # --bg1: mid background
    text: str = "#e2e8f0"           # --txt: main text
    text_dim: str = "#64748b"       # --dim: muted text
    win: str = "#22c55e"            # --win: win color (green)
    lose: str = "#ef4444"           # --lose: lose color (red)
    gold: str = "#f59e0b"           # --gold: accent gold
    # Game-specific extras (optional)
    extra: dict[str, str] = Field(default_factory=dict)


class ThemeConfig(BaseModel):
    """Visual identity — fonts, colors, branding"""
    name: str = "Default Theme"
    title: str = "ARCADE GAME"          # H1 text
    subtitle: str = "Phase 3"           # Subtitle under title
    icon: str = "🎮"                    # Title emoji
    font_display: str = "Inter"         # Display/heading font
    font_body: str = "Inter"            # Body text font
    font_import_url: str = "https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap"
    colors: ThemeColors = Field(default_factory=ThemeColors)


# ═══════════════════════════════════════════════════════════════
# Audio Configuration
# ═══════════════════════════════════════════════════════════════

class AudioConfig(BaseModel):
    """Procedural audio parameters"""
    scale: str = "pentatonic_minor"     # Musical scale for procedural tones
    base_freq: float = 440.0            # Base frequency (Hz)
    master_volume: float = 0.7
    sfx_volume: float = 0.6
    music_volume: float = 0.3
    ambient_volume: float = 0.2


# ═══════════════════════════════════════════════════════════════
# Betting Configuration
# ═══════════════════════════════════════════════════════════════

class BetConfig(BaseModel):
    """Universal betting parameters"""
    starting_balance: float = 1000.0
    bet_amounts: list[float] = Field(
        default=[0.10, 0.25, 0.50, 1.0, 2.0, 5.0, 10.0, 25.0]
    )
    default_bet: float = 1.0
    min_bet: float = 0.10
    max_bet: float = 100.0
    currency_symbol: str = "$"
    currency_decimals: int = 2


# ═══════════════════════════════════════════════════════════════
# Compliance Configuration
# ═══════════════════════════════════════════════════════════════

class ComplianceConfig(BaseModel):
    """RNG and regulatory settings"""
    rng_source: str = "math_random"     # "math_random" | "crypto" | "server_seed"
    result_logging: bool = False        # Log every outcome for audit
    session_time_limit_s: int = 0       # 0 = no limit
    reality_check_interval_s: int = 0   # 0 = disabled
    max_auto_plays: int = 0             # 0 = no autoplay
    responsible_gaming_url: str = ""


# ═══════════════════════════════════════════════════════════════
# Per-Game Math/Physics Configurations
# ═══════════════════════════════════════════════════════════════

class PlinkoConfig(BaseModel):
    """Plinko/Pachinko — ball drops through pegs into multiplier buckets"""
    default_rows: int = 12
    row_options: list[int] = Field(default=[8, 12, 16])
    default_risk: str = "low"
    risk_options: list[str] = Field(default=["low", "med", "high"])

    # Multiplier tables: risk → rows → bucket_multipliers
    # Each array has (rows+1) values for the buckets
    mult_tables: dict[str, dict[int, list[float]]] = Field(default_factory=lambda: {
        "low": {
            8:  [5.6, 2.1, 1.1, 0.5, 0.3, 0.5, 1.1, 2.1, 5.6],
            12: [8.4, 3, 1.4, 0.8, 0.5, 0.3, 0.3, 0.5, 0.8, 1.4, 3, 8.4],
            16: [16, 5, 2, 1.4, 0.7, 0.4, 0.3, 0.2, 0.2, 0.3, 0.4, 0.7, 1.4, 2, 5, 16],
        },
        "med": {
            8:  [13, 3, 1.3, 0.4, 0.2, 0.4, 1.3, 3, 13],
            12: [24, 5, 2, 0.7, 0.3, 0.2, 0.2, 0.3, 0.7, 2, 5, 24],
            16: [50, 10, 3, 1.5, 0.5, 0.3, 0.2, 0.1, 0.1, 0.2, 0.3, 0.5, 1.5, 3, 10, 50],
        },
        "high": {
            8:  [29, 4, 0.9, 0.2, 0.1, 0.2, 0.9, 4, 29],
            12: [77, 10, 2, 0.4, 0.1, 0.1, 0.1, 0.1, 0.4, 2, 10, 77],
            16: [170, 24, 4, 0.7, 0.2, 0.1, 0, 0, 0, 0.1, 0.2, 0.7, 4, 24, 170],
        },
    })

    # Physics
    ball_radius: float = 6.0
    peg_radius: float = 4.0
    bounce_damping: float = 0.55
    gravity: float = 800.0


class CrashConfig(BaseModel):
    """Crash — multiplier rises until random crash point"""
    house_edge: float = 0.03            # 3% house edge
    max_multiplier: float = 100.0       # Crash capped at 100x
    rise_speed: float = 1.0             # Base speed multiplier
    auto_cashout_options: list[float] = Field(
        default=[1.5, 2.0, 3.0, 5.0, 10.0, 25.0]
    )


class MinesConfig(BaseModel):
    """Mines — reveal gems on grid, avoid hidden mines"""
    grid_size: int = 25                 # Total cells (5x5)
    grid_cols: int = 5
    default_mines: int = 5
    mine_options: list[int] = Field(default=[1, 3, 5, 10, 15, 20])
    # Multiplier formula: mines / (safe_remaining / total_remaining)
    # The actual mult is calculated dynamically based on revealed count


class DiceConfig(BaseModel):
    """Dice — predict roll outcome over/under threshold"""
    house_edge_pct: float = 3.0         # 3% edge (97% RTP)
    default_threshold: int = 50
    min_threshold: int = 2
    max_threshold: int = 98
    predictions: list[str] = Field(default=["over", "under"])


class WheelConfig(BaseModel):
    """Wheel — spin to land on multiplier segments"""
    segments: list[dict] = Field(default_factory=lambda: [
        {"mult": 0,    "label": "BUST",    "color": "#1e293b", "tc": "#94a3b8"},
        {"mult": 1.2,  "label": "1.2x",    "color": "#164e63", "tc": "#67e8f9"},
        {"mult": 0,    "label": "BUST",    "color": "#1e293b", "tc": "#94a3b8"},
        {"mult": 1.5,  "label": "1.5x",    "color": "#155e75", "tc": "#67e8f9"},
        {"mult": 0,    "label": "BUST",    "color": "#1e293b", "tc": "#94a3b8"},
        {"mult": 2,    "label": "2x",      "color": "#0e7490", "tc": "#ecfeff"},
        {"mult": 0.5,  "label": "0.5x",    "color": "#134e4a", "tc": "#5eead4"},
        {"mult": 3,    "label": "3x",      "color": "#0891b2", "tc": "#fff"},
        {"mult": 0,    "label": "BUST",    "color": "#1e293b", "tc": "#94a3b8"},
        {"mult": 1.2,  "label": "1.2x",    "color": "#164e63", "tc": "#67e8f9"},
        {"mult": 5,    "label": "5x",      "color": "#0284c7", "tc": "#fff"},
        {"mult": 0.5,  "label": "0.5x",    "color": "#134e4a", "tc": "#5eead4"},
        {"mult": 0,    "label": "BUST",    "color": "#1e293b", "tc": "#94a3b8"},
        {"mult": 1.5,  "label": "1.5x",    "color": "#155e75", "tc": "#67e8f9"},
        {"mult": 10,   "label": "🔱10x",   "color": "#7c3aed", "tc": "#fff"},
        {"mult": 0.5,  "label": "0.5x",    "color": "#134e4a", "tc": "#5eead4"},
        {"mult": 0,    "label": "BUST",    "color": "#1e293b", "tc": "#94a3b8"},
        {"mult": 2,    "label": "2x",      "color": "#0e7490", "tc": "#ecfeff"},
        {"mult": 1.2,  "label": "1.2x",    "color": "#164e63", "tc": "#67e8f9"},
        {"mult": 25,   "label": "💎25x",   "color": "#dc2626", "tc": "#fff"},
    ])
    spin_duration_s: float = 4.0
    spin_revolutions: int = 5


class HiLoConfig(BaseModel):
    """HiLo — predict next card higher or lower"""
    suits: list[str] = Field(default=["♠", "♥", "♦", "♣"])
    suit_colors: dict[str, str] = Field(default_factory=lambda: {
        "♠": "#e0d4c0", "♥": "#ef4444", "♦": "#ef4444", "♣": "#e0d4c0"
    })
    values: list[str] = Field(
        default=["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
    )
    # Multiplier increases per correct streak
    streak_multipliers: list[float] = Field(
        default=[1.0, 1.5, 2.25, 3.38, 5.06, 7.59, 11.39, 17.09, 25.63]
    )
    max_streak: int = 8


class ChickenConfig(BaseModel):
    """Chicken/Runner — advance through lanes avoiding hazards"""
    total_lanes: int = 9                # Number of rows to cross
    columns: int = 4                    # Choices per lane
    hazards_per_lane: int = 1           # Mines per row
    # Multiplier per lane survived (cumulative)
    lane_multipliers: list[float] = Field(
        default=[1.18, 1.40, 1.65, 1.96, 2.32, 2.75, 3.26, 3.87, 4.59]
    )


class ScratchConfig(BaseModel):
    """Scratch card — reveal symbols, match 3 to win"""
    grid_rows: int = 3
    grid_cols: int = 3
    match_count: int = 3                # Symbols needed to win
    symbols: list[dict] = Field(default_factory=lambda: [
        {"emoji": "💎", "mult": 50,  "color": "#818cf8"},
        {"emoji": "👑", "mult": 25,  "color": "#fbbf24"},
        {"emoji": "🏺", "mult": 10,  "color": "#f59e0b"},
        {"emoji": "⭐", "mult": 5,   "color": "#fbbf24"},
        {"emoji": "🪙", "mult": 2,   "color": "#a16207"},
        {"emoji": "📜", "mult": 1,   "color": "#8b6914"},
        {"emoji": "🪨", "mult": 0,   "color": "#57534e"},
    ])
    win_probability: float = 0.30       # 30% chance of a winning card


# ═══════════════════════════════════════════════════════════════
# Master Configuration
# ═══════════════════════════════════════════════════════════════

class MiniGameConfig(BaseModel):
    """
    Master configuration for any mini-game.

    This gets serialized to JSON and injected into the HTML5 game as:
        window.GAME_CONFIG = { ... }

    The game reads ALL parameters from this config — no hardcoded constants.
    """
    game_type: MiniGameType
    version: str = "1.0.0"

    # Universal configs
    theme: ThemeConfig = Field(default_factory=ThemeConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    bet: BetConfig = Field(default_factory=BetConfig)
    compliance: ComplianceConfig = Field(default_factory=ComplianceConfig)

    # Per-game configs (only the matching game_type is used)
    plinko: Optional[PlinkoConfig] = None
    crash: Optional[CrashConfig] = None
    mines: Optional[MinesConfig] = None
    dice: Optional[DiceConfig] = None
    wheel: Optional[WheelConfig] = None
    hilo: Optional[HiLoConfig] = None
    chicken: Optional[ChickenConfig] = None
    scratch: Optional[ScratchConfig] = None

    def game_config(self) -> BaseModel:
        """Return the active per-game config based on game_type."""
        return getattr(self, self.game_type.value)

    def to_js_injection(self) -> str:
        """Generate the JS injection string for embedding in HTML."""
        json_str = self.model_dump_json(indent=2, exclude_none=True)
        return f"window.GAME_CONFIG = {json_str};"


# ═══════════════════════════════════════════════════════════════
# Factory: Default configs for each game type
# ═══════════════════════════════════════════════════════════════

def default_plinko_config(
    theme_name: str = "Glacier Drop",
    title: str = "❄️ GLACIER DROP",
    subtitle: str = "Plinko · Musical Physics",
) -> MiniGameConfig:
    return MiniGameConfig(
        game_type=MiniGameType.PLINKO,
        theme=ThemeConfig(
            name=theme_name, title=title, subtitle=subtitle, icon="❄️",
            font_display="Quicksand", font_body="Inter",
            font_import_url="https://fonts.googleapis.com/css2?family=Quicksand:wght@400;600;700&family=Inter:wght@400;600;700&display=swap",
            colors=ThemeColors(
                accent="#0284c7", accent2="#67e8f9",
                bg_dark="#000810", bg_mid="#001830",
                text="#d4f0ff", text_dim="#5b9bb5",
                extra={"hot": "#dc2626"},
            ),
        ),
        plinko=PlinkoConfig(),
    )


def default_crash_config(
    theme_name: str = "Cosmic Crash",
    title: str = "🚀 COSMIC CRASH",
    subtitle: str = "Crash · Space",
) -> MiniGameConfig:
    return MiniGameConfig(
        game_type=MiniGameType.CRASH,
        theme=ThemeConfig(
            name=theme_name, title=title, subtitle=subtitle, icon="🚀",
            font_display="Orbitron", font_body="Inter",
            font_import_url="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;800;900&family=Inter:wght@400;600;700&display=swap",
            colors=ThemeColors(
                accent="#6366f1", accent2="#06b6d4",
                bg_dark="#030014", bg_mid="#030014",
                text="#e2e8f0", text_dim="#64748b",
                extra={"warn": "#f59e0b"},
            ),
        ),
        crash=CrashConfig(),
    )


def default_mines_config(
    theme_name: str = "Neon Grid",
    title: str = "⚡ NEON::GRID",
    subtitle: str = "Mines · Cyberpunk",
) -> MiniGameConfig:
    return MiniGameConfig(
        game_type=MiniGameType.MINES,
        theme=ThemeConfig(
            name=theme_name, title=title, subtitle=subtitle, icon="⚡",
            font_display="Orbitron", font_body="Inter",
            font_import_url="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;800&family=Inter:wght@400;600;700&display=swap",
            colors=ThemeColors(
                accent="#d946ef", accent2="#06ffc7",
                bg_dark="#05000d", bg_mid="#12002e",
                text="#e0d4ff", text_dim="#6b5fa0",
                extra={"neon": "#06ffc7", "hot": "#e11d48", "purple": "#d946ef"},
            ),
        ),
        mines=MinesConfig(),
    )


def default_dice_config(
    theme_name: str = "Dragon Dice",
    title: str = "🐉 DRAGON DICE",
    subtitle: str = "Dice · Dragon",
) -> MiniGameConfig:
    return MiniGameConfig(
        game_type=MiniGameType.DICE,
        theme=ThemeConfig(
            name=theme_name, title=title, subtitle=subtitle, icon="🐉",
            font_display="Bebas Neue", font_body="Inter",
            font_import_url="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Inter:wght@400;600;700&display=swap",
            colors=ThemeColors(
                accent="#dc2626", accent2="#f59e0b",
                bg_dark="#1a0000", bg_mid="#2d0a00",
                text="#fde8d0", text_dim="#a0522d",
                extra={"fire": "#dc2626", "amber": "#fbbf24"},
            ),
        ),
        dice=DiceConfig(),
    )


def default_wheel_config(
    theme_name: str = "Trident Spin",
    title: str = "🔱 TRIDENT SPIN",
    subtitle: str = "Wheel · Ocean",
) -> MiniGameConfig:
    return MiniGameConfig(
        game_type=MiniGameType.WHEEL,
        theme=ThemeConfig(
            name=theme_name, title=title, subtitle=subtitle, icon="🔱",
            font_display="Playfair Display", font_body="Inter",
            font_import_url="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;900&family=Inter:wght@400;600;700&display=swap",
            colors=ThemeColors(
                accent="#0891b2", accent2="#67e8f9",
                bg_dark="#001520", bg_mid="#002030",
                text="#cce8f4", text_dim="#4a8a9e",
            ),
        ),
        wheel=WheelConfig(),
    )


def default_hilo_config(
    theme_name: str = "Pharaoh's Fortune",
    title: str = "🏛️ PHARAOH'S FORTUNE",
    subtitle: str = "HiLo · Egyptian",
) -> MiniGameConfig:
    return MiniGameConfig(
        game_type=MiniGameType.HILO,
        theme=ThemeConfig(
            name=theme_name, title=title, subtitle=subtitle, icon="🏛️",
            font_display="Cinzel", font_body="Inter",
            font_import_url="https://fonts.googleapis.com/css2?family=Cinzel:wght@600;700;900&family=Inter:wght@400;600;700&display=swap",
            colors=ThemeColors(
                accent="#b8860b", accent2="#ffd700",
                bg_dark="#1a0f00", bg_mid="#2d1800",
                text="#f5deb3", text_dim="#8b6914",
                extra={"gold": "#ffd700", "goldd": "#b8860b"},
            ),
        ),
        hilo=HiLoConfig(),
    )


def default_chicken_config(
    theme_name: str = "Jungle Runner",
    title: str = "🐔 JUNGLE RUNNER",
    subtitle: str = "Chicken · Jungle",
) -> MiniGameConfig:
    return MiniGameConfig(
        game_type=MiniGameType.CHICKEN,
        theme=ThemeConfig(
            name=theme_name, title=title, subtitle=subtitle, icon="🐔",
            font_display="Lora", font_body="Inter",
            font_import_url="https://fonts.googleapis.com/css2?family=Lora:wght@600;700&family=Inter:wght@400;600;700&display=swap",
            colors=ThemeColors(
                accent="#16a34a", accent2="#84cc16",
                bg_dark="#001a00", bg_mid="#0a2a0a",
                text="#c6f4c6", text_dim="#3d7a3d",
                extra={"grn": "#16a34a", "lime": "#84cc16"},
            ),
        ),
        chicken=ChickenConfig(),
    )


def default_scratch_config(
    theme_name: str = "Golden Vault",
    title: str = "🏆 GOLDEN VAULT",
    subtitle: str = "Scratch · Gold",
) -> MiniGameConfig:
    return MiniGameConfig(
        game_type=MiniGameType.SCRATCH,
        theme=ThemeConfig(
            name=theme_name, title=title, subtitle=subtitle, icon="🏆",
            font_display="Playfair Display", font_body="Inter",
            font_import_url="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;900&family=Inter:wght@400;600;700&display=swap",
            colors=ThemeColors(
                accent="#a16207", accent2="#fbbf24",
                bg_dark="#0a0500", bg_mid="#1a0f00",
                text="#f5deb3", text_dim="#8b6914",
                extra={"goldd": "#a16207"},
            ),
        ),
        scratch=ScratchConfig(),
    )


# Lookup by game type
DEFAULT_CONFIG_FACTORIES = {
    MiniGameType.PLINKO: default_plinko_config,
    MiniGameType.CRASH: default_crash_config,
    MiniGameType.MINES: default_mines_config,
    MiniGameType.DICE: default_dice_config,
    MiniGameType.WHEEL: default_wheel_config,
    MiniGameType.HILO: default_hilo_config,
    MiniGameType.CHICKEN: default_chicken_config,
    MiniGameType.SCRATCH: default_scratch_config,
}


def get_default_config(game_type: MiniGameType | str) -> MiniGameConfig:
    """Get the default config for any game type."""
    if isinstance(game_type, str):
        game_type = MiniGameType(game_type)
    factory = DEFAULT_CONFIG_FACTORIES.get(game_type)
    if not factory:
        raise ValueError(f"No default config for game type: {game_type}")
    return factory()
