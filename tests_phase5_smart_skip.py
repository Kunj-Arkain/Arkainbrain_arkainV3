#!/usr/bin/env python3
"""Phase 5 Tests: Smart Stage Skipping

Tests for:
  A) Constants & classification (priorities, thresholds, lite estimates)
  B) _budget_decision() logic (P0 always run, P1 tiered, P2 skip/run)
  C) _skip_stage() side effects (state tracking, emit events, stub files)
  D) Stage integration (preflight, research, checkpoints, mood_boards wired)
  E) Resume integration (preview, skip summary)
  F) PACKAGE_MANIFEST enrichment
  G) UI: CSS, JS event handlers, timeline skip class
"""
import ast
import json
import re
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).parent

# ── Helpers ──
def read(fname):
    return (ROOT / fname).read_text(encoding="utf-8")

def ast_tree(fname):
    return ast.parse(read(fname))

pipeline_src = read("flows/pipeline.py")
webapp_src = read("web_app.py")
tfeed_src = read("static/thought-feed.js")


# ════════════════════════════════════════════════════════════════
# A) Constants & Classification
# ════════════════════════════════════════════════════════════════

class TestConstants(unittest.TestCase):
    """Test STAGE_PRIORITY, BUDGET_SAFETY_MARGINS, STAGE_TIME_ESTIMATES_LITE."""

    def test_stage_priority_covers_all_stages(self):
        """STAGE_PRIORITY must have an entry for every stage in PIPELINE_STAGES."""
        self.assertIn("STAGE_PRIORITY", pipeline_src)
        # Extract the dict
        m = re.search(r'STAGE_PRIORITY\s*=\s*\{([^}]+)\}', pipeline_src)
        self.assertIsNotNone(m, "STAGE_PRIORITY dict not found")
        stages = ["initialize", "preflight", "research", "checkpoint_research",
                   "design_and_math", "checkpoint_design", "mood_boards",
                   "checkpoint_art", "production", "assemble_package"]
        for s in stages:
            self.assertIn(f'"{s}"', m.group(0), f"STAGE_PRIORITY missing '{s}'")

    def test_p0_stages(self):
        """P0 stages: initialize, design_and_math, production, assemble_package."""
        m = re.search(r'STAGE_PRIORITY\s*=\s*\{([^}]+)\}', pipeline_src)
        content = m.group(0)
        for s in ["initialize", "design_and_math", "production", "assemble_package"]:
            pat = rf'"{s}"\s*:\s*0'
            self.assertRegex(content, pat, f"{s} should be P0")

    def test_p1_stages(self):
        """P1 stages: research, mood_boards."""
        m = re.search(r'STAGE_PRIORITY\s*=\s*\{([^}]+)\}', pipeline_src)
        content = m.group(0)
        for s in ["research", "mood_boards"]:
            pat = rf'"{s}"\s*:\s*1'
            self.assertRegex(content, pat, f"{s} should be P1")

    def test_p2_stages(self):
        """P2 stages: preflight, checkpoint_research, checkpoint_design, checkpoint_art."""
        m = re.search(r'STAGE_PRIORITY\s*=\s*\{([^}]+)\}', pipeline_src)
        content = m.group(0)
        for s in ["preflight", "checkpoint_research", "checkpoint_design", "checkpoint_art"]:
            pat = rf'"{s}"\s*:\s*2'
            self.assertRegex(content, pat, f"{s} should be P2")

    def test_budget_safety_margins_keys(self):
        """BUDGET_SAFETY_MARGINS must have skip_p2, compress_p1, skip_p1."""
        self.assertIn("BUDGET_SAFETY_MARGINS", pipeline_src)
        for key in ["skip_p2", "compress_p1", "skip_p1"]:
            self.assertIn(f'"{key}"', pipeline_src)

    def test_compress_threshold_higher_than_skip(self):
        """compress_p1 threshold > skip_p1 threshold (compress before skip)."""
        m_compress = re.search(r'"compress_p1"\s*:\s*([\d.]+)', pipeline_src)
        m_skip = re.search(r'"skip_p1"\s*:\s*([\d.]+)', pipeline_src)
        self.assertIsNotNone(m_compress)
        self.assertIsNotNone(m_skip)
        self.assertGreater(float(m_compress.group(1)), float(m_skip.group(1)))

    def test_lite_time_estimates(self):
        """STAGE_TIME_ESTIMATES_LITE must exist for research and mood_boards."""
        self.assertIn("STAGE_TIME_ESTIMATES_LITE", pipeline_src)
        m = re.search(r'STAGE_TIME_ESTIMATES_LITE\s*=\s*\{([^}]+)\}', pipeline_src)
        self.assertIsNotNone(m)
        self.assertIn('"research"', m.group(0))
        self.assertIn('"mood_boards"', m.group(0))

    def test_lite_estimates_less_than_full(self):
        """Lite estimates should be smaller than full estimates."""
        # research: lite 200 < full 600
        self.assertIn('"research":    200', pipeline_src)
        # mood_boards: lite 150 < full 400
        self.assertIn('"mood_boards": 150', pipeline_src)


# ════════════════════════════════════════════════════════════════
# B) _budget_decision() Logic
# ════════════════════════════════════════════════════════════════

class TestBudgetDecision(unittest.TestCase):
    """Test _budget_decision method exists and has correct logic."""

    def test_method_exists(self):
        tree = ast_tree("flows/pipeline.py")
        methods = {
            node.name for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_budget_decision"
        }
        self.assertIn("_budget_decision", methods)

    def test_returns_run_for_p0(self):
        """P0 stages always return 'run'."""
        # The method checks `if priority == 0: return "run"`
        self.assertIn('if priority == 0:', pipeline_src)
        self.assertIn('return "run"', pipeline_src)

    def test_p1_tiered_degradation(self):
        """P1 stages have three possible returns: run, lite, skip."""
        # Pattern: if priority == 1: ... "skip" ... "lite" ... "run"
        p1_block = pipeline_src[pipeline_src.index('if priority == 1:'):]
        p1_end = p1_block[:500]  # next ~500 chars
        self.assertIn('"skip"', p1_end)
        self.assertIn('"lite"', p1_end)
        self.assertIn('"run"', p1_end)

    def test_p2_binary_decision(self):
        """P2 stages have binary: skip or run (no lite)."""
        p2_block = pipeline_src[pipeline_src.index('if priority == 2:'):]
        p2_end = p2_block[:300]
        self.assertIn('"skip"', p2_end)
        self.assertIn('"run"', p2_end)
        self.assertNotIn('"lite"', p2_end)

    def test_uses_p0_reserve(self):
        """Budget decision calculates P0 reserve from time_for_remaining_critical."""
        # Search within _budget_decision method (between def and next def)
        start = pipeline_src.index("def _budget_decision(self")
        end = pipeline_src.index("\n    def ", start + 10)
        block = pipeline_src[start:end]
        self.assertIn("time_for_remaining_critical", block)

    def test_safety_margin_calculation(self):
        """Budget decision divides available time by estimate to get safety margin."""
        idx = pipeline_src.index("def _budget_decision(self")
        block = pipeline_src[idx:idx+1000]
        self.assertIn("available", block)
        self.assertIn("safety", block)
        self.assertIn("estimate", block)


# ════════════════════════════════════════════════════════════════
# C) _skip_stage() Side Effects
# ════════════════════════════════════════════════════════════════

class TestSkipStage(unittest.TestCase):
    """Test _skip_stage method and its side effects."""

    def test_method_exists(self):
        tree = ast_tree("flows/pipeline.py")
        methods = {
            node.name for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_skip_stage"
        }
        self.assertIn("_skip_stage", methods)

    def test_appends_to_skipped_stages(self):
        """Skip decision appends stage to state.skipped_stages."""
        block = pipeline_src[pipeline_src.index("def _skip_stage"):][:800]
        self.assertIn("skipped_stages.append", block)

    def test_emits_stage_skip_event(self):
        """Skip emits a stage_skip structured event."""
        idx = pipeline_src.index("def _skip_stage")
        block = pipeline_src[idx:idx+2000]
        self.assertIn('emit("stage_skip"', block)

    def test_emits_stage_compress_event(self):
        """Compress emits a stage_compress structured event."""
        start = pipeline_src.index("def _skip_stage")
        end = pipeline_src.index("\n    def ", start + 10)
        block = pipeline_src[start:end]
        self.assertIn('emit("stage_compress"', block)

    def test_writes_stub_file(self):
        """Skip writes a _SKIPPED_{stage_name}.txt stub file."""
        idx = pipeline_src.index("def _skip_stage")
        block = pipeline_src[idx:idx+2500]
        self.assertIn("_SKIPPED_", block)
        self.assertIn(".write_text", block)

    def test_compress_does_not_append_skipped(self):
        """Compress (lite) should NOT add to skipped_stages list."""
        idx = pipeline_src.index("def _skip_stage")
        block = pipeline_src[idx:idx+2500]
        # The append is inside `if decision == "skip":` block only
        skip_block_start = block.index('if decision == "skip":')
        append_pos = block.index("skipped_stages.append")
        # Check there's an elif for lite
        self.assertIn('elif decision == "lite":', block)
        lite_block_start = block.index('elif decision == "lite":')
        self.assertGreater(append_pos, skip_block_start)
        self.assertLess(append_pos, lite_block_start)


# ════════════════════════════════════════════════════════════════
# D) Stage Integration — Budget Decisions Wired In
# ════════════════════════════════════════════════════════════════

class TestStageWiring(unittest.TestCase):
    """Verify _budget_decision is called in every skippable stage."""

    def test_preflight_has_budget_decision(self):
        """run_preflight calls _budget_decision('preflight')."""
        block = pipeline_src[pipeline_src.index("def run_preflight"):][:600]
        self.assertIn('_budget_decision("preflight")', block)

    def test_preflight_skips_on_skip(self):
        block = pipeline_src[pipeline_src.index("def run_preflight"):][:600]
        self.assertIn('if decision == "skip":', block)
        self.assertIn('_skip_stage("preflight"', block)

    def test_research_has_budget_decision(self):
        """run_research calls _budget_decision('research')."""
        block = pipeline_src[pipeline_src.index("def run_research"):][:800]
        self.assertIn('_budget_decision("research")', block)

    def test_research_skip_sets_minimal_data(self):
        """Research skip provides minimal market_research so downstream doesn't crash."""
        block = pipeline_src[pipeline_src.index("def run_research"):][:1000]
        self.assertIn("market_research", block)
        self.assertIn("Skipped due to time pressure", block)

    def test_research_lite_flag(self):
        """Research sets is_lite flag for crew selection."""
        idx = pipeline_src.index("def run_research")
        block = pipeline_src[idx:idx+1200]
        self.assertIn("is_lite", block)
        self.assertIn('decision == "lite"', block)

    def test_research_lite_runs_sweep_only(self):
        """In lite mode, research runs only sweep_task."""
        # Search entire run_research method
        start = pipeline_src.index("def run_research")
        end = pipeline_src.index("\n    @listen(run_research)", start + 10)
        block = pipeline_src[start:end]
        self.assertIn("if is_lite:", block)
        self.assertIn("tasks=[sweep_task]", block)

    def test_research_lite_skips_geo(self):
        """In lite mode, geographic research is skipped."""
        start = pipeline_src.index("def run_research")
        end = pipeline_src.index("\n    @listen(run_research)", start + 10)
        block = pipeline_src[start:end]
        self.assertIn("Skipping geo research (lite mode)", block)

    def test_checkpoint_research_has_budget_decision(self):
        block = pipeline_src[pipeline_src.index("def checkpoint_research"):][:500]
        self.assertIn('_budget_decision("checkpoint_research")', block)
        self.assertIn('research_approved = True', block)

    def test_checkpoint_design_has_budget_decision(self):
        block = pipeline_src[pipeline_src.index("def checkpoint_design"):][:500]
        self.assertIn('_budget_decision("checkpoint_design")', block)
        self.assertIn('design_math_approved = True', block)

    def test_mood_boards_has_budget_decision(self):
        block = pipeline_src[pipeline_src.index("def run_mood_boards"):][:800]
        self.assertIn('_budget_decision("mood_boards")', block)

    def test_mood_boards_skip_auto_approves(self):
        """Mood board skip sets mood_board_approved so production proceeds."""
        block = pipeline_src[pipeline_src.index("def run_mood_boards"):][:1000]
        self.assertIn("mood_board_approved = True", block)

    def test_mood_boards_lite_reduces_variants(self):
        """Lite mode generates 1 variant instead of PipelineConfig.MOOD_BOARD_VARIANTS."""
        block = pipeline_src[pipeline_src.index("def run_mood_boards"):][:2500]
        self.assertIn("variants = 1 if is_lite", block)

    def test_mood_boards_lite_skips_vision_qa(self):
        """Lite mode skips vision QA re-checks."""
        block = pipeline_src[pipeline_src.index("def run_mood_boards"):][:2500]
        self.assertIn("Skip vision_qa for speed", block)

    def test_checkpoint_art_has_budget_decision(self):
        block = pipeline_src[pipeline_src.index("def checkpoint_art"):][:500]
        self.assertIn('_budget_decision("checkpoint_art")', block)
        self.assertIn('mood_board_approved = True', block)

    def test_p0_stages_no_budget_decision(self):
        """P0 stages (initialize, design_and_math, production, assemble_package) 
        should NOT have budget_decision calls (they always run)."""
        for stage in ["def initialize(", "def run_design_and_math(", 
                       "def run_production(", "def assemble_package("]:
            idx = pipeline_src.index(stage)
            # Look at next 400 chars after the def
            block = pipeline_src[idx:idx+400]
            self.assertNotIn("_budget_decision", block,
                           f"P0 stage {stage} should not have _budget_decision")


# ════════════════════════════════════════════════════════════════
# E) Resume Integration
# ════════════════════════════════════════════════════════════════

class TestResumeIntegration(unittest.TestCase):
    """Budget decisions work correctly during resume."""

    def test_resume_previews_budget_decisions(self):
        """resume_from_stage prints budget decision preview for remaining stages."""
        idx = pipeline_src.index("def resume_from_stage")
        block = pipeline_src[idx:idx+3000]
        self.assertIn("_budget_decision(s)", block)
        self.assertIn("SKIP", block)
        self.assertIn("LITE", block)
        self.assertIn("RUN", block)

    def test_resume_skip_summary(self):
        """resume_from_stage prints skip summary on completion."""
        start = pipeline_src.index("def resume_from_stage")
        end = pipeline_src.index("\n    # ---- Stage 2:", start + 10)
        block = pipeline_src[start:end]
        self.assertIn("Budget-skipped", block)
        self.assertIn("skipped_stages", block)


# ════════════════════════════════════════════════════════════════
# F) PACKAGE_MANIFEST Enrichment
# ════════════════════════════════════════════════════════════════

class TestManifest(unittest.TestCase):
    """PACKAGE_MANIFEST.json checkpoint block is enriched with skip info."""

    def test_smart_skip_summary_in_manifest(self):
        self.assertIn('"smart_skip_summary"', pipeline_src)

    def test_manifest_has_stages_skipped_count(self):
        self.assertIn('"stages_skipped"', pipeline_src)

    def test_manifest_has_skipped_list(self):
        self.assertIn('"skipped_list"', pipeline_src)

    def test_manifest_has_priority_map(self):
        self.assertIn('"priority_map_used"', pipeline_src)

    def test_manifest_has_budget_pressure(self):
        self.assertIn('"budget_pressure"', pipeline_src)

    def test_completion_panel_shows_skips(self):
        """Completion Panel in assemble_package shows skipped stages."""
        # The skip info is in the Panel text before "Package Complete" title
        idx = pipeline_src.index("Package Complete")
        # Look backwards ~1000 chars to find skipped_stages reference
        block = pipeline_src[idx-1000:idx+200]
        self.assertIn("skipped_stages", block)

    def test_skip_metric_emitted(self):
        """A 'skipped' metric is emitted on completion if stages were skipped."""
        self.assertIn('emit("metric", key="skipped"', pipeline_src)


# ════════════════════════════════════════════════════════════════
# G) UI: CSS, JS, Timeline
# ════════════════════════════════════════════════════════════════

class TestUI(unittest.TestCase):
    """Test CSS styling, JS event handlers, and timeline visualization."""

    def test_ev_skip_css(self):
        """CSS class .ev-skip exists with amber styling."""
        self.assertIn(".ev-skip{", webapp_src)
        self.assertIn("f59e0b", webapp_src.split(".ev-skip{")[1][:200])

    def test_ev_compress_css(self):
        """CSS class .ev-compress exists with cyan styling."""
        self.assertIn(".ev-compress{", webapp_src)
        self.assertIn("06b6d4", webapp_src.split(".ev-compress{")[1][:200])

    def test_skip_label_css(self):
        self.assertIn(".skip-label{", webapp_src)

    def test_compress_label_css(self):
        self.assertIn(".compress-label{", webapp_src)

    def test_skip_reason_css(self):
        self.assertIn(".skip-reason{", webapp_src)

    def test_skip_budget_css(self):
        self.assertIn(".skip-budget{", webapp_src)

    def test_timeline_skipped_css(self):
        """Timeline dot has .pl-stage.skipped style."""
        self.assertIn(".pl-stage.skipped", webapp_src)
        self.assertIn("line-through", webapp_src)

    def test_js_stage_skip_handler(self):
        """thought-feed.js has case 'stage_skip' handler."""
        self.assertIn('case "stage_skip":', tfeed_src)
        self.assertIn("skip-label", tfeed_src)

    def test_js_stage_compress_handler(self):
        """thought-feed.js has case 'stage_compress' handler."""
        self.assertIn('case "stage_compress":', tfeed_src)
        self.assertIn("compress-label", tfeed_src)

    def test_js_skipSt_function(self):
        """thought-feed.js has skipSt function for timeline marking."""
        self.assertIn("function skipSt", tfeed_src)
        self.assertIn('"skipped"', tfeed_src)

    def test_js_skipMap_covers_stages(self):
        """skipMap maps stage names to timeline indices."""
        self.assertIn("skipMap", tfeed_src)
        for stage in ["preflight", "research", "mood_boards", "production"]:
            self.assertIn(stage, tfeed_src.split("skipMap")[1][:300])

    def test_js_stage_skip_calls_skipSt(self):
        """stage_skip event handler calls skipSt()."""
        skip_handler = tfeed_src[tfeed_src.index('case "stage_skip":'):][:200]
        self.assertIn("skipSt(", skip_handler)

    def test_js_setSt_preserves_skipped(self):
        """setSt function preserves .skipped class when advancing timeline."""
        setSt_block = tfeed_src[tfeed_src.index("function setSt"):][:500]
        self.assertIn("skipped", setSt_block)

    def test_skip_tag_shows_priority(self):
        """Skip event card shows priority tag (e.g., P1, P2)."""
        idx = tfeed_src.index('case "stage_skip":')
        skip_handler = tfeed_src[idx:idx+500]
        self.assertIn("skip-tag", skip_handler)
        self.assertIn("e.priority", skip_handler)

    def test_compress_shows_time_estimates(self):
        """Compress event shows full → lite time estimate."""
        idx = tfeed_src.index('case "stage_compress":')
        compress_handler = tfeed_src[idx:idx+600]
        self.assertIn("full_estimate", compress_handler)
        self.assertIn("lite_estimate", compress_handler)


# ════════════════════════════════════════════════════════════════
# H) Research mode tracking
# ════════════════════════════════════════════════════════════════

class TestResearchModeTracking(unittest.TestCase):
    """Research and mood board outputs track their execution mode."""

    def test_research_stores_mode(self):
        """market_research dict includes 'mode' key."""
        start = pipeline_src.index("def run_research")
        end = pipeline_src.index("\n    @listen(run_research)", start + 10)
        block = pipeline_src[start:end]
        self.assertIn('"mode":', block)
        self.assertIn("lite", block)
        self.assertIn("full", block)

    def test_mood_board_stores_mode(self):
        """mood_board dict includes 'mode' key."""
        idx = pipeline_src.index("def run_mood_boards")
        block = pipeline_src[idx:idx+5000]
        self.assertIn('"mode":', block)
        self.assertIn("lite", block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
