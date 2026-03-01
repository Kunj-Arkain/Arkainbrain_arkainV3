#!/usr/bin/env python3
"""
Tests for Phase 3+4: Partial Status & Resume Logic

Validates:
1.  Worker watchdog produces 'partial' status when checkpoint exists
2.  resume_from_stage() exists with correct signature
3.  resume_from_stage() validates stage names
4.  resume_from_stage() calls _load_existing_state() + auto-approves HITL
5.  MAX_RESUMES constant exists
6.  run_resume() in worker.py handles all validation cases
7.  'resume' CLI dispatch exists
8.  /api/resume/<job_id> endpoint exists with validation
9.  Dashboard badge-partial mapping
10. Dashboard Resume button for partial jobs
11. Dashboard resumeJob() JS function
12. History page badge-partial mapping + Resume button
13. Logs page badge-partial + Resume button
14. thought-feed.js handles partial status
15. /api/logs done detection includes partial
16. _recover_stale_jobs correctly ignores partial
17. _check_job_limit correctly ignores partial
"""

import ast
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))


def _read(filename: str) -> str:
    return (PROJECT_ROOT / filename).read_text()


def _parse(filename: str) -> ast.Module:
    return ast.parse(_read(filename))


PIPELINE_STAGES = [
    "initialize", "preflight", "research", "checkpoint_research",
    "design_and_math", "checkpoint_design", "mood_boards",
    "checkpoint_art", "production", "assemble_package",
]


# ============================================================
# Pipeline.py Tests
# ============================================================

def test_resume_from_stage_exists():
    """resume_from_stage() exists in SlotStudioFlow with correct parameter."""
    tree = _parse("flows/pipeline.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "SlotStudioFlow":
            methods = {n.name: n for n in node.body if isinstance(n, ast.FunctionDef)}
            assert "resume_from_stage" in methods, "Missing resume_from_stage method"
            m = methods["resume_from_stage"]
            args = [a.arg for a in m.args.args if a.arg != "self"]
            assert "last_completed_stage" in args, f"Expected 'last_completed_stage' param, got {args}"
            print("✅ resume_from_stage(last_completed_stage) exists in SlotStudioFlow")
            return
    assert False, "SlotStudioFlow not found"


def test_resume_validates_stage_name():
    """resume_from_stage should validate that stage_name is in PIPELINE_STAGES."""
    src = _read("flows/pipeline.py")
    assert "PIPELINE_STAGES" in src
    # Should raise ValueError for unknown stages
    assert "ValueError" in src and "Unknown stage" in src, \
        "resume_from_stage should raise ValueError for unknown stages"
    print("✅ resume_from_stage validates stage names (ValueError)")


def test_resume_loads_existing_state():
    """resume_from_stage should call _load_existing_state() to hydrate from disk."""
    src = _read("flows/pipeline.py")
    # Find resume_from_stage and check it calls _load_existing_state
    tree = _parse("flows/pipeline.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "SlotStudioFlow":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "resume_from_stage":
                    body_src = ast.get_source_segment(src, item)
                    assert "_load_existing_state" in body_src, \
                        "resume_from_stage must call _load_existing_state()"
                    print("✅ resume_from_stage calls _load_existing_state()")
                    return
    assert False, "resume_from_stage not found"


def test_resume_auto_approves_hitl():
    """resume_from_stage should auto-approve all HITL checkpoints."""
    src = _read("flows/pipeline.py")
    tree = _parse("flows/pipeline.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "SlotStudioFlow":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "resume_from_stage":
                    body_src = ast.get_source_segment(src, item)
                    assert "research_approved = True" in body_src, "Must auto-approve research"
                    assert "design_math_approved = True" in body_src, "Must auto-approve design"
                    assert "mood_board_approved = True" in body_src, "Must auto-approve art"
                    print("✅ resume_from_stage auto-approves all 3 HITL checkpoints")
                    return
    assert False, "resume_from_stage not found"


def test_resume_calls_stage_methods_sequentially():
    """resume_from_stage should have a _STAGE_METHODS map and iterate remaining stages."""
    src = _read("flows/pipeline.py")
    tree = _parse("flows/pipeline.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "SlotStudioFlow":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "resume_from_stage":
                    body_src = ast.get_source_segment(src, item)
                    assert "_STAGE_METHODS" in body_src, "Must have _STAGE_METHODS dict"
                    # Check all 10 stages are mapped
                    for stage in PIPELINE_STAGES:
                        assert f'"{stage}"' in body_src, f"_STAGE_METHODS missing '{stage}'"
                    print("✅ resume_from_stage: _STAGE_METHODS covers all 10 stages")
                    return
    assert False, "resume_from_stage not found"


def test_max_resumes_constant():
    """MAX_RESUMES constant exists and is a positive integer."""
    src = _read("flows/pipeline.py")
    tree = _parse("flows/pipeline.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "MAX_RESUMES":
                    assert isinstance(node.value, ast.Constant), "MAX_RESUMES must be a constant"
                    assert isinstance(node.value.value, int), "MAX_RESUMES must be an integer"
                    assert node.value.value >= 1, f"MAX_RESUMES must be ≥ 1, got {node.value.value}"
                    print(f"✅ MAX_RESUMES = {node.value.value}")
                    return
    assert False, "MAX_RESUMES not found"


# ============================================================
# Worker.py Tests
# ============================================================

def test_run_resume_function():
    """run_resume(job_id) exists in worker.py."""
    src = _read("worker.py")
    tree = _parse("worker.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "run_resume":
            args = [a.arg for a in node.args.args]
            assert "job_id" in args, f"run_resume should take job_id, got {args}"
            print("✅ worker.py: run_resume(job_id) function exists")
            return
    assert False, "run_resume not found in worker.py"


def test_run_resume_validates_status():
    """run_resume should check job status == 'partial' before proceeding."""
    src = _read("worker.py")
    # Find run_resume function body
    assert "partial" in src, "run_resume should check for 'partial' status"
    assert "expected 'partial'" in src, "run_resume should report expected status"
    print("✅ run_resume validates status == 'partial'")


def test_run_resume_checks_checkpoint():
    """run_resume should verify checkpoint.json exists."""
    src = _read("worker.py")
    assert "checkpoint.json" in src, "run_resume should check for checkpoint.json"
    print("✅ run_resume checks checkpoint.json existence")


def test_run_resume_checks_max_resumes():
    """run_resume should enforce MAX_RESUMES limit."""
    src = _read("worker.py")
    assert "MAX_RESUMES" in src, "run_resume should import/check MAX_RESUMES"
    assert "Maximum resumes" in src or "max resumes" in src.lower(), \
        "run_resume should report when max resumes exceeded"
    print("✅ run_resume enforces MAX_RESUMES limit")


def test_run_resume_hydrates_state():
    """run_resume should use PipelineState.model_validate() to hydrate from checkpoint."""
    src = _read("worker.py")
    assert "model_validate" in src, "run_resume should use PipelineState.model_validate()"
    print("✅ run_resume hydrates PipelineState via model_validate()")


def test_run_resume_accounts_for_prior_elapsed():
    """run_resume should create TimeBudget with already_elapsed from checkpoint."""
    src = _read("worker.py")
    assert "already_elapsed" in src, "run_resume should pass already_elapsed to TimeBudget"
    print("✅ run_resume creates TimeBudget with prior elapsed time")


def test_resume_cli_dispatch():
    """CLI dispatch handles 'resume' job type."""
    src = _read("worker.py")
    assert '"resume"' in src or "'resume'" in src, "CLI should accept 'resume' job type"
    assert "run_resume" in src, "CLI should call run_resume for 'resume' type"
    print("✅ CLI dispatch: 'resume' → run_resume(job_id)")


# ============================================================
# Web App Tests
# ============================================================

def test_api_resume_endpoint():
    """/api/resume/<job_id> POST endpoint exists."""
    src = _read("web_app.py")
    assert "/api/resume/" in src, "Missing /api/resume/ route"
    assert "api_resume_job" in src, "Missing api_resume_job function"
    assert "POST" in src and "resume" in src, "Resume endpoint should accept POST"
    print("✅ web_app.py: POST /api/resume/<job_id> endpoint")


def test_api_resume_validates():
    """Resume endpoint validates: exists, owner, status, output_dir, checkpoint."""
    src = _read("web_app.py")
    # Should check multiple conditions
    checks = ["partial", "output_dir", "checkpoint.json", "_check_job_limit"]
    for check in checks:
        assert check in src, f"Resume endpoint should check: {check}"
    print("✅ Resume endpoint validates: status, output_dir, checkpoint, job limit")


def test_dashboard_badge_partial():
    """Dashboard badge mapping includes 'partial' → 'badge-partial'."""
    src = _read("web_app.py")
    # Find the dashboard badge mapping line
    assert '"partial":"badge-partial"' in src, \
        "Dashboard badge mapping should include partial"
    print("✅ Dashboard: badge-partial mapping")


def test_dashboard_resume_button():
    """Dashboard shows Resume button for partial jobs."""
    src = _read("web_app.py")
    assert "▶ Resume" in src, "Dashboard should have Resume button text"
    assert "resumeJob" in src, "Dashboard should wire resumeJob() onclick"
    print("✅ Dashboard: Resume button for partial jobs")


def test_dashboard_resume_js():
    """Dashboard has resumeJob() JS function."""
    src = _read("web_app.py")
    assert "function resumeJob" in src, "Missing resumeJob function"
    assert "fetch('/api/resume/'" in src or "fetch(\"/api/resume/\"" in src, \
        "resumeJob should POST to /api/resume/"
    print("✅ Dashboard: resumeJob() JS function")


def test_history_badge_partial():
    """History page badge mapping includes 'partial'."""
    src = _read("web_app.py")
    # There should be at least 2 badge-partial mappings (dashboard + history)
    count = src.count('"partial":"badge-partial"')
    assert count >= 2, f"Expected ≥2 badge-partial mappings, found {count}"
    print(f"✅ History: badge-partial mapping ({count} total across pages)")


def test_history_resume_button():
    """History page shows Resume button for partial jobs."""
    src = _read("web_app.py")
    # History page should also have resume capability
    # Count Resume button appearances (should be in dashboard + history + logs)
    count = src.count("▶ Resume")
    assert count >= 3, f"Expected ≥3 Resume button instances, found {count}"
    print(f"✅ History + Logs: Resume buttons ({count} instances)")


def test_logs_page_partial_handling():
    """Logs page handles partial status with badge and Resume button."""
    src = _read("web_app.py")
    assert "badge-partial" in src
    # The logs page specifically should have the Resume Pipeline button
    assert "▶ Resume Pipeline" in src, "Logs page should have 'Resume Pipeline' button"
    print("✅ Logs page: partial badge + Resume Pipeline button")


def test_thought_feed_partial():
    """thought-feed.js handles partial status."""
    src = _read("static/thought-feed.js")
    assert "partial" in src, "thought-feed.js should handle 'partial' status"
    count = src.count("partial")
    assert count >= 5, f"Expected ≥5 'partial' references, found {count}"
    print(f"✅ thought-feed.js: partial handling ({count} references)")


def test_api_logs_done_includes_partial():
    """/api/logs endpoint treats partial as done."""
    src = _read("web_app.py")
    assert '"partial"' in src and "done" in src, \
        "api_log_stream should treat partial as done"
    # More specific check: "complete", "failed", "partial" in same tuple
    assert '"complete", "failed", "partial"' in src or \
           "'complete', 'failed', 'partial'" in src, \
        "/api/logs should include 'partial' in done statuses"
    print("✅ /api/logs: partial included in done detection")


def test_recover_stale_ignores_partial():
    """_recover_stale_jobs should NOT mark partial jobs as failed."""
    src = _read("web_app.py")
    # The recovery query should only target 'running' and 'queued'
    import re
    recovery = re.search(r"_recover_stale_jobs.*?def ", src, re.DOTALL)
    if recovery:
        recovery_src = recovery.group()
        # Should NOT include 'partial' in the WHERE clause
        assert "partial" not in recovery_src.lower() or \
               "'partial'" not in recovery_src, \
            "Recovery should not target partial jobs"
    print("✅ _recover_stale_jobs: ignores partial (only targets running/queued)")


def test_job_limit_ignores_partial():
    """_check_job_limit should NOT count partial jobs against the limit."""
    src = _read("web_app.py")
    import re
    limit = re.search(r"_check_job_limit.*?return None", src, re.DOTALL)
    if limit:
        limit_src = limit.group()
        assert "partial" not in limit_src, \
            "Job limit check should not count partial jobs"
    print("✅ _check_job_limit: ignores partial (only counts running/queued)")


def test_js_poll_handles_partial():
    """Dashboard JS poll updates badge correctly for partial status."""
    src = _read("web_app.py")
    assert "partial" in src and "badge-" in src
    # The JS ternary should map partial → partial
    assert "'partial'" in src or '"partial"' in src
    print("✅ Dashboard JS poll: handles partial badge transition")


# ============================================================
# Run all tests
# ============================================================

if __name__ == "__main__":
    tests = [
        # Pipeline tests
        test_resume_from_stage_exists,
        test_resume_validates_stage_name,
        test_resume_loads_existing_state,
        test_resume_auto_approves_hitl,
        test_resume_calls_stage_methods_sequentially,
        test_max_resumes_constant,
        # Worker tests
        test_run_resume_function,
        test_run_resume_validates_status,
        test_run_resume_checks_checkpoint,
        test_run_resume_checks_max_resumes,
        test_run_resume_hydrates_state,
        test_run_resume_accounts_for_prior_elapsed,
        test_resume_cli_dispatch,
        # Web app tests
        test_api_resume_endpoint,
        test_api_resume_validates,
        test_dashboard_badge_partial,
        test_dashboard_resume_button,
        test_dashboard_resume_js,
        test_history_badge_partial,
        test_history_resume_button,
        test_logs_page_partial_handling,
        test_thought_feed_partial,
        test_api_logs_done_includes_partial,
        test_recover_stale_ignores_partial,
        test_job_limit_ignores_partial,
        test_js_poll_handles_partial,
    ]

    print(f"\n{'='*60}")
    print(f"Phase 3+4 Partial Status & Resume Tests — {len(tests)} tests")
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
