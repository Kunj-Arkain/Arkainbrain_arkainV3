"""
ARKAINBRAIN — Mini-Game Config Injector (Phase 1 — aligned with minigame_config.py)

Takes a game HTML file + MiniGameConfig → produces a parameterized game
that reads all constants from window.GAME_CONFIG.

Architecture:
    MiniGameConfig (Pydantic) → config_to_game_js() → JS injection block
    Each game has a "reader" that maps config fields to the exact variable
    names the game code expects (MULT_TABLES, SEGMENTS, HOUSE_EDGE, etc.)
"""

import json
import math
import re
from pathlib import Path
from typing import Optional

from tools.minigame_config import MiniGameConfig, MiniGameType

GAMES_DIR = Path(__file__).parent.parent / "static" / "arcade" / "games" / "phase3"
OUTPUT_DIR = Path(__file__).parent.parent / "static" / "arcade" / "games" / "generated"


# ═══════════════════════════════════════════════════════════════
# Config → JS Translation
# ═══════════════════════════════════════════════════════════════

def config_to_css_vars(config: MiniGameConfig) -> str:
    """Generate :root CSS variables from theme config."""
    t = config.theme
    pairs = {
        "--acc": t.primary, "--acc2": t.secondary,
        "--bg0": t.bg_start, "--bg1": t.bg_end,
        "--txt": t.text, "--dim": t.text_dim,
        "--win": t.win, "--lose": t.lose, "--gold": t.gold,
    }
    pairs.update(t.extra_vars)
    css = ";".join(f"{k}:{v}" for k, v in pairs.items())
    return f":root{{{css}}}"


def _js_esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")


def _extra_vars_js(extra: dict) -> str:
    if not extra:
        return ""
    return "\n".join(f"  r.setProperty('{k}', '{v}');" for k, v in extra.items())


def _interpolate_plinko(mults_12: list[float], target_rows: int) -> list[float]:
    """Interpolate 12-row plinko multipliers to different row counts."""
    n12 = len(mults_12)
    nt = target_rows + 1
    if nt == n12:
        return mults_12
    probs_t = [math.comb(target_rows, k) / (2 ** target_rows) for k in range(nt)]
    probs_12 = [math.comb(12, k) / (2 ** 12) for k in range(13)]
    rtp_12 = sum(p * m for p, m in zip(probs_12, mults_12))
    result = []
    for i in range(nt):
        pos = i / (nt - 1) if nt > 1 else 0.5
        op = pos * (n12 - 1)
        lo, hi = int(op), min(int(op) + 1, n12 - 1)
        f = op - lo
        result.append(round(mults_12[lo] * (1 - f) + mults_12[hi] * f, 2))
    cur = sum(p * m for p, m in zip(probs_t, result))
    if cur > 0:
        sc = rtp_12 / cur
        result = [round(m * sc, 2) for m in result]
    return result


def config_to_game_js(config: MiniGameConfig) -> str:
    """Translate MiniGameConfig → JS block that sets window.GAME_CONFIG.
    
    Games read from this using: const GC = window.GAME_CONFIG || {};
    with fallbacks like: let balance = GC.balance ?? 1000;
    """
    m = config.math
    t = config.theme
    
    # Build flat config object with the exact keys games read via GC.xxx
    gc = {
        "balance": m.starting_balance,
        "currentBet": m.default_bet,
        "bets": m.bet_options,
    }
    
    gt = config.game_type
    
    if gt == MiniGameType.PLINKO:
        profiles = m.plinko_risk_profiles or {}
        mt = {}
        for risk, m12 in profiles.items():
            mt[risk] = {
                8: _interpolate_plinko(m12, 8),
                12: m12,
                16: _interpolate_plinko(m12, 16),
            }
        gc["ROWS"] = m.plinko_rows
        gc["RISK"] = "low"
        gc["MULT_TABLES"] = mt
    
    elif gt == MiniGameType.CRASH:
        gc["HOUSE_EDGE"] = m.crash_house_edge
        gc["MAX_MULT"] = m.crash_max_mult
    
    elif gt == MiniGameType.MINES:
        gc["mineCount"] = m.mines_default
        gc["GRID"] = m.mines_grid_size
        gc["COLS"] = m.mines_cols
        gc["MINE_OPTIONS"] = m.mines_options
        gc["MINES_EDGE"] = m.mines_edge_factor
    
    elif gt == MiniGameType.DICE:
        gc["prediction"] = "over"
        gc["threshold"] = 50
        gc["EDGE_PCT"] = m.dice_edge_factor
    
    elif gt == MiniGameType.WHEEL:
        gc["SEGMENTS"] = m.wheel_segments
    
    elif gt == MiniGameType.HILO:
        pass  # HiLo only needs balance/bets
    
    elif gt == MiniGameType.CHICKEN:
        gc["totalLanes"] = m.chicken_lanes
        gc["hazardsPerLane"] = m.chicken_hazards_per_lane
        gc["COLS"] = m.chicken_cols
    
    elif gt == MiniGameType.SCRATCH:
        gc["SYMBOLS"] = m.scratch_symbols
        gc["WIN_PROB"] = m.scratch_win_chance
    
    gc_json = json.dumps(gc, separators=(",", ":"))
    
    parts = [f"""
// ═══════════════════════════════════════════════════════════════
// § GAME CONFIG (injected by ArkainBrain Mini-Games Pipeline)
// ═══════════════════════════════════════════════════════════════
window.GAME_CONFIG = {gc_json};
"""]
    
    # Theme applier
    parts.append(f"""
(function applyTheme() {{
  const r = document.documentElement.style;
  r.setProperty('--acc', '{t.primary}');
  r.setProperty('--acc2', '{t.secondary}');
  r.setProperty('--bg0', '{t.bg_start}');
  r.setProperty('--bg1', '{t.bg_end}');
  r.setProperty('--txt', '{t.text}');
  r.setProperty('--dim', '{t.text_dim}');
  r.setProperty('--win', '{t.win}');
  r.setProperty('--lose', '{t.lose}');
  r.setProperty('--gold', '{t.gold}');
  {_extra_vars_js(t.extra_vars)}
  const h1 = document.querySelector('.hdr h1');
  if (h1) h1.innerHTML = '{_js_esc(t.title or t.name)}';
  const sub = document.querySelector('.hdr .sub');
  if (sub) sub.textContent = '{_js_esc(t.subtitle)}';
}})();
""")
    
    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════
# Main Injection Functions
# ═══════════════════════════════════════════════════════════════

def inject_config(html: str, config: MiniGameConfig) -> str:
    """Inject GAME_CONFIG into an existing game HTML file.
    
    Since games read window.GAME_CONFIG with ?? fallbacks, we only need
    to set the config object before the game script runs. No neutralization.
    """
    css_override = f"<style>{config_to_css_vars(config)}</style>"
    js_block = config_to_game_js(config)

    # Inject CSS after <head>
    head_idx = html.lower().find("<head>")
    if head_idx != -1:
        pos = head_idx + len("<head>")
        html = html[:pos] + "\n" + css_override + "\n" + html[pos:]

    # Inject GAME_CONFIG script BEFORE the main game script
    # This ensures window.GAME_CONFIG is set when the game reads it
    m = re.search(r'<script>\s*\n?"use strict";', html)
    if m:
        pos = m.start()
        html = html[:pos] + f"<script>\n{js_block}\n</script>\n" + html[pos:]
    else:
        idx = html.find("<script>")
        if idx != -1:
            html = html[:idx] + f"<script>\n{js_block}\n</script>\n" + html[idx:]

    if config.theme.title:
        html = re.sub(r'<title>[^<]+</title>',
                       f'<title>ARCADE — {config.theme.title}</title>', html, count=1)

    return html


def build_themed_game(game_type: str, config: MiniGameConfig,
                      template_path: Optional[Path] = None) -> str:
    """Load template + inject config → themed game HTML."""
    if template_path is None:
        templates = list(GAMES_DIR.glob(f"{game_type}_*.html"))
        if not templates:
            raise FileNotFoundError(f"No template for: {game_type}")
        template_path = templates[0]
    html = template_path.read_text(encoding="utf-8")
    return inject_config(html, config)


def save_themed_game(game_type: str, config: MiniGameConfig,
                     output_name: Optional[str] = None,
                     output_dir: Optional[Path] = None) -> Path:
    """Build and save a themed game variant."""
    html = build_themed_game(game_type, config)
    out_dir = output_dir or OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    if output_name is None:
        safe_name = re.sub(r'[^a-z0-9]+', '-', config.theme.name.lower()).strip('-')
        output_name = f"{game_type}_{safe_name}.html"
    out_path = out_dir / output_name
    out_path.write_text(html, encoding="utf-8")
    return out_path
