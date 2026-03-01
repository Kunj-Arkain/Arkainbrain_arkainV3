"""
ARKAINBRAIN — Mini-Game Monte Carlo Validator (Phase 2)

Simulates millions of game rounds to validate theoretical math models.
Produces certification-grade validation reports showing that measured RTP
matches theoretical RTP within statistical tolerance.

Usage:
    from tools.minigame_validator import MiniGameValidator
    validator = MiniGameValidator()

    # Validate crash game
    report = validator.validate_crash(house_edge=0.03, n_rounds=10_000_000)
    print(f"Theoretical: {report['theoretical_rtp']:.4%}")
    print(f"Measured:    {report['measured_rtp']:.4%}")
    print(f"Status:      {report['status']}")

    # Validate from a MiniGameConfig
    report = validator.validate_config(config, n_rounds=1_000_000)
"""

from __future__ import annotations

import json
import math
import random
import statistics
import time
from dataclasses import dataclass, field
from typing import Optional


# ═══════════════════════════════════════════════════════════════
# Validation Report
# ═══════════════════════════════════════════════════════════════

@dataclass
class ValidationReport:
    """Results of a Monte Carlo validation run."""
    game_type: str
    n_rounds: int
    theoretical_rtp: float
    measured_rtp: float
    deviation_pct: float         # |measured - theoretical| as %
    within_tolerance: bool       # deviation < tolerance
    tolerance_pct: float         # Acceptable deviation

    # Distribution metrics
    measured_hit_freq: float     # P(payout > 0)
    measured_std_dev: float
    measured_max_mult: float
    measured_min_mult: float

    # Confidence interval
    ci_lower: float              # 95% CI lower bound
    ci_upper: float              # 95% CI upper bound

    # Histogram of payouts
    payout_histogram: dict = field(default_factory=dict)

    # Session metrics (if multi-session)
    session_rtp_min: float = 0
    session_rtp_max: float = 0
    session_rtp_std: float = 0

    # Timing
    duration_seconds: float = 0
    rounds_per_second: float = 0

    # Parameters
    parameters: dict = field(default_factory=dict)

    @property
    def status(self) -> str:
        return "✅ PASS" if self.within_tolerance else "❌ FAIL"

    def to_dict(self) -> dict:
        return {
            "report_type": "Monte Carlo Validation",
            "game_type": self.game_type,
            "status": self.status,
            "n_rounds": self.n_rounds,
            "theoretical_rtp_pct": round(self.theoretical_rtp * 100, 4),
            "measured_rtp_pct": round(self.measured_rtp * 100, 4),
            "deviation_pct": round(self.deviation_pct, 6),
            "tolerance_pct": self.tolerance_pct,
            "within_tolerance": self.within_tolerance,
            "confidence_interval_95": {
                "lower_pct": round(self.ci_lower * 100, 4),
                "upper_pct": round(self.ci_upper * 100, 4),
            },
            "distribution": {
                "hit_frequency_pct": round(self.measured_hit_freq * 100, 2),
                "std_dev": round(self.measured_std_dev, 4),
                "max_mult": self.measured_max_mult,
                "min_mult": self.measured_min_mult,
            },
            "session_variance": {
                "session_rtp_min_pct": round(self.session_rtp_min * 100, 2),
                "session_rtp_max_pct": round(self.session_rtp_max * 100, 2),
                "session_rtp_std_pct": round(self.session_rtp_std * 100, 4),
            },
            "performance": {
                "duration_seconds": round(self.duration_seconds, 2),
                "rounds_per_second": int(self.rounds_per_second),
            },
            "parameters": self.parameters,
            "payout_histogram": self.payout_histogram,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


# ═══════════════════════════════════════════════════════════════
# Monte Carlo Validator
# ═══════════════════════════════════════════════════════════════

class MiniGameValidator:
    """Monte Carlo simulator for validating mini-game math models.

    Runs N rounds of each game and compares measured RTP to theoretical.
    Default tolerance: ±0.1% (industry standard for 10M+ rounds).
    """

    DEFAULT_ROUNDS = 1_000_000
    DEFAULT_TOLERANCE = 0.001  # 0.1%
    SESSION_SIZE = 1000         # Rounds per "session" for variance analysis

    # ── CRASH ──────────────────────────────────────────────────

    def validate_crash(self, house_edge: float = 0.03,
                       max_mult: float = 100.0,
                       n_rounds: int = None,
                       tolerance: float = None) -> ValidationReport:
        """Simulate crash game rounds.

        Strategy: optimal auto-cashout (random cashout point for measurement).
        For RTP measurement, we simulate the crash point and assume the player
        cashes out at the crash point (perfect play minus bust).
        """
        n = n_rounds or self.DEFAULT_ROUNDS
        tol = tolerance or self.DEFAULT_TOLERANCE
        start = time.time()

        payouts = []
        for _ in range(n):
            r = random.random()
            if r < house_edge:
                payouts.append(0.0)  # Bust
            else:
                crash_point = min((1 - house_edge) / (1 - r), max_mult)
                # Simulate random cashout between 1.0 and crash_point
                # For fair RTP measurement, use expected value
                payouts.append(crash_point)

        theoretical = 1 - house_edge
        return self._build_report(
            "crash", payouts, theoretical, tol, time.time() - start,
            {"house_edge": house_edge, "max_mult": max_mult},
        )

    # ── PLINKO ─────────────────────────────────────────────────

    def validate_plinko(self, rows: int = 12,
                        bucket_multipliers: list[float] = None,
                        n_rounds: int = None,
                        tolerance: float = None) -> ValidationReport:
        """Simulate plinko drops."""
        n = n_rounds or self.DEFAULT_ROUNDS
        tol = tolerance or self.DEFAULT_TOLERANCE
        start = time.time()

        if bucket_multipliers is None:
            bucket_multipliers = [5.6, 2.1, 1.1, 0.5, 0.3, 0.5, 1.1, 2.1, 5.6]
            rows = 8

        n_buckets = rows + 1
        assert len(bucket_multipliers) >= n_buckets

        # Theoretical RTP from binomial
        probs = [math.comb(rows, k) / (2 ** rows) for k in range(n_buckets)]
        theoretical = sum(p * m for p, m in zip(probs, bucket_multipliers[:n_buckets]))

        payouts = []
        for _ in range(n):
            # Simulate ball path: each row is 50/50
            bucket = sum(1 for _ in range(rows) if random.random() < 0.5)
            payouts.append(bucket_multipliers[min(bucket, n_buckets - 1)])

        return self._build_report(
            "plinko", payouts, theoretical, tol, time.time() - start,
            {"rows": rows, "multipliers": bucket_multipliers[:n_buckets]},
        )

    # ── MINES ──────────────────────────────────────────────────

    def validate_mines(self, grid_size: int = 25, mine_count: int = 5,
                       edge_factor: float = 0.97,
                       n_rounds: int = None,
                       tolerance: float = None) -> ValidationReport:
        """Simulate mines games with random cashout strategy."""
        n = n_rounds or self.DEFAULT_ROUNDS
        tol = tolerance or self.DEFAULT_TOLERANCE
        start = time.time()

        safe_total = grid_size - mine_count
        payouts = []

        for _ in range(n):
            # Random strategy: reveal 1 to safe_total tiles, cashout randomly
            max_reveals = min(safe_total, 10)  # Cap for performance
            target_reveals = random.randint(1, max_reveals)

            remaining_safe = safe_total
            remaining_total = grid_size
            survived = True

            for reveal in range(target_reveals):
                p_safe = remaining_safe / remaining_total
                if random.random() < p_safe:
                    remaining_safe -= 1
                    remaining_total -= 1
                else:
                    survived = False
                    break

            if survived:
                # Calculate multiplier at this point
                p_survived = 1.0
                rs, rt = safe_total, grid_size
                for i in range(target_reveals):
                    p_survived *= rs / rt
                    rs -= 1
                    rt -= 1
                mult = edge_factor / p_survived if p_survived > 0 else 0
                payouts.append(mult)
            else:
                payouts.append(0.0)

        # Theoretical RTP for mines is edge_factor (by construction)
        theoretical = edge_factor

        return self._build_report(
            "mines", payouts, theoretical, tol, time.time() - start,
            {"grid_size": grid_size, "mine_count": mine_count, "edge_factor": edge_factor},
        )

    # ── DICE ───────────────────────────────────────────────────

    def validate_dice(self, edge_factor: float = 0.97,
                      n_rounds: int = None,
                      tolerance: float = None) -> ValidationReport:
        """Simulate dice rolls with random thresholds."""
        n = n_rounds or self.DEFAULT_ROUNDS
        tol = tolerance or self.DEFAULT_TOLERANCE
        start = time.time()

        payouts = []
        for _ in range(n):
            threshold = random.randint(2, 98)
            is_over = random.random() < 0.5

            if is_over:
                chance = (100 - threshold) / 100.0
            else:
                chance = threshold / 100.0

            mult = edge_factor / chance if chance > 0 else 0
            roll = random.randint(1, 100)

            if (is_over and roll > threshold) or (not is_over and roll <= threshold):
                payouts.append(mult)
            else:
                payouts.append(0.0)

        theoretical = edge_factor
        return self._build_report(
            "dice", payouts, theoretical, tol, time.time() - start,
            {"edge_factor": edge_factor},
        )

    # ── WHEEL ──────────────────────────────────────────────────

    def validate_wheel(self, segments: list[dict] = None,
                       n_rounds: int = None,
                       tolerance: float = None) -> ValidationReport:
        """Simulate wheel spins."""
        n = n_rounds or self.DEFAULT_ROUNDS
        tol = tolerance or self.DEFAULT_TOLERANCE
        start = time.time()

        if segments is None:
            segments = [
                {"mult": 0}, {"mult": 1.2}, {"mult": 0}, {"mult": 1.5},
                {"mult": 0}, {"mult": 2}, {"mult": 0.5}, {"mult": 3},
                {"mult": 0}, {"mult": 1.2}, {"mult": 5}, {"mult": 0.5},
                {"mult": 0}, {"mult": 1.5}, {"mult": 10}, {"mult": 0.5},
                {"mult": 0}, {"mult": 2}, {"mult": 1.2}, {"mult": 25},
            ]

        mults = [s["mult"] for s in segments]
        ns = len(mults)
        theoretical = sum(mults) / ns

        payouts = []
        for _ in range(n):
            seg = random.randint(0, ns - 1)
            payouts.append(mults[seg])

        return self._build_report(
            "wheel", payouts, theoretical, tol, time.time() - start,
            {"n_segments": ns, "segment_mults": mults},
        )

    # ── HILO ───────────────────────────────────────────────────

    def validate_hilo(self, deck_size: int = 13,
                      max_streak: int = 12,
                      target_rtp: float = 0.96,
                      n_rounds: int = None,
                      tolerance: float = None) -> ValidationReport:
        """Simulate HiLo games with play-until-bust strategy."""
        n = n_rounds or self.DEFAULT_ROUNDS
        tol = tolerance or self.DEFAULT_TOLERANCE
        start = time.time()

        # Get the scaled multiplier table from the math model
        from tools.minigame_math import MiniGameMathEngine
        engine = MiniGameMathEngine()
        model = engine.hilo_model(deck_size=deck_size, max_streak=max_streak,
                                  target_rtp=target_rtp)
        theoretical = model.theoretical_rtp

        # Extract scaled multipliers from paytable
        streak_mults = {}
        for e in model.paytable:
            if e.outcome_id.startswith("streak_"):
                s = int(e.outcome_id.split("_")[1])
                streak_mults[s] = e.multiplier

        # P(correct) per card value
        p_correct_per_card = []
        for v in range(1, deck_size + 1):
            p_hi = (deck_size - v) / (deck_size - 1) if deck_size > 1 else 0.5
            p_lo = (v - 1) / (deck_size - 1) if deck_size > 1 else 0.5
            p_correct_per_card.append(max(p_hi, p_lo))

        payouts = []
        for _ in range(n):
            current_card = random.randint(1, deck_size)
            streak = 0
            busted = False

            for _ in range(max_streak):
                p_correct = p_correct_per_card[current_card - 1]
                if random.random() < p_correct:
                    streak += 1
                    current_card = random.randint(1, deck_size)
                else:
                    busted = True
                    break

            if busted or streak == 0:
                payouts.append(0.0)
            else:
                payouts.append(streak_mults.get(streak, 0))

        return self._build_report(
            "hilo", payouts, theoretical, tol, time.time() - start,
            {"deck_size": deck_size, "max_streak": max_streak},
        )

    # ── CHICKEN ────────────────────────────────────────────────

    def validate_chicken(self, cols: int = 4, total_lanes: int = 9,
                         hazards_per_lane: int = 1,
                         target_rtp: float = 0.96,
                         n_rounds: int = None,
                         tolerance: float = None) -> ValidationReport:
        """Simulate chicken/runner games with play-until-bust strategy."""
        n = n_rounds or self.DEFAULT_ROUNDS
        tol = tolerance or self.DEFAULT_TOLERANCE
        start = time.time()

        from tools.minigame_math import MiniGameMathEngine
        engine = MiniGameMathEngine()
        model = engine.chicken_model(cols=cols, total_lanes=total_lanes,
                                     hazards_per_lane=hazards_per_lane,
                                     target_rtp=target_rtp)
        theoretical = model.theoretical_rtp

        # Extract scaled multipliers
        lane_mults = {}
        for e in model.paytable:
            if e.outcome_id.startswith("lane_"):
                lane = int(e.outcome_id.split("_")[1])
                lane_mults[lane] = e.multiplier

        p_safe = (cols - hazards_per_lane) / cols
        payouts = []

        for _ in range(n):
            survived_lanes = 0
            for lane in range(1, total_lanes + 1):
                if random.random() < p_safe:
                    survived_lanes = lane
                else:
                    break

            if survived_lanes > 0:
                payouts.append(lane_mults.get(survived_lanes, 0))
            else:
                payouts.append(0.0)

        return self._build_report(
            "chicken", payouts, theoretical, tol, time.time() - start,
            {"cols": cols, "total_lanes": total_lanes,
             "hazards_per_lane": hazards_per_lane},
        )

    # ── SCRATCH ────────────────────────────────────────────────

    def validate_scratch(self, symbols: list[dict] = None,
                         win_chance: float = 0.35,
                         n_rounds: int = None,
                         tolerance: float = None) -> ValidationReport:
        """Simulate scratch card outcomes."""
        n = n_rounds or self.DEFAULT_ROUNDS
        tol = tolerance or self.DEFAULT_TOLERANCE
        start = time.time()

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

        # Build weighted selection for winning symbol
        win_symbols = [s for s in symbols if s["mult"] > 0]
        total_w = sum(s.get("weight", 1) for s in win_symbols)
        cum_weights = []
        cum = 0
        for s in win_symbols:
            cum += s.get("weight", 1) / total_w
            cum_weights.append(cum)

        payouts = []
        for _ in range(n):
            if random.random() < win_chance:
                # Win — pick symbol by weight
                r = random.random()
                for i, cw in enumerate(cum_weights):
                    if r < cw:
                        payouts.append(win_symbols[i]["mult"])
                        break
                else:
                    payouts.append(win_symbols[-1]["mult"])
            else:
                payouts.append(0.0)

        # Theoretical
        from tools.minigame_math import MiniGameMathEngine
        engine = MiniGameMathEngine()
        model = engine.scratch_model(symbols=symbols, win_chance=win_chance)
        theoretical = model.theoretical_rtp

        return self._build_report(
            "scratch", payouts, theoretical, tol, time.time() - start,
            {"win_chance": win_chance, "n_symbols": len(symbols)},
        )

    # ── CONFIG-DRIVEN ──────────────────────────────────────────

    def validate_config(self, config, n_rounds: int = None,
                        tolerance: float = None) -> ValidationReport:
        """Validate a MiniGameConfig's math model via simulation."""
        m = config.math
        gt = config.game_type.value
        n = n_rounds or self.DEFAULT_ROUNDS
        tol = tolerance or self.DEFAULT_TOLERANCE

        if gt == "crash":
            return self.validate_crash(m.crash_house_edge, m.crash_max_mult, n, tol)
        elif gt == "plinko":
            profiles = m.plinko_risk_profiles or {}
            mults = profiles.get("low", None)
            return self.validate_plinko(m.plinko_rows, mults, n, tol)
        elif gt == "mines":
            return self.validate_mines(m.mines_grid_size, m.mines_default,
                                       m.mines_edge_factor, n, tol)
        elif gt == "dice":
            return self.validate_dice(m.dice_edge_factor, n, tol)
        elif gt == "wheel":
            return self.validate_wheel(m.wheel_segments or None, n, tol)
        elif gt == "hilo":
            return self.validate_hilo(n_rounds=n, tolerance=tol)
        elif gt == "chicken":
            return self.validate_chicken(m.chicken_cols, m.chicken_lanes,
                                         m.chicken_hazards_per_lane, n, tol)
        elif gt == "scratch":
            syms = m.scratch_symbols or None
            if syms:
                syms = [{**s, "weight": max(1, 50 - s.get("mult", 0))} for s in syms]
            return self.validate_scratch(syms, m.scratch_win_chance, n, tol)
        else:
            raise ValueError(f"Unknown game type: {gt}")

    # ── Report Builder ─────────────────────────────────────────

    def _build_report(self, game_type: str, payouts: list[float],
                      theoretical: float, tolerance: float,
                      duration: float, params: dict) -> ValidationReport:
        """Build validation report from simulation payouts."""
        n = len(payouts)
        measured_rtp = sum(payouts) / n if n > 0 else 0
        deviation = abs(measured_rtp - theoretical)

        # Standard deviation
        std_dev = statistics.stdev(payouts) if n > 1 else 0

        # 95% confidence interval
        se = std_dev / math.sqrt(n) if n > 0 else 0
        ci_lower = measured_rtp - 1.96 * se
        ci_upper = measured_rtp + 1.96 * se

        # Hit frequency
        hit_count = sum(1 for p in payouts if p > 0)
        hit_freq = hit_count / n if n > 0 else 0

        # Payout histogram
        buckets = {"0x": 0, "0-1x": 0, "1-2x": 0, "2-5x": 0,
                   "5-10x": 0, "10-25x": 0, "25-50x": 0, "50-100x": 0, "100x+": 0}
        for p in payouts:
            if p <= 0:
                buckets["0x"] += 1
            elif p < 1:
                buckets["0-1x"] += 1
            elif p < 2:
                buckets["1-2x"] += 1
            elif p < 5:
                buckets["2-5x"] += 1
            elif p < 10:
                buckets["5-10x"] += 1
            elif p < 25:
                buckets["10-25x"] += 1
            elif p < 50:
                buckets["25-50x"] += 1
            elif p < 100:
                buckets["50-100x"] += 1
            else:
                buckets["100x+"] += 1

        histogram = {k: round(v / n * 100, 2) for k, v in buckets.items() if v > 0}

        # Session-level variance
        session_rtps = []
        ss = self.SESSION_SIZE
        for i in range(0, n, ss):
            chunk = payouts[i:i + ss]
            if len(chunk) >= ss // 2:
                session_rtps.append(sum(chunk) / len(chunk))

        return ValidationReport(
            game_type=game_type,
            n_rounds=n,
            theoretical_rtp=theoretical,
            measured_rtp=measured_rtp,
            deviation_pct=round(deviation * 100, 6),
            within_tolerance=deviation <= tolerance,
            tolerance_pct=round(tolerance * 100, 4),
            measured_hit_freq=hit_freq,
            measured_std_dev=std_dev,
            measured_max_mult=max(payouts) if payouts else 0,
            measured_min_mult=min(payouts) if payouts else 0,
            ci_lower=ci_lower,
            ci_upper=ci_upper,
            payout_histogram=histogram,
            session_rtp_min=min(session_rtps) if session_rtps else 0,
            session_rtp_max=max(session_rtps) if session_rtps else 0,
            session_rtp_std=statistics.stdev(session_rtps) if len(session_rtps) > 1 else 0,
            duration_seconds=duration,
            rounds_per_second=n / duration if duration > 0 else 0,
            parameters=params,
        )


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    n = int(sys.argv[2]) if len(sys.argv) > 2 else 500_000
    game = sys.argv[1] if len(sys.argv) > 1 else "all"

    validator = MiniGameValidator()

    games = {
        "crash": lambda: validator.validate_crash(n_rounds=n),
        "plinko": lambda: validator.validate_plinko(n_rounds=n),
        "mines": lambda: validator.validate_mines(n_rounds=n),
        "dice": lambda: validator.validate_dice(n_rounds=n),
        "wheel": lambda: validator.validate_wheel(n_rounds=n),
        "hilo": lambda: validator.validate_hilo(n_rounds=n),
        "chicken": lambda: validator.validate_chicken(n_rounds=n),
        "scratch": lambda: validator.validate_scratch(n_rounds=n),
    }

    if game == "all":
        print(f"Running {n:,} rounds per game...\n")
        for name, fn in games.items():
            report = fn()
            print(f"  {report.status} {name:8s} | "
                  f"theory={report.theoretical_rtp*100:.2f}% "
                  f"measured={report.measured_rtp*100:.2f}% "
                  f"dev={report.deviation_pct:.4f}% "
                  f"hit={report.measured_hit_freq*100:.1f}% "
                  f"σ={report.measured_std_dev:.2f} "
                  f"({report.duration_seconds:.1f}s)")
    else:
        report = games.get(game, games["crash"])()
        print(report.to_json())
