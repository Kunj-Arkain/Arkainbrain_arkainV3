// ARCADE CONFIG LOADER v1.0
// Every game reads config from window.GAME_CONFIG (injected by server).
// This loader provides typed accessors with safe fallbacks.
// Usage: const CFG = new GameConfig();  CFG.math.target_rtp  // 96.0
"use strict";

class GameConfig {
  constructor() {
    const raw = window.GAME_CONFIG || {};
    this.version   = raw.version || "1.0.0";
    this.game_type = raw.game_type || "unknown";
    this.theme     = raw.theme || {};
    this.math      = raw.math || {};
    this.physics   = raw.physics || {};
    this.audio     = raw.audio || {};
    this.compliance= raw.compliance || {};
    this._raw = raw;
  }

  // ── Theme accessors ──
  get title()      { return this.theme.title || this.theme.name || "ARCADE" }
  get subtitle()   { return this.theme.subtitle || "" }
  get icon()       { return this.theme.icon || "🎮" }
  get titleFont()  { return this.theme.title_font || "Inter" }
  get bodyFont()   { return this.theme.body_font || "Inter" }
  get primary()    { return this.theme.primary || "#6366f1" }
  get secondary()  { return this.theme.secondary || "#06b6d4" }

  // ── Math accessors ──
  get rtp()           { return this.math.target_rtp || 96.0 }
  get houseEdge()     { return this.math.house_edge || 0.04 }
  get betOptions()    { return this.math.bet_options || [0.10,0.25,0.50,1,2,5,10,25] }
  get defaultBet()    { return this.math.default_bet || 1.0 }
  get maxWinMult()    { return this.math.max_win_multiplier || 1000 }
  get startBalance()  { return this.math.starting_balance || 1000 }
  get minBet()        { return this.math.min_bet || 0.10 }
  get maxBet()        { return this.math.max_bet || 100.0 }

  // Crash
  get crashHouseEdge(){ return this.math.crash_house_edge || 0.03 }
  get crashMaxMult()  { return this.math.crash_max_mult || 100 }

  // Plinko
  get plinkoRows()    { return this.math.plinko_rows || 12 }
  get plinkoRisk()    { return this.math.plinko_risk_profiles || {} }

  // Mines
  get minesGrid()     { return this.math.mines_grid_size || 25 }
  get minesCols()     { return this.math.mines_cols || 5 }
  get minesOptions()  { return this.math.mines_options || [1,3,5,10,15] }
  get minesDefault()  { return this.math.mines_default || 5 }
  get minesEdge()     { return this.math.mines_edge_factor || 0.97 }

  // Dice
  get diceEdge()      { return this.math.dice_edge_factor || 0.97 }

  // Wheel
  get wheelSegments() { return this.math.wheel_segments || [] }

  // HiLo
  get hiloDeck()      { return this.math.hilo_deck_size || 52 }
  get hiloValues()    { return this.math.hilo_values || ["A","2","3","4","5","6","7","8","9","10","J","Q","K"] }
  get hiloSuits()     { return this.math.hilo_suits || ["♠","♥","♦","♣"] }

  // Chicken
  get chickenLanes()  { return this.math.chicken_lanes || 9 }
  get chickenCols()   { return this.math.chicken_cols || 4 }
  get chickenHazards(){ return this.math.chicken_hazards_per_lane || 1 }

  // Scratch
  get scratchSymbols(){ return this.math.scratch_symbols || [] }
  get scratchWinChance(){ return this.math.scratch_win_chance || 0.35 }

  // ── Physics ──
  get gravity()       { return this.physics.gravity || 0.3 }
  get bounceDamping() { return this.physics.bounce_damping || 0.6 }
  get pegRadius()     { return this.physics.peg_radius || 4 }
  get ballRadius()    { return this.physics.ball_radius || 6 }

  // ── Compliance ──
  get rngSource()     { return this.compliance.rng_source || "math_random" }
  get isDemo()        { return this.compliance.jurisdiction === "demo" }

  // ── Utility ──
  has(path) {
    return path.split('.').reduce((o,k) => o && o[k], this._raw) !== undefined;
  }

  dump() {
    console.log('[GAME_CONFIG]', JSON.stringify(this._raw, null, 2));
  }
}

// Auto-instantiate
window.CFG = new GameConfig();
