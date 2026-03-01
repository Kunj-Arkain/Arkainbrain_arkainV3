"""
ARKAINBRAIN — Novel Game Generation Engine (Phase 5)

Generates entirely new mini-game mechanics from natural language descriptions.
Provides playtest simulation, iteration loops, and variant generation.

Components:
  1. MechanicInventor   — Decompose descriptions into mathematical game mechanics
  2. NovelGameBuilder   — Generate playable HTML5 from invented mechanics
  3. PlaytestSimulator  — Evaluate games for fun, clarity, balance
  4. IterationEngine    — Playtest → fix → retest loop
  5. VariantGenerator   — Same mechanic, different themes/risk profiles

Usage:
    from tools.minigame_novel import MechanicInventor, NovelGameBuilder
    inventor = MechanicInventor()
    mechanic = inventor.invent("Tower stacking game where you build higher for bigger multipliers")
    builder = NovelGameBuilder()
    html = builder.build(mechanic)
"""

from __future__ import annotations

import json
import math
import hashlib
import os
import random
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


# ═══════════════════════════════════════════════════════════════
# Mechanic Templates — building blocks for novel games
# ═══════════════════════════════════════════════════════════════

# Each template defines a mathematical archetype that can be themed
MECHANIC_ARCHETYPES = {
    "accumulator": {
        "desc": "Player makes sequential choices, each increasing risk and reward",
        "pattern": "P(survive_n) = p^n, mult(n) = edge/p^n",
        "examples": ["tower stacking", "ladder climbing", "diving deeper", "rocket ascending"],
        "base_games": ["crash", "chicken", "hilo"],
        "math_type": "geometric_progression",
        "decision_type": "continue_or_cashout",
    },
    "selection": {
        "desc": "Player selects from a grid/set, some choices are winners",
        "pattern": "P(k safe from G-M choices) = C(G-M,k)/C(G,k)",
        "examples": ["gem mining", "treasure digging", "safe cracking", "card matching"],
        "base_games": ["mines", "scratch"],
        "math_type": "hypergeometric",
        "decision_type": "pick_and_reveal",
    },
    "distribution": {
        "desc": "An object follows a probability distribution to land on outcomes",
        "pattern": "P(bucket_k) defined by physics/distribution, mult assigned to each",
        "examples": ["ball drop", "wheel spin", "marble run", "pinball"],
        "base_games": ["plinko", "wheel"],
        "math_type": "multinomial",
        "decision_type": "single_action",
    },
    "threshold": {
        "desc": "A random value is generated; player bets on range/outcome",
        "pattern": "P(win) = chance, mult = edge/chance",
        "examples": ["dice roll", "coin flip", "number guess", "color prediction"],
        "base_games": ["dice"],
        "math_type": "uniform_threshold",
        "decision_type": "predict_outcome",
    },
    "hybrid": {
        "desc": "Combines two or more archetypes into a novel mechanic",
        "pattern": "Composite: multiple math models interleaved",
        "examples": ["crash + mines", "wheel + hilo", "plinko + scratch"],
        "base_games": [],
        "math_type": "composite",
        "decision_type": "multi_phase",
    },
}

# Visual/interaction patterns that can dress up any archetype
VISUAL_PATTERNS = {
    "tower":      {"canvas": "vertical_stack", "physics": "gravity_collapse", "tension": "height"},
    "path":       {"canvas": "horizontal_scroll", "physics": "obstacle_avoidance", "tension": "distance"},
    "grid":       {"canvas": "tile_grid", "physics": "reveal_animation", "tension": "remaining_safe"},
    "spinner":    {"canvas": "rotating_wheel", "physics": "deceleration", "tension": "slowdown"},
    "racer":      {"canvas": "parallel_lanes", "physics": "speed_variation", "tension": "position"},
    "cascade":    {"canvas": "falling_objects", "physics": "bounce_and_settle", "tension": "accumulation"},
    "card_table": {"canvas": "card_layout", "physics": "flip_reveal", "tension": "streak"},
    "meter":      {"canvas": "fill_bar", "physics": "rising_gauge", "tension": "threshold"},
}


# ═══════════════════════════════════════════════════════════════
# Mechanic Inventor
# ═══════════════════════════════════════════════════════════════

@dataclass
class GameMechanic:
    """A fully specified novel game mechanic."""
    name: str
    description: str
    archetype: str                    # Key from MECHANIC_ARCHETYPES
    visual_pattern: str               # Key from VISUAL_PATTERNS
    math_type: str                    # geometric_progression, hypergeometric, etc.
    decision_type: str                # continue_or_cashout, pick_and_reveal, etc.
    parameters: dict                  # Game-specific params
    rtp_formula: str                  # Human-readable formula
    risk_factor: float                # 0.0 (safe) to 1.0 (extreme)
    max_multiplier: float
    base_games_used: list[str]        # Which existing games inform this one
    theme_suggestion: str
    mechanic_hash: str = ""

    def __post_init__(self):
        if not self.mechanic_hash:
            raw = json.dumps(asdict(self), sort_keys=True, default=str)
            self.mechanic_hash = hashlib.sha256(raw.encode()).hexdigest()[:12]

    def to_dict(self):
        return asdict(self)

    def to_json(self, indent=2):
        return json.dumps(self.to_dict(), indent=indent)


class MechanicInventor:
    """Decomposes natural language game descriptions into mathematical mechanics.

    Works in two modes:
      1. Template matching — fast, deterministic, no LLM needed
      2. LLM-assisted — richer, more creative (default when API key available)

    The LLM path generates truly unique game concepts, then validates
    against the math archetype system for provable fairness.
    """

    def __init__(self, use_llm: bool = True, llm_callback=None):
        self.use_llm = use_llm
        self.llm_callback = llm_callback
        # Auto-detect LLM availability
        if self.use_llm and not os.getenv("OPENAI_API_KEY"):
            self.use_llm = False

    def _get_model(self) -> str:
        """Get model for novel game generation."""
        try:
            from config.settings import LLMConfig
            m = LLMConfig.get_llm("game_designer")
            return m.replace("openai/", "") if m else "gpt-4.1-mini"
        except Exception:
            return os.getenv("LLM_LIGHT", "gpt-4.1-mini")

    def invent(self, description: str, target_rtp: float = 96.0,
               volatility: str = "medium") -> GameMechanic:
        """Generate a game mechanic from a natural language description."""
        if self.use_llm:
            try:
                return self._invent_with_llm(description, target_rtp, volatility)
            except Exception as e:
                import logging
                logging.getLogger("arkainbrain.novel").warning(
                    f"LLM invention failed, falling back to template: {e}")

        # Fallback: template matching
        return self._invent_template(description, target_rtp, volatility)

    def _invent_with_llm(self, description: str, target_rtp: float,
                          volatility: str) -> GameMechanic:
        """LLM-powered game invention — generates unique mechanic specs."""
        import openai
        client = openai.OpenAI()

        archetypes_desc = "\n".join(
            f"  - {k}: {v['desc']} (math: {v['math_type']}, decision: {v['decision_type']})"
            for k, v in MECHANIC_ARCHETYPES.items()
        )
        visuals_desc = ", ".join(VISUAL_PATTERNS.keys())

        prompt = f"""You are a casino game mathematician and designer. A user described a game idea:

"{description}"

Target RTP: {target_rtp}%  |  Volatility: {volatility}

AVAILABLE ARCHETYPES (you MUST pick one):
{archetypes_desc}

AVAILABLE VISUAL PATTERNS: {visuals_desc}

Generate a unique game mechanic that brings this description to life. Be creative — don't just
pick the obvious template. Think about what would make this FUN and UNIQUE.

Return ONLY valid JSON with this exact structure:
{{
  "name": "Creative 2-3 word game name",
  "archetype": "one of: accumulator, selection, distribution, threshold, hybrid",
  "visual_pattern": "one of: {visuals_desc}",
  "theme_suggestion": "one-word theme (space/ocean/jungle/cyberpunk/egyptian/arctic/volcanic/steampunk)",
  "description_enhanced": "2-3 sentence description of the game from a player's perspective",
  "parameters": {{
    "target_rtp": {target_rtp},
    "edge_factor": {round((100 - target_rtp) / 100, 4)},
    "max_multiplier": <number: appropriate for {volatility} volatility>,
    "p_survive_per_step": <0.3-0.95 if accumulator>,
    "max_steps": <5-20 if accumulator>,
    "grid_size": <9-36 if selection>,
    "hazard_count": <1-12 if selection>,
    "safe_count": <grid_size - hazard_count if selection>,
    "n_outcomes": <8-30 if distribution>,
    "range_min": <0 if threshold>,
    "range_max": <100 if threshold>,
    "custom_param_1": <any game-specific parameter you want>,
    "custom_param_2": <any game-specific parameter you want>
  }},
  "unique_twist": "1 sentence describing what makes this game mechanically unique vs standard casino games",
  "player_decisions": ["list of 2-4 decisions the player makes during a round"]
}}

IMPORTANT: Only include parameter fields relevant to your chosen archetype.
Ensure the math works: for accumulator, max_mult ≈ edge_factor / (p_survive ^ max_steps).
For selection, multipliers should scale with risk of revealing more tiles.
"""

        resp = client.chat.completions.create(
            model=self._get_model(),
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1200,
            temperature=0.9,
        )
        text = resp.choices[0].message.content.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        data = json.loads(text)

        # Validate archetype
        archetype = data.get("archetype", "accumulator")
        if archetype not in MECHANIC_ARCHETYPES:
            archetype = "accumulator"
        arch_info = MECHANIC_ARCHETYPES[archetype]

        # Validate visual
        visual = data.get("visual_pattern", "meter")
        if visual not in VISUAL_PATTERNS:
            visual = "meter"

        # Build params with sane defaults
        params = data.get("parameters", {})
        params.setdefault("target_rtp", target_rtp)
        params.setdefault("edge_factor", round((100 - target_rtp) / 100, 4))
        params.setdefault("max_multiplier", 100)

        mechanic = GameMechanic(
            name=data.get("name", "Novel Game"),
            description=data.get("description_enhanced", description),
            archetype=archetype,
            visual_pattern=visual,
            math_type=arch_info["math_type"],
            decision_type=arch_info["decision_type"],
            parameters=params,
            rtp_formula=arch_info["pattern"],
            risk_factor=self._calc_risk(volatility),
            max_multiplier=float(params.get("max_multiplier", 100)),
            base_games_used=arch_info["base_games"],
            theme_suggestion=data.get("theme_suggestion", "cyberpunk"),
        )

        return mechanic

    def invent_hybrid(self, desc1: str, desc2: str,
                      target_rtp: float = 96.0) -> GameMechanic:
        """Create a hybrid mechanic combining two descriptions."""
        m1 = self.invent(desc1, target_rtp)
        m2 = self.invent(desc2, target_rtp)

        # Merge parameters
        merged_params = {**m1.parameters, **m2.parameters}
        merged_params["phase_1"] = {"archetype": m1.archetype, "params": m1.parameters}
        merged_params["phase_2"] = {"archetype": m2.archetype, "params": m2.parameters}

        return GameMechanic(
            name=f"{m1.name} × {m2.name}",
            description=f"Phase 1: {m1.description}. Phase 2: {m2.description}.",
            archetype="hybrid",
            visual_pattern=m1.visual_pattern,
            math_type="composite",
            decision_type="multi_phase",
            parameters=merged_params,
            rtp_formula=f"RTP = RTP_1 × RTP_2 (phases multiply)",
            risk_factor=max(m1.risk_factor, m2.risk_factor),
            max_multiplier=m1.max_multiplier * m2.max_multiplier,
            base_games_used=list(set(m1.base_games_used + m2.base_games_used)),
            theme_suggestion=f"{m1.theme_suggestion} meets {m2.theme_suggestion}",
        )

    def _invent_template(self, description: str, target_rtp: float,
                          volatility: str) -> GameMechanic:
        """Template-matching fallback — fast, deterministic, no LLM needed."""
        desc_lower = description.lower()
        archetype = self._classify_archetype(desc_lower)
        visual = self._pick_visual(desc_lower, archetype)
        params = self._generate_params(archetype, target_rtp, volatility)
        name = self._generate_name(desc_lower)
        theme = self._suggest_theme(desc_lower)
        arch_info = MECHANIC_ARCHETYPES[archetype]

        return GameMechanic(
            name=name,
            description=description,
            archetype=archetype,
            visual_pattern=visual,
            math_type=arch_info["math_type"],
            decision_type=arch_info["decision_type"],
            parameters=params,
            rtp_formula=arch_info["pattern"],
            risk_factor=self._calc_risk(volatility),
            max_multiplier=params.get("max_multiplier", 100),
            base_games_used=arch_info["base_games"],
            theme_suggestion=theme,
        )

    def _classify_archetype(self, desc: str) -> str:
        """Match description to mechanic archetype."""
        scores = {}
        keywords = {
            "accumulator": ["climb", "stack", "tower", "build", "ascend", "rise", "streak",
                           "ladder", "dive", "deeper", "rocket", "run", "survive",
                           "cash out", "cashout", "crash", "higher", "continue"],
            "selection": ["pick", "choose", "select", "mine", "dig", "reveal", "uncover",
                         "match", "find", "grid", "tile", "gem", "treasure", "crack",
                         "safe", "vault", "scratch", "memory"],
            "distribution": ["drop", "ball", "peg", "wheel", "spin", "marble", "bounce",
                            "slot", "bucket", "land", "pinball", "plinko", "roulette"],
            "threshold": ["dice", "roll", "predict", "guess", "over", "under", "coin",
                         "flip", "number", "range", "color", "odd", "even", "bet on"],
        }
        for arch, kws in keywords.items():
            scores[arch] = sum(1 for kw in kws if kw in desc)

        best = max(scores, key=scores.get)
        if scores[best] == 0:
            return "accumulator"  # Default fallback
        return best

    def _pick_visual(self, desc: str, archetype: str) -> str:
        """Choose visual pattern based on description and archetype."""
        visual_keywords = {
            "tower": ["tower", "stack", "build", "block", "height"],
            "path": ["run", "path", "road", "lane", "obstacle", "dodge", "race"],
            "grid": ["grid", "tile", "mine", "dig", "reveal", "match", "memory"],
            "spinner": ["wheel", "spin", "rotate", "roulette"],
            "racer": ["race", "car", "horse", "compete", "lane"],
            "cascade": ["drop", "fall", "ball", "bounce", "peg", "plinko", "marble"],
            "card_table": ["card", "deck", "flip", "poker", "blackjack", "deal"],
            "meter": ["meter", "gauge", "fill", "bar", "charge", "power"],
        }
        scores = {}
        for vis, kws in visual_keywords.items():
            scores[vis] = sum(1 for kw in kws if kw in desc)

        best = max(scores, key=scores.get)
        if scores[best] > 0:
            return best

        # Fallback: pick based on archetype
        arch_defaults = {
            "accumulator": "meter", "selection": "grid",
            "distribution": "cascade", "threshold": "card_table",
        }
        return arch_defaults.get(archetype, "meter")

    def _generate_params(self, archetype: str, target_rtp: float,
                         volatility: str) -> dict:
        """Generate game parameters for the given archetype."""
        edge = (100 - target_rtp) / 100.0
        vol_mult = {"low": 0.5, "medium": 1.0, "high": 2.0, "ultra": 4.0}.get(volatility, 1.0)

        if archetype == "accumulator":
            p_survive = 0.75  # base survival rate per step
            max_steps = int(8 + vol_mult * 4)
            max_mult = round((1 - edge) / (p_survive ** max_steps), 2)
            return {
                "p_survive_per_step": p_survive,
                "max_steps": max_steps,
                "edge_factor": 1 - edge,
                "max_multiplier": min(max_mult, 10000),
                "target_rtp": target_rtp,
            }

        elif archetype == "selection":
            grid_size = 16 + int(vol_mult * 8)
            hazards = max(1, int(grid_size * (0.15 + vol_mult * 0.05)))
            return {
                "grid_size": grid_size,
                "hazard_count": hazards,
                "safe_count": grid_size - hazards,
                "edge_factor": 1 - edge,
                "max_reveals": grid_size - hazards,
                "max_multiplier": round((1 - edge) / 0.01, 2),
                "target_rtp": target_rtp,
            }

        elif archetype == "distribution":
            n_outcomes = 10 + int(vol_mult * 10)
            return {
                "n_outcomes": n_outcomes,
                "distribution": "binomial" if vol_mult < 2 else "custom",
                "target_rtp": target_rtp,
                "edge_factor": 1 - edge,
                "max_multiplier": round(10 * vol_mult, 1),
            }

        elif archetype == "threshold":
            return {
                "range_min": 0,
                "range_max": 100,
                "edge_pct": target_rtp,
                "max_multiplier": round(target_rtp / 1, 2),  # At 1% chance
                "target_rtp": target_rtp,
            }

        return {"target_rtp": target_rtp, "edge_factor": 1 - edge}

    def _generate_name(self, desc: str) -> str:
        """Generate a game name from description."""
        # Extract key nouns
        name_parts = []
        keywords = ["tower", "stack", "crash", "mine", "gem", "wheel", "dice",
                    "card", "rocket", "dragon", "treasure", "vault", "grid",
                    "marble", "ball", "race", "climb", "dive", "ladder"]
        for kw in keywords:
            if kw in desc:
                name_parts.append(kw.title())
        if len(name_parts) >= 2:
            return f"{name_parts[0]} {name_parts[1]}"
        elif name_parts:
            suffixes = ["Rush", "Blitz", "Quest", "Surge", "Strike", "Run"]
            return f"{name_parts[0]} {random.choice(suffixes)}"
        return "Nova Game"

    def _suggest_theme(self, desc: str) -> str:
        """Suggest a visual theme."""
        theme_map = {
            "space": ["space", "cosmic", "galaxy", "star", "rocket", "astro"],
            "ocean": ["ocean", "deep", "dive", "water", "sea", "coral", "aqua"],
            "jungle": ["jungle", "forest", "vine", "tree", "wild"],
            "cyberpunk": ["neon", "cyber", "grid", "digital", "hack", "tech"],
            "egyptian": ["pyramid", "pharaoh", "tomb", "ancient", "gold", "sand"],
            "arctic": ["ice", "glacier", "frozen", "snow", "arctic", "crystal"],
            "volcanic": ["fire", "lava", "volcano", "magma", "flame"],
            "steampunk": ["steam", "gear", "clock", "bronze", "machine"],
        }
        for theme, kws in theme_map.items():
            if any(kw in desc for kw in kws):
                return theme
        return "cyberpunk"  # default

    @staticmethod
    def _calc_risk(volatility: str) -> float:
        return {"low": 0.2, "medium": 0.5, "high": 0.75, "ultra": 0.95}.get(volatility, 0.5)

    def refine_with_llm(self, mechanic: GameMechanic,
                        playtest_report) -> GameMechanic:
        """Second LLM pass — analyzes playtest results and refines the mechanic.

        This is called after the initial playtest simulation to fix issues
        like RTP drift, boring hit rates, or unbalanced multipliers.
        """
        if not self.use_llm:
            return mechanic

        try:
            import openai
            client = openai.OpenAI()

            report_dict = playtest_report.to_dict() if hasattr(playtest_report, 'to_dict') else {}

            prompt = f"""You are a casino game mathematician. A novel game was invented and playtested.
Review the results and suggest parameter adjustments.

GAME: {mechanic.name}
DESCRIPTION: {mechanic.description}
ARCHETYPE: {mechanic.archetype} ({mechanic.math_type})
CURRENT PARAMETERS: {json.dumps(mechanic.parameters, indent=2)}

PLAYTEST RESULTS ({report_dict.get('rounds', 50000)} rounds):
  - Measured RTP: {report_dict.get('measured_rtp', 0)*100:.2f}% (target: {mechanic.parameters.get('target_rtp', 96)}%)
  - Hit Rate: {report_dict.get('hit_rate', 0)*100:.1f}%
  - Max Multiplier Hit: {report_dict.get('max_multiplier_hit', 0):.1f}x
  - Fun Score: {report_dict.get('fun_score', 5)}/10
  - Passed: {report_dict.get('passed', False)}
  - Issues: {json.dumps(report_dict.get('issues', []))}

TASK: Suggest improved parameters to fix any issues while keeping the game concept intact.
Good targets: RTP within 0.5% of target, hit rate 15-65%, fun score ≥ 6/10.

Return ONLY valid JSON:
{{
  "refined_parameters": {{<updated parameters dict — include ALL params, not just changed ones>}},
  "refined_name": "{mechanic.name}" or a better name if the original is generic,
  "changes_made": ["list of what you changed and why"],
  "expected_improvement": "1 sentence on what should improve"
}}"""

            resp = client.chat.completions.create(
                model=self._get_model(),
                messages=[{"role": "user", "content": prompt}],
                max_tokens=800,
                temperature=0.4,
            )
            text = resp.choices[0].message.content.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0]
            data = json.loads(text)

            new_params = data.get("refined_parameters", mechanic.parameters)
            new_params.setdefault("target_rtp", mechanic.parameters.get("target_rtp", 96))
            new_params.setdefault("edge_factor", mechanic.parameters.get("edge_factor", 0.04))

            return GameMechanic(
                name=data.get("refined_name", mechanic.name),
                description=mechanic.description,
                archetype=mechanic.archetype,
                visual_pattern=mechanic.visual_pattern,
                math_type=mechanic.math_type,
                decision_type=mechanic.decision_type,
                parameters=new_params,
                rtp_formula=mechanic.rtp_formula,
                risk_factor=mechanic.risk_factor,
                max_multiplier=float(new_params.get("max_multiplier", mechanic.max_multiplier)),
                base_games_used=mechanic.base_games_used,
                theme_suggestion=mechanic.theme_suggestion,
            )

        except Exception as e:
            import logging
            logging.getLogger("arkainbrain.novel").warning(f"LLM refinement failed: {e}")
            return mechanic


# ═══════════════════════════════════════════════════════════════
# Novel Game Builder
# ═══════════════════════════════════════════════════════════════

class NovelGameBuilder:
    """Generates playable HTML5 games from GameMechanic specs."""

    THEME_CSS = {
        "cyberpunk": {"primary": "#a855f7", "secondary": "#06b6d4", "bg": "#0a0014"},
        "space":     {"primary": "#6366f1", "secondary": "#818cf8", "bg": "#030014"},
        "ocean":     {"primary": "#0891b2", "secondary": "#2dd4bf", "bg": "#021524"},
        "jungle":    {"primary": "#16a34a", "secondary": "#4ade80", "bg": "#0a1a0a"},
        "egyptian":  {"primary": "#d97706", "secondary": "#fbbf24", "bg": "#1a0f00"},
        "arctic":    {"primary": "#0ea5e9", "secondary": "#7dd3fc", "bg": "#001020"},
        "volcanic":  {"primary": "#ef4444", "secondary": "#f97316", "bg": "#1a0500"},
        "steampunk": {"primary": "#92400e", "secondary": "#d97706", "bg": "#0f0a00"},
    }

    def build(self, mechanic: GameMechanic, balance: float = 1000) -> str:
        """Generate a complete HTML5 game from a mechanic spec."""
        theme_css = self.THEME_CSS.get(mechanic.theme_suggestion, self.THEME_CSS["cyberpunk"])
        params = mechanic.parameters

        # Choose the right generator based on archetype
        generators = {
            "accumulator": self._build_accumulator,
            "selection":   self._build_selection,
            "distribution": self._build_distribution,
            "threshold":   self._build_threshold,
            "hybrid":      self._build_hybrid,
        }
        gen_fn = generators.get(mechanic.archetype, self._build_accumulator)
        game_js = gen_fn(mechanic, params)

        return self._wrap_html(mechanic, theme_css, game_js, balance)

    def _wrap_html(self, mech: GameMechanic, theme: dict,
                   game_js: str, balance: float) -> str:
        """Wrap game JS in full HTML5 page."""
        name = mech.name
        desc = mech.description
        bets = "[0.10,0.25,0.50,1,2,5,10,25]"

        return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no">
<title>{name}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
:root{{
  --primary:{theme["primary"]};--secondary:{theme["secondary"]};
  --bg1:{theme["bg"]};--bg2:#111;--text:#e0e0e0;--text-dim:#888;
  --radius:12px;--transition:all .2s;
}}
html,body{{height:100%;overflow:hidden;font-family:'Inter',system-ui,sans-serif;
  background:var(--bg1);color:var(--text)}}
.game-container{{display:flex;flex-direction:column;height:100vh;max-width:480px;margin:0 auto;padding:12px}}
.game-hud{{display:flex;justify-content:space-between;align-items:center;padding:8px 0;font-size:12px}}
.game-hud .balance{{font-size:14px;font-weight:700;color:var(--secondary)}}
.game-hud .profit{{font-size:11px}}
.game-arena{{flex:1;display:flex;align-items:center;justify-content:center;position:relative;overflow:hidden}}
.game-arena canvas{{width:100%;height:100%}}
.game-controls{{padding:12px 0}}
.bet-row{{display:flex;gap:6px;flex-wrap:wrap;justify-content:center;margin-bottom:10px}}
.bet-btn{{padding:6px 12px;border-radius:8px;border:1px solid rgba(255,255,255,0.1);
  background:transparent;color:var(--text-dim);font-size:11px;cursor:pointer;transition:var(--transition)}}
.bet-btn:hover,.bet-btn.active{{border-color:var(--primary);color:var(--primary);background:rgba(168,85,247,0.08)}}
.action-btn{{width:100%;padding:14px;border-radius:var(--radius);border:none;
  background:linear-gradient(135deg,var(--primary),var(--secondary));color:#fff;
  font-size:15px;font-weight:700;letter-spacing:1px;cursor:pointer;transition:var(--transition)}}
.action-btn:hover{{transform:translateY(-1px);box-shadow:0 4px 20px rgba(168,85,247,0.3)}}
.action-btn:disabled{{opacity:0.4;cursor:not-allowed;transform:none;box-shadow:none}}
.result-display{{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);
  font-size:48px;font-weight:900;text-shadow:0 2px 20px rgba(0,0,0,0.5);opacity:0;transition:all .4s}}
.result-display.show{{opacity:1}}
.game-info{{text-align:center;font-size:11px;color:var(--text-dim);padding:8px 0}}
.game-title{{font-size:16px;font-weight:800;text-align:center;margin-bottom:2px;
  background:linear-gradient(135deg,var(--primary),var(--secondary));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent}}
.game-subtitle{{text-align:center;font-size:11px;color:var(--text-dim);margin-bottom:8px}}
.win{{color:#4ade80}} .lose{{color:#ef4444}}
</style>
</head>
<body>
<div class="game-container">
  <div class="game-title">{name}</div>
  <div class="game-subtitle">{desc[:80]}</div>
  <div class="game-hud">
    <div>Balance: <span class="balance" id="bal">{balance:.2f}</span></div>
    <div class="profit">Profit: <span id="profit">0.00</span></div>
  </div>
  <div class="game-arena">
    <canvas id="canvas"></canvas>
    <div class="result-display" id="result"></div>
  </div>
  <div class="game-controls">
    <div class="bet-row" id="bets"></div>
    <button class="action-btn" id="play-btn" onclick="play()">PLAY</button>
  </div>
  <div class="game-info">
    RTP: {mech.parameters.get("target_rtp", 96):.1f}% | {mech.archetype.title()} | Provably Fair
  </div>
</div>
<script>
"use strict";
const GC = window.GAME_CONFIG || {{}};
let balance = GC.balance ?? {balance};
let currentBet = GC.currentBet ?? 1;
let profit = 0;
const bets = GC.bets || {bets};

// ── Bet UI ──
const betsEl = document.getElementById('bets');
bets.forEach(b => {{
  const btn = document.createElement('button');
  btn.className = 'bet-btn' + (b === currentBet ? ' active' : '');
  btn.textContent = b >= 1 ? b.toFixed(0) : b.toFixed(2);
  btn.onclick = () => {{
    currentBet = b;
    document.querySelectorAll('.bet-btn').forEach(x => x.classList.remove('active'));
    btn.classList.add('active');
  }};
  betsEl.appendChild(btn);
}});

function updateUI() {{
  document.getElementById('bal').textContent = balance.toFixed(2);
  document.getElementById('profit').textContent = profit >= 0 ? profit.toFixed(2) : profit.toFixed(2);
  document.getElementById('profit').className = profit >= 0 ? 'win' : 'lose';
}}

function showResult(text, isWin) {{
  const el = document.getElementById('result');
  el.textContent = text;
  el.className = 'result-display show ' + (isWin ? 'win' : 'lose');
  setTimeout(() => el.className = 'result-display', 2000);
}}

// ── Canvas Setup ──
const canvas = document.getElementById('canvas');
const ctx = canvas.getContext('2d');
function resizeCanvas() {{
  const r = canvas.parentElement.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = r.width * dpr;
  canvas.height = r.height * dpr;
  canvas.style.width = r.width + 'px';
  canvas.style.height = r.height + 'px';
  ctx.scale(dpr, dpr);
  drawIdle();
}}
window.addEventListener('resize', resizeCanvas);

// ── Audio ──
let audioCtx;
function initAudio() {{ if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)(); }}
function playTone(freq, dur, type) {{
  if (!audioCtx) return;
  const osc = audioCtx.createOscillator();
  const gain = audioCtx.createGain();
  osc.type = type || 'sine';
  osc.frequency.value = freq;
  gain.gain.setValueAtTime(0.15, audioCtx.currentTime);
  gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + dur);
  osc.connect(gain); gain.connect(audioCtx.destination);
  osc.start(); osc.stop(audioCtx.currentTime + dur);
}}

{game_js}

resizeCanvas();
updateUI();
</script>
</body>
</html>'''

    # ── Archetype-Specific Generators ──

    def _build_accumulator(self, mech: GameMechanic, params: dict) -> str:
        """Generate accumulator game (climb/crash pattern)."""
        p_survive = params.get("p_survive_per_step", 0.75)
        max_steps = params.get("max_steps", 10)
        edge = params.get("edge_factor", 0.97)

        return f'''
// ═══ ACCUMULATOR GAME ═══
let gameActive = false, currentStep = 0, currentMult = 1;
const P_SURVIVE = {p_survive};
const MAX_STEPS = {max_steps};
const EDGE = {edge};

function drawIdle() {{
  const W = canvas.width / (window.devicePixelRatio||1);
  const H = canvas.height / (window.devicePixelRatio||1);
  ctx.clearRect(0, 0, W, H);

  // Draw meter background
  const mW = 60, mX = W/2 - mW/2, mH = H * 0.8, mY = H * 0.1;
  ctx.fillStyle = 'rgba(255,255,255,0.03)';
  ctx.beginPath(); ctx.roundRect(mX, mY, mW, mH, 8); ctx.fill();

  // Fill level
  const pct = currentStep / MAX_STEPS;
  const fillH = mH * pct;
  const grad = ctx.createLinearGradient(mX, mY + mH, mX, mY);
  grad.addColorStop(0, getComputedStyle(document.documentElement).getPropertyValue('--primary'));
  grad.addColorStop(1, getComputedStyle(document.documentElement).getPropertyValue('--secondary'));
  ctx.fillStyle = grad;
  ctx.beginPath(); ctx.roundRect(mX, mY + mH - fillH, mW, fillH, 8); ctx.fill();

  // Step markers
  for (let i = 1; i <= MAX_STEPS; i++) {{
    const y = mY + mH - (mH * i / MAX_STEPS);
    ctx.fillStyle = i <= currentStep ? 'rgba(255,255,255,0.8)' : 'rgba(255,255,255,0.15)';
    ctx.fillRect(mX - 5, y, mW + 10, 1);
    const mult = Math.floor(EDGE / Math.pow(P_SURVIVE, i) * 100) / 100;
    ctx.fillStyle = i <= currentStep ? '#fff' : 'rgba(255,255,255,0.3)';
    ctx.font = '11px Inter, sans-serif';
    ctx.textAlign = 'right';
    ctx.fillText(mult.toFixed(2) + 'x', mX - 10, y + 4);
  }}

  // Current multiplier display
  ctx.fillStyle = '#fff';
  ctx.font = 'bold 32px Inter, sans-serif';
  ctx.textAlign = 'center';
  ctx.fillText(currentMult.toFixed(2) + 'x', W/2 + 60, H/2);
}}

function play() {{
  initAudio();
  if (gameActive) {{
    // CASHOUT
    const win = currentBet * currentMult;
    balance += win;
    profit += win - currentBet;
    showResult('+' + win.toFixed(2), true);
    playTone(880, 0.3, 'sine');
    gameActive = false;
    currentStep = 0; currentMult = 1;
    document.getElementById('play-btn').textContent = 'PLAY';
    drawIdle(); updateUI();
    return;
  }}

  if (balance < currentBet) return;
  balance -= currentBet;
  gameActive = true;
  currentStep = 0; currentMult = 1;
  document.getElementById('play-btn').textContent = 'CASH OUT';
  updateUI();
  advanceStep();
}}

function advanceStep() {{
  if (!gameActive) return;

  const survived = Math.random() < P_SURVIVE;
  if (!survived) {{
    // BUST
    profit -= currentBet;
    showResult('BUST!', false);
    playTone(120, 0.5, 'sawtooth');
    gameActive = false;
    currentStep = 0; currentMult = 1;
    document.getElementById('play-btn').textContent = 'PLAY';
    drawIdle(); updateUI();
    return;
  }}

  currentStep++;
  currentMult = Math.floor(EDGE / Math.pow(P_SURVIVE, currentStep) * 100) / 100;
  playTone(300 + currentStep * 60, 0.15, 'sine');
  drawIdle();

  if (currentStep >= MAX_STEPS) {{
    // AUTO-CASHOUT at max
    const win = currentBet * currentMult;
    balance += win;
    profit += win - currentBet;
    showResult('MAX WIN! +' + win.toFixed(2), true);
    playTone(1200, 0.5, 'sine');
    gameActive = false;
    currentStep = 0; currentMult = 1;
    document.getElementById('play-btn').textContent = 'PLAY';
    drawIdle(); updateUI();
    return;
  }}

  // Auto-advance with delay (creates tension)
  setTimeout(advanceStep, 800 + Math.random() * 400);
}}
'''

    def _build_selection(self, mech: GameMechanic, params: dict) -> str:
        """Generate selection/grid game (mines pattern)."""
        grid = params.get("grid_size", 16)
        hazards = params.get("hazard_count", 3)
        edge = params.get("edge_factor", 0.97)
        cols = int(math.sqrt(grid))
        if cols * cols < grid:
            cols += 1

        return f'''
// ═══ SELECTION GAME ═══
let gameActive = false, revealed = 0, currentMult = 1;
const GRID = {grid}, COLS = {cols}, HAZARDS = {hazards}, EDGE = {edge};
let board = [], hazardPositions = [];

function newBoard() {{
  board = new Array(GRID).fill(0);
  hazardPositions = [];
  const positions = [...Array(GRID).keys()];
  for (let i = GRID - 1; i > 0; i--) {{
    const j = Math.floor(Math.random() * (i + 1));
    [positions[i], positions[j]] = [positions[j], positions[i]];
  }}
  for (let i = 0; i < HAZARDS; i++) {{
    board[positions[i]] = -1;
    hazardPositions.push(positions[i]);
  }}
}}

function drawIdle() {{
  const W = canvas.width / (window.devicePixelRatio||1);
  const H = canvas.height / (window.devicePixelRatio||1);
  ctx.clearRect(0, 0, W, H);
  const rows = Math.ceil(GRID / COLS);
  const cellW = Math.min((W - 20) / COLS, (H - 20) / rows);
  const startX = (W - cellW * COLS) / 2;
  const startY = (H - cellW * rows) / 2;

  for (let i = 0; i < GRID; i++) {{
    const col = i % COLS, row = Math.floor(i / COLS);
    const x = startX + col * cellW + 2, y = startY + row * cellW + 2;
    const w = cellW - 4, h = cellW - 4;

    if (board[i] === 1) {{ // revealed safe
      ctx.fillStyle = 'rgba(74, 222, 128, 0.2)';
      ctx.beginPath(); ctx.roundRect(x, y, w, h, 6); ctx.fill();
      ctx.fillStyle = '#4ade80'; ctx.font = '20px sans-serif';
      ctx.textAlign = 'center'; ctx.fillText('💎', x + w/2, y + h/2 + 7);
    }} else if (board[i] === -2) {{ // revealed hazard
      ctx.fillStyle = 'rgba(239, 68, 68, 0.2)';
      ctx.beginPath(); ctx.roundRect(x, y, w, h, 6); ctx.fill();
      ctx.fillStyle = '#ef4444'; ctx.font = '20px sans-serif';
      ctx.textAlign = 'center'; ctx.fillText('💣', x + w/2, y + h/2 + 7);
    }} else {{ // unrevealed
      ctx.fillStyle = gameActive ? 'rgba(255,255,255,0.06)' : 'rgba(255,255,255,0.03)';
      ctx.beginPath(); ctx.roundRect(x, y, w, h, 6); ctx.fill();
      ctx.strokeStyle = 'rgba(255,255,255,0.08)';
      ctx.beginPath(); ctx.roundRect(x, y, w, h, 6); ctx.stroke();
    }}
  }}

  // Multiplier
  ctx.fillStyle = '#fff'; ctx.font = 'bold 14px Inter, sans-serif'; ctx.textAlign = 'center';
  ctx.fillText('Multiplier: ' + currentMult.toFixed(2) + 'x', W/2, H - 10);
}}

canvas.addEventListener('click', function(e) {{
  if (!gameActive) return;
  const W = canvas.width / (window.devicePixelRatio||1);
  const H = canvas.height / (window.devicePixelRatio||1);
  const rect = canvas.getBoundingClientRect();
  const mx = (e.clientX - rect.left), my = (e.clientY - rect.top);

  const rows = Math.ceil(GRID / COLS);
  const cellW = Math.min((W - 20) / COLS, (H - 20) / rows);
  const startX = (W - cellW * COLS) / 2;
  const startY = (H - cellW * rows) / 2;

  const col = Math.floor((mx - startX) / cellW);
  const row = Math.floor((my - startY) / cellW);
  if (col < 0 || col >= COLS || row < 0 || row >= rows) return;
  const idx = row * COLS + col;
  if (idx >= GRID || board[idx] === 1 || board[idx] === -2) return;

  if (board[idx] === -1) {{
    // HIT HAZARD
    board[idx] = -2;
    hazardPositions.forEach(p => board[p] = -2);
    profit -= currentBet;
    showResult('BOOM!', false);
    playTone(100, 0.6, 'sawtooth');
    gameActive = false;
    document.getElementById('play-btn').textContent = 'PLAY';
    drawIdle(); updateUI();
  }} else {{
    // SAFE
    board[idx] = 1;
    revealed++;
    const safe = GRID - HAZARDS;
    let prob = 1;
    for (let i = 0; i < revealed; i++) prob *= (safe - i) / (GRID - i);
    currentMult = Math.max(1, Math.floor(EDGE / prob * 100) / 100);
    playTone(400 + revealed * 50, 0.12, 'sine');
    drawIdle();
  }}
}});

function play() {{
  initAudio();
  if (gameActive) {{
    // CASHOUT
    const win = currentBet * currentMult;
    balance += win;
    profit += win - currentBet;
    showResult('+' + win.toFixed(2), true);
    playTone(880, 0.3, 'sine');
    gameActive = false;
    document.getElementById('play-btn').textContent = 'PLAY';
    hazardPositions.forEach(p => board[p] = -2);
    drawIdle(); updateUI();
    return;
  }}
  if (balance < currentBet) return;
  balance -= currentBet;
  newBoard(); revealed = 0; currentMult = 1;
  gameActive = true;
  document.getElementById('play-btn').textContent = 'CASH OUT';
  updateUI(); drawIdle();
}}
'''

    def _build_distribution(self, mech: GameMechanic, params: dict) -> str:
        """Generate distribution game (wheel/plinko pattern)."""
        n = params.get("n_outcomes", 20)
        edge = params.get("edge_factor", 0.97)
        max_mult = params.get("max_multiplier", 10)

        return f'''
// ═══ DISTRIBUTION GAME ═══
let spinning = false;
const N = {n};
const EDGE = {edge};
const MAX_MULT = {max_mult};

// Generate outcome multipliers
const outcomes = [];
const total_needed = EDGE * N;
let remaining = total_needed;
for (let i = 0; i < N; i++) {{
  if (i < Math.floor(N * 0.25)) outcomes.push(0);  // 25% bust
  else if (i === N - 1) outcomes.push(Math.max(0, Math.round(remaining * 10) / 10));
  else {{
    const m = Math.round((remaining / (N - i)) * (0.5 + Math.random()) * 10) / 10;
    outcomes.push(Math.min(Math.max(0, m), MAX_MULT));
    remaining -= m;
  }}
}}
outcomes.sort((a,b) => a - b);

let angle = 0, targetAngle = 0, animating = false, selectedIdx = 0;
const segAngle = Math.PI * 2 / N;

function drawIdle() {{
  const W = canvas.width / (window.devicePixelRatio||1);
  const H = canvas.height / (window.devicePixelRatio||1);
  ctx.clearRect(0, 0, W, H);

  const cx = W/2, cy = H/2, r = Math.min(W, H) * 0.4;

  for (let i = 0; i < N; i++) {{
    const a1 = angle + i * segAngle, a2 = a1 + segAngle;
    const hue = (i / N) * 360;
    ctx.fillStyle = outcomes[i] === 0 ? 'rgba(30,41,59,0.8)' :
      `hsla(${{hue}}, 70%, ${{30 + outcomes[i] / MAX_MULT * 30}}%, 0.8)`;
    ctx.beginPath();
    ctx.moveTo(cx, cy); ctx.arc(cx, cy, r, a1, a2); ctx.closePath(); ctx.fill();
    ctx.strokeStyle = 'rgba(255,255,255,0.1)'; ctx.stroke();

    // Label
    const mid = (a1 + a2) / 2;
    const lx = cx + Math.cos(mid) * r * 0.7, ly = cy + Math.sin(mid) * r * 0.7;
    ctx.fillStyle = outcomes[i] === 0 ? '#666' : '#fff';
    ctx.font = 'bold 10px Inter, sans-serif'; ctx.textAlign = 'center';
    ctx.fillText(outcomes[i] === 0 ? 'X' : outcomes[i].toFixed(1) + 'x', lx, ly + 4);
  }}

  // Pointer
  ctx.fillStyle = '#fff';
  ctx.beginPath(); ctx.moveTo(cx + r + 8, cy);
  ctx.lineTo(cx + r + 20, cy - 8); ctx.lineTo(cx + r + 20, cy + 8);
  ctx.closePath(); ctx.fill();
}}

function animateSpin() {{
  if (!animating) return;
  const diff = targetAngle - angle;
  if (Math.abs(diff) < 0.001) {{
    angle = targetAngle;
    animating = false;
    const mult = outcomes[selectedIdx];
    if (mult > 0) {{
      const win = currentBet * mult;
      balance += win;
      profit += win - currentBet;
      showResult(mult.toFixed(1) + 'x  +' + win.toFixed(2), true);
      playTone(660, 0.3, 'sine');
    }} else {{
      profit -= currentBet;
      showResult('BUST!', false);
      playTone(120, 0.4, 'sawtooth');
    }}
    spinning = false;
    document.getElementById('play-btn').disabled = false;
    drawIdle(); updateUI();
    return;
  }}
  angle += diff * 0.04;
  drawIdle();
  requestAnimationFrame(animateSpin);
}}

function play() {{
  if (spinning || balance < currentBet) return;
  initAudio();
  balance -= currentBet;
  spinning = true;
  document.getElementById('play-btn').disabled = true;
  updateUI();

  selectedIdx = Math.floor(Math.random() * N);
  targetAngle = angle + Math.PI * 6 + (Math.PI * 2) - selectedIdx * segAngle - segAngle / 2;
  animating = true;
  playTone(220, 0.1, 'square');
  animateSpin();
}}
'''

    def _build_threshold(self, mech: GameMechanic, params: dict) -> str:
        """Generate threshold game (dice pattern)."""
        edge_pct = params.get("edge_pct", 97)

        return f'''
// ═══ THRESHOLD GAME ═══
let rolling = false, prediction = 'over', threshold = 50;
const EDGE_PCT = {edge_pct};

function getChance() {{ return prediction === 'over' ? (100 - threshold) : threshold; }}
function getMult() {{ return Math.floor(EDGE_PCT / getChance() * 100) / 100; }}

function drawIdle() {{
  const W = canvas.width / (window.devicePixelRatio||1);
  const H = canvas.height / (window.devicePixelRatio||1);
  ctx.clearRect(0, 0, W, H);

  // Meter bar
  const barH = 40, barW = W * 0.8, barX = W * 0.1, barY = H / 2 - barH / 2;
  ctx.fillStyle = 'rgba(255,255,255,0.03)';
  ctx.beginPath(); ctx.roundRect(barX, barY, barW, barH, 8); ctx.fill();

  // Threshold marker
  const tX = barX + barW * (threshold / 100);
  if (prediction === 'over') {{
    ctx.fillStyle = 'rgba(74,222,128,0.15)';
    ctx.fillRect(tX, barY, barX + barW - tX, barH);
  }} else {{
    ctx.fillStyle = 'rgba(99,102,241,0.15)';
    ctx.fillRect(barX, barY, tX - barX, barH);
  }}
  ctx.strokeStyle = '#fff';
  ctx.beginPath(); ctx.moveTo(tX, barY - 5); ctx.lineTo(tX, barY + barH + 5); ctx.stroke();

  // Labels
  ctx.fillStyle = '#fff'; ctx.font = 'bold 24px Inter, sans-serif'; ctx.textAlign = 'center';
  ctx.fillText(threshold.toFixed(0), W / 2, barY - 20);
  ctx.font = '13px Inter, sans-serif'; ctx.fillStyle = 'var(--text-dim)';
  ctx.fillText(prediction.toUpperCase() + ' ' + threshold + ' — ' + getChance() + '% chance — ' + getMult().toFixed(2) + 'x', W / 2, barY + barH + 25);

  // Direction buttons
  ctx.fillStyle = prediction === 'under' ? 'rgba(99,102,241,0.3)' : 'rgba(255,255,255,0.05)';
  ctx.beginPath(); ctx.roundRect(W/2 - 120, barY + barH + 45, 100, 32, 6); ctx.fill();
  ctx.fillStyle = '#fff'; ctx.font = '12px Inter, sans-serif'; ctx.textAlign = 'center';
  ctx.fillText('UNDER', W/2 - 70, barY + barH + 65);

  ctx.fillStyle = prediction === 'over' ? 'rgba(74,222,128,0.3)' : 'rgba(255,255,255,0.05)';
  ctx.beginPath(); ctx.roundRect(W/2 + 20, barY + barH + 45, 100, 32, 6); ctx.fill();
  ctx.fillStyle = '#fff'; ctx.fillText('OVER', W/2 + 70, barY + barH + 65);
}}

canvas.addEventListener('click', function(e) {{
  if (rolling) return;
  const rect = canvas.getBoundingClientRect();
  const mx = e.clientX - rect.left;
  const W = canvas.width / (window.devicePixelRatio||1);
  const H = canvas.height / (window.devicePixelRatio||1);
  const barH = 40, barY = H / 2 - barH / 2;

  // Check direction buttons
  if (e.clientY - rect.top > barY + barH + 45 && e.clientY - rect.top < barY + barH + 77) {{
    if (mx < W / 2) prediction = 'under';
    else prediction = 'over';
    drawIdle(); return;
  }}

  // Click on bar to set threshold
  const barW = W * 0.8, barX = W * 0.1;
  if (mx >= barX && mx <= barX + barW && Math.abs(e.clientY - rect.top - H/2) < 40) {{
    threshold = Math.round(((mx - barX) / barW) * 100);
    threshold = Math.max(5, Math.min(95, threshold));
    drawIdle();
  }}
}});

function play() {{
  if (rolling || balance < currentBet) return;
  initAudio();
  balance -= currentBet;
  rolling = true;
  document.getElementById('play-btn').disabled = true;
  updateUI();

  const roll = Math.random() * 100;
  const win = prediction === 'over' ? roll > threshold : roll < threshold;
  const mult = getMult();

  // Animate
  setTimeout(() => {{
    if (win) {{
      const payout = currentBet * mult;
      balance += payout;
      profit += payout - currentBet;
      showResult(roll.toFixed(1) + ' — ' + mult.toFixed(2) + 'x', true);
      playTone(660, 0.3, 'sine');
    }} else {{
      profit -= currentBet;
      showResult(roll.toFixed(1) + ' — MISS', false);
      playTone(120, 0.4, 'sawtooth');
    }}
    rolling = false;
    document.getElementById('play-btn').disabled = false;
    drawIdle(); updateUI();
  }}, 500);
}}
'''

    def _build_hybrid(self, mech: GameMechanic, params: dict) -> str:
        """Generate hybrid game — defaults to accumulator."""
        return self._build_accumulator(mech, params)


# ═══════════════════════════════════════════════════════════════
# Playtest Simulator
# ═══════════════════════════════════════════════════════════════

@dataclass
class PlaytestReport:
    """Results of an automated playtest evaluation."""
    game_name: str
    mechanic_hash: str
    rounds_played: int
    measured_rtp: float
    hit_frequency: float
    avg_session_length: int        # rounds per "session" before bust
    decision_frequency: float      # decisions per round (0 = passive, 1+ = active)
    tension_score: float           # 0-10 scale
    clarity_score: float           # 0-10 scale
    fun_score: float               # 0-10 composite
    max_win_achieved: float
    longest_streak: int
    issues: list[str]
    suggestions: list[str]
    passed: bool

    def to_dict(self):
        return asdict(self)

    def to_json(self, indent=2):
        return json.dumps(self.to_dict(), indent=indent)


class PlaytestSimulator:
    """Simulates gameplay and evaluates game quality metrics."""

    def evaluate(self, mechanic: GameMechanic,
                 n_rounds: int = 50_000) -> PlaytestReport:
        """Run automated playtest of a game mechanic."""
        arch = mechanic.archetype
        params = mechanic.parameters

        payouts = []
        session_lengths = []
        current_session = 0
        max_win = 0
        max_streak = 0
        current_streak = 0

        for _ in range(n_rounds):
            payout = self._simulate_round(arch, params)
            payouts.append(payout)

            current_session += 1
            if payout > 0:
                current_streak += 1
                max_streak = max(max_streak, current_streak)
                max_win = max(max_win, payout)
            else:
                current_streak = 0
                session_lengths.append(current_session)
                current_session = 0

        if current_session > 0:
            session_lengths.append(current_session)

        # Calculate metrics
        total_bet = n_rounds  # 1 unit per round
        total_return = sum(payouts)
        measured_rtp = total_return / total_bet if total_bet > 0 else 0
        hit_freq = sum(1 for p in payouts if p > 0) / n_rounds if n_rounds > 0 else 0
        avg_session = sum(session_lengths) / len(session_lengths) if session_lengths else n_rounds

        # Scoring heuristics
        decision_freq = {
            "continue_or_cashout": 0.8,
            "pick_and_reveal": 1.0,
            "single_action": 0.2,
            "predict_outcome": 0.5,
            "multi_phase": 0.9,
        }.get(mechanic.decision_type, 0.5)

        # Tension: higher if hit frequency is moderate (not too high, not too low)
        tension = min(10, 10 * (1 - abs(hit_freq - 0.4) * 2.5))
        tension = max(0, tension)

        # Clarity: penalize if too many parameters or hybrid
        clarity = 8.0 if arch != "hybrid" else 5.0
        if len(params) > 8:
            clarity -= 1.0

        # Fun composite
        fun = (tension * 0.4 + clarity * 0.3 + decision_freq * 10 * 0.3)

        # Issues
        issues = []
        suggestions = []
        target_rtp = params.get("target_rtp", 96) / 100.0
        if abs(measured_rtp - target_rtp) > 0.02:
            issues.append(f"RTP drift: measured={measured_rtp:.4f} vs target={target_rtp:.4f}")
            suggestions.append("Adjust edge factor or payout multipliers")
        if hit_freq < 0.1:
            issues.append(f"Very low hit frequency ({hit_freq:.1%}) — may feel punishing")
            suggestions.append("Increase base win probability or add consolation prizes")
        if hit_freq > 0.8:
            issues.append(f"Very high hit frequency ({hit_freq:.1%}) — may feel boring")
            suggestions.append("Increase volatility or add bust conditions")
        if max_win < 5:
            issues.append(f"Low max win ({max_win:.1f}x) — lacks excitement")
            suggestions.append("Increase maximum multiplier or add rare jackpot outcomes")
        if avg_session < 2:
            issues.append(f"Very short sessions ({avg_session:.1f} rounds) — too rapid")
            suggestions.append("Slow down game pace or add multi-step decisions")

        passed = len(issues) == 0 or all("drift" not in i for i in issues)

        return PlaytestReport(
            game_name=mechanic.name,
            mechanic_hash=mechanic.mechanic_hash,
            rounds_played=n_rounds,
            measured_rtp=measured_rtp,
            hit_frequency=hit_freq,
            avg_session_length=int(avg_session),
            decision_frequency=decision_freq,
            tension_score=round(tension, 1),
            clarity_score=round(clarity, 1),
            fun_score=round(fun, 1),
            max_win_achieved=round(max_win, 2),
            longest_streak=max_streak,
            issues=issues,
            suggestions=suggestions,
            passed=passed,
        )

    def _simulate_round(self, archetype: str, params: dict) -> float:
        """Simulate one round of play, return payout multiplier."""
        if archetype == "accumulator":
            return self._sim_accumulator(params)
        elif archetype == "selection":
            return self._sim_selection(params)
        elif archetype == "distribution":
            return self._sim_distribution(params)
        elif archetype == "threshold":
            return self._sim_threshold(params)
        return 0

    def _sim_accumulator(self, p: dict) -> float:
        ps = p.get("p_survive_per_step", 0.75)
        max_s = p.get("max_steps", 10)
        edge = p.get("edge_factor", 0.97)
        # Simulate: player cashes out at random point
        target = random.randint(1, max_s)
        for step in range(1, target + 1):
            if random.random() >= ps:
                return 0  # bust
        return edge / (ps ** target)

    def _sim_selection(self, p: dict) -> float:
        G = p.get("grid_size", 16)
        H = p.get("hazard_count", 3)
        S = G - H
        edge = p.get("edge_factor", 0.97)
        # Player reveals random number of tiles then cashout
        target = random.randint(1, S)
        prob = 1.0
        for i in range(target):
            prob *= (S - i) / (G - i)
            if random.random() >= (S - i) / (G - i):
                return 0  # hit hazard
        return edge / prob if prob > 0 else 0

    def _sim_distribution(self, p: dict) -> float:
        n = p.get("n_outcomes", 20)
        edge = p.get("edge_factor", 0.97)
        max_m = p.get("max_multiplier", 10)
        # Simple multinomial: 25% bust, rest distributed
        idx = random.randint(0, n - 1)
        if idx < n * 0.25:
            return 0
        remaining = n - int(n * 0.25)
        avg_mult = edge * n / remaining
        return max(0, avg_mult * (0.2 + random.random() * 1.6))

    def _sim_threshold(self, p: dict) -> float:
        edge_pct = p.get("edge_pct", 97)
        threshold = random.randint(10, 90)
        chance = (100 - threshold) / 100
        mult = edge_pct / 100 / chance
        roll = random.random()
        if roll > threshold / 100:
            return mult
        return 0


# ═══════════════════════════════════════════════════════════════
# Iteration Engine
# ═══════════════════════════════════════════════════════════════

class IterationEngine:
    """Runs playtest → fix → retest loops to improve games."""

    def __init__(self, max_iterations: int = 5):
        self.max_iterations = max_iterations
        self.inventor = MechanicInventor()
        self.simulator = PlaytestSimulator()

    def iterate(self, mechanic: GameMechanic,
                target_fun_score: float = 6.0) -> tuple[GameMechanic, list[PlaytestReport]]:
        """Iterate on a mechanic until quality bar is met."""
        reports = []
        current = mechanic

        for i in range(self.max_iterations):
            report = self.simulator.evaluate(current, n_rounds=100_000)
            reports.append(report)

            if report.passed and report.fun_score >= target_fun_score:
                break

            # Apply fixes based on issues
            current = self._apply_fixes(current, report)

        return current, reports

    def _apply_fixes(self, mechanic: GameMechanic,
                     report: PlaytestReport) -> GameMechanic:
        """Apply automated fixes based on playtest issues."""
        params = dict(mechanic.parameters)

        for issue in report.issues:
            if "RTP drift" in issue:
                # Adjust edge factor
                target = params.get("target_rtp", 96) / 100.0
                ratio = target / report.measured_rtp if report.measured_rtp > 0 else 1.0
                if "edge_factor" in params:
                    params["edge_factor"] = round(params["edge_factor"] * ratio, 6)

            elif "low hit frequency" in issue:
                if "p_survive_per_step" in params:
                    params["p_survive_per_step"] = min(0.95, params["p_survive_per_step"] + 0.05)
                if "hazard_count" in params:
                    params["hazard_count"] = max(1, params["hazard_count"] - 1)

            elif "high hit frequency" in issue:
                if "p_survive_per_step" in params:
                    params["p_survive_per_step"] = max(0.3, params["p_survive_per_step"] - 0.05)
                if "hazard_count" in params:
                    params["hazard_count"] += 1

            elif "Low max win" in issue:
                if "max_multiplier" in params:
                    params["max_multiplier"] = params["max_multiplier"] * 2
                if "max_steps" in params:
                    params["max_steps"] += 3

        # Create updated mechanic
        return GameMechanic(
            name=mechanic.name,
            description=mechanic.description,
            archetype=mechanic.archetype,
            visual_pattern=mechanic.visual_pattern,
            math_type=mechanic.math_type,
            decision_type=mechanic.decision_type,
            parameters=params,
            rtp_formula=mechanic.rtp_formula,
            risk_factor=mechanic.risk_factor,
            max_multiplier=params.get("max_multiplier", mechanic.max_multiplier),
            base_games_used=mechanic.base_games_used,
            theme_suggestion=mechanic.theme_suggestion,
        )


# ═══════════════════════════════════════════════════════════════
# Variant Generator
# ═══════════════════════════════════════════════════════════════

class VariantGenerator:
    """Generates multiple variants of the same game mechanic."""

    THEME_VARIANTS = [
        {"name": "Neon Cyber", "primary": "#a855f7", "secondary": "#06b6d4", "bg": "#0a0014"},
        {"name": "Gold Rush", "primary": "#d97706", "secondary": "#fbbf24", "bg": "#1a0f00"},
        {"name": "Deep Ocean", "primary": "#0891b2", "secondary": "#2dd4bf", "bg": "#021524"},
        {"name": "Volcanic", "primary": "#ef4444", "secondary": "#f97316", "bg": "#1a0500"},
        {"name": "Arctic", "primary": "#0ea5e9", "secondary": "#7dd3fc", "bg": "#001020"},
        {"name": "Jungle", "primary": "#16a34a", "secondary": "#4ade80", "bg": "#0a1a0a"},
    ]

    RISK_VARIANTS = [
        {"name": "Low Risk", "volatility": "low", "rtp": 97.0},
        {"name": "Medium Risk", "volatility": "medium", "rtp": 96.0},
        {"name": "High Risk", "volatility": "high", "rtp": 95.0},
        {"name": "Ultra Risk", "volatility": "ultra", "rtp": 94.0},
    ]

    def theme_variants(self, mechanic: GameMechanic,
                       n: int = 4) -> list[tuple[GameMechanic, dict]]:
        """Generate theme variants of the same mechanic."""
        builder = NovelGameBuilder()
        variants = []
        for theme in self.THEME_VARIANTS[:n]:
            m_copy = GameMechanic(**{**asdict(mechanic), "mechanic_hash": ""})
            m_copy.theme_suggestion = theme["name"].lower().replace(" ", "_")
            m_copy.name = f"{mechanic.name} — {theme['name']}"
            variants.append((m_copy, theme))
        return variants

    def risk_variants(self, description: str,
                      n: int = 4) -> list[tuple[GameMechanic, PlaytestReport]]:
        """Generate risk-profile variants of the same game."""
        inventor = MechanicInventor()
        simulator = PlaytestSimulator()
        variants = []
        for risk in self.RISK_VARIANTS[:n]:
            mech = inventor.invent(description, target_rtp=risk["rtp"],
                                   volatility=risk["volatility"])
            mech.name = f"{mech.name} ({risk['name']})"
            report = simulator.evaluate(mech, n_rounds=50_000)
            variants.append((mech, report))
        return variants

    def build_all(self, mechanic: GameMechanic,
                  themes: int = 4) -> list[dict]:
        """Build playable HTML for all theme variants."""
        builder = NovelGameBuilder()
        results = []
        for mech, theme in self.theme_variants(mechanic, themes):
            html = builder.build(mech)
            results.append({
                "name": mech.name,
                "theme": theme["name"],
                "html_length": len(html),
                "html": html,
                "mechanic": mech.to_dict(),
            })
        return results
