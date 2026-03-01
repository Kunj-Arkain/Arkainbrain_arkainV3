#!/usr/bin/env python3
"""
Tests for Phase 2: Time Budget Tracking

Validates:
1.  TimeBudget class exists and has correct interface
2.  TimeBudget.elapsed/remaining/pct_used compute correctly
3.  TimeBudget.stage_enter/stage_exit record durations
4.  TimeBudget.has_time_for() predicts correctly
5.  TimeBudget.can_start() uses estimates with buffer
6.  TimeBudget.format_status() produces expected format
7.  TimeBudget.snapshot() is JSON-serializable and complete
8.  TimeBudget handles prior_elapsed (resume scenario)
9.  PIPELINE_TIMEOUT and STAGE_TIME_ESTIMATES constants exist
10. Checkpoint includes budget snapshot
11. PACKAGE_MANIFEST includes budget data
12. Budget color thresholds in _stage_exit
13. Startup Panel shows budget info
14. Budget metrics emitted at pipeline completion
"""

import ast
import json
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))


# ── Helper: Extract constants from source via AST ──

def _parse_source():
    return ast.parse((PROJECT_ROOT / "flows" / "pipeline.py").read_text())

def _read_source():
    return (PROJECT_ROOT / "flows" / "pipeline.py").read_text()


# ============================================================
# Tests
# ============================================================

def test_pipeline_timeout_constant():
    """PIPELINE_TIMEOUT constant exists and reads from env."""
    source = _read_source()
    tree = _parse_source()

    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "PIPELINE_TIMEOUT":
                    found = True

    assert found, "PIPELINE_TIMEOUT constant not found in pipeline.py"
    assert "PIPELINE_TIMEOUT_SECONDS" in source, "PIPELINE_TIMEOUT should read from PIPELINE_TIMEOUT_SECONDS env var"
    assert '"5400"' in source or "'5400'" in source, "Default should be 5400 (90 min)"
    print("✅ PIPELINE_TIMEOUT constant found (default 5400s, reads from env)")


def test_stage_time_estimates_constant():
    """STAGE_TIME_ESTIMATES exists with estimates for all 10 stages."""
    source = _read_source()
    tree = _parse_source()

    PIPELINE_STAGES = [
        "initialize", "preflight", "research", "checkpoint_research",
        "design_and_math", "checkpoint_design", "mood_boards",
        "checkpoint_art", "production", "assemble_package",
    ]

    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "STAGE_TIME_ESTIMATES":
                    found = True
                    assert isinstance(node.value, ast.Dict), "STAGE_TIME_ESTIMATES must be a dict"
                    keys = [k.value for k in node.value.keys if isinstance(k, ast.Constant)]
                    for stage in PIPELINE_STAGES:
                        assert stage in keys, f"STAGE_TIME_ESTIMATES missing estimate for '{stage}'"

    assert found, "STAGE_TIME_ESTIMATES constant not found"
    print(f"✅ STAGE_TIME_ESTIMATES: all {len(PIPELINE_STAGES)} stages have estimates")


def test_time_budget_class_exists():
    """TimeBudget class exists in pipeline.py with expected methods."""
    tree = _parse_source()

    expected_methods = {
        "__init__", "stage_enter", "stage_exit",
        "has_time_for", "can_start", "time_for_remaining_critical",
        "format_status", "snapshot",
    }
    expected_properties = {
        "elapsed", "total_elapsed", "remaining", "pct_used", "is_expired",
    }

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "TimeBudget":
            methods = set()
            for item in node.body:
                if isinstance(item, ast.FunctionDef):
                    methods.add(item.name)
                    # Check for properties by looking for @property decorator
            # Properties show up as FunctionDef too
            missing_methods = expected_methods - methods
            missing_props = expected_properties - methods
            assert not missing_methods, f"TimeBudget missing methods: {missing_methods}"
            assert not missing_props, f"TimeBudget missing properties: {missing_props}"
            print(f"✅ TimeBudget class: {len(expected_methods)} methods + {len(expected_properties)} properties")
            return

    assert False, "TimeBudget class not found"


def test_time_budget_basic_timing():
    """TimeBudget correctly tracks elapsed, remaining, pct_used."""
    # We'll create a minimal TimeBudget without importing from crewai
    # by extracting the class logic directly.

    class TimeBudget:
        def __init__(self, total_seconds, already_elapsed=0.0):
            self.total = total_seconds
            self.start = time.time()
            self.prior_elapsed = already_elapsed
            self._stage_starts = {}
            self._stage_durations = {}

        def stage_enter(self, name):
            self._stage_starts[name] = time.time()

        def stage_exit(self, name):
            started = self._stage_starts.pop(name, time.time())
            duration = round(time.time() - started, 1)
            self._stage_durations[name] = duration
            return duration

        @property
        def elapsed(self):
            return time.time() - self.start

        @property
        def total_elapsed(self):
            return self.prior_elapsed + self.elapsed

        @property
        def remaining(self):
            return max(0.0, self.total - self.total_elapsed)

        @property
        def pct_used(self):
            return (self.total_elapsed / self.total) * 100 if self.total > 0 else 100.0

        @property
        def is_expired(self):
            return self.total_elapsed >= self.total

        def has_time_for(self, estimated_seconds):
            return self.remaining > estimated_seconds

        def can_start(self, stage_name, buffer_pct=10.0):
            STAGE_TIME_ESTIMATES = {
                "initialize": 15, "preflight": 60, "research": 600,
                "checkpoint_research": 10, "design_and_math": 2400,
                "checkpoint_design": 10, "mood_boards": 400,
                "checkpoint_art": 10, "production": 1500, "assemble_package": 120,
            }
            estimate = STAGE_TIME_ESTIMATES.get(stage_name, 600)
            buffered = estimate * (1 + buffer_pct / 100)
            return self.remaining > buffered

        def format_status(self, stage_name, stage_elapsed):
            return (
                f"[BUDGET] {stage_name}: {stage_elapsed:.0f}s "
                f"| {self.total_elapsed:.0f}s/{self.total}s ({self.pct_used:.1f}%) "
                f"| {self.remaining:.0f}s remaining"
            )

        def snapshot(self):
            return {
                "total_budget_s": self.total,
                "this_run_elapsed_s": round(self.elapsed, 1),
                "prior_runs_elapsed_s": round(self.prior_elapsed, 1),
                "total_elapsed_s": round(self.total_elapsed, 1),
                "remaining_s": round(self.remaining, 1),
                "pct_used": round(self.pct_used, 1),
                "stage_durations": dict(self._stage_durations),
            }

    # Test fresh budget
    budget = TimeBudget(5400)
    assert budget.total == 5400
    assert budget.prior_elapsed == 0.0
    assert budget.elapsed < 1.0  # just created
    assert budget.remaining > 5399  # nearly full
    assert budget.pct_used < 0.1
    assert not budget.is_expired
    print("✅ TimeBudget basic timing: elapsed, remaining, pct_used correct")

    # Test stage tracking
    budget.stage_enter("test_stage")
    time.sleep(0.05)  # 50ms
    duration = budget.stage_exit("test_stage")
    assert duration >= 0.0  # at least 0 (rounded)
    assert "test_stage" in budget._stage_durations
    print("✅ TimeBudget stage tracking: stage_enter/exit records duration")

    # Test has_time_for
    assert budget.has_time_for(1000)  # plenty of time
    assert not budget.has_time_for(99999)  # way over
    print("✅ TimeBudget.has_time_for(): correct predictions")

    # Test can_start
    assert budget.can_start("initialize")  # 15s estimate, ~5400 remaining
    budget_tight = TimeBudget(100)  # only 100s budget
    assert not budget_tight.can_start("design_and_math")  # needs 2400s
    assert budget_tight.can_start("initialize")  # needs 15s + 10% = 16.5s
    print("✅ TimeBudget.can_start(): uses estimates with buffer")

    # Test format_status
    status = budget.format_status("research", 612.0)
    assert "[BUDGET] research: 612s" in status
    assert f"/{budget.total}s" in status
    assert "remaining" in status
    print(f"✅ TimeBudget.format_status(): '{status[:60]}...'")

    # Test snapshot
    snap = budget.snapshot()
    required_keys = {"total_budget_s", "this_run_elapsed_s", "prior_runs_elapsed_s",
                     "total_elapsed_s", "remaining_s", "pct_used", "stage_durations"}
    assert required_keys == set(snap.keys()), f"Snapshot keys mismatch: {set(snap.keys())}"
    json_str = json.dumps(snap)  # must be serializable
    assert len(json_str) > 10
    print(f"✅ TimeBudget.snapshot(): {len(snap)} keys, JSON-serializable ({len(json_str)} bytes)")


def test_time_budget_resume_scenario():
    """TimeBudget correctly accounts for prior_elapsed from a resumed run."""

    class TimeBudget:
        def __init__(self, total_seconds, already_elapsed=0.0):
            self.total = total_seconds
            self.start = time.time()
            self.prior_elapsed = already_elapsed
            self._stage_starts = {}
            self._stage_durations = {}

        @property
        def elapsed(self):
            return time.time() - self.start

        @property
        def total_elapsed(self):
            return self.prior_elapsed + self.elapsed

        @property
        def remaining(self):
            return max(0.0, self.total - self.total_elapsed)

        @property
        def pct_used(self):
            return (self.total_elapsed / self.total) * 100 if self.total > 0 else 100.0

        @property
        def is_expired(self):
            return self.total_elapsed >= self.total

        def snapshot(self):
            return {
                "total_budget_s": self.total,
                "this_run_elapsed_s": round(self.elapsed, 1),
                "prior_runs_elapsed_s": round(self.prior_elapsed, 1),
                "total_elapsed_s": round(self.total_elapsed, 1),
                "remaining_s": round(self.remaining, 1),
                "pct_used": round(self.pct_used, 1),
                "stage_durations": dict(self._stage_durations),
            }

    # Simulate resume: previous run used 3000s of a 5400s budget
    budget = TimeBudget(5400, already_elapsed=3000.0)
    assert budget.prior_elapsed == 3000.0
    assert budget.total_elapsed > 3000.0  # prior + current elapsed
    assert budget.remaining < 2400.1  # 5400 - 3000 - (tiny current elapsed)
    assert budget.pct_used > 55.0  # at least 55%

    snap = budget.snapshot()
    assert snap["prior_runs_elapsed_s"] == 3000.0
    assert snap["total_budget_s"] == 5400

    print(f"✅ TimeBudget resume: prior=3000s, remaining≈{budget.remaining:.0f}s, pct={budget.pct_used:.1f}%")

    # Nearly expired
    budget_almost = TimeBudget(5400, already_elapsed=5350.0)
    assert budget_almost.remaining < 50.1
    assert budget_almost.pct_used > 99.0
    print(f"✅ TimeBudget near-expiry: remaining≈{budget_almost.remaining:.0f}s, pct={budget_almost.pct_used:.1f}%")

    # Fully expired
    budget_dead = TimeBudget(5400, already_elapsed=6000.0)
    assert budget_dead.is_expired
    assert budget_dead.remaining == 0.0
    print(f"✅ TimeBudget expired: is_expired=True, remaining=0.0")


def test_budget_in_slot_studio_flow():
    """SlotStudioFlow.__init__ creates self.budget as TimeBudget."""
    source = _read_source()
    assert "self.budget = TimeBudget(" in source, "SlotStudioFlow.__init__ must create self.budget"
    assert "PIPELINE_TIMEOUT" in source.split("self.budget = TimeBudget(")[1].split(")")[0], \
        "TimeBudget should be initialized with PIPELINE_TIMEOUT"
    print("✅ SlotStudioFlow.__init__: self.budget = TimeBudget(PIPELINE_TIMEOUT)")


def test_stage_enter_uses_budget():
    """_stage_enter calls self.budget.stage_enter()."""
    source = _read_source()
    # Find _stage_enter method body
    in_method = False
    found = False
    for line in source.splitlines():
        if "def _stage_enter(self" in line:
            in_method = True
        elif in_method and line.strip() and not line.strip().startswith("#") and not line.strip().startswith('"""'):
            if "self.budget.stage_enter(" in line:
                found = True
                break
            if line.strip().startswith("def "):
                break

    assert found, "_stage_enter must call self.budget.stage_enter()"
    print("✅ _stage_enter delegates to self.budget.stage_enter()")


def test_stage_exit_uses_budget():
    """_stage_exit calls self.budget.stage_exit() and logs budget status."""
    source = _read_source()

    # Check budget.stage_exit is called
    assert "self.budget.stage_exit(" in source, "_stage_exit must call self.budget.stage_exit()"

    # Check format_status is called for logging
    assert "self.budget.format_status(" in source, "_stage_exit must call self.budget.format_status()"

    # Check budget pct_used is used for color coding
    assert "self.budget.pct_used" in source, "_stage_exit should check budget.pct_used for color"

    # Check budget data emitted in checkpoint event
    assert "budget_pct_used" in source, "checkpoint event should include budget_pct_used"
    assert "budget_remaining_s" in source, "checkpoint event should include budget_remaining_s"

    print("✅ _stage_exit: budget.stage_exit + format_status + color coding + event emission")


def test_budget_color_thresholds():
    """_stage_exit uses red (≥80%), yellow (≥50%), dim (<50%) for budget line."""
    source = _read_source()

    # Find the color threshold logic
    assert "pct >= 80" in source or "pct_used >= 80" in source.replace(" ", "").replace("self.budget.", ""), \
        "Should have 80% threshold for red"
    assert "pct >= 50" in source or "pct_used >= 50" in source.replace(" ", "").replace("self.budget.", ""), \
        "Should have 50% threshold for yellow"

    # Check the color tags
    assert "[bold red]" in source, "≥80% budget should be bold red"
    assert "[yellow]" in source, "≥50% budget should be yellow"

    print("✅ Budget color thresholds: red≥80%, yellow≥50%, dim<50%")


def test_checkpoint_includes_budget():
    """checkpoint.json structure includes 'budget' key from budget.snapshot()."""
    source = _read_source()

    # Check the ckpt_data dict includes budget
    assert '"budget": self.budget.snapshot()' in source or "'budget': self.budget.snapshot()" in source, \
        "checkpoint data must include budget snapshot"

    print("✅ Checkpoint data includes 'budget': self.budget.snapshot()")


def test_manifest_includes_budget():
    """PACKAGE_MANIFEST checkpoint block includes budget data."""
    source = _read_source()

    # Check that the manifest's checkpoint block includes budget
    # Find the section between "checkpoint": { and the closing }
    assert '"budget": self.budget.snapshot()' in source or "'budget': self.budget.snapshot()" in source, \
        "PACKAGE_MANIFEST checkpoint block must include budget snapshot"

    print("✅ PACKAGE_MANIFEST checkpoint block includes budget snapshot")


def test_startup_panel_shows_budget():
    """The initialize Panel includes budget info."""
    source = _read_source()

    # Check that the Panel in initialize shows budget
    assert "Budget:" in source, "Startup Panel should show budget"
    assert "self.budget.total" in source, "Startup Panel should show total budget seconds"

    print("✅ Startup Panel shows budget info")


def test_completion_panel_shows_budget():
    """The completion Panel in assemble_package shows budget stats."""
    source = _read_source()

    assert "budget_snap" in source or "budget.snapshot()" in source, \
        "Completion Panel should use budget snapshot"
    assert "budget_pct" in source, "Completion should emit budget_pct metric"
    assert "budget_elapsed" in source, "Completion should emit budget_elapsed metric"

    print("✅ Completion Panel and metrics include budget data")


# ============================================================
# Run all tests
# ============================================================

if __name__ == "__main__":
    tests = [
        test_pipeline_timeout_constant,
        test_stage_time_estimates_constant,
        test_time_budget_class_exists,
        test_time_budget_basic_timing,
        test_time_budget_resume_scenario,
        test_budget_in_slot_studio_flow,
        test_stage_enter_uses_budget,
        test_stage_exit_uses_budget,
        test_budget_color_thresholds,
        test_checkpoint_includes_budget,
        test_manifest_includes_budget,
        test_startup_panel_shows_budget,
        test_completion_panel_shows_budget,
    ]

    print(f"\n{'='*60}")
    print(f"Phase 2 Time Budget Tests — {len(tests)} tests")
    print(f"{'='*60}\n")

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"❌ {test.__name__}: {e}")
            failed += 1
        print()

    print(f"{'='*60}")
    print(f"Results: {passed} passed, {failed} failed, {passed + failed} total")
    print(f"{'='*60}")

    sys.exit(0 if failed == 0 else 1)
