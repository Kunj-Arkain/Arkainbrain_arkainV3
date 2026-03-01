# PHASE 12A: FULL SEND — Tier 3 Max Performance

## Summary
GPT-5.2 for precision agents, 128K output tokens, 10M sim spins, 6 new parallel blocks,
1 new agent (GDD Proofreader). Target: **~55% wall-clock time reduction** from original.

## Timing Comparison

| Stage | Phase 11 | Phase 12 | Phase 12A | Savings vs 11 |
|-------|----------|----------|-----------|---------------|
| Preflight | ~8s | ~8s | ~8s | — |
| Research | ~15 min | ~8 min | ~8 min | **7 min** |
| GDD + Math initial | ~20 min | ~20 min | ~13 min (∥ + proofread) | **7 min** |
| OODA convergence | 3 loops × 15 min | 3 loops × 10 min | 1 loop × 7 min (∥ scan + 10M spins) | **38 min** |
| Mood boards | ~10 min | ~10 min | ~3 min (4-way ∥) | **7 min** |
| Production | ~20 min | ~12 min | ~12 min | **8 min** |
| Assembly | ~5 min | ~5 min | ~2.5 min (∥ proto+exports) | **2.5 min** |
| **TOTAL** | **~78 min** | **~53 min** | **~35 min** | **~43 min (55%)** |

Key insight: 10M spins + GDD proofreader reduces OODA loops from 3 → 1 in most cases.

## Model Routing (Tier 3 GPT-5.2)

| Role | Phase 12 | Phase 12A | Why |
|------|----------|-----------|-----|
| Lead Producer | GPT-5 | **GPT-5.2** | Convergence decisions need best reasoning |
| Mathematician | GPT-5 | **GPT-5.2** | RTP calculations demand precision |
| Compliance | GPT-5 | **GPT-5.2** | Legal analysis needs strongest reasoning |
| All others | GPT-5 | GPT-5 | Creative quality already excellent |
| Validators | GPT-5-mini | GPT-5-mini | Speed > depth for checks |

All agents: **128,000 max output tokens** (GPT-5 family max).
Token budgets: **4x increased** (Tier 3 at 2M TPM handles it easily).

## New Agent: GDD Proofreader (Elena Vasquez)
- **Role**: Fast gap detection between GDD and math model after parallel initial pass
- **Model**: GPT-5-mini (speed-critical, structured validation)
- **Purpose**: Catches symbol mismatches, RTP budget gaps, feature inconsistencies
  BEFORE OODA loop starts → reduces loops from 3 → 1
- Produces `proofread_report.json` with issues for designer and mathematician

## New Parallelization Blocks (4)

### 1. GDD ∥ Math Scaffold → Proofreader Reconcile
```
BEFORE: GDD (10 min) → Math waits for GDD (10 min) = 20 min
AFTER:  [GDD ∥ Math scaffold] → Proofreader → Quick fixes = 13 min
```
Math builds from GameIdeaInput directly (grid, RTP, volatility, features).
Proofreader reconciles differences. Quick parallel fix pass if needed.

### 2. Compliance Scan ∥ Math Validation (in OODA loop)
```
BEFORE: Compliance scan (5 min) → then Producer assesses
AFTER:  [Compliance scan ∥ Math validation] → Producer merges both
```
Saves ~3 min per OODA loop iteration.

### 3. Mood Board Variants (4-way parallel)
```
BEFORE: 1 agent generates variant 1 → 2 → 3 → 4 sequentially (~10 min)
AFTER:  [V1 ∥ V2 ∥ V3 ∥ V4] each with distinct style direction (~3 min)
```
Style directions: Bold/vivid, Elegant/refined, Dark/atmospheric, Whimsical/playful.

### 4. Assembly: Prototype ∥ Revenue+Exports
```
BEFORE: Prototype → Revenue → Exports → PDFs (sequential, ~5 min)
AFTER:  [Prototype ∥ Revenue+Exports] → PDFs (~2.5 min)
```

## Other Changes
- Simulation spins: 1M → **10M** (better RTP convergence, fewer OODA loops)
- Pipeline version: 4.0.0 → 5.0.0
- Manifest includes `premium_model`, `animation_package` tracking
- Total agents: 12 (6 core + 6 specialist)
- Total parallel blocks in pipeline: 9

## Cost Impact
Estimated per-run: **$8-15** (GPT-5.2 adds ~$2-3 premium over GPT-5).
Well under $20 ceiling. The time savings (43 min) vastly outweigh the cost increase.

## Files Modified
- `config/settings.py` — GPT-5.2 routing, 128K tokens, 10M spins, proofreader config
- `flows/pipeline.py` — +921 lines: proofreader agent, 4 new parallel blocks, refactored assembly
