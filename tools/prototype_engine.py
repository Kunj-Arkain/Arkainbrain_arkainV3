"""
ARKAINBRAIN — Self-Contained Slot Prototype Generator (URSO Engine)

Generates a FULLY SELF-CONTAINED single-file HTML5 slot game.
Zero external dependencies — no CDN, no external JS/CSS/images.

Architecture:
  - URSO MathModelController embedded inline (weighted reels, win eval, RTP)
  - URSO FreeSpinsController embedded inline (bonus rounds)
  - Canvas-based reel renderer (spin animation, win highlights, particles)
  - Symbol art: DALL-E images as base64 data URIs, or rich SVG fallbacks
  - Game config generated from pipeline paytable/reels/GDD

Source engine: https://github.com/megbrimef/urso-slot-base
Extensions:   engine/urso/ (MathModel, Analytics, FreeSpins, ResponsibleGaming)
"""

import base64
import csv
import json
import logging
import os
import re
import shutil
from io import StringIO
from pathlib import Path
try:
    from pydantic import BaseModel, Field
    _HAS_PYDANTIC = True
except ImportError:
    _HAS_PYDANTIC = False

logger = logging.getLogger("arkainbrain.proto")

_ENGINE_DIR = Path(__file__).resolve().parent.parent / "engine" / "urso"


if _HAS_PYDANTIC:
    class PrototypeInput(BaseModel):
        game_title: str = Field(description="Game title")
        theme: str = Field(description="Game theme")
        grid_cols: int = Field(default=5)
        grid_rows: int = Field(default=3)
        symbols: list[str] = Field(default_factory=list)
        paytable_summary: str = Field(default="")
        features: list[str] = Field(default_factory=list)
        color_primary: str = Field(default="#1a1a2e")
        color_accent: str = Field(default="#e6b800")
        color_text: str = Field(default="#ffffff")
        target_rtp: float = Field(default=96.0)
        output_dir: str = Field(default="./output")
        art_dir: str = Field(default="")
        audio_dir: str = Field(default="")
        gdd_context: str = Field(default="")
        math_context: str = Field(default="")
        volatility: str = Field(default="medium")
        max_win_multiplier: int = Field(default=5000)
else:
    from dataclasses import dataclass, field
    @dataclass
    class PrototypeInput:
        game_title: str = ""
        theme: str = ""
        grid_cols: int = 5
        grid_rows: int = 3
        symbols: list = field(default_factory=list)
        paytable_summary: str = ""
        features: list = field(default_factory=list)
        color_primary: str = "#1a1a2e"
        color_accent: str = "#e6b800"
        color_text: str = "#ffffff"
        target_rtp: float = 96.0
        output_dir: str = "./output"
        art_dir: str = ""
        audio_dir: str = ""
        gdd_context: str = ""
        math_context: str = ""
        volatility: str = "medium"
        max_win_multiplier: int = 5000


# ============================================================
# URSO JS Module Loader
# ============================================================

def _load_engine_js() -> str:
    """Load URSO extension JS files and bundle them inline."""
    files = [
        _ENGINE_DIR / "modules" / "mathModel" / "controller.js",
        _ENGINE_DIR / "components" / "freeSpins" / "controller.js",
    ]
    chunks = []
    for f in files:
        if not f.exists():
            logger.warning(f"Engine file missing: {f}")
            continue
        code = f.read_text(encoding="utf-8")
        code = re.sub(r"^\s*import\s+.*?;\s*$", "", code, flags=re.MULTILINE)
        code = re.sub(r"^\s*export\s+default\s+\w+;\s*$", "", code, flags=re.MULTILINE)
        code = re.sub(r"^\s*export\s+", "", code, flags=re.MULTILINE)
        chunks.append(code)
    return "\n".join(chunks)


# ============================================================
# Symbol Discovery
# ============================================================

def _discover_symbol_images(art_dir: str, symbol_names: list[str]) -> dict[str, str]:
    if not art_dir or not Path(art_dir).exists():
        return {}
    found = {}
    art_path = Path(art_dir)
    image_files = []
    for ext in ("*.png", "*.jpg", "*.jpeg", "*.webp"):
        image_files.extend(art_path.glob(ext))
        for sub in art_path.iterdir():
            if sub.is_dir():
                image_files.extend(sub.glob(ext))
    for sym in symbol_names:
        sym_lower = sym.lower().replace(" ", "_").replace("'", "")
        for img_file in image_files:
            fname = img_file.stem.lower()
            if sym_lower in fname or fname in sym_lower:
                found[sym] = str(img_file)
                break
    if len(found) < len(symbol_names) // 2 and image_files:
        sorted_imgs = sorted(image_files, key=lambda p: p.name)
        for i, sym in enumerate(symbol_names):
            if sym not in found and i < len(sorted_imgs):
                found[sym] = str(sorted_imgs[i])
    return found


def _discover_background(art_dir: str) -> str:
    if not art_dir or not Path(art_dir).exists():
        return ""
    art_path = Path(art_dir)
    for pattern in ["*background*", "*bg*", "*backdrop*"]:
        for ext in (".png", ".jpg", ".jpeg", ".webp"):
            for f in art_path.glob(f"{pattern}{ext}"):
                return str(f)
    return ""


def _image_to_data_uri(file_path: str) -> str:
    try:
        p = Path(file_path)
        if not p.exists():
            return ""
        data = p.read_bytes()
        ext = p.suffix.lower()
        mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".webp": "image/webp"}.get(ext, "image/png")
        return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    except Exception:
        return ""


# ============================================================
# Math Model Parser
# ============================================================

def _parse_paytable_csv(math_dir: str) -> dict[str, dict]:
    csv_path = Path(math_dir) / "paytable.csv"
    if not csv_path.exists():
        return {}
    result = {}
    try:
        reader = csv.DictReader(StringIO(csv_path.read_text()))
        for row in reader:
            sym = row.get("Symbol", "").strip()
            if not sym or sym.lower() in ("symbol", ""):
                continue
            result[sym] = {
                "w5": _safe_int(row.get("5OAK", 0)), "w4": _safe_int(row.get("4OAK", 0)),
                "w3": _safe_int(row.get("3OAK", 0)), "w2": _safe_int(row.get("2OAK", 0)), "w1": 0}
    except Exception as e:
        logger.warning(f"Could not parse paytable.csv: {e}")
    return result


def _parse_reels_csv(math_dir: str) -> list[list[str]]:
    for name in ("BaseReels.csv", "reel_strips.csv"):
        csv_path = Path(math_dir) / name
        if not csv_path.exists():
            continue
        try:
            reader = csv.reader(StringIO(csv_path.read_text()))
            header = next(reader, None)
            if not header:
                continue
            reels = [[] for _ in range(len(header))]
            for row in reader:
                for i, val in enumerate(row):
                    v = val.strip()
                    if v and i < len(reels):
                        reels[i].append(v)
            return [r for r in reels if r]
        except Exception:
            pass
    return []


def _safe_int(val) -> int:
    try:
        return int(float(str(val).strip() or 0))
    except (ValueError, TypeError):
        return 0


# ============================================================
# SVG Symbol Generator
# ============================================================

_THEME_PALETTES = {
    "egyptian": ["#D4AF37", "#8B4513", "#1a1a2e", "#FFD700", "#4a2c00", "#CD853F", "#B8860B", "#DEB887", "#8B6914", "#DAA520"],
    "chinese":  ["#FF0000", "#FFD700", "#8B0000", "#FF4500", "#B8860B", "#DC143C", "#FF6347", "#FF8C00", "#CD5C5C", "#F4A460"],
    "dragon":   ["#FF4500", "#8B0000", "#2F4F4F", "#FFD700", "#DC143C", "#B22222", "#FF6347", "#CD853F", "#A0522D", "#D2691E"],
    "ocean":    ["#006994", "#20B2AA", "#104E8B", "#48D1CC", "#2F4F4F", "#008B8B", "#5F9EA0", "#4682B4", "#00CED1", "#40E0D0"],
    "space":    ["#4B0082", "#8A2BE2", "#191970", "#9400D3", "#483D8B", "#663399", "#7B68EE", "#6A5ACD", "#9370DB", "#BA55D3"],
    "default":  ["#8B5CF6", "#EC4899", "#3B82F6", "#10B981", "#F59E0B", "#EF4444", "#06B6D4", "#84CC16", "#F97316", "#6366F1"],
}

_SYMBOL_ICONS = {
    "pharaoh": "\U0001F451", "scarab": "\U0001FAB2", "ankh": "\u2625", "eye of horus": "\U0001F441",
    "dragon": "\U0001F409", "phoenix": "\U0001F525", "golden coin": "\U0001FA99", "lantern": "\U0001F3EE",
    "trident": "\U0001F531", "mermaid": "\U0001F9DC", "pearl": "\u26AA", "seahorse": "\U0001F434",
    "astronaut": "\U0001F468\u200D\U0001F680", "planet": "\U0001FA90", "rocket": "\U0001F680", "crystal": "\U0001F48E",
    "buffalo": "\U0001F9AC", "eagle": "\U0001F985", "wolf": "\U0001F43A", "cougar": "\U0001F406",
    "seven": "7", "cherry": "\U0001F352", "bar": "\U0001F4CA", "bell": "\U0001F514",
    "crown": "\U0001F451", "diamond": "\U0001F48E", "trophy": "\U0001F3C6", "star": "\u2B50",
    "ace": "A", "king": "K", "queen": "Q", "jack": "J", "10": "10",
    "wild": "W", "scatter": "SC", "bonus": "\u26A1",
}


def _get_palette(theme: str) -> list[str]:
    tl = theme.lower()
    for key, pal in _THEME_PALETTES.items():
        if key in tl:
            return pal
    return _THEME_PALETTES["default"]


def _generate_svg_data_uri(name: str, index: int, theme: str, is_wild=False, is_scatter=False) -> str:
    palette = _get_palette(theme)
    bg1 = palette[index % len(palette)]
    bg2 = palette[(index + 3) % len(palette)]
    fg = "#ffffff"
    name_lower = name.lower()
    icon = _SYMBOL_ICONS.get(name_lower, "")
    if not icon:
        for key, ico in _SYMBOL_ICONS.items():
            if key in name_lower:
                icon = ico
                break
    if not icon:
        icon = name[:2].upper()
    label = name[:14]
    icon_size, border_color = 36, bg1
    if is_wild:
        bg1, bg2, fg = "#FFD700", "#FF8C00", "#000"
        icon, label, border_color, icon_size = "W", "WILD", "#FFD700", 42
    elif is_scatter:
        bg1, bg2, fg = "#9400D3", "#FF00FF", "#fff"
        icon, label, border_color, icon_size = "SC", "SCATTER", "#FF00FF", 42

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="120" height="120" viewBox="0 0 120 120">
  <defs><linearGradient id="sg{index}" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0%" stop-color="{bg1}"/><stop offset="100%" stop-color="{bg2}"/>
  </linearGradient></defs>
  <rect width="120" height="120" rx="16" fill="url(#sg{index})"/>
  <rect x="3" y="3" width="114" height="114" rx="14" fill="none" stroke="{border_color}" stroke-opacity="0.3" stroke-width="1.5"/>
  <text x="60" y="62" text-anchor="middle" dominant-baseline="middle"
        font-family="Segoe UI Emoji,Apple Color Emoji,Noto Color Emoji,Arial,sans-serif"
        font-size="{icon_size}" font-weight="bold" fill="{fg}">{icon}</text>
  <rect y="92" width="120" height="28" rx="0" fill="rgba(0,0,0,0.35)"/>
  <text x="60" y="108" text-anchor="middle" font-family="Arial,sans-serif" font-size="10"
        font-weight="600" fill="{fg}" opacity="0.9">{label}</text>
</svg>"""
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


# ============================================================
# Game Config Builder
# ============================================================

def _build_game_config(symbol_names, paytable, reels_raw, volatility, target_rtp,
                       theme, features, grid_cols=5, grid_rows=3):
    """Build URSO-compatible game config from pipeline data."""
    name_to_idx = {}
    symbol_weights = {}
    for i, name in enumerate(symbol_names):
        name_to_idx[name] = i
        nl = name.lower()
        if "wild" in nl:
            w = 4
        elif "scatter" in nl or "bonus" in nl:
            w = 3
        elif i < len(symbol_names) * 0.5:
            w = max(5, 30 - i * 3)
        else:
            w = max(3, 15 - (i - len(symbol_names) // 2) * 3)
        symbol_weights[i] = {"weight": w, "name": name}

    # Paytable: {symbolId-matchCount: payout}
    pt = {}
    for i, name in enumerate(symbol_names):
        pay = paytable.get(name, {})
        nl = name.lower()
        if not pay:
            if "wild" in nl:
                pay = {"w3": 50, "w4": 250, "w5": 2500}
            elif "scatter" in nl or "bonus" in nl:
                pay = {}
            else:
                tier = min(i, 8)
                base = max(1, 10 - tier)
                pay = {"w3": base * 3, "w4": base * 8, "w5": base * 20}
                if tier < 4:
                    pay["w2"] = base
        for k, v in pay.items():
            count = int(k[1])
            if v > 0:
                pt[f"{i}-{count}"] = v

    # Reel strips
    reel_strips = []
    if reels_raw and len(reels_raw) >= grid_cols:
        for reel_syms in reels_raw[:grid_cols]:
            indices = []
            for sn in reel_syms:
                idx = name_to_idx.get(sn)
                if idx is None:
                    for kn, ki in name_to_idx.items():
                        if kn.lower() in sn.lower() or sn.lower() in kn.lower():
                            idx = ki
                            break
                indices.append(idx if idx is not None else 0)
            reel_strips.append(indices)
    else:
        import random
        for _ in range(grid_cols):
            strip = []
            for _ in range(40):
                total = sum(sw["weight"] for sw in symbol_weights.values())
                r = random.randint(0, total - 1)
                for sid, sw in symbol_weights.items():
                    r -= sw["weight"]
                    if r < 0:
                        strip.append(sid)
                        break
            reel_strips.append(strip)

    paylines = [
        [1,1,1,1,1],[0,0,0,0,0],[2,2,2,2,2],[0,1,2,1,0],[2,1,0,1,2],
        [0,0,1,2,2],[2,2,1,0,0],[1,0,0,0,1],[1,2,2,2,1],[0,1,0,1,0],
        [2,1,2,1,2],[1,0,1,0,1],[1,2,1,2,1],[0,1,1,1,0],[2,1,1,1,2],
        [0,0,1,0,0],[2,2,1,2,2],[1,0,1,2,1],[1,2,1,0,1],[0,2,0,2,0],
    ]

    wild_id, scatter_id = None, None
    for i, name in enumerate(symbol_names):
        nl = name.lower()
        if "wild" in nl:
            wild_id = i
        if "scatter" in nl or "bonus" in nl:
            scatter_id = i

    wild_rules = {}
    if wild_id is not None:
        wild_rules = {"symbolId": wild_id,
                      "substitutesFor": [j for j in range(len(symbol_names)) if j != wild_id and j != scatter_id]}

    scatter_rules = []
    if scatter_id is not None:
        scatter_rules = [{"symbolId": scatter_id, "minCount": 3,
                          "payMultipliers": {3: 2, 4: 10, 5: 50}}]

    free_spin_rules = {}
    if scatter_id is not None:
        free_spin_rules = {
            "triggerSymbol": scatter_id, "minCount": 3,
            "spinsAwarded": {3: 10, 4: 15, 5: 25},
            "retriggerEnabled": True, "retriggerMinCount": 3,
            "retriggerSpins": {3: 5, 4: 10, 5: 15}, "maxRetriggers": 3,
            "multiplierMode": "escalating", "baseMultiplier": 1,
            "escalationStep": 1, "maxMultiplier": 10,
        }

    return {
        "reelsCount": grid_cols, "rowsCount": grid_rows,
        "targetRTP": target_rtp, "volatility": volatility,
        "winType": "lines",
        "symbolWeights": {str(k): v for k, v in symbol_weights.items()},
        "reelStrips": reel_strips, "paytable": pt,
        "paylines": paylines[:min(20, len(paylines))],
        "wildRules": wild_rules, "scatterRules": scatter_rules,
        "freeSpinRules": free_spin_rules,
        "betConfig": {"defaultBet": 1, "bets": [1, 2, 5, 10, 20, 50],
                      "defaultLines": min(20, len(paylines))},
    }


# ============================================================
# Load renderer JS from file
# ============================================================

def _load_renderer_js() -> str:
    """Load the canvas slot renderer from the engine directory."""
    renderer_path = _ENGINE_DIR / "slot_renderer.js"
    if renderer_path.exists():
        return renderer_path.read_text(encoding="utf-8")
    logger.warning(f"Renderer JS not found at {renderer_path}")
    return "// Renderer not found"


# ============================================================
# HTML Template
# ============================================================

def _generate_html(game_title, theme, config_json, symbols_json, engine_js,
                   renderer_js, color_primary, color_accent, target_rtp,
                   volatility, features, bg_data_uri=""):
    features_str = ", ".join(features[:5]) if features else "Free Spins"
    bg_css = ""
    if bg_data_uri:
        bg_css = f"background-image:url('{bg_data_uri}');background-size:cover;background-position:center;"

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no">
<title>{game_title} — ARKAINBRAIN Prototype</title>
<style>
:root {{ --primary:{color_primary}; --accent:{color_accent}; --text:#fff; }}
*,*::before,*::after {{ box-sizing:border-box }}
body {{ margin:0;padding:0;width:100vw;height:100vh;overflow:hidden;{bg_css}
  background-color:{color_primary};font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:var(--text);
  display:flex;flex-direction:column }}
.top-bar {{ display:flex;align-items:center;justify-content:space-between;padding:6px 16px;
  background:rgba(0,0,0,0.88);backdrop-filter:blur(12px);border-bottom:1px solid rgba(255,255,255,0.06);
  flex-shrink:0;z-index:10 }}
.top-bar .mark {{ width:22px;height:22px;border-radius:5px;background:linear-gradient(135deg,#7c6aef,#5a48c2);
  display:grid;place-items:center;font-size:11px;font-weight:800;color:#fff }}
.top-bar .title {{ font-size:13px;font-weight:700;color:#e8eaf0;margin-left:8px }}
.top-bar .tag {{ padding:2px 7px;border-radius:4px;background:rgba(124,106,239,0.15);color:#9b8aff;
  font-size:9px;font-weight:700;letter-spacing:0.5px;margin-left:8px }}
.top-bar .stats {{ display:flex;gap:14px;font-size:10px;color:#555 }}
.top-bar .stats b {{ color:#9b8aff }}
#slot-area {{ flex:1;position:relative;overflow:hidden }}
#slot-canvas {{ position:absolute;inset:0;width:100%;height:100% }}
#win-msg {{ position:fixed;top:40%;left:50%;transform:translate(-50%,-50%) scale(0);font-size:28px;font-weight:800;
  z-index:50;pointer-events:none;text-shadow:0 4px 20px rgba(0,0,0,0.5);transition:transform .3s cubic-bezier(0.34,1.56,0.64,1),opacity .3s }}
#win-msg.show {{ transform:translate(-50%,-50%) scale(1) }}
.controls {{ display:flex;align-items:center;justify-content:center;gap:8px;padding:10px 16px;
  background:rgba(0,0,0,0.88);backdrop-filter:blur(12px);border-top:1px solid rgba(255,255,255,0.06);flex-shrink:0;z-index:10 }}
.ctrl-group {{ text-align:center }}
.ctrl-label {{ font-size:9px;color:#666;text-transform:uppercase;letter-spacing:0.5px }}
.ctrl-val {{ font-size:14px;font-weight:700;color:#e8eaf0;font-variant-numeric:tabular-nums }}
.btn {{ padding:8px 16px;border:1px solid rgba(255,255,255,0.15);background:transparent;color:#ccc;
  border-radius:8px;font-size:12px;font-weight:600;cursor:pointer;transition:all .15s }}
.btn:hover {{ border-color:var(--accent);color:#fff }}
.btn-spin {{ padding:10px 32px;background:linear-gradient(135deg,var(--accent),#ff8c00);color:#000;
  border:none;font-size:14px;font-weight:800;letter-spacing:1px;text-transform:uppercase }}
.btn-spin:hover {{ box-shadow:0 4px 20px rgba(230,184,0,0.3);transform:translateY(-1px) }}
.btn-spin.spinning {{ opacity:0.5;pointer-events:none }}
.hist {{ padding:4px 16px 6px;max-height:50px;overflow-y:auto;background:rgba(0,0,0,0.5);flex-shrink:0 }}
.hist h3 {{ font-size:8px;color:#444;text-transform:uppercase;letter-spacing:1px;margin:0 0 2px }}
.hist-row {{ display:flex;gap:8px;font-size:10px;padding:1px 0;border-bottom:1px solid rgba(255,255,255,0.03) }}
.hist-row .m {{ font-weight:700 }} .hist-row .w {{ color:#22c55e }} .hist-row .l {{ color:#ef4444 }}
#paytable-overlay {{ position:fixed;inset:0;z-index:100;background:rgba(0,0,0,0.92);backdrop-filter:blur(8px);
  display:flex;flex-direction:column;align-items:center;padding:40px 20px;overflow-y:auto;
  opacity:0;pointer-events:none;transition:opacity .3s }}
#paytable-overlay.show {{ opacity:1;pointer-events:auto }}
.pt-close {{ position:fixed;top:12px;right:16px;background:none;border:none;color:#888;font-size:24px;cursor:pointer;z-index:101 }}
#paytable-grid {{ display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:12px;max-width:600px;width:100% }}
.pt-card {{ background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.08);border-radius:10px;
  padding:12px;text-align:center }}
.pt-card img {{ display:block;margin:0 auto 6px;border-radius:6px }}
.pt-name {{ font-size:11px;font-weight:700;margin-bottom:4px }}
.pt-pays {{ font-size:10px;color:#888;line-height:1.6 }} .pt-pays b {{ color:#e8eaf0 }}
</style>
<script>
window.GAME_CONFIG = {config_json};
window.SYMBOL_IMAGES = {symbols_json};
window.THEME = {{ bg0: "{color_primary}", bg1: "#030014", accent: "{color_accent}" }};
</script>
<script>
// URSO Math Model + Free Spins Engine (embedded — no CDN)
{engine_js}
</script>
</head>
<body>
<div class="top-bar">
  <div style="display:flex;align-items:center">
    <div class="mark">A</div>
    <span class="title">{game_title}</span>
    <span class="tag">PROTOTYPE</span>
  </div>
  <div class="stats">
    <span>RTP <b>{target_rtp}%</b></span>
    <span>Vol <b>{volatility.title()}</b></span>
    <span>Features <b>{features_str}</b></span>
  </div>
</div>
<div id="slot-area"><canvas id="slot-canvas"></canvas></div>
<div id="win-msg"></div>
<div class="controls">
  <div class="ctrl-group"><div class="ctrl-label">Balance</div><div class="ctrl-val" id="ctrl-balance">500.00</div></div>
  <button class="btn" onclick="changeBet(-1)">&minus;</button>
  <div class="ctrl-group"><div class="ctrl-label">Total Bet</div><div class="ctrl-val" id="ctrl-bet">20.00</div></div>
  <button class="btn" onclick="changeBet(1)">+</button>
  <button class="btn btn-spin" id="btn-spin" onclick="doSpin()">SPIN</button>
  <button class="btn" id="btn-auto" onclick="toggleAuto()" title="AutoSpin">&#9654;&#9654;</button>
  <button class="btn" onclick="togglePaytable()" title="Paytable">&#9776;</button>
</div>
<div class="hist"><h3>History</h3><div id="hist-list"></div></div>
<div id="paytable-overlay">
  <button class="pt-close" onclick="togglePaytable()">&times;</button>
  <div style="text-align:center;margin-bottom:24px">
    <h2 style="font-size:18px;font-weight:800;color:#e8eaf0;margin:0">{game_title} — Paytable</h2>
    <p style="font-size:11px;color:#666;margin:4px 0 0">RTP: {target_rtp}% &middot; {volatility.title()} Volatility</p>
  </div>
  <div id="paytable-grid"></div>
</div>
<script>
{renderer_js}
</script>
</body>
</html>'''


# ============================================================
# Main Entry Point
# ============================================================

def generate_prototype(
    game_title: str, theme: str,
    grid_cols: int = 5, grid_rows: int = 3,
    symbols: list[str] = None, features: list[str] = None,
    color_primary: str = "#1a1a2e", color_accent: str = "#e6b800",
    color_text: str = "#ffffff", target_rtp: float = 96.0,
    output_dir: str = "./output", paytable_summary: str = "",
    art_dir: str = "", audio_dir: str = "",
    gdd_context: str = "", math_context: str = "",
    volatility: str = "medium", max_win_multiplier: int = 5000,
) -> str:
    """Generate a playable HTML5 slot prototype using embedded URSO engine.
    Returns JSON with file_path and metadata."""

    if not symbols or symbols == ["\U0001F451", "\U0001F48E", "\U0001F3C6", "\U0001F31F", "A", "K", "Q", "J", "10"]:
        symbols = _get_default_symbols(theme)
    features = features or ["Free Spins"]

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # 1. Discover art assets
    sym_imgs = _discover_symbol_images(art_dir, symbols)
    bg = _discover_background(art_dir)
    logger.info(f"URSO engine | {len(symbols)} symbols, {len(sym_imgs)} DALL-E images found")

    # 2. Build symbol image array (data URIs — all inline, zero external deps)
    symbol_data_uris = []
    n_real_art = 0
    for i, sn in enumerate(symbols):
        nl = sn.lower()
        is_wild = "wild" in nl
        is_scatter = "scatter" in nl or "bonus" in nl

        if sn in sym_imgs and Path(sym_imgs[sn]).exists():
            uri = _image_to_data_uri(sym_imgs[sn])
            if uri:
                symbol_data_uris.append(uri)
                n_real_art += 1
                continue
        # Fallback: generate SVG data URI
        symbol_data_uris.append(_generate_svg_data_uri(sn, i, theme, is_wild, is_scatter))

    # 3. Background data URI
    bg_data_uri = _image_to_data_uri(bg) if bg else ""

    # 4. Parse math model from pipeline output
    math_dir = ""
    for candidate in [
        str(Path(art_dir).parent / "03_math") if art_dir else "",
        str(Path(output_dir).parent / "03_math"),
    ]:
        if candidate and Path(candidate).exists():
            math_dir = candidate
            break

    paytable = _parse_paytable_csv(math_dir) if math_dir else {}
    reels = _parse_reels_csv(math_dir) if math_dir else []
    if paytable:
        logger.info(f"  Paytable: {len(paytable)} symbols")
    if reels:
        logger.info(f"  Reels: {len(reels)} x {len(reels[0]) if reels else 0}")

    # 5. Build game config
    game_config = _build_game_config(
        symbols, paytable, reels, volatility, target_rtp,
        theme, features, grid_cols, grid_rows,
    )
    config_json = json.dumps(game_config, indent=2)
    symbols_json = json.dumps(symbol_data_uris)

    # 6. Load URSO engine JS (embedded — no CDN)
    engine_js = _load_engine_js()
    renderer_js = _load_renderer_js()

    # 7. Generate self-contained HTML
    html = _generate_html(
        game_title, theme, config_json, symbols_json, engine_js, renderer_js,
        color_primary, color_accent, target_rtp, volatility, features, bg_data_uri,
    )
    html_path = out / "index.html"
    html_path.write_text(html, encoding="utf-8")

    file_size_kb = html_path.stat().st_size / 1024
    logger.info(f"DONE {html_path} | {file_size_kb:.0f}KB | DALL-E: {n_real_art}/{len(symbols)} | Engine: URSO embedded")

    return json.dumps({
        "file_path": str(html_path), "engine": "urso-embedded",
        "engine_source": "https://github.com/megbrimef/urso-slot-base",
        "symbols_total": len(symbols), "symbols_with_art": n_real_art,
        "has_paytable": bool(paytable), "has_reels": bool(reels),
        "has_background": bool(bg_data_uri), "bonus_name": features[0] if features else "Free Spins",
        "config_symbols": len(game_config.get("symbolWeights", {})),
        "config_reels": len(game_config.get("reelStrips", [])),
        "file_size_kb": round(file_size_kb, 1),
    })


# ============================================================
# Default Symbols by Theme
# ============================================================

def _get_default_symbols(theme: str) -> list[str]:
    tl = theme.lower()
    if any(k in tl for k in ("egypt", "pharaoh", "pyramid", "nile", "cleopatra")):
        return ["10", "J", "Q", "K", "A", "Scarab", "Ankh", "Eye of Horus", "Pharaoh", "Wild", "Scatter"]
    elif any(k in tl for k in ("chinese", "dragon", "fortune", "lunar", "888")):
        return ["10", "J", "Q", "K", "A", "Dragon", "Phoenix", "Golden Coin", "Lantern", "Wild", "Scatter"]
    elif any(k in tl for k in ("ocean", "sea", "underwater", "atlantis")):
        return ["10", "J", "Q", "K", "A", "Trident", "Pearl", "Seahorse", "Mermaid", "Wild", "Scatter"]
    elif any(k in tl for k in ("space", "cosmic", "galaxy", "star", "alien")):
        return ["10", "J", "Q", "K", "A", "Astronaut", "Planet", "Rocket", "Crystal", "Wild", "Scatter"]
    elif any(k in tl for k in ("buffalo", "animal", "safari", "wolf")):
        return ["10", "J", "Q", "K", "A", "Buffalo", "Eagle", "Wolf", "Cougar", "Wild", "Scatter"]
    elif any(k in tl for k in ("fruit", "classic", "retro", "cherry")):
        return ["10", "J", "Q", "K", "A", "Seven", "Cherry", "Bar", "Bell", "Wild", "Scatter"]
    else:
        return ["10", "J", "Q", "K", "A", "Crown", "Diamond", "Trophy", "Star", "Wild", "Scatter"]
