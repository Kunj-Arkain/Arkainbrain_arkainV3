"""
ARKAINBRAIN — Market Intelligence Engine (v2)

Replaces the old market_scraper.py with a full intelligence system:
  1. Live web scraping via Serper + web fetch
  2. Competitor game tracking with structured extraction
  3. Opportunity Finder (blue ocean = high demand × low supply)
  4. Historical snapshots for trend momentum
  5. LLM-powered market analysis
  6. Concept positioning (where does YOUR game sit?)

Data sources:
  - Serper API (web search)
  - BigWinBoard, SlotCatalog, AskGamblers (scraped)
  - LLM synthesis for gap analysis
  - Internal DB for historical comparison
"""

import json
import logging
import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger("arkainbrain.market_intel")


# ═══════════════════════════════════════════════════════════
#  Constants & Taxonomies
# ═══════════════════════════════════════════════════════════

THEME_TAXONOMY = {
    "Egyptian":      ["egypt", "pharaoh", "pyramid", "cleopatra", "book of", "ankh", "scarab", "nile"],
    "Asian":         ["dragon", "fortune", "asian", "oriental", "lucky", "koi", "lantern", "jade", "lunar"],
    "Norse/Viking":  ["norse", "viking", "thor", "odin", "valhalla", "rune", "ragnarok", "fenrir"],
    "Greek/Roman":   ["olympus", "zeus", "hercules", "athena", "greek", "roman", "medusa", "poseidon"],
    "Irish/Celtic":  ["irish", "celtic", "leprechaun", "shamrock", "rainbow", "pot of gold"],
    "Fruit/Classic": ["fruit", "classic", "retro", "7s", "bar", "cherry", "joker"],
    "Aztec/Mayan":   ["aztec", "mayan", "temple", "gold mask", "obsidian", "quetzalcoatl"],
    "Ocean/Aqua":    ["underwater", "ocean", "fish", "deep sea", "atlantis", "mermaid", "coral", "pearl"],
    "Horror/Dark":   ["horror", "vampire", "zombie", "dark", "halloween", "witch", "werewolf", "undead"],
    "Space/Sci-Fi":  ["space", "cosmic", "alien", "sci-fi", "galaxy", "stellar", "nebula", "mars"],
    "Candy/Sweet":   ["candy", "sweet", "sugar", "cake", "chocolate", "bonanza", "gummy"],
    "Pirate":        ["pirate", "treasure", "ship", "captain", "skull", "buccaneer", "galleon"],
    "Western":       ["western", "cowboy", "sheriff", "dead or alive", "tombstone", "bounty", "saloon"],
    "Steampunk":     ["steampunk", "clockwork", "victorian", "gear", "airship", "brass"],
    "Cyberpunk":     ["cyberpunk", "neon", "cyber", "synth", "hacker", "digital", "glitch"],
    "Music":         ["music", "rock", "band", "dj", "jazz", "disco", "guitar"],
    "Sports":        ["football", "boxing", "racing", "sport", "soccer", "basketball"],
    "Animals":       ["animal", "wildlife", "safari", "jungle", "wolf", "buffalo", "eagle", "bear"],
    "Fairy Tale":    ["fairy", "enchanted", "castle", "princess", "magic", "wonderland", "grimm"],
    "Samurai/Japan": ["samurai", "katana", "shogun", "geisha", "ninja", "bushido", "sakura"],
}

MECHANIC_TAXONOMY = {
    "Megaways":         ["megaways", "big time gaming"],
    "Cluster Pays":     ["cluster pays", "cluster"],
    "Cascading":        ["cascading", "tumble", "avalanche", "tumbling"],
    "Hold & Spin":      ["hold and spin", "hold & spin", "respin", "hold and respin"],
    "Bonus Buy":        ["bonus buy", "feature buy", "ante bet"],
    "Multipliers":      ["multiplier", "multi"],
    "Progressive JP":   ["progressive", "jackpot"],
    "Free Spins":       ["free spins", "free spin"],
    "Expanding Wilds":  ["expanding wild"],
    "Sticky Wilds":     ["sticky wild"],
    "Walking Wilds":    ["walking wild"],
    "Split Symbols":    ["split symbol"],
    "Mystery Symbols":  ["mystery symbol", "mystery"],
    "Infinity Reels":   ["infinity reel"],
    "Ways to Win":      ["ways to win", "1024 ways", "243 ways"],
    "Pick & Click":     ["pick bonus", "pick and click", "wheel bonus"],
    "Reel Modifiers":   ["reel modifier", "random wild", "nudge"],
    "Link & Win":       ["link & win", "link and win", "cash collect"],
}

PROVIDER_TAXONOMY = {
    "Pragmatic Play":   ["pragmatic play", "pragmatic"],
    "NetEnt":           ["netent"],
    "Play'n GO":        ["play'n go", "playngo", "play n go"],
    "Push Gaming":      ["push gaming"],
    "Nolimit City":     ["nolimit city", "nolimit"],
    "Hacksaw Gaming":   ["hacksaw gaming", "hacksaw"],
    "Big Time Gaming":  ["big time gaming", "btg"],
    "Red Tiger":        ["red tiger"],
    "Yggdrasil":        ["yggdrasil"],
    "Relax Gaming":     ["relax gaming"],
    "ELK Studios":      ["elk studios", "elk"],
    "Thunderkick":      ["thunderkick"],
    "Blueprint Gaming": ["blueprint gaming", "blueprint"],
    "iSoftBet":         ["isoftbet"],
    "Spinomenal":       ["spinomenal"],
    "Wazdan":           ["wazdan"],
    "Betsoft":          ["betsoft"],
    "Microgaming":      ["microgaming"],
    "IGT":              ["igt", "international game technology"],
    "Aristocrat":       ["aristocrat", "big fish"],
}


# ═══════════════════════════════════════════════════════════
#  Core: Web Scraping
# ═══════════════════════════════════════════════════════════

def _serper_search(query: str, num: int = 8) -> list[dict]:
    """Search via Serper API. Returns list of {title, snippet, link}."""
    key = os.getenv("SERPER_API_KEY")
    if not key:
        return []
    try:
        import httpx
        resp = httpx.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": key, "Content-Type": "application/json"},
            json={"q": query, "num": num},
            timeout=15.0,
        )
        return resp.json().get("organic", [])[:num]
    except Exception as e:
        logger.warning(f"Serper search failed: {e}")
        return []


def _fetch_page(url: str, max_chars: int = 6000) -> str:
    """Fetch page content. Returns text."""
    try:
        import httpx
        resp = httpx.get(url, timeout=20.0, follow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0 ArkainBrain/2.0 MarketIntel"})
        text = resp.text[:max_chars]
        # Strip HTML tags for analysis
        text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text
    except Exception as e:
        logger.warning(f"Fetch failed for {url}: {e}")
        return ""


# ═══════════════════════════════════════════════════════════
#  Taxonomy Scoring
# ═══════════════════════════════════════════════════════════

def _score_taxonomy(text: str, taxonomy: dict) -> dict[str, int]:
    """Count keyword mentions for each category in taxonomy."""
    text_lower = text.lower()
    scores = {}
    for category, keywords in taxonomy.items():
        count = sum(text_lower.count(kw) for kw in keywords)
        scores[category] = count
    return scores


def _rank_scores(scores: dict, min_mentions: int = 1) -> list[dict]:
    """Convert scores dict to sorted list with signal strength."""
    ranked = []
    max_score = max(scores.values()) if scores else 1
    for name, count in sorted(scores.items(), key=lambda x: x[1], reverse=True):
        if count < min_mentions:
            continue
        pct = (count / max_score * 100) if max_score > 0 else 0
        signal = "🔴 HOT" if pct >= 70 else "🟠 WARM" if pct >= 35 else "🟡 RISING" if pct >= 15 else "⚪ COOL"
        ranked.append({"name": name, "mentions": count, "strength_pct": round(pct, 1), "signal": signal})
    return ranked


# ═══════════════════════════════════════════════════════════
#  Full Market Scan
# ═══════════════════════════════════════════════════════════

def run_full_scan(db: sqlite3.Connection, theme_filter: str = "") -> dict:
    """Execute a comprehensive market scan. Returns structured intel."""
    year = datetime.now().year
    month = datetime.now().strftime("%B %Y")

    # ── Phase 1: Search queries ──
    queries = [
        f"new slot game releases {month} trending themes",
        f"most popular slot games {year} player favorites top rated",
        f"slot game market trends {year} mechanics features innovation",
        f"slot industry report {year} providers studios market share",
        f"new slot releases this month site:bigwinboard.com",
        f"top slot games {year} site:slotcatalog.com",
        f"slot game RTP highest {year} best payout",
        f"upcoming slot releases {year} announced preview",
        f"slot volatility trends player preference {year}",
        f"iGaming market growth regulation {year} new markets",
    ]
    if theme_filter:
        queries.append(f'"{theme_filter}" slot games {year} new releases performance')
        queries.append(f'"{theme_filter}" theme slot saturation competition {year}')

    all_snippets = []
    all_urls = {}
    for q in queries:
        results = _serper_search(q, num=6)
        for r in results:
            snippet = r.get("snippet", "")
            if snippet:
                all_snippets.append(snippet)
            url = r.get("link", "")
            if url and url not in all_urls:
                all_urls[url] = r.get("title", "")

    # ── Phase 2: Fetch priority sources ──
    priority_domains = ["bigwinboard.com", "slotcatalog.com", "casino.guru",
                        "igamingbusiness.com", "casinomeister.com", "askgamblers.com"]
    ranked = sorted(all_urls.items(),
                    key=lambda x: sum(5 for p in priority_domains if p in x[0].lower()),
                    reverse=True)

    articles = []
    for url, title in ranked[:6]:
        content = _fetch_page(url, max_chars=6000)
        if content and len(content) > 200:
            articles.append({"url": url, "title": title, "content": content})

    # ── Phase 3: Analyze ──
    all_text = " ".join(all_snippets + [a["content"] for a in articles])
    theme_scores = _score_taxonomy(all_text, THEME_TAXONOMY)
    mechanic_scores = _score_taxonomy(all_text, MECHANIC_TAXONOMY)
    provider_scores = _score_taxonomy(all_text, PROVIDER_TAXONOMY)

    # ── Phase 4: Extract competitor games ──
    games = _extract_game_mentions(all_text, all_snippets)

    # ── Phase 5: Build result ──
    scan_date = datetime.now().isoformat()
    result = {
        "scan_date": scan_date,
        "sources": {"snippets": len(all_snippets), "articles": len(articles),
                     "urls_found": len(all_urls)},
        "themes": _rank_scores(theme_scores),
        "mechanics": _rank_scores(mechanic_scores),
        "providers": _rank_scores(provider_scores),
        "games_detected": games[:30],
        "articles": [{"url": a["url"], "title": a["title"]} for a in articles],
    }

    # Theme-specific analysis
    if theme_filter:
        tf_lower = theme_filter.lower()
        mentions = sum(1 for s in all_snippets if tf_lower in s.lower())
        mentions += sum(1 for a in articles if tf_lower in a["content"].lower())
        total = len(all_snippets) + len(articles)
        saturation = mentions / total * 100 if total > 0 else 0
        result["theme_analysis"] = {
            "theme": theme_filter,
            "mentions": mentions,
            "saturation_pct": round(saturation, 1),
            "signal": "SATURATED" if saturation > 30 else "MODERATE" if saturation > 10 else "UNDERSERVED",
            "recommendation": (
                f"'{theme_filter}' is heavily saturated ({saturation:.0f}%). Needs strong differentiation."
                if saturation > 30 else
                f"'{theme_filter}' has moderate presence ({saturation:.0f}%). Room for a standout entry."
                if saturation > 10 else
                f"'{theme_filter}' is underrepresented ({saturation:.0f}%). Potential blue ocean."
            ),
        }

    # ── Phase 6: Persist ──
    _save_snapshot(db, "full_scan", result)
    _update_market_trends(db, theme_scores, mechanic_scores, provider_scores)
    _save_competitor_games(db, games)

    return result


def _extract_game_mentions(text: str, snippets: list[str]) -> list[dict]:
    """Extract specific game titles from text using patterns."""
    games = []
    seen = set()

    # Pattern: "Game Title by Provider" or "Game Title (Provider)"
    patterns = [
        r"([A-Z][A-Za-z0-9'\s:&!-]{3,40})\s+(?:by|from)\s+([A-Z][A-Za-z'\s]{3,25})",
        r"([A-Z][A-Za-z0-9'\s:&!-]{3,40})\s*\(([A-Z][A-Za-z'\s]{3,25})\)",
    ]
    for pattern in patterns:
        for m in re.finditer(pattern, text):
            title = m.group(1).strip().rstrip(".,;:")
            provider = m.group(2).strip()
            key = title.lower()
            if key not in seen and len(title) > 3 and len(title) < 45:
                # Validate provider
                prov_lower = provider.lower()
                known_provider = any(
                    any(kw in prov_lower for kw in kws)
                    for kws in PROVIDER_TAXONOMY.values()
                )
                if known_provider:
                    seen.add(key)
                    games.append({"title": title, "provider": provider})

    # Also look for known game titles in snippets
    known_games = [
        ("Book of Dead", "Play'n GO"), ("Sweet Bonanza", "Pragmatic Play"),
        ("Reactoonz", "Play'n GO"), ("Gonzo's Quest", "NetEnt"),
        ("Dead or Alive 2", "NetEnt"), ("Money Train", "Relax Gaming"),
        ("Starburst", "NetEnt"), ("Gates of Olympus", "Pragmatic Play"),
        ("Big Bass Bonanza", "Pragmatic Play"), ("Jammin' Jars", "Push Gaming"),
        ("Tombstone", "Nolimit City"), ("Fire in the Hole", "Nolimit City"),
        ("Mental", "Nolimit City"), ("Wanted Dead or a Wild", "Hacksaw Gaming"),
        ("Chaos Crew", "Hacksaw Gaming"), ("Legacy of Dead", "Play'n GO"),
    ]
    text_lower = text.lower()
    for title, provider in known_games:
        if title.lower() in text_lower and title.lower() not in seen:
            seen.add(title.lower())
            games.append({"title": title, "provider": provider})

    return games


# ═══════════════════════════════════════════════════════════
#  Opportunity Finder (Blue Ocean Analysis)
# ═══════════════════════════════════════════════════════════

def find_opportunities(db: sqlite3.Connection, scan_result: dict = None) -> list[dict]:
    """Identify blue ocean opportunities: high demand themes × underserved mechanics.

    Score = demand_signal × (1 - supply_saturation) × trend_momentum

    demand_signal:     How much players/market wants this theme (0-1)
    supply_saturation: How crowded the niche is (0-1, lower = more opportunity)
    trend_momentum:    Is this trending up or stable? (0.5-1.5)
    """
    # Get latest scan or run one
    if not scan_result:
        latest = db.execute(
            "SELECT data FROM market_snapshots WHERE snapshot_type='full_scan' "
            "ORDER BY scan_date DESC LIMIT 1"
        ).fetchone()
        if latest:
            scan_result = json.loads(latest["data"])
        else:
            return []

    themes = {t["name"]: t for t in scan_result.get("themes", [])}
    mechanics = {m["name"]: m for m in scan_result.get("mechanics", [])}

    # ── Historical comparison for momentum ──
    prev_snapshot = db.execute(
        "SELECT data FROM market_snapshots WHERE snapshot_type='full_scan' "
        "ORDER BY scan_date DESC LIMIT 1 OFFSET 1"
    ).fetchone()
    prev_themes = {}
    if prev_snapshot:
        prev_data = json.loads(prev_snapshot["data"])
        prev_themes = {t["name"]: t["mentions"] for t in prev_data.get("themes", [])}

    # ── Theme demand signals ──
    # Normalize theme mentions to 0-1 demand signal
    max_theme = max((t.get("mentions", 0) for t in themes.values()), default=1) or 1
    theme_demand = {}
    for name, data in themes.items():
        demand = data.get("mentions", 0) / max_theme
        # Boost underrepresented themes that still have SOME signal
        if 0.05 < demand < 0.3:
            demand *= 1.3  # Emerging themes get a boost
        theme_demand[name] = min(demand, 1.0)

    # ── Supply saturation from competitor games ──
    comp_games = db.execute("SELECT theme FROM competitor_games").fetchall()
    theme_supply = {}
    total_games = max(len(comp_games), 1)
    for name in THEME_TAXONOMY:
        theme_lower_kws = [kw for kw in THEME_TAXONOMY[name]]
        count = sum(
            1 for g in comp_games
            if g["theme"] and any(kw in g["theme"].lower() for kw in theme_lower_kws)
        )
        theme_supply[name] = count / total_games

    # Also use current trends as supply proxy
    for name, data in themes.items():
        supply_from_mentions = data.get("strength_pct", 0) / 100
        existing = theme_supply.get(name, 0)
        theme_supply[name] = max(existing, supply_from_mentions * 0.7)

    # ── Trend momentum ──
    theme_momentum = {}
    for name in THEME_TAXONOMY:
        current = themes.get(name, {}).get("mentions", 0)
        previous = prev_themes.get(name, current)
        if previous > 0:
            ratio = current / previous
            momentum = min(max(ratio, 0.5), 1.5)
        else:
            momentum = 1.2 if current > 0 else 0.8
        theme_momentum[name] = momentum

    # ── Cross-product: Theme × Mechanic opportunities ──
    opportunities = []
    scan_date = datetime.now().isoformat()

    # Top themes with opportunity
    for theme_name in THEME_TAXONOMY:
        demand = theme_demand.get(theme_name, 0.1)
        supply = theme_supply.get(theme_name, 0.5)
        momentum = theme_momentum.get(theme_name, 1.0)

        # Blue ocean score for theme alone
        theme_score = demand * (1 - supply) * momentum

        # Now cross with top mechanics
        for mech_name, mech_data in mechanics.items():
            mech_heat = mech_data.get("strength_pct", 0) / 100
            # Mechanic is hot = more demand signal
            # But we want combos that aren't overdone
            combined_score = theme_score * (0.5 + mech_heat * 0.5)

            if combined_score > 0.02:  # Minimum threshold
                reasoning = _build_opportunity_reasoning(
                    theme_name, mech_name, demand, supply, momentum, mech_heat
                )
                opportunities.append({
                    "theme": theme_name,
                    "mechanic": mech_name,
                    "opportunity_score": round(combined_score, 4),
                    "demand_signal": round(demand, 3),
                    "supply_saturation": round(supply, 3),
                    "trend_momentum": round(momentum, 3),
                    "mechanic_heat": round(mech_heat, 3),
                    "reasoning": reasoning,
                    "scan_date": scan_date,
                })

    # Sort by opportunity score
    opportunities.sort(key=lambda x: x["opportunity_score"], reverse=True)

    # Persist top 50
    _save_opportunities(db, opportunities[:50])

    return opportunities[:50]


def _build_opportunity_reasoning(theme, mechanic, demand, supply, momentum, mech_heat):
    """Build human-readable reasoning for an opportunity."""
    parts = []

    if supply < 0.15:
        parts.append(f"{theme} is an underserved niche (supply {supply:.0%})")
    elif supply < 0.35:
        parts.append(f"{theme} has moderate competition (supply {supply:.0%})")
    else:
        parts.append(f"{theme} is crowded (supply {supply:.0%}) — strong differentiation needed")

    if demand > 0.6:
        parts.append(f"high player demand ({demand:.0%})")
    elif demand > 0.25:
        parts.append(f"moderate demand ({demand:.0%})")
    else:
        parts.append(f"emerging demand ({demand:.0%})")

    if momentum > 1.15:
        parts.append("trending upward ↑")
    elif momentum < 0.85:
        parts.append("declining ↓")

    if mech_heat > 0.5:
        parts.append(f"{mechanic} is a hot mechanic ({mech_heat:.0%} heat)")
    elif mech_heat > 0.2:
        parts.append(f"{mechanic} has growing adoption")
    else:
        parts.append(f"{mechanic} is niche — differentiator potential")

    return ". ".join(parts) + "."


# ═══════════════════════════════════════════════════════════
#  Concept Positioning
# ═══════════════════════════════════════════════════════════

def position_concept(db: sqlite3.Connection, theme: str, features: list[str],
                     volatility: str = "medium", rtp: float = 96.0) -> dict:
    """Position a proposed game concept against the current market landscape."""
    # Find which theme categories this concept matches
    theme_lower = theme.lower()
    matched_themes = []
    for cat, keywords in THEME_TAXONOMY.items():
        if any(kw in theme_lower for kw in keywords):
            matched_themes.append(cat)

    # Get latest opportunities
    opps = db.execute(
        "SELECT * FROM opportunity_scores ORDER BY opportunity_score DESC LIMIT 50"
    ).fetchall()
    opps = [dict(o) for o in opps]

    # Find this concept's opportunity score
    concept_opp = None
    for o in opps:
        if o["theme"] in matched_themes:
            concept_opp = o
            break

    # Count competitors in same theme
    competitors = []
    for t in matched_themes:
        kws = THEME_TAXONOMY.get(t, [])
        games = db.execute(
            "SELECT title, provider, rtp, volatility FROM competitor_games"
        ).fetchall()
        for g in games:
            if g["theme"] and any(kw in g["theme"].lower() for kw in kws):
                competitors.append(dict(g))

    # Market trends for this theme
    trends = db.execute(
        "SELECT * FROM market_trends WHERE category='theme' ORDER BY market_share DESC"
    ).fetchall()
    theme_rank = None
    for i, t in enumerate(trends):
        if t["name"] in matched_themes:
            theme_rank = i + 1
            break

    return {
        "matched_categories": matched_themes,
        "opportunity_score": concept_opp["opportunity_score"] if concept_opp else None,
        "opportunity_reasoning": concept_opp["reasoning"] if concept_opp else "No data — run a market scan first",
        "direct_competitors": len(competitors),
        "competitor_sample": competitors[:5],
        "theme_market_rank": theme_rank,
        "positioning": (
            "🟢 Blue Ocean — low competition, good opportunity"
            if (concept_opp and concept_opp["opportunity_score"] > 0.15) else
            "🟡 Moderate — some competition, differentiation recommended"
            if (concept_opp and concept_opp["opportunity_score"] > 0.05) else
            "🔴 Red Ocean — high competition, strong USP required"
            if concept_opp else
            "⚪ Unknown — run a market scan for positioning data"
        ),
    }


# ═══════════════════════════════════════════════════════════
#  LLM-Powered Analysis
# ═══════════════════════════════════════════════════════════

def llm_market_analysis(scan_result: dict) -> Optional[str]:
    """Use LLM to synthesize market intelligence into actionable insights."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)

        top_themes = scan_result.get("themes", [])[:8]
        top_mechs = scan_result.get("mechanics", [])[:8]
        top_provs = scan_result.get("providers", [])[:8]
        games = scan_result.get("games_detected", [])[:10]

        prompt = (
            "You are a senior iGaming market analyst. Based on this market scan data, "
            "write a concise 200-word intelligence brief covering:\n"
            "1. The 2-3 most promising market gaps (theme × mechanic combos nobody is doing)\n"
            "2. What's oversaturated and should be avoided\n"
            "3. One bold prediction for what will be hot in 6 months\n\n"
            f"Trending themes: {json.dumps(top_themes)}\n"
            f"Hot mechanics: {json.dumps(top_mechs)}\n"
            f"Active providers: {json.dumps(top_provs)}\n"
            f"Recent games spotted: {json.dumps(games)}\n\n"
            "Be specific and actionable. No fluff."
        )

        resp = client.chat.completions.create(
            model="gpt-4.1-nano-2025-04-14",
            max_tokens=400,
            temperature=0.7,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logger.warning(f"LLM analysis failed: {e}")
        return None


# ═══════════════════════════════════════════════════════════
#  Persistence Helpers
# ═══════════════════════════════════════════════════════════

def _save_snapshot(db: sqlite3.Connection, snapshot_type: str, data: dict):
    """Save a market scan snapshot."""
    try:
        db.execute(
            "INSERT INTO market_snapshots (id, snapshot_type, scan_date, data, sources_count) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4())[:12], snapshot_type, datetime.now().isoformat(),
             json.dumps(data), data.get("sources", {}).get("snippets", 0))
        )
        db.commit()
    except Exception as e:
        logger.warning(f"Snapshot save failed: {e}")


def _update_market_trends(db: sqlite3.Connection, theme_scores, mech_scores, provider_scores):
    """Update market_trends table with latest scores."""
    period = datetime.now().strftime("%Y-%m")
    now = datetime.now().isoformat()

    for category, scores in [("theme", theme_scores), ("mechanic", mech_scores), ("provider", provider_scores)]:
        for name, value in scores.items():
            if value < 1:
                continue
            existing = db.execute(
                "SELECT id FROM market_trends WHERE category=? AND name=? AND period=?",
                (category, name, period)
            ).fetchone()
            if existing:
                db.execute(
                    "UPDATE market_trends SET value=?, market_share=? WHERE id=?",
                    (value, value, existing["id"])
                )
            else:
                db.execute(
                    "INSERT INTO market_trends (id, category, name, value, market_share, source, period) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4())[:8], category, name, value, value, "market_scan", period)
                )
    db.commit()


def _save_competitor_games(db: sqlite3.Connection, games: list[dict]):
    """Save detected competitor games."""
    for g in games:
        title = g.get("title", "")
        existing = db.execute(
            "SELECT id FROM competitor_games WHERE title=?", (title,)
        ).fetchone()
        if not existing:
            db.execute(
                "INSERT INTO competitor_games (id, title, provider, theme, source, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4())[:8], title, g.get("provider", ""),
                 g.get("theme", ""), "market_scan", datetime.now().isoformat())
            )
    db.commit()


def _save_opportunities(db: sqlite3.Connection, opportunities: list[dict]):
    """Save opportunity scores, replacing previous scan's data."""
    if not opportunities:
        return
    scan_date = opportunities[0].get("scan_date", datetime.now().isoformat())
    # Don't delete old — keep for history. Just insert new batch.
    for o in opportunities:
        db.execute(
            "INSERT INTO opportunity_scores "
            "(id, theme, mechanic, opportunity_score, demand_signal, supply_saturation, "
            "trend_momentum, reasoning, scan_date) VALUES (?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4())[:8], o["theme"], o["mechanic"], o["opportunity_score"],
             o["demand_signal"], o["supply_saturation"], o["trend_momentum"],
             o["reasoning"], scan_date)
        )
    db.commit()


# ═══════════════════════════════════════════════════════════
#  Dashboard Data Helpers
# ═══════════════════════════════════════════════════════════

def get_dashboard_data(db: sqlite3.Connection) -> dict:
    """Get all data needed for the /trends dashboard."""
    # Latest scan
    latest = db.execute(
        "SELECT * FROM market_snapshots WHERE snapshot_type='full_scan' "
        "ORDER BY scan_date DESC LIMIT 1"
    ).fetchone()
    scan_data = json.loads(latest["data"]) if latest else None
    scan_date = latest["scan_date"] if latest else None

    # Historical snapshots count
    snap_count = db.execute("SELECT COUNT(*) as c FROM market_snapshots").fetchone()["c"]

    # Top opportunities
    opps = [dict(o) for o in db.execute(
        "SELECT * FROM opportunity_scores ORDER BY opportunity_score DESC LIMIT 15"
    ).fetchall()]

    # Competitor games
    comp_count = db.execute("SELECT COUNT(*) as c FROM competitor_games").fetchone()["c"]
    recent_comps = [dict(g) for g in db.execute(
        "SELECT * FROM competitor_games ORDER BY created_at DESC LIMIT 10"
    ).fetchall()]

    # Market trends by category
    themes = [dict(t) for t in db.execute(
        "SELECT * FROM market_trends WHERE category='theme' ORDER BY market_share DESC LIMIT 15"
    ).fetchall()]
    mechanics = [dict(m) for m in db.execute(
        "SELECT * FROM market_trends WHERE category='mechanic' ORDER BY market_share DESC LIMIT 15"
    ).fetchall()]
    providers = [dict(p) for p in db.execute(
        "SELECT * FROM market_trends WHERE category='provider' ORDER BY market_share DESC LIMIT 15"
    ).fetchall()]

    return {
        "scan_data": scan_data,
        "scan_date": scan_date,
        "snapshot_count": snap_count,
        "opportunities": opps,
        "competitor_count": comp_count,
        "recent_competitors": recent_comps,
        "themes": themes,
        "mechanics": mechanics,
        "providers": providers,
    }


# ═══════════════════════════════════════════════════════════
#  Seed Data (used when no scan has been run yet)
# ═══════════════════════════════════════════════════════════

def seed_baseline_if_empty(db: sqlite3.Connection):
    """Seed baseline market data if tables are empty (for first-load UX)."""
    from tools.market_scraper import seed_market_data
    seed_market_data(db)
