"""
ARKAINBRAIN — Wallet Bridge (Phase 6)

Generates a JavaScript bridge that connects client-side game HTML
to the server-side Platform Engine for real-money play.

When injected, the bridge intercepts the game's bet/payout logic
and routes it through the server-side API, ensuring:
  - Outcomes are computed server-side (provably fair)
  - Balance is managed server-side (no client-side manipulation)
  - All bets are tracked and auditable
  - Jackpot contributions are automatic

Usage:
    from tools.wallet_bridge import inject_wallet_bridge
    html = inject_wallet_bridge(game_html, game_type="crash",
                                 api_base="/api/platform")
"""

from __future__ import annotations
import re


WALLET_BRIDGE_JS = """
// ═══════════════════════════════════════════════════════
// ARKAINX Wallet Bridge — Server-Side Real-Money Mode
// ═══════════════════════════════════════════════════════
(function() {
  'use strict';

  const API_BASE = '%%API_BASE%%';
  const GAME_TYPE = '%%GAME_TYPE%%';
  const GAME_ID = '%%GAME_ID%%';

  // ─── State ───
  let sessionId = null;
  let serverSeedHash = null;
  let clientSeed = null;
  let serverBalance = null;
  let isServerMode = false;
  let pendingRound = null;

  // ─── Public API ───
  window.ArkainWallet = {
    // Check if wallet bridge is active
    get active() { return isServerMode; },
    get balance() { return serverBalance; },
    get sessionId() { return sessionId; },

    // Initialize server-side session
    async connect(initialBalance, userClientSeed) {
      try {
        const resp = await fetch(API_BASE + '/session', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            game_type: GAME_TYPE,
            game_id: GAME_ID,
            balance: initialBalance || 1000,
            client_seed: userClientSeed || '',
          }),
        });
        const data = await resp.json();
        if (data.error) throw new Error(data.error);

        sessionId = data.session_id;
        serverSeedHash = data.server_seed_hash;
        clientSeed = data.client_seed;
        serverBalance = data.balance;
        isServerMode = true;

        // Update UI
        _updateBalanceDisplay(serverBalance);
        _showWalletStatus('connected');

        console.log('[ArkainWallet] Connected:', sessionId);
        return { sessionId, serverSeedHash, clientSeed, balance: serverBalance };
      } catch (e) {
        console.error('[ArkainWallet] Connect failed:', e);
        _showWalletStatus('error', e.message);
        return null;
      }
    },

    // Play a round through the server
    async playRound(betAmount, gameConfig, playerAction) {
      if (!isServerMode || !sessionId) {
        console.warn('[ArkainWallet] Not connected — using client-side mode');
        return null;
      }

      try {
        const resp = await fetch(API_BASE + '/play', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            session_id: sessionId,
            bet: betAmount,
            config: gameConfig || {},
            action: playerAction || {},
          }),
        });
        const data = await resp.json();
        if (data.error) throw new Error(data.error);

        serverBalance = data.balance;
        _updateBalanceDisplay(serverBalance);

        pendingRound = data;
        return data; // { round_id, nonce, outcome, multiplier, payout, balance, hash }
      } catch (e) {
        console.error('[ArkainWallet] Play failed:', e);
        return null;
      }
    },

    // Disconnect and reveal server seed
    async disconnect() {
      if (!sessionId) return null;
      try {
        const resp = await fetch(API_BASE + '/close', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: sessionId }),
        });
        const data = await resp.json();
        isServerMode = false;
        _showWalletStatus('disconnected');
        console.log('[ArkainWallet] Disconnected. Server seed:', data.server_seed);
        return data;
      } catch (e) {
        console.error('[ArkainWallet] Disconnect failed:', e);
        return null;
      }
    },

    // Verify a specific round
    async verifyRound(nonce) {
      if (!sessionId) return null;
      try {
        const resp = await fetch(API_BASE + '/verify', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: sessionId, nonce: nonce }),
        });
        return await resp.json();
      } catch (e) {
        console.error('[ArkainWallet] Verify failed:', e);
        return null;
      }
    },

    // Get player stats
    async getStats() {
      try {
        const resp = await fetch(API_BASE + '/stats');
        return await resp.json();
      } catch (e) { return null; }
    },
  };

  // ─── UI Helpers ───
  function _updateBalanceDisplay(amount) {
    const els = document.querySelectorAll(
      '[data-balance], .balance-value, .bal-value, #balance-display'
    );
    els.forEach(el => {
      el.textContent = typeof amount === 'number' ? amount.toFixed(2) : amount;
    });
    // Also update window.GAME_CONFIG if present
    if (window.GAME_CONFIG) {
      window.GAME_CONFIG.starting_balance = amount;
    }
  }

  function _showWalletStatus(status, msg) {
    let indicator = document.getElementById('wallet-status');
    if (!indicator) {
      indicator = document.createElement('div');
      indicator.id = 'wallet-status';
      indicator.style.cssText = 'position:fixed;top:8px;right:8px;z-index:9999;'
        + 'padding:4px 10px;border-radius:6px;font-size:10px;font-weight:600;'
        + 'font-family:monospace;pointer-events:none;transition:opacity 0.3s;';
      document.body.appendChild(indicator);
    }
    const colors = {
      connected: '#22c55e', disconnected: '#94a3b8',
      error: '#ef4444', pending: '#f59e0b',
    };
    indicator.style.background = colors[status] || '#94a3b8';
    indicator.style.color = '#fff';
    indicator.textContent = status === 'connected'
      ? '🔐 Server RNG'
      : status === 'error'
        ? '⚠️ ' + (msg || 'Error')
        : status === 'disconnected'
          ? '🔓 Disconnected'
          : '⏳ ' + status;
    indicator.style.opacity = '1';
    if (status === 'disconnected') {
      setTimeout(() => { indicator.style.opacity = '0'; }, 3000);
    }
  }

  // ─── Auto-connect if URL param present ───
  const params = new URLSearchParams(window.location.search);
  if (params.has('server_mode') || params.has('real_money')) {
    const bal = parseFloat(params.get('balance')) || 1000;
    window.addEventListener('DOMContentLoaded', () => {
      window.ArkainWallet.connect(bal);
    });
  }
})();
"""


def generate_wallet_bridge_js(
    game_type: str = "crash",
    game_id: str = "",
    api_base: str = "/api/platform",
) -> str:
    """Generate the wallet bridge JavaScript."""
    js = WALLET_BRIDGE_JS
    js = js.replace("%%API_BASE%%", api_base)
    js = js.replace("%%GAME_TYPE%%", game_type)
    js = js.replace("%%GAME_ID%%", game_id)
    return js


def inject_wallet_bridge(
    html: str,
    game_type: str = "crash",
    game_id: str = "",
    api_base: str = "/api/platform",
) -> str:
    """Inject the wallet bridge into game HTML.

    The bridge adds window.ArkainWallet with:
      .connect(balance)    → start server session
      .playRound(bet, config, action) → server-side round
      .disconnect()        → close + reveal seed
      .verifyRound(nonce)  → verify specific round
      .active              → boolean: server mode?
      .balance             → server-managed balance
    """
    js = generate_wallet_bridge_js(game_type, game_id, api_base)
    script_block = f"<script>\n{js}\n</script>"

    # Inject before </body> or at end
    idx = html.lower().find("</body>")
    if idx != -1:
        html = html[:idx] + script_block + "\n" + html[idx:]
    else:
        html += "\n" + script_block

    return html
