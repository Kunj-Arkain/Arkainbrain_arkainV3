"""
ARKAINBRAIN — Arcade Engineer Agent (Phase 3)

Codename: Kenji Sato
Role: Generate complete, playable, single-file HTML5 mini-games from
      MiniGameConfig JSON + MathModel JSON + theme assets.

The agent:
  1. Reads the config + math model
  2. Selects the appropriate game scaffold
  3. Injects config-driven math, theme, audio, and compliance code
  4. Validates the output (syntax, RTP, responsiveness)
  5. Returns a complete, self-contained HTML5 file

Usage:
    from agents.arcade_engineer import create_arcade_engineer, ARCADE_ENGINEER_TASK
    agent = create_arcade_engineer()
    # Use in CrewAI task or standalone
"""

from __future__ import annotations

import os
import json


def create_arcade_engineer():
    """Create the Arcade Engineer agent for CrewAI pipeline integration."""
    try:
        from crewai import Agent
        from config.settings import LLMConfig
        from tools.minigame_codegen import (
            GameScaffoldTool,
            GameValidatorTool,
            ReferenceCodeTool,
        )

        return Agent(
            role="Arcade Engineer & HTML5 Game Developer",
            goal=(
                "Generate complete, playable, single-file HTML5 mini-games from config specifications. "
                "Every game must be self-contained (no external dependencies), responsive (mobile-first), "
                "mathematically certified (RTP matches config), visually polished, and include provably-fair "
                "RNG integration. The games should feel premium, with smooth animations, procedural audio, "
                "and a professional UI that rivals top iGaming studios."
            ),
            backstory=(
                "You are Kenji Sato, a legendary game developer who spent 15 years as lead engineer at "
                "a major iGaming studio, building HTML5 casino games played by millions. You've shipped "
                "over 200 titles across every game type — slots, crash, plinko, mines, dice, roulette. "
                "You know every trick: Canvas rendering pipelines, procedural audio synthesis with Web Audio API, "
                "spring-based animations, responsive layouts that work from 320px phones to 4K monitors. "
                "You write games that are not just functional but beautiful — every detail polished, every "
                "animation smooth, every sound perfectly timed. Your code is clean, well-structured, and "
                "always uses CSS custom properties for themability. You never use external CDN links or "
                "dependencies — everything is self-contained in a single HTML file."
            ),
            llm=LLMConfig.get_llm("lead_producer"),  # GPT-5 / 128K output
            max_iter=5,
            verbose=True,
            tools=[
                GameScaffoldTool(),
                GameValidatorTool(),
                ReferenceCodeTool(),
            ],
        )
    except ImportError:
        # Standalone mode (no crewai)
        return None


# ═══════════════════════════════════════════════════════════════
# Task Prompts
# ═══════════════════════════════════════════════════════════════

GAME_GENERATION_PROMPT = """\
ARCADE ENGINEER TASK: Generate Mini-Game

You are generating a complete, playable HTML5 mini-game from the following specifications:

=== GAME CONFIG ===
{config_json}

=== MATH MODEL ===
{math_json}

=== INSTRUCTIONS ===

1. **Use the scaffold tool** to get the base template for this game type.
   The scaffold includes the HTML structure, CSS theme system, JS state machine,
   and audio engine — all pre-wired.

2. **Use the reference code tool** to examine how existing games implement
   specific features (physics, animations, UI patterns).

3. **Generate the complete game** following these requirements:
   - Single HTML file, no external dependencies
   - All CSS/JS embedded inline
   - Theme from config (CSS custom properties: --primary, --secondary, etc.)
   - Math from model (house edge, multipliers, payouts)
   - Provably-fair RNG integration (window.GAME_CONFIG.rng)
   - Responsive: works on 320px-4K
   - Procedural audio via Web Audio API
   - Smooth animations (CSS transitions + requestAnimationFrame)
   - Professional UI: balance display, bet controls, cashout button
   - Compliance: session time tracking, reality check popups

4. **Validate the game** using the validator tool. Fix any issues found.

5. **Return the complete HTML file** as your output.

CRITICAL: The game MUST read its math parameters from window.GAME_CONFIG,
not from hardcoded values. This allows the config injector to theme and
re-parameterize the game dynamically.
"""

NOVEL_GAME_PROMPT = """\
ARCADE ENGINEER TASK: Generate Novel Mini-Game

Create an entirely NEW game mechanic that doesn't exist in our template library.

=== MECHANIC DESCRIPTION ===
{mechanic_description}

=== THEME ===
{theme_json}

=== MATH CONSTRAINTS ===
Target RTP: {target_rtp}%
Volatility: {volatility}
Max Win: {max_win}x

=== INSTRUCTIONS ===

1. **Design the game logic** from the mechanic description.
   - Define clear win/loss conditions
   - Map the math constraints to game outcomes
   - Design the UI/UX flow

2. **Use reference code** from existing games for:
   - Canvas rendering patterns
   - Audio synthesis
   - UI layout systems
   - Animation techniques

3. **Generate the complete game** as a single HTML file.
   - All the same requirements as standard games
   - PLUS: novel mechanic implementation
   - PLUS: ensure the math model produces the target RTP

4. **Validate thoroughly** — novel games are more likely to have edge cases.

CRITICAL: Even though this is a novel mechanic, it MUST use the standard
config system (window.GAME_CONFIG) for all parameterizable values.
"""


# ═══════════════════════════════════════════════════════════════
# Standalone Generator (no CrewAI needed)
# ═══════════════════════════════════════════════════════════════

class ArcadeEngineerStandalone:
    """Standalone game generator that works without CrewAI.

    Uses the scaffold + config injection system directly.
    This is the fast path for generating themed variants of existing games.
    """

    def __init__(self):
        self._scaffolds = {}

    def generate_game(self, game_type: str, config_json: str,
                      math_json: str = None, output_path: str = None) -> str:
        """Generate a complete themed game from config.

        For existing game types, this uses the scaffold + injection system
        rather than LLM generation (faster, deterministic, cheaper).

        Args:
            game_type: One of the 8 game types
            config_json: MiniGameConfig JSON string
            math_json: Optional MathModel JSON string
            output_path: Where to save (if None, returns HTML string)

        Returns:
            Complete HTML5 game as string
        """
        from tools.minigame_codegen import (
            get_scaffold,
            inject_config_into_scaffold,
            validate_game_html,
        )

        # Get scaffold
        scaffold_html = get_scaffold(game_type)

        # Parse config
        config = json.loads(config_json) if isinstance(config_json, str) else config_json

        # Inject config into scaffold
        game_html = inject_config_into_scaffold(scaffold_html, config, math_json)

        # Validate
        issues = validate_game_html(game_html, game_type)
        if issues.get("critical"):
            raise ValueError(f"Game validation failed: {issues['critical']}")

        # Save if requested
        if output_path:
            os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
            with open(output_path, "w") as f:
                f.write(game_html)

        return game_html

    def generate_from_existing(self, game_type: str, source_html: str,
                               config: dict, output_path: str = None) -> str:
        """Generate by injecting config into an existing game file.

        This is the fastest path — takes a Phase 3 game HTML and
        applies config-driven theming + math overrides.
        """
        from tools.minigame_injector import inject_config as _inject

        # Build a config object if needed
        try:
            from tools.minigame_config import build_config, MiniGameConfig
            if isinstance(config, dict):
                cfg = build_config(
                    game_type=game_type,
                    theme_overrides=config.get("theme", {}),
                    target_rtp=config.get("math", {}).get("target_rtp", 96.0),
                    volatility=config.get("volatility", "medium"),
                    starting_balance=config.get("math", {}).get("starting_balance", 1000),
                )
            else:
                cfg = config
        except ImportError:
            raise RuntimeError("pydantic required for config-based injection")

        game_html = _inject(source_html, cfg)

        if output_path:
            os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
            with open(output_path, "w") as f:
                f.write(game_html)

        return game_html
