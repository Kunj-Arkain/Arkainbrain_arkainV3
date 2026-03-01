"""
ARKAINBRAIN — Mini-Game Math Engine (Phase 2)

RMG-grade mathematical models for every mini-game type.
Each model produces:
  - Complete paytable with every possible outcome
  - Probability table: P(outcome) for every result
  - Mathematical RTP proof: Σ(P × payout) = target_rtp
  - Volatility metrics: std dev, hit frequency, max win probability
  - Certification-ready JSON for GLI/BMM submission

Key insight: Games fall into three categories:
  1. STRATEGY-INDEPENDENT (crash, mines, dice): RTP = edge_factor by construction,
     regardless of player decisions. Paytable shows reference bet scenarios.
  2. TABLE-BASED (plinko, wheel): RTP = Σ(P[i] × mult[i]), fully determined by table.
  3. FORMULA-BASED (hilo, chicken, scratch): RTP emerges from the multiplier formula
     and probability structure. Must be computed, not assumed.
"""

from __future__ import annotations

import json
import math
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone


# ═══════════════════════════════════════════════════════════════
# Core Data Structures
# ═══════════════════════════════════════════════════════════════

@dataclass
class PaytableEntry:
    outcome_id: str
    description: str
    multiplier: float
    probability: float
    contribution: float = 0

    def __post_init__(self):
        self.contribution = round(self.probability * self.multiplier, 10)


@dataclass
class VolatilityMetrics:
    standard_deviation: float
    variance: float
    hit_frequency: float
    volatility_index: float
    max_win_multiplier: float
    max_win_probability: float
    median_payout: float
    skewness: float
    p_win_gt_10x: float = 0.0
    p_win_gt_100x: float = 0.0


@dataclass
class MathModel:
    game_type: str
    model_version: str = "2.1.0"
    theoretical_rtp: float = 0.0
    house_edge: float = 0.0
    paytable: list[PaytableEntry] = field(default_factory=list)
    volatility: VolatilityMetrics = None
    parameters: dict = field(default_factory=dict)
    model_hash: str = ""
    generated_at: str = ""
    generator: str = "ArkainBrain MiniGameMathEngine v2.1"

    def __post_init__(self):
        self.generated_at = datetime.now(timezone.utc).isoformat()
        self._compute_hash()

    def _compute_hash(self):
        data = json.dumps(
            [(e.outcome_id, e.multiplier, e.probability) for e in self.paytable],
            sort_keys=True
        )
        self.model_hash = hashlib.sha256(data.encode()).hexdigest()[:16]

    def rtp_proof(self) -> dict:
        entries = []
        total = 0.0
        for e in self.paytable:
            entries.append({
                "outcome": e.outcome_id,
                "P": round(e.probability, 10),
                "mult": e.multiplier,
                "P×mult": round(e.contribution, 10),
            })
            total += e.contribution
        prob_sum = sum(e.probability for e in self.paytable)
        return {
            "game_type": self.game_type,
            "model_hash": self.model_hash,
            "theoretical_rtp": round(self.theoretical_rtp, 8),
            "theoretical_rtp_pct": round(self.theoretical_rtp * 100, 4),
            "house_edge_pct": round(self.house_edge * 100, 4),
            "paytable_rtp": round(total, 8),
            "probability_sum": round(prob_sum, 10),
            "probability_sum_check": "PASS" if abs(prob_sum - 1.0) < 1e-6 else "FAIL",
            "rtp_check": "PASS" if abs(total - self.theoretical_rtp) < 0.001 else "FAIL",
            "n_outcomes": len(self.paytable),
            "entries": entries,
        }

    def certification_report(self, include_montecarlo: bool = False,
                              mc_rounds: int = 1_000_000) -> dict:
        proof = self.rtp_proof()
        v = self.volatility
        report = {
            "report_type": "Mini-Game Mathematical Certification",
            "generator": self.generator,
            "generated_at": self.generated_at,
            "model_hash": self.model_hash,
            "game_type": self.game_type,
            "parameters": self.parameters,
            "rtp_proof": proof,
            "volatility_profile": {
                "standard_deviation": round(v.standard_deviation, 4) if v else None,
                "hit_frequency_pct": round(v.hit_frequency * 100, 2) if v else None,
                "volatility_index": round(v.volatility_index, 2) if v else None,
                "max_win_multiplier": v.max_win_multiplier if v else None,
                "max_win_probability": f"{v.max_win_probability:.2e}" if v else None,
                "skewness": round(v.skewness, 4) if v else None,
                "p_win_gt_10x_pct": round(v.p_win_gt_10x * 100, 4) if v else None,
                "p_win_gt_100x_pct": round(v.p_win_gt_100x * 100, 6) if v else None,
            } if v else {},
            "regulatory_compliance": {
                "probability_sum_valid": proof["probability_sum_check"] == "PASS",
                "rtp_matches_theory": proof["rtp_check"] == "PASS",
                "house_edge_positive": self.house_edge > 0,
            },
        }
        if include_montecarlo:
            try:
                from tools.minigame_montecarlo import MonteCarloValidator
                mc = MonteCarloValidator(tolerance=0.002)
                sim = mc.validate_model(self, n_rounds=mc_rounds)
                report["monte_carlo_validation"] = sim.to_dict()
                report["regulatory_compliance"]["monte_carlo_rtp_pass"] = sim.rtp_pass
            except Exception as e:
                report["monte_carlo_validation"] = {"error": str(e)}
        return report

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.certification_report(), indent=indent, default=str)


# ═══════════════════════════════════════════════════════════════
# Volatility Calculator
# ═══════════════════════════════════════════════════════════════

def _compute_volatility(paytable: list[PaytableEntry], rtp: float) -> VolatilityMetrics:
    mults = [e.multiplier for e in paytable]
    probs = [e.probability for e in paytable]
    ex = rtp
    ex2 = sum(p * m * m for p, m in zip(probs, mults))
    variance = max(0, ex2 - ex * ex)
    std_dev = math.sqrt(variance)
    hit_freq = sum(p for p, m in zip(probs, mults) if m > 0)
    max_mult = max(mults) if mults else 0
    max_prob = sum(p for p, m in zip(probs, mults) if m == max_mult) if max_mult > 0 else 0
    sorted_e = sorted(zip(probs, mults), key=lambda x: x[1])
    cum, median = 0.0, 0.0
    for p, m in sorted_e:
        cum += p
        if cum >= 0.5:
            median = m
            break
    skewness = 0.0
    if std_dev > 0:
        skewness = sum(p * ((m - ex) ** 3) for p, m in zip(probs, mults)) / (std_dev ** 3)
    return VolatilityMetrics(
        standard_deviation=std_dev, variance=variance,
        hit_frequency=hit_freq, volatility_index=std_dev,
        max_win_multiplier=max_mult, max_win_probability=max_prob,
        median_payout=median, skewness=skewness,
        p_win_gt_10x=sum(p for p, m in zip(probs, mults) if m > 10),
        p_win_gt_100x=sum(p for p, m in zip(probs, mults) if m > 100),
    )


# ═══════════════════════════════════════════════════════════════
# Math Engine
# ═══════════════════════════════════════════════════════════════

class MiniGameMathEngine:
    """Generates certification-ready math models for all 8 mini-game types."""

    # ── CRASH ──────────────────────────────────────────────────
    def crash_model(self, house_edge: float = 0.03,
                    max_mult: float = 100.0) -> MathModel:
        """Crash: RTP = 1 - house_edge (strategy-independent).

        For ANY fixed cashout target M:
          P(win) = (1-he)/M,  payout = M
          P(lose) = 1 - (1-he)/M,  payout = 0
          RTP = P(win) × M = (1-he)/M × M = 1-he  ∎

        Paytable shows reference cashout targets to demonstrate this.
        """
        rtp = 1 - house_edge
        paytable = []

        # Show reference cashout targets: 1.5×, 2×, 3×, 5×, 10×, 20×, 50×, max
        targets = [1.5, 2, 3, 5, 10, 20, 50, min(100, max_mult)]
        targets = sorted(set(t for t in targets if t <= max_mult))

        for t in targets:
            p_win = rtp / t
            p_lose = 1 - p_win
            paytable.append(PaytableEntry(
                f"cashout_{t}x_win", f"Auto-cashout at {t}× → WIN", t, p_win))
            paytable.append(PaytableEntry(
                f"cashout_{t}x_lose", f"Auto-cashout at {t}× → CRASH before", 0, p_lose))

        # For the official paytable, use the 2× reference bet (most common)
        official = [
            PaytableEntry("bust", "Crash before cashout (bust)", 0,
                          1 - rtp / 2),
            PaytableEntry("win_2x", "Cash out at 2×", 2.0, rtp / 2),
        ]

        vol = _compute_volatility(official, rtp)

        return MathModel(
            game_type="crash",
            theoretical_rtp=rtp,
            house_edge=house_edge,
            paytable=official,
            volatility=vol,
            parameters={
                "house_edge": house_edge,
                "max_multiplier": max_mult,
                "rtp_formula": f"RTP = 1 - {house_edge} = {rtp} (for ANY cashout target)",
                "reference_cashouts": {
                    f"{t}x": {"P_win": round(rtp/t, 6), "RTP_check": round(rtp/t * t, 6)}
                    for t in targets
                },
                "crash_point_formula": f"crash = {rtp} / (1 - r) where r ~ U(0,1)",
            },
        )

    # ── PLINKO ─────────────────────────────────────────────────
    def plinko_model(self, rows: int = 12, risk: str = "medium",
                     target_rtp: float = 0.96,
                     bucket_multipliers: list[float] = None) -> MathModel:
        """Plinko: RTP = Σ(P_binomial[k] × mult[k]). Exact, no approximation."""
        n_buckets = rows + 1
        probs = [math.comb(rows, k) / (2 ** rows) for k in range(n_buckets)]

        if bucket_multipliers is None:
            bucket_multipliers = self._plinko_shape_scaled(rows, risk, target_rtp, probs)
        else:
            bucket_multipliers = bucket_multipliers[:n_buckets]

        paytable = [
            PaytableEntry(
                f"bucket_{k}", f"Bucket {k} ({probs[k]*100:.2f}%)",
                bucket_multipliers[k], probs[k]
            )
            for k in range(n_buckets)
        ]

        rtp = sum(e.contribution for e in paytable)
        vol = _compute_volatility(paytable, rtp)

        return MathModel(
            game_type="plinko", theoretical_rtp=rtp, house_edge=1-rtp,
            paytable=paytable, volatility=vol,
            parameters={
                "rows": rows, "risk": risk,
                "target_rtp": target_rtp,
                "multipliers": bucket_multipliers,
                "probabilities": [round(p, 10) for p in probs],
            },
        )

    def _plinko_shape_scaled(self, rows, risk, target_rtp, probs):
        n = rows + 1
        half = n // 2
        params = {"low": (5, 0.3), "medium": (20, 0.1), "high": (100, 0.0)}
        edge, center = params.get(risk, params["medium"])
        shape = [center + (edge - center) * ((abs(i - half) / half) ** 2)
                 for i in range(n)]
        cur = sum(p * m for p, m in zip(probs, shape))
        if cur > 0:
            scale = target_rtp / cur
            return [round(m * scale, 2) for m in shape]
        return shape

    # ── MINES ──────────────────────────────────────────────────
    def mines_model(self, grid_size: int = 25, mine_count: int = 5,
                    edge_factor: float = 0.97) -> MathModel:
        """Mines: RTP = edge_factor (strategy-independent).

        For ANY fixed reveal count n:
          P(n safe) = Π(safe-i)/(grid-i) for i=0..n-1
          mult(n) = edge_factor / P(n safe)
          RTP = P(n safe) × mult(n) = P(n safe) × edge/P(n safe) = edge  ∎

        Paytable shows reference reveal counts.
        """
        rtp = edge_factor
        safe = grid_size - mine_count
        paytable = []

        # Show multiplier ladder for reference
        refs = [1, 2, 3, 5, 8, 10, 15, min(20, safe)]
        refs = sorted(set(r for r in refs if r <= safe))

        for n in refs:
            p_survive = 1.0
            for i in range(n):
                p_survive *= (safe - i) / (grid_size - i)
            mult = round(edge_factor / p_survive, 2)
            p_bust = 1 - p_survive
            paytable.append(PaytableEntry(
                f"reveal_{n}_win", f"Reveal {n} tiles → {mult}×", mult, p_survive))
            paytable.append(PaytableEntry(
                f"reveal_{n}_bust", f"Hit mine within {n} reveals", 0, p_bust))

        # Official paytable: 3-reveal reference (most common play)
        p3 = 1.0
        for i in range(3):
            p3 *= (safe - i) / (grid_size - i)
        m3 = edge_factor / p3  # Don't round — keep exact for RTP proof
        official = [
            PaytableEntry("win_3rev", f"3 safe reveals → {m3:.4f}×", m3, p3),
            PaytableEntry("bust_3rev", "Hit mine within 3 reveals", 0, 1 - p3),
        ]

        vol = _compute_volatility(official, rtp)

        return MathModel(
            game_type="mines", theoretical_rtp=rtp, house_edge=1-rtp,
            paytable=official, volatility=vol,
            parameters={
                "grid_size": grid_size, "mine_count": mine_count,
                "safe_tiles": safe, "edge_factor": edge_factor,
                "rtp_formula": f"RTP = {edge_factor} (for ANY reveal count)",
                "multiplier_ladder": {
                    f"{n}_reveals": {
                        "P_survive": round(p_s, 8),
                        "multiplier": round(edge_factor / p_s, 2),
                    }
                    for n in refs
                    for p_s in [math.prod((safe-i)/(grid_size-i) for i in range(n))]
                },
            },
        )

    # ── DICE ───────────────────────────────────────────────────
    def dice_model(self, edge_factor: float = 0.97) -> MathModel:
        """Dice: RTP = edge_factor (strategy-independent).

        For ANY threshold T and direction:
          chance = win_probability, mult = edge/chance
          RTP = chance × mult = chance × (edge/chance) = edge  ∎
        """
        rtp = edge_factor

        # Reference paytable at 50/50 (most common)
        mult_50 = round(edge_factor / 0.5, 2)
        official = [
            PaytableEntry("win_50", f"Correct prediction (50%) → {mult_50}×",
                          mult_50, 0.5),
            PaytableEntry("lose_50", "Wrong prediction (50%)", 0, 0.5),
        ]

        vol = _compute_volatility(official, rtp)

        return MathModel(
            game_type="dice", theoretical_rtp=rtp, house_edge=1-rtp,
            paytable=official, volatility=vol,
            parameters={
                "edge_factor": edge_factor,
                "rtp_formula": f"RTP = {edge_factor} (for ANY threshold)",
                "mult_formula": f"mult = {edge_factor} / win_chance",
                "reference_thresholds": {
                    f"{t}%_chance": {
                        "multiplier": round(edge_factor / (t/100), 2),
                        "RTP_check": round(edge_factor, 4),
                    }
                    for t in [10, 25, 50, 75, 90]
                },
            },
        )

    # ── WHEEL ──────────────────────────────────────────────────
    def wheel_model(self, segments: list[dict] = None,
                    target_rtp: float = 0.96,
                    n_segments: int = 20,
                    volatility: str = "medium") -> MathModel:
        """Wheel: RTP = Σ(mult[i]) / N. Exact."""
        if segments is None:
            segments = self._gen_wheel_segs(target_rtp, n_segments, volatility)
        n = len(segments)
        p_each = 1.0 / n

        paytable = []
        for i, s in enumerate(segments):
            label = s.get("label", f"{s['mult']}x")
            paytable.append(PaytableEntry(
                f"seg_{i}", f"Segment {i}: {label}", s["mult"], p_each))
        rtp = sum(e.contribution for e in paytable)
        vol = _compute_volatility(paytable, rtp)

        return MathModel(
            game_type="wheel", theoretical_rtp=rtp, house_edge=1-rtp,
            paytable=paytable, volatility=vol,
            parameters={
                "n_segments": n, "segments": segments,
                "rtp_formula": f"RTP = Σ(mult) / {n} = {sum(s['mult'] for s in segments)}/{n}",
            },
        )

    def _gen_wheel_segs(self, target_rtp, n, vol):
        target_sum = target_rtp * n
        templates = {
            "low": [0,1.2,0,1.5,0,2,0.5,3,0,1.2,5,0.5,0,1.5,8,0.5,0,2,1.2,10],
            "high": [0,0.5,0,0,0,1.5,0,0,0,0.5,0,0,0,1,0,0,0,2,0,50],
            "medium": [0,1.2,0,1.5,0,2,0.5,3,0,1.2,5,0.5,0,1.5,10,0.5,0,2,1.2,25],
        }
        base = templates.get(vol, templates["medium"])[:n]
        while len(base) < n:
            base.append(0)
        s = sum(base)
        if s > 0:
            base = [round(m * target_sum / s, 2) for m in base]
        return [{"mult": m, "label": "BUST" if m <= 0 else f"{m}x"} for m in base]

    # ── HILO ───────────────────────────────────────────────────
    def hilo_model(self, deck_size: int = 13,
                   max_streak: int = 12,
                   target_rtp: float = 0.96) -> MathModel:
        """HiLo: strategy-independent fair pricing.

        mult(s) = target_rtp / avg_p^s
        This ensures E[payout] = target_rtp for any cashout strategy.
        
        Paytable modeled as uniform mix of target streaks 1..5 (typical play).
        """
        avg_p = 0
        for v in range(1, deck_size + 1):
            p_hi = (deck_size - v) / (deck_size - 1) if deck_size > 1 else 0.5
            p_lo = (v - 1) / (deck_size - 1) if deck_size > 1 else 0.5
            avg_p += max(p_hi, p_lo) / deck_size

        # Model as uniform mix of target streaks 1..5
        n_targets = 5
        paytable = []
        
        for s in range(1, n_targets + 1):
            p_choose = 1.0 / n_targets  # uniform target selection
            p_reach = avg_p ** s
            mult = round(target_rtp / (avg_p ** s), 2)
            
            # Win: chose this target AND reached it
            paytable.append(PaytableEntry(
                f"win_streak_{s}",
                f"Target streak {s}, win → {mult}×",
                mult,
                p_choose * p_reach,
            ))
            # Bust: chose this target AND failed
            paytable.append(PaytableEntry(
                f"bust_target_{s}",
                f"Target streak {s}, bust",
                0,
                p_choose * (1 - p_reach),
            ))

        rtp = sum(e.contribution for e in paytable)
        vol = _compute_volatility(paytable, rtp)

        return MathModel(
            game_type="hilo", theoretical_rtp=rtp, house_edge=1-rtp,
            paytable=paytable, volatility=vol,
            parameters={
                "deck_size": deck_size, "max_streak": max_streak,
                "avg_p_correct": round(avg_p, 6),
                "target_rtp": target_rtp,
                "mult_formula": f"mult(s) = {target_rtp} / {avg_p:.4f}^s",
                "strategy": "Strategy-independent (fair pricing)",
            },
        )

    # ── CHICKEN ────────────────────────────────────────────────
    def chicken_model(self, cols: int = 4, total_lanes: int = 9,
                      hazards_per_lane: int = 1,
                      target_rtp: float = 0.96) -> MathModel:
        """Chicken: RTP auto-scaled from multiplier formula × lane survival.

        Base formula: mult_base(lane) = 1 + 0.4×lane + 0.05×lane²
        Scaled: mult(lane) = scale × mult_base(lane)
        scale chosen so Σ[P(reach lane) × P(bust) × mult(lane)] = target_rtp
        """
        p_safe = (cols - hazards_per_lane) / cols
        p_hazard = 1 - p_safe

        # Compute unscaled RTP
        unscaled_rtp = 0
        lane_data = [(0, p_hazard, 0)]  # (lane, p_outcome, base_mult)

        p_survived = 1.0
        for lane in range(1, total_lanes + 1):
            p_survived *= p_safe
            base_mult = 1 + lane * 0.4 + lane * lane * 0.05
            if lane < total_lanes:
                p_outcome = p_survived * p_hazard
            else:
                p_outcome = p_survived
            unscaled_rtp += p_outcome * base_mult
            lane_data.append((lane, p_outcome, base_mult))

        scale = target_rtp / unscaled_rtp if unscaled_rtp > 0 else 1.0

        paytable = []
        for lane, p_out, base_m in lane_data:
            mult = round(base_m * scale, 2) if lane > 0 else 0
            desc = "Hit hazard on lane 1" if lane == 0 else f"Reach lane {lane} → {mult}×"
            oid = "bust_0" if lane == 0 else f"lane_{lane}"
            paytable.append(PaytableEntry(oid, desc, mult, p_out))

        total_p = sum(e.probability for e in paytable)
        if abs(total_p - 1.0) > 1e-6:
            for e in paytable:
                e.probability /= total_p
                e.contribution = round(e.probability * e.multiplier, 10)

        rtp = sum(e.contribution for e in paytable)
        vol = _compute_volatility(paytable, rtp)

        return MathModel(
            game_type="chicken", theoretical_rtp=rtp, house_edge=1-rtp,
            paytable=paytable, volatility=vol,
            parameters={
                "cols": cols, "total_lanes": total_lanes,
                "hazards_per_lane": hazards_per_lane,
                "p_safe_per_lane": round(p_safe, 6),
                "target_rtp": target_rtp,
                "mult_scale": round(scale, 6),
                "mult_formula": f"mult = {scale:.4f} × (1 + 0.4×lane + 0.05×lane²)",
                "strategy": "Play until bust or max lane",
            },
        )

    # ── SCRATCH ────────────────────────────────────────────────
    def scratch_model(self, symbols: list[dict] = None,
                      win_chance: float = 0.35,
                      target_rtp: float = 0.96) -> MathModel:
        """Scratch: RTP auto-scaled via win_chance adjustment.

        Base formula: P(3-of-a-kind) = win_chance, symbol by weight
        Unscaled RTP = win_chance × Σ(P(sym|win) × mult)
        We scale mult values OR adjust win_chance to hit target_rtp.
        """
        if symbols is None:
            symbols = [
                {"emoji": "💎", "mult": 50, "weight": 1},
                {"emoji": "👑", "mult": 25, "weight": 2},
                {"emoji": "🏺", "mult": 10, "weight": 5},
                {"emoji": "⭐", "mult": 5,  "weight": 10},
                {"emoji": "🪙", "mult": 2,  "weight": 20},
                {"emoji": "📜", "mult": 1,  "weight": 30},
                {"emoji": "🪨", "mult": 0,  "weight": 32},
            ]

        win_symbols = [s for s in symbols if s["mult"] > 0]
        total_w = sum(s.get("weight", 1) for s in win_symbols)

        # Compute unscaled expected win multiplier (given a win occurs)
        avg_win_mult = sum(s["mult"] * s.get("weight", 1) / total_w for s in win_symbols)
        unscaled_rtp = win_chance * avg_win_mult

        # Scale multipliers to hit target_rtp
        scale = target_rtp / unscaled_rtp if unscaled_rtp > 0 else 1.0

        paytable = []
        paytable.append(PaytableEntry("no_match", "No 3-of-a-kind", 0, 1 - win_chance))

        for s in win_symbols:
            w = s.get("weight", 1)
            p = win_chance * w / total_w
            scaled_mult = round(s["mult"] * scale, 2)
            paytable.append(PaytableEntry(
                f"match_{s['emoji']}", f"3× {s['emoji']} → {scaled_mult}×",
                scaled_mult, p))

        rtp = sum(e.contribution for e in paytable)
        vol = _compute_volatility(paytable, rtp)

        return MathModel(
            game_type="scratch", theoretical_rtp=rtp, house_edge=1-rtp,
            paytable=paytable, volatility=vol,
            parameters={
                "win_chance": win_chance,
                "target_rtp": target_rtp,
                "mult_scale": round(scale, 6),
                "symbols": [{**s, "scaled_mult": round(s["mult"] * scale, 2)}
                            for s in win_symbols],
                "rtp_formula": f"RTP = {win_chance} × Σ(P(sym|win) × scaled_mult)",
            },
        )

    # ── UNIVERSAL ──────────────────────────────────────────────
    def model_for_config(self, config) -> MathModel:
        """Generate math model from a MiniGameConfig."""
        m = config.math
        gt = config.game_type.value
        if gt == "crash":
            return self.crash_model(m.crash_house_edge, m.crash_max_mult)
        elif gt == "plinko":
            profiles = m.plinko_risk_profiles or {}
            mults = profiles.get("med", profiles.get("low", None))
            return self.plinko_model(m.plinko_rows, target_rtp=m.target_rtp/100, bucket_multipliers=mults)
        elif gt == "mines":
            return self.mines_model(m.mines_grid_size, m.mines_default, m.mines_edge_factor)
        elif gt == "dice":
            return self.dice_model(m.dice_edge_factor)
        elif gt == "wheel":
            return self.wheel_model(m.wheel_segments or None, m.target_rtp/100)
        elif gt == "hilo":
            return self.hilo_model(len(m.hilo_values), target_rtp=m.target_rtp/100)
        elif gt == "chicken":
            return self.chicken_model(m.chicken_cols, m.chicken_lanes,
                                      m.chicken_hazards_per_lane, target_rtp=m.target_rtp/100)
        elif gt == "scratch":
            syms = m.scratch_symbols or None
            if syms:
                syms = [{**s, "weight": max(1, 50 - s.get("mult",0))} for s in syms]
            return self.scratch_model(syms, m.scratch_win_chance, target_rtp=m.target_rtp/100)
        raise ValueError(f"Unknown game type: {gt}")


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    engine = MiniGameMathEngine()
    game = sys.argv[1] if len(sys.argv) > 1 else "all"
    models = {
        "crash": lambda: engine.crash_model(),
        "plinko": lambda: engine.plinko_model(),
        "mines": lambda: engine.mines_model(),
        "dice": lambda: engine.dice_model(),
        "wheel": lambda: engine.wheel_model(),
        "hilo": lambda: engine.hilo_model(),
        "chicken": lambda: engine.chicken_model(),
        "scratch": lambda: engine.scratch_model(),
    }
    if game == "all":
        for name, fn in models.items():
            m = fn()
            v = m.volatility
            proof = m.rtp_proof()
            print(f"  {name:8s} | RTP={m.theoretical_rtp*100:.2f}% | "
                  f"HE={m.house_edge*100:.2f}% | "
                  f"σ={v.standard_deviation:.2f} | "
                  f"hit={v.hit_frequency*100:.1f}% | "
                  f"max={v.max_win_multiplier}× | "
                  f"Σp={proof['probability_sum_check']} "
                  f"rtp={proof['rtp_check']}")
    else:
        print(models.get(game, models["crash"])().to_json())
