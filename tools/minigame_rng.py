"""
ARKAINBRAIN — Provably Fair RNG System (Phase 2)

Server-seed + client-seed + nonce system for verifiable random outcomes.
Compatible with GLI-19 (iGaming RNG requirements) and BMM standards.

Architecture:
    Server generates server_seed_hash = SHA-256(server_seed).
    Client provides client_seed (or it's auto-generated).
    For each game round:
        combined = SHA-256(server_seed + ":" + client_seed + ":" + nonce)
        result = derive_outcome(combined, game_type)
    After the game, server_seed is revealed for verification.

Usage:
    from tools.minigame_rng import ProvablyFairRNG, GameRound

    rng = ProvablyFairRNG()
    session = rng.new_session()
    print(f"Server seed hash: {session.server_seed_hash}")  # Share with player

    # Generate outcomes
    result = rng.generate_crash_point(session, nonce=0)
    result = rng.generate_plinko_path(session, nonce=1, rows=12)
    result = rng.generate_dice_roll(session, nonce=2)

    # After game, reveal server_seed for verification
    print(f"Server seed: {session.server_seed}")
    # Player can verify: SHA-256(server_seed) == server_seed_hash
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import struct
import time
from dataclasses import dataclass, field
from typing import Optional


# ═══════════════════════════════════════════════════════════════
# Data Structures
# ═══════════════════════════════════════════════════════════════

@dataclass
class GameSession:
    """A provably fair game session."""
    session_id: str
    server_seed: str          # Secret until session ends
    server_seed_hash: str     # SHA-256 of server_seed (shared upfront)
    client_seed: str          # Player-provided or auto-generated
    nonce: int = 0            # Increments per round
    created_at: float = 0
    rounds: list = field(default_factory=list)

    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.time()


@dataclass
class GameRound:
    """Result of a single game round with full audit trail."""
    session_id: str
    nonce: int
    game_type: str
    combined_hash: str        # The hash used to derive outcome
    raw_value: float          # Float 0-1 derived from hash
    outcome: dict             # Game-specific result (e.g., crash_point, bucket_index)
    timestamp: float = 0

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()

    def verification_data(self) -> dict:
        """Data needed to independently verify this round."""
        return {
            "session_id": self.session_id,
            "nonce": self.nonce,
            "game_type": self.game_type,
            "combined_hash": self.combined_hash,
            "raw_value": self.raw_value,
            "outcome": self.outcome,
            "verification_steps": [
                "1. Compute: combined = HMAC-SHA256(server_seed, client_seed + ':' + str(nonce))",
                "2. Take first 8 hex chars of combined → int value",
                "3. raw_value = int_value / 0x100000000",
                "4. Apply game-specific derivation to get outcome",
            ],
        }

    def to_audit_json(self) -> str:
        return json.dumps(self.verification_data(), indent=2)


# ═══════════════════════════════════════════════════════════════
# Core RNG
# ═══════════════════════════════════════════════════════════════

class ProvablyFairRNG:
    """Provably fair random number generator.

    Uses HMAC-SHA256 for deterministic, verifiable randomness.
    Compatible with standard provably-fair verification tools.
    """

    def new_session(self, client_seed: str = None) -> GameSession:
        """Create a new game session with fresh server seed."""
        server_seed = os.urandom(32).hex()
        server_seed_hash = hashlib.sha256(server_seed.encode()).hexdigest()
        session_id = hashlib.sha256(
            f"{server_seed}:{time.time()}".encode()
        ).hexdigest()[:16]

        if client_seed is None:
            client_seed = os.urandom(16).hex()

        return GameSession(
            session_id=session_id,
            server_seed=server_seed,
            server_seed_hash=server_seed_hash,
            client_seed=client_seed,
        )

    def _derive_hash(self, session: GameSession, nonce: int) -> str:
        """Compute HMAC-SHA256(server_seed, client_seed:nonce)."""
        message = f"{session.client_seed}:{nonce}"
        return hmac.new(
            session.server_seed.encode(),
            message.encode(),
            hashlib.sha256,
        ).hexdigest()

    def _hash_to_float(self, hex_hash: str, offset: int = 0) -> float:
        """Convert 8 hex characters to float in [0, 1)."""
        segment = hex_hash[offset:offset + 8]
        int_val = int(segment, 16)
        return int_val / 0x100000000  # 2^32

    def _hash_to_int(self, hex_hash: str, max_val: int, offset: int = 0) -> int:
        """Convert 8 hex characters to int in [0, max_val)."""
        return int(self._hash_to_float(hex_hash, offset) * max_val)

    # ── Game-Specific Generators ──────────────────────────────

    def generate_crash_point(self, session: GameSession, nonce: int = None,
                             house_edge: float = 0.03,
                             max_mult: float = 100.0) -> GameRound:
        """Generate a provably fair crash point.

        Formula: crashPoint = (1 - house_edge) / (1 - r)
        where r = hash_to_float(HMAC(server_seed, client_seed:nonce))
        If r < house_edge: instant bust at 1.0
        """
        if nonce is None:
            nonce = session.nonce
            session.nonce += 1

        combined = self._derive_hash(session, nonce)
        r = self._hash_to_float(combined)

        if r < house_edge:
            crash_point = 1.0  # Instant bust
        else:
            crash_point = min((1 - house_edge) / (1 - r), max_mult)
            crash_point = math.floor(crash_point * 100) / 100  # Floor to 2dp

        round_data = GameRound(
            session_id=session.session_id,
            nonce=nonce,
            game_type="crash",
            combined_hash=combined,
            raw_value=r,
            outcome={
                "crash_point": crash_point,
                "is_instant_bust": r < house_edge,
                "derivation": f"r={r:.8f}, he={house_edge}, "
                              f"crash={(1-house_edge)/(1-r) if r >= house_edge else 1.0:.4f}",
            },
        )
        session.rounds.append(round_data)
        return round_data

    def generate_plinko_path(self, session: GameSession, nonce: int = None,
                             rows: int = 12) -> GameRound:
        """Generate a provably fair Plinko ball path.

        Each row: 1 bit from the hash determines left (0) or right (1).
        Bucket = number of right bounces (binomial).
        """
        if nonce is None:
            nonce = session.nonce
            session.nonce += 1

        combined = self._derive_hash(session, nonce)

        # Use each bit of the hash to determine left/right
        path = []
        bucket = 0
        hash_bytes = bytes.fromhex(combined)

        for row in range(rows):
            byte_idx = row // 8
            bit_idx = row % 8
            if byte_idx < len(hash_bytes):
                bit = (hash_bytes[byte_idx] >> bit_idx) & 1
            else:
                # If we need more bits than hash provides, extend
                extra = self._derive_hash(session, nonce + 10000 + row)
                bit = int(extra[0], 16) & 1

            direction = "R" if bit else "L"
            path.append(direction)
            bucket += bit

        round_data = GameRound(
            session_id=session.session_id,
            nonce=nonce,
            game_type="plinko",
            combined_hash=combined,
            raw_value=bucket / rows,
            outcome={
                "bucket": bucket,
                "path": "".join(path),
                "rows": rows,
                "derivation": f"path={''.join(path)}, bucket={bucket}/{rows+1}",
            },
        )
        session.rounds.append(round_data)
        return round_data

    def generate_mines_board(self, session: GameSession, nonce: int = None,
                             grid_size: int = 25,
                             mine_count: int = 5) -> GameRound:
        """Generate a provably fair mines board layout.

        Uses Fisher-Yates shuffle seeded by the hash to place mines.
        """
        if nonce is None:
            nonce = session.nonce
            session.nonce += 1

        combined = self._derive_hash(session, nonce)

        # Generate mine positions using hash-seeded shuffle
        positions = list(range(grid_size))
        for i in range(grid_size - 1, 0, -1):
            # Use different hash segments for each swap
            offset = ((grid_size - 1 - i) * 8) % 56
            if offset + 8 > 64:
                # Need more hash bytes — chain with additional nonce
                extra = self._derive_hash(session, nonce + 20000 + i)
                j = self._hash_to_int(extra, i + 1)
            else:
                j = self._hash_to_int(combined, i + 1, offset)
            positions[i], positions[j] = positions[j], positions[i]

        mine_positions = sorted(positions[:mine_count])

        board = [0] * grid_size
        for pos in mine_positions:
            board[pos] = 1

        round_data = GameRound(
            session_id=session.session_id,
            nonce=nonce,
            game_type="mines",
            combined_hash=combined,
            raw_value=mine_positions[0] / grid_size if mine_positions else 0,
            outcome={
                "mine_positions": mine_positions,
                "grid_size": grid_size,
                "mine_count": mine_count,
                "board": board,
                "derivation": f"Fisher-Yates shuffle of [0..{grid_size-1}], "
                              f"first {mine_count} = mines",
            },
        )
        session.rounds.append(round_data)
        return round_data

    def generate_dice_roll(self, session: GameSession,
                           nonce: int = None) -> GameRound:
        """Generate a provably fair dice roll (1-100)."""
        if nonce is None:
            nonce = session.nonce
            session.nonce += 1

        combined = self._derive_hash(session, nonce)
        r = self._hash_to_float(combined)
        roll = int(r * 100) + 1  # 1-100

        round_data = GameRound(
            session_id=session.session_id,
            nonce=nonce,
            game_type="dice",
            combined_hash=combined,
            raw_value=r,
            outcome={
                "roll": roll,
                "derivation": f"r={r:.8f} → roll={roll}",
            },
        )
        session.rounds.append(round_data)
        return round_data

    def generate_wheel_spin(self, session: GameSession,
                            nonce: int = None,
                            n_segments: int = 20) -> GameRound:
        """Generate a provably fair wheel landing segment."""
        if nonce is None:
            nonce = session.nonce
            session.nonce += 1

        combined = self._derive_hash(session, nonce)
        r = self._hash_to_float(combined)
        segment = int(r * n_segments)

        round_data = GameRound(
            session_id=session.session_id,
            nonce=nonce,
            game_type="wheel",
            combined_hash=combined,
            raw_value=r,
            outcome={
                "segment": segment,
                "n_segments": n_segments,
                "derivation": f"r={r:.8f} → segment={segment}/{n_segments}",
            },
        )
        session.rounds.append(round_data)
        return round_data

    def generate_card_draw(self, session: GameSession,
                           nonce: int = None,
                           n_values: int = 13) -> GameRound:
        """Generate a provably fair card draw (HiLo)."""
        if nonce is None:
            nonce = session.nonce
            session.nonce += 1

        combined = self._derive_hash(session, nonce)
        r = self._hash_to_float(combined)
        value = int(r * n_values) + 1  # 1 to n_values

        suits = ["♠", "♥", "♦", "♣"]
        suit_idx = self._hash_to_int(combined, 4, offset=8)

        round_data = GameRound(
            session_id=session.session_id,
            nonce=nonce,
            game_type="hilo",
            combined_hash=combined,
            raw_value=r,
            outcome={
                "value": value,
                "suit": suits[suit_idx],
                "derivation": f"r={r:.8f} → value={value}, suit={suits[suit_idx]}",
            },
        )
        session.rounds.append(round_data)
        return round_data

    def generate_chicken_lane(self, session: GameSession,
                              nonce: int = None,
                              cols: int = 4,
                              hazards: int = 1) -> GameRound:
        """Generate a provably fair hazard position for one chicken lane."""
        if nonce is None:
            nonce = session.nonce
            session.nonce += 1

        combined = self._derive_hash(session, nonce)

        # Pick hazard columns
        hazard_cols = []
        available = list(range(cols))
        for h in range(hazards):
            offset = h * 8
            idx = self._hash_to_int(combined, len(available), offset)
            hazard_cols.append(available.pop(idx))

        round_data = GameRound(
            session_id=session.session_id,
            nonce=nonce,
            game_type="chicken",
            combined_hash=combined,
            raw_value=hazard_cols[0] / cols if hazard_cols else 0,
            outcome={
                "hazard_columns": sorted(hazard_cols),
                "safe_columns": sorted(set(range(cols)) - set(hazard_cols)),
                "cols": cols,
                "derivation": f"hazard at col(s) {hazard_cols}",
            },
        )
        session.rounds.append(round_data)
        return round_data

    def generate_scratch_card(self, session: GameSession,
                              nonce: int = None,
                              n_symbols: int = 7,
                              win_chance: float = 0.35) -> GameRound:
        """Generate a provably fair scratch card outcome."""
        if nonce is None:
            nonce = session.nonce
            session.nonce += 1

        combined = self._derive_hash(session, nonce)
        r = self._hash_to_float(combined)

        is_win = r < win_chance
        if is_win:
            # Determine which symbol wins (use next hash segment)
            win_symbol_idx = self._hash_to_int(combined, n_symbols, offset=8)
        else:
            win_symbol_idx = -1

        round_data = GameRound(
            session_id=session.session_id,
            nonce=nonce,
            game_type="scratch",
            combined_hash=combined,
            raw_value=r,
            outcome={
                "is_win": is_win,
                "win_symbol_index": win_symbol_idx if is_win else None,
                "derivation": f"r={r:.8f} {'<' if is_win else '>='} "
                              f"{win_chance} → {'WIN' if is_win else 'LOSS'}"
                              f"{f', symbol={win_symbol_idx}' if is_win else ''}",
            },
        )
        session.rounds.append(round_data)
        return round_data

    # ── Verification ──────────────────────────────────────────

    @staticmethod
    def verify_round(server_seed: str, client_seed: str,
                     nonce: int, expected_hash: str) -> bool:
        """Verify a round's hash matches the seeds + nonce.

        This is what the player does after the server seed is revealed.
        """
        computed = hmac.new(
            server_seed.encode(),
            f"{client_seed}:{nonce}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return computed == expected_hash

    @staticmethod
    def verify_server_seed(server_seed: str, expected_hash: str) -> bool:
        """Verify the server seed matches the hash shared before the game."""
        computed = hashlib.sha256(server_seed.encode()).hexdigest()
        return computed == expected_hash

    def session_audit_log(self, session: GameSession) -> dict:
        """Generate full audit log for a session."""
        return {
            "session_id": session.session_id,
            "server_seed_hash": session.server_seed_hash,
            "client_seed": session.client_seed,
            "total_rounds": len(session.rounds),
            "created_at": session.created_at,
            "rounds": [r.verification_data() for r in session.rounds],
            "verification_instructions": {
                "step_1": "Verify: SHA-256(server_seed) == server_seed_hash",
                "step_2": "For each round: HMAC-SHA256(server_seed, client_seed:nonce) == combined_hash",
                "step_3": "Derive outcome from combined_hash using game-specific formula",
                "tools": "Use any HMAC-SHA256 calculator to verify independently",
            },
        }


# ═══════════════════════════════════════════════════════════════
# JS Code Generator — for client-side verification
# ═══════════════════════════════════════════════════════════════

def generate_verification_js() -> str:
    """Generate JavaScript code for client-side verification.

    This can be embedded in the game or provided as a standalone tool.
    """
    return '''
// ═══ PROVABLY FAIR VERIFICATION (ArkainBrain) ═══
// Players can use this code to verify any game round independently.

async function verifyRound(serverSeed, clientSeed, nonce, expectedHash) {
    const enc = new TextEncoder();
    const key = await crypto.subtle.importKey(
        'raw', enc.encode(serverSeed), {name: 'HMAC', hash: 'SHA-256'}, false, ['sign']
    );
    const sig = await crypto.subtle.sign('HMAC', key, enc.encode(clientSeed + ':' + nonce));
    const hash = Array.from(new Uint8Array(sig)).map(b => b.toString(16).padStart(2,'0')).join('');
    return hash === expectedHash;
}

async function verifyServerSeed(serverSeed, expectedHash) {
    const enc = new TextEncoder();
    const hash = await crypto.subtle.digest('SHA-256', enc.encode(serverSeed));
    const hex = Array.from(new Uint8Array(hash)).map(b => b.toString(16).padStart(2,'0')).join('');
    return hex === expectedHash;
}

function hashToFloat(hexHash, offset = 0) {
    const segment = hexHash.substring(offset, offset + 8);
    return parseInt(segment, 16) / 0x100000000;
}

function deriveCrashPoint(hexHash, houseEdge = 0.03, maxMult = 100) {
    const r = hashToFloat(hexHash);
    if (r < houseEdge) return 1.0;
    return Math.min(Math.floor(((1 - houseEdge) / (1 - r)) * 100) / 100, maxMult);
}

function derivePlinkoBucket(hexHash, rows = 12) {
    const bytes = [];
    for (let i = 0; i < hexHash.length; i += 2) bytes.push(parseInt(hexHash.substring(i, i+2), 16));
    let bucket = 0;
    for (let row = 0; row < rows; row++) {
        const byteIdx = Math.floor(row / 8);
        const bitIdx = row % 8;
        if (byteIdx < bytes.length && (bytes[byteIdx] >> bitIdx) & 1) bucket++;
    }
    return bucket;
}

function deriveDiceRoll(hexHash) {
    return Math.floor(hashToFloat(hexHash) * 100) + 1;
}

function deriveWheelSegment(hexHash, nSegments = 20) {
    return Math.floor(hashToFloat(hexHash) * nSegments);
}
'''


# Need this import for crash_model
import math


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    rng = ProvablyFairRNG()
    session = rng.new_session(client_seed="test_client_seed_123")
    print(f"Session: {session.session_id}")
    print(f"Server hash: {session.server_seed_hash}")
    print(f"Client seed: {session.client_seed}")

    # Generate one of each
    crash = rng.generate_crash_point(session)
    print(f"\nCrash: {crash.outcome['crash_point']}× "
          f"(bust={crash.outcome['is_instant_bust']})")

    plinko = rng.generate_plinko_path(session, rows=12)
    print(f"Plinko: bucket {plinko.outcome['bucket']} "
          f"(path={plinko.outcome['path']})")

    dice = rng.generate_dice_roll(session)
    print(f"Dice: {dice.outcome['roll']}")

    wheel = rng.generate_wheel_spin(session)
    print(f"Wheel: segment {wheel.outcome['segment']}")

    card = rng.generate_card_draw(session)
    print(f"HiLo: {card.outcome['value']}{card.outcome['suit']}")

    mines = rng.generate_mines_board(session)
    print(f"Mines: mines at {mines.outcome['mine_positions']}")

    chicken = rng.generate_chicken_lane(session)
    print(f"Chicken: hazard at col {chicken.outcome['hazard_columns']}")

    scratch = rng.generate_scratch_card(session)
    print(f"Scratch: {'WIN' if scratch.outcome['is_win'] else 'LOSS'}")

    # Verify
    print(f"\nVerifying server seed...")
    ok = rng.verify_server_seed(session.server_seed, session.server_seed_hash)
    print(f"  Server seed hash: {'✅ PASS' if ok else '❌ FAIL'}")

    print(f"Verifying crash round...")
    ok2 = rng.verify_round(
        session.server_seed, session.client_seed,
        crash.nonce, crash.combined_hash
    )
    print(f"  Round hash: {'✅ PASS' if ok2 else '❌ FAIL'}")

    print(f"\nAudit log: {len(session.rounds)} rounds")
