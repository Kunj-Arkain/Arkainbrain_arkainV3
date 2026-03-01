"""
ARKAINBRAIN — Mini-Game Code Generation Tools (Phase 3)

Tools for the Arcade Engineer agent:
  1. GameScaffoldTool  — Get base HTML5 scaffold for a game type
  2. ReferenceCodeTool — Search existing games for code patterns
  3. GameValidatorTool — Validate generated game HTML
  4. Standalone functions for non-CrewAI usage

The scaffold system works by:
  1. Loading the Phase 3 game template for the requested type
  2. Stripping hardcoded values (neutralizing them)
  3. Inserting config placeholders that window.GAME_CONFIG fills
  4. Returning a "blank" game that boots from config
"""

from __future__ import annotations

import os
import re
import json
import hashlib
from pathlib import Path
from typing import Optional


# ═══════════════════════════════════════════════════════════════
# Game File Registry
# ═══════════════════════════════════════════════════════════════

# Map game types to their Phase 3 HTML files
_GAME_FILES = {
    "crash":   "crash_cosmic-crash.html",
    "plinko":  "plinko_glacier-drop.html",
    "mines":   "mines_neon-grid.html",
    "dice":    "dice_dragon-dice.html",
    "wheel":   "wheel_trident-spin.html",
    "hilo":    "hilo_pharaohs-fortune.html",
    "chicken": "chicken_jungle-runner.html",
    "scratch": "scratch_golden-vault.html",
}

_GAMES_DIR = Path(__file__).parent.parent / "static" / "arcade" / "games" / "phase3"


def _game_path(game_type: str) -> Path:
    fname = _GAME_FILES.get(game_type)
    if not fname:
        raise ValueError(f"Unknown game type: {game_type}. Valid: {list(_GAME_FILES.keys())}")
    return _GAMES_DIR / fname


# ═══════════════════════════════════════════════════════════════
# Reference Code Library
# ═══════════════════════════════════════════════════════════════

# Code pattern index: maps pattern names to extraction regexes
_CODE_PATTERNS = {
    "canvas_setup": {
        "desc": "Canvas initialization, resize handlers, DPR scaling",
        "regex": r"(const canvas.*?ctx\.scale\(\w+,\w+\))",
        "flags": re.DOTALL,
    },
    "physics_loop": {
        "desc": "requestAnimationFrame game loop with physics updates",
        "regex": r"(function (?:update|tick|loop|animate|frame)\b[\s\S]*?requestAnimationFrame)",
        "flags": re.DOTALL,
    },
    "audio_engine": {
        "desc": "Web Audio API context, oscillator creation, procedural sounds",
        "regex": r"((?:const|let|var)\s+(?:audioCtx|ac|ctx)\s*=[\s\S]*?(?:oscillator|gain|\.start)[\s\S]*?(?:\}\s*function|\}\s*\/\/))",
        "flags": re.DOTALL,
    },
    "bet_controls": {
        "desc": "Bet amount buttons, balance display, profit tracking",
        "regex": r"(class=\"bet-controls\"[\s\S]*?(?:</div>\s*</div>|class=\"controls\"))",
        "flags": re.DOTALL,
    },
    "theme_css": {
        "desc": "CSS custom properties for theming (--primary, --secondary, etc.)",
        "regex": r"(:root\s*\{[^}]+\}|\.theme\s*\{[^}]+\}|--[\w-]+:\s*[^;]+;)",
        "flags": re.DOTALL,
    },
    "state_machine": {
        "desc": "Game state management (idle, playing, crashed, etc.)",
        "regex": r"((?:let|const)\s+(?:state|gameState|phase)\s*=[\s\S]*?function\s+(?:setState|changeState|transition)[\s\S]*?\})",
        "flags": re.DOTALL,
    },
    "particle_system": {
        "desc": "Particle effects (explosions, trails, celebrations)",
        "regex": r"((?:particles|confetti|sparks)\s*[\[=][\s\S]*?(?:function\s+(?:emit|spawn|createParticle)|\.push\(\{)[\s\S]*?\})",
        "flags": re.DOTALL,
    },
    "responsive_layout": {
        "desc": "Media queries, viewport handling, mobile adaptations",
        "regex": r"(@media[\s\S]*?\}[\s\S]*?\})",
        "flags": re.DOTALL,
    },
}


def get_reference_code(pattern_name: str, game_type: str = None) -> dict:
    """Extract a code pattern from existing games.

    Args:
        pattern_name: One of the pattern names (canvas_setup, physics_loop, etc.)
        game_type: Specific game to extract from (default: search all)

    Returns:
        {"pattern": name, "game": type, "code": extracted_code, "description": desc}
    """
    pattern = _CODE_PATTERNS.get(pattern_name)
    if not pattern:
        return {
            "error": f"Unknown pattern: {pattern_name}",
            "available": list(_CODE_PATTERNS.keys()),
        }

    results = []
    games = [game_type] if game_type else list(_GAME_FILES.keys())

    for gt in games:
        try:
            html = _game_path(gt).read_text()
            matches = re.findall(pattern["regex"], html, pattern.get("flags", 0))
            if matches:
                # Take the longest match (most complete)
                best = max(matches, key=len) if isinstance(matches[0], str) else matches[0]
                results.append({
                    "pattern": pattern_name,
                    "game": gt,
                    "description": pattern["desc"],
                    "code": best[:2000],  # Cap at 2KB to fit in context
                    "code_length": len(best),
                })
        except (FileNotFoundError, ValueError):
            continue

    return results if results else {"error": f"Pattern '{pattern_name}' not found in any game"}


def get_full_game_source(game_type: str) -> str:
    """Get the complete HTML source of an existing game (for RAG/reference)."""
    return _game_path(game_type).read_text()


def list_reference_patterns() -> list[dict]:
    """List all available code patterns for reference."""
    return [
        {"name": name, "description": p["desc"]}
        for name, p in _CODE_PATTERNS.items()
    ]


# ═══════════════════════════════════════════════════════════════
# Game Scaffolds
# ═══════════════════════════════════════════════════════════════

def get_scaffold(game_type: str) -> str:
    """Get a config-ready scaffold for a game type.

    This loads the Phase 3 game, neutralizes hardcoded values,
    and inserts config loading placeholders.

    The scaffold is a complete game that boots from window.GAME_CONFIG.
    """
    html = get_full_game_source(game_type)

    # Insert config loading at the top of <script>
    config_loader = """
// ═══ CONFIG LOADING ═══
// Game boots from window.GAME_CONFIG (injected by server or embedded)
const CFG = window.GAME_CONFIG || {};
const THEME = CFG.theme || {};
const MATH = CFG.math || {};
const PHYSICS = CFG.physics || {};
const AUDIO_CFG = CFG.audio || {};
const COMPLIANCE = CFG.compliance || {};

// Apply theme CSS custom properties
if (THEME.primary) {
    const root = document.documentElement;
    root.style.setProperty('--primary', THEME.primary);
    root.style.setProperty('--secondary', THEME.secondary || THEME.primary);
    root.style.setProperty('--bg-start', THEME.bg_start || '#030014');
    root.style.setProperty('--bg-end', THEME.bg_end || '#0a0028');
    root.style.setProperty('--text', THEME.text || '#e0e0e0');
    root.style.setProperty('--text-dim', THEME.text_dim || '#888');
    root.style.setProperty('--win', THEME.win || '#00ff88');
    root.style.setProperty('--lose', THEME.lose || '#ff4444');
    root.style.setProperty('--gold', THEME.gold || '#ffd700');
}

// Session tracking
let _sessionStart = Date.now();
let _roundCount = 0;
let _totalWagered = 0;
let _sessionTimeLimit = (COMPLIANCE.session_limit_minutes || 60) * 60000;
"""

    # Find <script>"use strict" and insert after it
    m = re.search(r'(<script>\s*"use strict";)', html)
    if m:
        pos = m.end()
        html = html[:pos] + "\n" + config_loader + "\n" + html[pos:]

    return html


def inject_config_into_scaffold(scaffold_html: str, config: dict,
                                math_json: str = None) -> str:
    """Inject a config object into a scaffold to produce a runnable game.

    Args:
        scaffold_html: HTML from get_scaffold()
        config: MiniGameConfig dict
        math_json: Optional MathModel JSON for embedded certification

    Returns:
        Complete, runnable HTML5 game
    """
    # Build the GAME_CONFIG JS object
    config_js = f"window.GAME_CONFIG = {json.dumps(config, indent=2)};"

    if math_json:
        config_js += f"\nwindow.MATH_MODEL = {math_json};"

    # Insert before the config loader
    scaffold_html = scaffold_html.replace(
        "// ═══ CONFIG LOADING ═══",
        config_js + "\n// ═══ CONFIG LOADING ═══",
    )

    # Update title
    theme = config.get("theme", {})
    if theme.get("title"):
        scaffold_html = re.sub(
            r"<title>.*?</title>",
            f"<title>{theme['title']}</title>",
            scaffold_html,
        )

    return scaffold_html


# ═══════════════════════════════════════════════════════════════
# Game Validator
# ═══════════════════════════════════════════════════════════════

def validate_game_html(html: str, game_type: str = None) -> dict:
    """Validate a generated game HTML file.

    Checks:
    - HTML structure (doctype, viewport meta, etc.)
    - JavaScript syntax (basic checks)
    - Config system integration
    - Theme CSS variables
    - Required UI elements
    - Math integration points
    - File size

    Returns:
        {"critical": [...], "warnings": [...], "info": [...], "passed": bool}
    """
    critical = []
    warnings = []
    info = []

    # 1. HTML Structure
    if "<!DOCTYPE html>" not in html and "<!doctype html>" not in html:
        warnings.append("Missing DOCTYPE declaration")
    if '<meta name="viewport"' not in html:
        warnings.append("Missing viewport meta tag (mobile support)")
    if "<canvas" not in html:
        warnings.append("No <canvas> element found (most games need one)")
    if "</html>" not in html:
        critical.append("HTML file not properly closed")

    # 2. Config System
    if "GAME_CONFIG" not in html and "window.GAME_CONFIG" not in html:
        critical.append("No GAME_CONFIG reference — game won't read config")
    if "use strict" not in html:
        warnings.append("Missing 'use strict' — recommended for safety")

    # 3. Theme Integration
    css_vars_found = len(re.findall(r"var\(--[\w-]+\)", html))
    if css_vars_found < 3:
        warnings.append(f"Only {css_vars_found} CSS variable references — theme may not apply properly")
    else:
        info.append(f"{css_vars_found} CSS variable references found")

    # 4. UI Elements
    if "balance" not in html.lower():
        warnings.append("No balance display found")
    if "bet" not in html.lower():
        warnings.append("No bet controls found")

    # 5. Audio
    if "AudioContext" in html or "audioCtx" in html or "webkitAudioContext" in html:
        info.append("Web Audio API integration detected")
    else:
        warnings.append("No Web Audio API detected — game will be silent")

    # 6. Responsive
    if "@media" in html:
        info.append("Responsive media queries found")
    else:
        warnings.append("No @media queries — may not be mobile responsive")

    # 7. File size
    size_kb = len(html) / 1024
    info.append(f"File size: {size_kb:.1f} KB")
    if size_kb > 100:
        warnings.append(f"Large file ({size_kb:.0f}KB) — consider optimization")
    if size_kb < 5:
        warnings.append(f"Very small file ({size_kb:.1f}KB) — might be incomplete")

    # 8. Game-type specific checks
    if game_type:
        _type_specific_checks(html, game_type, critical, warnings, info)

    # 9. JS syntax (basic — look for common errors)
    # Check balanced braces in <script> blocks
    script_blocks = re.findall(r"<script[^>]*>([\s\S]*?)</script>", html)
    for i, block in enumerate(script_blocks):
        opens = block.count("{")
        closes = block.count("}")
        if abs(opens - closes) > 2:
            warnings.append(f"Script block #{i+1}: unbalanced braces ({opens} open, {closes} close)")

    return {
        "critical": critical,
        "warnings": warnings,
        "info": info,
        "passed": len(critical) == 0,
        "score": max(0, 100 - len(critical) * 25 - len(warnings) * 5),
    }


def _type_specific_checks(html: str, game_type: str,
                          critical: list, warnings: list, info: list):
    """Game-type specific validation checks."""
    checks = {
        "crash": [
            ("HOUSE_EDGE", "house edge variable"),
            ("cashout", "cashout button/mechanism"),
            ("multiplier", "multiplier display"),
        ],
        "plinko": [
            ("MULT_TABLES", "multiplier tables"),
            ("ball", "ball physics reference"),
            ("bucket", "bucket/bin reference"),
        ],
        "mines": [
            ("mine", "mine reference"),
            ("reveal", "reveal mechanism"),
            ("grid", "grid reference"),
        ],
        "dice": [
            ("prediction", "prediction input"),
            ("roll", "dice roll mechanism"),
            ("slider", "probability slider"),
        ],
        "wheel": [
            ("SEGMENTS", "wheel segments"),
            ("spin", "spin mechanism"),
            ("rotate", "rotation animation"),
        ],
        "hilo": [
            ("card", "card reference"),
            ("higher", "higher/lower buttons"),
            ("streak", "streak/multiplier tracking"),
        ],
        "chicken": [
            ("lane", "lane reference"),
            ("hazard", "hazard/obstacle reference"),
            ("column", "column selection"),
        ],
        "scratch": [
            ("SYMBOLS", "symbol definitions"),
            ("scratch", "scratch mechanism"),
            ("match", "matching logic"),
        ],
    }

    for keyword, desc in checks.get(game_type, []):
        if keyword.lower() not in html.lower():
            warnings.append(f"Missing {desc} ('{keyword}')")
        else:
            info.append(f"Found {desc}")


# ═══════════════════════════════════════════════════════════════
# CrewAI Tool Wrappers (for agent integration)
# ═══════════════════════════════════════════════════════════════

try:
    from crewai.tools import BaseTool

    class GameScaffoldTool(BaseTool):
        name: str = "game_scaffold"
        description: str = (
            "Get a config-ready HTML5 game scaffold for a specific game type. "
            "Input: game type (crash, plinko, mines, dice, wheel, hilo, chicken, scratch). "
            "Returns: Complete HTML scaffold that boots from window.GAME_CONFIG."
        )

        def _run(self, game_type: str) -> str:
            try:
                html = get_scaffold(game_type)
                return f"Scaffold loaded ({len(html):,} bytes). "
                f"Insert config via window.GAME_CONFIG before the config loader block."
            except Exception as e:
                return f"Error: {e}"

    class ReferenceCodeTool(BaseTool):
        name: str = "reference_code"
        description: str = (
            "Search existing games for code patterns. "
            "Input: pattern name (canvas_setup, physics_loop, audio_engine, bet_controls, "
            "theme_css, state_machine, particle_system, responsive_layout) "
            "and optional game type. Returns: extracted code snippet."
        )

        def _run(self, query: str) -> str:
            parts = query.split(",")
            pattern = parts[0].strip()
            game = parts[1].strip() if len(parts) > 1 else None
            results = get_reference_code(pattern, game)
            if isinstance(results, dict) and "error" in results:
                return json.dumps(results)
            return json.dumps(results[:2], indent=2)  # Limit to 2 results

    class GameValidatorTool(BaseTool):
        name: str = "game_validator"
        description: str = (
            "Validate a generated game HTML file. "
            "Input: HTML content (first 5000 chars) and optional game type. "
            "Returns: validation report with critical issues, warnings, and score."
        )

        def _run(self, html_and_type: str) -> str:
            # Parse input: first line may be game type, rest is HTML
            lines = html_and_type.split("\n", 1)
            game_type = None
            html = html_and_type
            if lines[0].strip() in _GAME_FILES:
                game_type = lines[0].strip()
                html = lines[1] if len(lines) > 1 else ""
            result = validate_game_html(html, game_type)
            return json.dumps(result, indent=2)

except ImportError:
    # CrewAI not installed — tools not available but functions still work
    pass


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python -m tools.minigame_codegen scaffold <game_type>")
        print("  python -m tools.minigame_codegen validate <game_type>")
        print("  python -m tools.minigame_codegen patterns")
        print("  python -m tools.minigame_codegen reference <pattern> [game_type]")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "scaffold":
        gt = sys.argv[2] if len(sys.argv) > 2 else "crash"
        html = get_scaffold(gt)
        print(f"Scaffold for {gt}: {len(html):,} bytes")
        out = f"/tmp/scaffold_{gt}.html"
        with open(out, "w") as f:
            f.write(html)
        print(f"Saved to {out}")

    elif cmd == "validate":
        gt = sys.argv[2] if len(sys.argv) > 2 else "crash"
        html = get_full_game_source(gt)
        result = validate_game_html(html, gt)
        print(json.dumps(result, indent=2))

    elif cmd == "patterns":
        for p in list_reference_patterns():
            print(f"  {p['name']:20s} — {p['description']}")

    elif cmd == "reference":
        pattern = sys.argv[2] if len(sys.argv) > 2 else "canvas_setup"
        gt = sys.argv[3] if len(sys.argv) > 3 else None
        results = get_reference_code(pattern, gt)
        if isinstance(results, list):
            for r in results:
                print(f"\n=== {r['game']} ({r['code_length']} chars) ===")
                print(r['code'][:500])
        else:
            print(json.dumps(results, indent=2))
