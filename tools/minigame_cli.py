#!/usr/bin/env python3
"""
ARKAINBRAIN — Mini-Game Generator CLI (Phase 1)

Usage:
    python -m tools.minigame_cli plinko
    python -m tools.minigame_cli crash --rtp 97 --theme "Neon Rocket"
    python -m tools.minigame_cli all
    python -m tools.minigame_cli plinko --dump-config
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.minigame_config import (
    MiniGameType, build_config, default_config, validate_config,
)
from tools.minigame_injector import save_themed_game


def main():
    parser = argparse.ArgumentParser(description="Generate themed mini-games")
    parser.add_argument("game_type", choices=[
        "plinko", "crash", "mines", "dice", "wheel", "hilo", "chicken", "scratch", "all"
    ])
    parser.add_argument("--theme", type=str, help="Theme name")
    parser.add_argument("--title", type=str, help="Game title")
    parser.add_argument("--rtp", type=float, default=96.0, help="Target RTP")
    parser.add_argument("--volatility", type=str, default="medium")
    parser.add_argument("--primary", type=str, help="Primary accent color (hex)")
    parser.add_argument("--secondary", type=str, help="Secondary accent color (hex)")
    parser.add_argument("--output-dir", type=str, default=None)
    parser.add_argument("--dump-config", action="store_true")
    args = parser.parse_args()

    game_ids = (
        ["crash", "plinko", "mines", "dice", "wheel", "hilo", "chicken", "scratch"]
        if args.game_type == "all" else [args.game_type]
    )
    out_dir = Path(args.output_dir) if args.output_dir else None

    for gid in game_ids:
        config = default_config(gid)

        # Apply overrides
        if args.theme:
            config.theme.name = args.theme
        if args.title:
            config.theme.title = args.title
        if args.primary:
            config.theme.primary = args.primary
        if args.secondary:
            config.theme.secondary = args.secondary
        if args.rtp != 96.0:
            config = build_config(
                game_type=gid, target_rtp=args.rtp, volatility=args.volatility,
                theme_overrides={"name": args.theme or config.theme.name},
            )

        if args.dump_config:
            print(config.model_dump_json(indent=2))
            warnings = validate_config(config)
            if warnings:
                print("\n⚠️  Warnings:")
                for w in warnings:
                    print(f"  - {w}")
            continue

        try:
            path = save_themed_game(gid, config, output_dir=out_dir)
            print(f"✅ {gid}: {path} ({path.stat().st_size:,} bytes)")
        except Exception as e:
            print(f"❌ {gid}: {e}")


if __name__ == "__main__":
    main()
