// ARCADE CONFIG BRIDGE v1.0
// Patches hardcoded game constants with values from window.GAME_CONFIG.
// Injected AFTER the game's own <script> block so all vars exist.
// Safe: only patches if GAME_CONFIG is present; no-op otherwise.
"use strict";

(function(){
  const C = window.GAME_CONFIG;
  if (!C) return; // No config injected — game runs with hardcoded defaults

  const M = C.math || {};
  const T = C.theme || {};
  const GT = C.game_type;

  // ── Universal patches ──

  // Balance
  if (typeof balance !== 'undefined' && M.starting_balance)
    balance = M.starting_balance;

  // Default bet
  if (typeof currentBet !== 'undefined' && M.default_bet)
    currentBet = M.default_bet;

  // Bet options — rebuild bet row if exists
  if (M.bet_options && document.getElementById('bet-row')) {
    const betRow = document.getElementById('bet-row');
    betRow.innerHTML = '';
    M.bet_options.forEach(b => {
      const btn = document.createElement('button');
      btn.className = 'bb' + (b === (M.default_bet || 1) ? ' on' : '');
      btn.textContent = '$' + b.toFixed(2);
      btn.onclick = function() {
        currentBet = b;
        document.querySelectorAll('.bb').forEach(x => x.classList.remove('on'));
        btn.classList.add('on');
        if (typeof updateUI === 'function') updateUI();
      };
      betRow.appendChild(btn);
    });
  }

  // Title
  if (T.title) {
    const h1 = document.querySelector('.hdr h1');
    if (h1) h1.innerHTML = T.title;
  }
  if (T.subtitle) {
    const sub = document.querySelector('.hdr .sub');
    if (sub) sub.textContent = T.subtitle;
  }

  // ── Game-specific patches ──

  if (GT === 'crash') {
    // Crash: patch house edge and max multiplier in the round-start function
    if (M.crash_house_edge !== undefined || M.crash_max_mult !== undefined) {
      const _origOnPlay = window.onPlayClick;
      if (typeof _origOnPlay === 'function') {
        // The crash point formula is inline in onPlayClick — we override via prototype
        // Instead, we monkey-patch the crash point generation
        window._crashHE = M.crash_house_edge || 0.03;
        window._crashMax = M.crash_max_mult || 100;
      }
    }
  }

  if (GT === 'mines') {
    // Mines: patch grid size, default mine count, edge factor
    if (M.mines_grid_size && typeof GRID !== 'undefined') {
      // Note: GRID is const — can't reassign. Bridge works via config read pattern.
      // Games refactored to Phase 4 will read from CFG directly.
      // For now, we just patch what we can.
    }
    if (M.mines_default && typeof mineCount !== 'undefined') {
      mineCount = M.mines_default;
      if (typeof updateUI === 'function') updateUI();
    }
  }

  if (GT === 'wheel') {
    // Wheel: patch SEGMENTS array
    if (M.wheel_segments && M.wheel_segments.length > 0) {
      if (typeof SEGMENTS !== 'undefined' && Array.isArray(SEGMENTS)) {
        SEGMENTS.length = 0;
        M.wheel_segments.forEach(s => SEGMENTS.push(s));
      }
    }
  }

  if (GT === 'scratch') {
    // Scratch: patch SYMBOLS array
    if (M.scratch_symbols && M.scratch_symbols.length > 0) {
      if (typeof SYMBOLS !== 'undefined' && Array.isArray(SYMBOLS)) {
        SYMBOLS.length = 0;
        M.scratch_symbols.forEach(s => SYMBOLS.push(s));
      }
    }
  }

  if (GT === 'dice') {
    // Dice: the multiplier formula reads from a local var
    // We override the updateSliderUI function if it exists
    if (M.dice_edge_factor && typeof updateSliderUI === 'function') {
      const _origSliderUI = updateSliderUI;
      window.updateSliderUI = function() {
        const chance = (typeof prediction !== 'undefined' && prediction === 'over')
          ? (100 - threshold) : threshold;
        const mult = chance > 0 ? Math.floor((M.dice_edge_factor * 100 / chance) * 100) / 100 : 0;
        document.getElementById('win-chance').textContent = chance + '%';
        document.getElementById('mult-val').textContent = mult.toFixed(2) + 'x';
        document.getElementById('slider-track').style.setProperty('--split', threshold + '%');
        document.getElementById('slider-thumb').style.left = threshold + '%';
      };
    }
  }

  if (GT === 'chicken') {
    // Chicken: patch lane count and hazards
    if (M.chicken_lanes && typeof totalLanes !== 'undefined')
      totalLanes = M.chicken_lanes;
    if (M.chicken_hazards_per_lane && typeof hazardsPerLane !== 'undefined')
      hazardsPerLane = M.chicken_hazards_per_lane;
    if (M.chicken_cols && typeof COLS !== 'undefined') {
      // COLS is const — handled in Phase 4 refactor
    }
  }

  // ── Refresh UI after patches ──
  if (typeof updateUI === 'function') {
    try { updateUI(false); } catch(e) {}
  }

  console.log(`[CONFIG BRIDGE] Patched ${GT} | RTP=${M.target_rtp}% | hash=${C.config_hash || 'none'}`);
})();
