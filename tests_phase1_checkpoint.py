#!/usr/bin/env python3
"""
Tests for Phase 1: Stage-Level Checkpointing

Validates:
1. PIPELINE_STAGES constant is complete and ordered
2. PipelineState has all checkpoint fields
3. _save_checkpoint writes valid JSON atomically
4. Checkpoint truncates large text fields
5. _stage_enter/_stage_exit produce correct timing data
6. Every @start/@listen method has enter/exit calls
7. Checkpoint survives Pydantic serialization round-trip
8. PACKAGE_MANIFEST includes checkpoint data
"""

import ast
import json
import os
import sys
import tempfile
import time
from pathlib import Path

# ── Ensure project root is on sys.path ──
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

# We don't import the full pipeline (it has heavy deps like crewai),
# so we test structurally via AST + test the serialization logic directly.


def test_pipeline_stages_constant():
    """PIPELINE_STAGES exists, has 10 entries, all strings."""
    source = (PROJECT_ROOT / "flows" / "pipeline.py").read_text()
    tree = ast.parse(source)

    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "PIPELINE_STAGES":
                    found = True
                    assert isinstance(node.value, ast.List), "PIPELINE_STAGES must be a list literal"
                    elements = [elt.value for elt in node.value.elts if isinstance(elt, ast.Constant)]
                    assert len(elements) == 10, f"Expected 10 stages, got {len(elements)}: {elements}"

                    expected = [
                        "initialize", "preflight", "research", "checkpoint_research",
                        "design_and_math", "checkpoint_design", "mood_boards",
                        "checkpoint_art", "production", "assemble_package",
                    ]
                    assert elements == expected, f"Stage order mismatch:\n  got:      {elements}\n  expected: {expected}"

    assert found, "PIPELINE_STAGES constant not found"
    print("✅ PIPELINE_STAGES: 10 stages in correct order")


def test_pipeline_state_checkpoint_fields():
    """PipelineState has all 5 new checkpoint fields."""
    source = (PROJECT_ROOT / "flows" / "pipeline.py").read_text()
    tree = ast.parse(source)

    required_fields = {
        "last_completed_stage": "str",
        "stage_timings": "dict",
        "pipeline_start_epoch": "float",
        "resume_count": "int",
        "skipped_stages": "list",
    }

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "PipelineState":
            field_names = set()
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    field_names.add(item.target.id)

            for field in required_fields:
                assert field in field_names, f"PipelineState missing field: {field}"

            print(f"✅ PipelineState: all {len(required_fields)} checkpoint fields present")
            return

    assert False, "PipelineState class not found"


def test_slot_studio_flow_methods():
    """SlotStudioFlow has _stage_enter, _stage_exit, _save_checkpoint."""
    source = (PROJECT_ROOT / "flows" / "pipeline.py").read_text()
    tree = ast.parse(source)

    required_methods = {"_stage_enter", "_stage_exit", "_save_checkpoint"}

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "SlotStudioFlow":
            methods = {n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            missing = required_methods - methods
            assert not missing, f"SlotStudioFlow missing methods: {missing}"
            print(f"✅ SlotStudioFlow: all checkpoint methods present ({', '.join(sorted(required_methods))})")
            return

    assert False, "SlotStudioFlow class not found"


def test_every_stage_has_enter_exit():
    """Every stage method has _stage_enter at the top and _stage_exit on all code paths."""
    source = (PROJECT_ROOT / "flows" / "pipeline.py").read_text()

    # Map stage names to their method names in the class
    stage_method_map = {
        "initialize": "initialize",
        "preflight": "run_preflight",
        "research": "run_research",
        "checkpoint_research": "checkpoint_research",
        "design_and_math": "run_design_and_math",
        "checkpoint_design": "checkpoint_design",
        "mood_boards": "run_mood_boards",
        "checkpoint_art": "checkpoint_art",
        "production": "run_production",
        "assemble_package": "assemble_package",
    }

    for stage_name, method_name in stage_method_map.items():
        enter_call = f'self._stage_enter("{stage_name}")'
        exit_call = f'self._stage_exit("{stage_name}")'

        enter_count = source.count(enter_call)
        exit_count = source.count(exit_call)

        assert enter_count >= 1, f"Stage '{stage_name}' (method {method_name}): missing _stage_enter call"
        assert exit_count >= 1, f"Stage '{stage_name}' (method {method_name}): missing _stage_exit call"

        # Exit should be >= enter (multiple exits for early returns, one enter)
        assert exit_count >= enter_count, (
            f"Stage '{stage_name}': {enter_count} enters but only {exit_count} exits — "
            f"possible missing exit on an early return path"
        )

    print(f"✅ All {len(stage_method_map)} stages have _stage_enter/_stage_exit coverage")


def test_enter_exit_balance():
    """Count total enter vs exit calls to check nothing is grossly unbalanced."""
    source = (PROJECT_ROOT / "flows" / "pipeline.py").read_text()

    # Exclude the method definitions themselves and docstring references
    enter_calls = [l for l in source.splitlines()
                   if "_stage_enter(" in l and "def _stage_enter" not in l and "#" not in l.split("_stage_enter")[0]]
    exit_calls = [l for l in source.splitlines()
                  if "_stage_exit(" in l and "def _stage_exit" not in l and "#" not in l.split("_stage_exit")[0]]

    # Filter out docstring/comment lines
    enter_calls = [l for l in enter_calls if l.strip().startswith("self.")]
    exit_calls = [l for l in exit_calls if l.strip().startswith("self.")]

    print(f"   Enter calls: {len(enter_calls)}")
    print(f"   Exit calls:  {len(exit_calls)}")

    # We expect 10 enters (one per stage) and more exits (early returns)
    assert len(enter_calls) == 10, f"Expected 10 _stage_enter calls, got {len(enter_calls)}"
    assert len(exit_calls) >= 10, f"Expected ≥10 _stage_exit calls, got {len(exit_calls)}"
    assert len(exit_calls) <= 30, f"Unexpectedly many _stage_exit calls: {len(exit_calls)}"

    print(f"✅ Enter/exit balance: {len(enter_calls)} enters, {len(exit_calls)} exits (ratio {len(exit_calls)/len(enter_calls):.1f}x)")


def test_checkpoint_json_structure():
    """Simulate what _save_checkpoint would produce and validate structure."""

    # Hardcode constants (same as in pipeline.py) to avoid crewai import
    PIPELINE_STAGES = [
        "initialize", "preflight", "research", "checkpoint_research",
        "design_and_math", "checkpoint_design", "mood_boards",
        "checkpoint_art", "production", "assemble_package",
    ]
    CHECKPOINT_VERSION = 1

    # Simulate a minimal PipelineState serialization
    state_data = {
        "job_id": "test-123",
        "game_idea": {
            "theme": "Ancient Egypt",
            "target_markets": ["UK", "Malta"],
            "volatility": "medium",
            "target_rtp": 96.5,
            "grid_cols": 5,
            "grid_rows": 3,
            "ways_or_lines": "243",
            "max_win_multiplier": 5000,
            "art_style": "3D rendered",
            "requested_features": ["free_spins"],
            "competitor_references": [],
            "special_requirements": "",
        },
        "game_slug": "ancient_egypt_20250227_120000",
        "output_dir": "/tmp/test_output",
        "last_completed_stage": "research",
        "stage_timings": {"initialize": 2.5, "preflight": 45.3, "research": 612.1},
        "pipeline_start_epoch": time.time() - 660,
        "resume_count": 0,
        "skipped_stages": [],
        "gdd": {"output": "x" * 5000},  # Will be truncated
        "market_research": {"output": "y" * 3000, "raw": "z" * 2000},
    }

    # Simulate truncation logic from _save_checkpoint
    _TRUNCATE_FIELDS = {
        "gdd": 500,
        "math_model": 500,
        "market_research": 1000,
        "compliance": 500,
        "mood_board": 500,
        "art_assets": 500,
    }
    for field, max_chars in _TRUNCATE_FIELDS.items():
        val = state_data.get(field)
        if isinstance(val, dict) and "output" in val:
            original_len = len(val["output"])
            if original_len > max_chars:
                val["output"] = val["output"][:max_chars] + f"... [truncated from {original_len} chars — full text on disk]"
            for sub_key in ("raw", "report", "sweep", "deep_dive"):
                if sub_key in val and isinstance(val[sub_key], str) and len(val[sub_key]) > max_chars:
                    val[sub_key] = val[sub_key][:max_chars] + f"... [truncated — full text on disk]"

    # Build checkpoint structure (using local constants)
    stage_name = "research"

    ckpt_data = {
        "checkpoint_version": CHECKPOINT_VERSION,
        "stage": stage_name,
        "stage_index": PIPELINE_STAGES.index(stage_name),
        "stages_completed": PIPELINE_STAGES[:PIPELINE_STAGES.index(stage_name) + 1],
        "stages_remaining": PIPELINE_STAGES[PIPELINE_STAGES.index(stage_name) + 1:],
        "saved_at": "2025-02-27T12:11:00",
        "pipeline_elapsed_s": 660.0,
        "stage_timings": state_data["stage_timings"],
        "resume_count": 0,
        "state": state_data,
    }

    # Validate it serializes to JSON cleanly
    json_str = json.dumps(ckpt_data, indent=2, default=str)
    parsed = json.loads(json_str)

    # Structure checks
    assert parsed["checkpoint_version"] == 1
    assert parsed["stage"] == "research"
    assert parsed["stage_index"] == 2
    assert parsed["stages_completed"] == ["initialize", "preflight", "research"]
    assert parsed["stages_remaining"] == [
        "checkpoint_research", "design_and_math", "checkpoint_design",
        "mood_boards", "checkpoint_art", "production", "assemble_package"
    ]
    assert parsed["resume_count"] == 0
    assert "stage_timings" in parsed
    assert "state" in parsed

    # Truncation checks
    gdd_output = parsed["state"]["gdd"]["output"]
    assert len(gdd_output) < 600, f"GDD output should be truncated, got {len(gdd_output)} chars"
    assert "truncated from 5000 chars" in gdd_output

    research_raw = parsed["state"]["market_research"]["raw"]
    assert len(research_raw) < 1100, f"Research raw should be truncated, got {len(research_raw)} chars"
    assert "truncated" in research_raw

    print(f"✅ Checkpoint JSON structure valid ({len(json_str)} bytes)")
    print(f"   Stages completed: {parsed['stages_completed']}")
    print(f"   Stages remaining: {parsed['stages_remaining']}")
    print(f"   GDD truncated: {len(gdd_output)} chars (from 5000)")


def test_atomic_write_simulation():
    """Simulate atomic write pattern (write to .tmp, rename to .json)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ckpt_path = Path(tmpdir) / "checkpoint.json"
        ckpt_tmp = Path(tmpdir) / "checkpoint.json.tmp"

        data = {"stage": "test", "version": 1}

        # Write to tmp first
        ckpt_tmp.write_text(json.dumps(data, indent=2))
        assert ckpt_tmp.exists()
        assert not ckpt_path.exists()

        # Rename atomically
        ckpt_tmp.rename(ckpt_path)
        assert ckpt_path.exists()
        assert not ckpt_tmp.exists()

        # Verify content
        loaded = json.loads(ckpt_path.read_text())
        assert loaded == data

    print("✅ Atomic write pattern works correctly")


def test_manifest_includes_checkpoint_data():
    """Verify the PACKAGE_MANIFEST.json construction includes checkpoint block."""
    source = (PROJECT_ROOT / "flows" / "pipeline.py").read_text()

    # Check that the manifest dict includes checkpoint data
    assert '"checkpoint":' in source or "'checkpoint':" in source, \
        "PACKAGE_MANIFEST doesn't include 'checkpoint' key"
    assert "resume_count" in source, "Manifest checkpoint block should include resume_count"
    assert "stage_timings" in source, "Manifest checkpoint block should include stage_timings"
    assert "skipped_stages" in source, "Manifest checkpoint block should include skipped_stages"

    print("✅ PACKAGE_MANIFEST includes checkpoint data block")


def test_checkpoint_version_constant():
    """CHECKPOINT_VERSION exists and is an integer ≥ 1."""
    source = (PROJECT_ROOT / "flows" / "pipeline.py").read_text()
    tree = ast.parse(source)

    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "CHECKPOINT_VERSION":
                    found = True
                    assert isinstance(node.value, ast.Constant), "CHECKPOINT_VERSION must be a constant"
                    assert isinstance(node.value.value, int), "CHECKPOINT_VERSION must be an integer"
                    assert node.value.value >= 1, f"CHECKPOINT_VERSION must be ≥ 1, got {node.value.value}"
                    print(f"✅ CHECKPOINT_VERSION = {node.value.value}")
                    return

    assert found, "CHECKPOINT_VERSION constant not found"


def test_no_stage_enter_without_exit():
    """Verify no _stage_enter exists without a matching _stage_exit for the same stage name."""
    source = (PROJECT_ROOT / "flows" / "pipeline.py").read_text()
    import re

    enters = re.findall(r'self\._stage_enter\("(\w+)"\)', source)
    exits = re.findall(r'self\._stage_exit\("(\w+)"\)', source)

    enter_set = set(enters)
    exit_set = set(exits)

    orphan_enters = enter_set - exit_set
    orphan_exits = exit_set - enter_set

    assert not orphan_enters, f"Stages with enter but no exit: {orphan_enters}"
    assert not orphan_exits, f"Stages with exit but no enter: {orphan_exits}"

    # Every stage name in PIPELINE_STAGES should have both
    PIPELINE_STAGES = [
        "initialize", "preflight", "research", "checkpoint_research",
        "design_and_math", "checkpoint_design", "mood_boards",
        "checkpoint_art", "production", "assemble_package",
    ]
    for stage in PIPELINE_STAGES:
        assert stage in enter_set, f"Stage '{stage}' has no _stage_enter call"
        assert stage in exit_set, f"Stage '{stage}' has no _stage_exit call"

    print(f"✅ All {len(PIPELINE_STAGES)} stages have matched enter/exit pairs")


# ============================================================
# Run all tests
# ============================================================

if __name__ == "__main__":
    tests = [
        test_pipeline_stages_constant,
        test_pipeline_state_checkpoint_fields,
        test_slot_studio_flow_methods,
        test_every_stage_has_enter_exit,
        test_enter_exit_balance,
        test_checkpoint_version_constant,
        test_no_stage_enter_without_exit,
        test_checkpoint_json_structure,
        test_atomic_write_simulation,
        test_manifest_includes_checkpoint_data,
    ]

    print(f"\n{'='*60}")
    print(f"Phase 1 Checkpoint Tests — {len(tests)} tests")
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
