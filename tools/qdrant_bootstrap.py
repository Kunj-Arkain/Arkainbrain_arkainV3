"""
ARKAINBRAIN — Qdrant Bootstrap / Seed

The missing piece: populates Qdrant with initial jurisdiction intelligence
so the pipeline has data to work with from day one.

How it works:
  1. Connects to Qdrant, creates collection if needed
  2. For each jurisdiction, calls GPT to generate a comprehensive regulatory profile
  3. Chunks the profile text
  4. Embeds via text-embedding-3-small
  5. Upserts into Qdrant with jurisdiction metadata
  6. Saves .md files to data/regulations/ for reference

Usage:
    # From CLI:
    python -m tools.qdrant_bootstrap                    # Seed all jurisdictions
    python -m tools.qdrant_bootstrap --jurisdictions UK Malta "New Jersey"
    python -m tools.qdrant_bootstrap --us-priority      # US states that need recon
    python -m tools.qdrant_bootstrap --international     # Regulated markets only
    python -m tools.qdrant_bootstrap --check             # Just check status, don't seed

    # From code:
    from tools.qdrant_bootstrap import seed_qdrant
    seed_qdrant()                                       # Seed everything
    seed_qdrant(jurisdictions=["UK", "Malta"])           # Specific ones

    # From web (added as API endpoint):
    POST /api/qdrant/seed
"""

import hashlib
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger("arkainbrain.qdrant_bootstrap")


# ═══════════════════════════════════════════════════════════
# Jurisdiction Registry
# ═══════════════════════════════════════════════════════════

INTERNATIONAL_JURISDICTIONS = {
    "UK": {
        "full_name": "United Kingdom (UKGC)",
        "regulator": "UK Gambling Commission (UKGC)",
        "category": "regulated_international",
        "topics": ["Gambling Act 2005", "UKGC LCCP", "remote gambling license",
                   "RTP disclosure", "responsible gambling", "affordability checks",
                   "B2 gaming machines", "online slots restrictions"],
    },
    "Malta": {
        "full_name": "Malta (MGA)",
        "regulator": "Malta Gaming Authority (MGA)",
        "category": "regulated_international",
        "topics": ["Gaming Act 2018", "B2C license types", "player protection",
                   "type 1/2/3/4 licenses", "MGA compliance", "AML requirements"],
    },
    "Ontario": {
        "full_name": "Ontario, Canada (AGCO/iGO)",
        "regulator": "Alcohol and Gaming Commission of Ontario (AGCO)",
        "category": "regulated_international",
        "topics": ["iGaming Ontario", "AGCO standards", "RG requirements",
                   "operator registration", "game testing GLI", "duty to report"],
    },
    "Curacao": {
        "full_name": "Curaçao",
        "regulator": "Curaçao Gaming Control Board (GCB)",
        "category": "regulated_international",
        "topics": ["2023 National Ordinance on Games of Hazard", "GCB licensing",
                   "new regulatory framework", "B2C sublicensing end", "compliance requirements"],
    },
    "Gibraltar": {
        "full_name": "Gibraltar",
        "regulator": "Gibraltar Gambling Commissioner",
        "category": "regulated_international",
        "topics": ["Gambling Act 2005 Gibraltar", "remote gambling license",
                   "B2B/B2C licensing", "compliance code", "player protection"],
    },
    "Isle of Man": {
        "full_name": "Isle of Man (GSC)",
        "regulator": "Isle of Man Gambling Supervision Commission",
        "category": "regulated_international",
        "topics": ["Online Gambling Regulation Act 2001", "GSC license",
                   "software supplier license", "network services license"],
    },
    "Sweden": {
        "full_name": "Sweden (Spelinspektionen)",
        "regulator": "Swedish Gambling Authority (Spelinspektionen)",
        "category": "regulated_international",
        "topics": ["Gambling Act 2018 Sweden", "online casino license",
                   "deposit limits 5000 SEK", "bonus restrictions", "spin speed limits"],
    },
    "Denmark": {
        "full_name": "Denmark (Spillemyndigheden)",
        "regulator": "Danish Gambling Authority (Spillemyndigheden)",
        "category": "regulated_international",
        "topics": ["Danish Gambling Act", "online casino license Denmark",
                   "RTP minimum 85%", "ROFUS self-exclusion", "technical requirements"],
    },
    "Germany": {
        "full_name": "Germany (GGL)",
        "regulator": "Gemeinsame Glücksspielbehörde der Länder (GGL)",
        "category": "regulated_international",
        "topics": ["Interstate Treaty on Gambling 2021", "Glücksspielstaatsvertrag",
                   "€1 per spin limit", "5 second spin speed", "€1000/month deposit",
                   "autoplay ban", "slot restrictions Germany"],
    },
    "Spain": {
        "full_name": "Spain (DGOJ)",
        "regulator": "Dirección General de Ordenación del Juego (DGOJ)",
        "category": "regulated_international",
        "topics": ["Gambling Act 13/2011 Spain", "DGOJ license", "advertising restrictions",
                   "Royal Decree 958/2020", "responsible gambling Spain"],
    },
    "Italy": {
        "full_name": "Italy (ADM)",
        "regulator": "Agenzia delle Dogane e dei Monopoli (ADM)",
        "category": "regulated_international",
        "topics": ["Italian gambling regulation", "ADM license", "Decreto Dignità",
                   "advertising ban Italy gambling", "RTP requirements Italy"],
    },
}

US_REGULATED_JURISDICTIONS = {
    "New Jersey": {
        "full_name": "New Jersey (DGE)",
        "regulator": "NJ Division of Gaming Enforcement",
        "category": "us_regulated",
        "topics": ["Casino Control Act NJ", "iGaming Act 2013", "online slots NJ",
                   "DGE technical requirements", "GLI-11 certification", "server-based gaming"],
    },
    "Pennsylvania": {
        "full_name": "Pennsylvania (PGCB)",
        "regulator": "PA Gaming Control Board (PGCB)",
        "category": "us_regulated",
        "topics": ["PA iGaming Act 2017", "Act 42 online gambling", "PGCB requirements",
                   "slot machine regulations PA", "interactive gaming"],
    },
    "Michigan": {
        "full_name": "Michigan (MGCB)",
        "regulator": "MI Gaming Control Board",
        "category": "us_regulated",
        "topics": ["Lawful Internet Gaming Act Michigan", "MGCB regulations",
                   "online casino Michigan", "tribal gaming compacts"],
    },
    "West Virginia": {
        "full_name": "West Virginia (Lottery Commission)",
        "regulator": "WV Lottery Commission",
        "category": "us_regulated",
        "topics": ["WV Lottery Interactive Wagering Act", "iGaming West Virginia",
                   "online casino WV", "racetrack casino"],
    },
    "Connecticut": {
        "full_name": "Connecticut (DCP)",
        "regulator": "CT Department of Consumer Protection",
        "category": "us_regulated",
        "topics": ["CT online gambling tribal compact", "Mohegan Sun Foxwoods online",
                   "sports wagering Act 21-23", "iGaming Connecticut"],
    },
    "Nevada": {
        "full_name": "Nevada (NGC)",
        "regulator": "Nevada Gaming Commission / Control Board",
        "category": "us_regulated",
        "topics": ["Nevada Gaming Control Act", "NGC regulations", "Technical Standard 3",
                   "slot machine certification Nevada", "interactive gaming"],
    },
}

US_GRAY_AREA_JURISDICTIONS = {
    "Georgia": {
        "full_name": "Georgia",
        "regulator": "Georgia Lottery Corporation / AG",
        "category": "us_gray_area",
        "topics": ["O.C.G.A. § 16-12-20 gambling definition", "coin-operated amusement",
                   "COAM Act", "skill game Georgia", "Georgia Lottery"],
    },
    "Texas": {
        "full_name": "Texas",
        "regulator": "Texas AG / TABC",
        "category": "us_gray_area",
        "topics": ["Texas Penal Code § 47 gambling", "eight-liner machines",
                   "fuzzy animal exception", "Texas skill games", "amusement redemption"],
    },
    "Virginia": {
        "full_name": "Virginia",
        "regulator": "Virginia Lottery Board",
        "category": "us_gray_area",
        "topics": ["Virginia Code § 18.2-325 gambling definition", "skill games ban 2020",
                   "skill gaming machines", "Virginia casino legislation"],
    },
    "Illinois": {
        "full_name": "Illinois",
        "regulator": "Illinois Gaming Board",
        "category": "us_gray_area",
        "topics": ["Illinois gambling statutes", "video gaming terminal Act",
                   "IGB terminal regulations", "amusement devices Illinois"],
    },
    "Florida": {
        "full_name": "Florida",
        "regulator": "FL Division of Pari-Mutuel Wagering",
        "category": "us_gray_area",
        "topics": ["Florida gambling statutes 849", "Seminole Compact",
                   "internet cafe machines", "skill games Florida", "adult arcades"],
    },
    "North Carolina": {
        "full_name": "North Carolina",
        "regulator": "NC AG / ALE",
        "category": "us_gray_area",
        "topics": ["NC General Statutes 14-292", "skill game exemption NC",
                   "video sweepstakes", "sweepstakes cafes", "Hest Technologies"],
    },
    "South Carolina": {
        "full_name": "South Carolina",
        "regulator": "SC Law Enforcement Division",
        "category": "us_gray_area",
        "topics": ["SC Code 32-1 gambling", "video game machines SC",
                   "coin-operated device regulations", "constitutional gambling ban"],
    },
    "Nebraska": {
        "full_name": "Nebraska",
        "regulator": "Nebraska Racing & Gaming Commission",
        "category": "us_gray_area",
        "topics": ["Nebraska Constitution gambling provision", "LB 561 casino gaming",
                   "racetrack casinos", "historical horse racing Nebraska"],
    },
}


# ═══════════════════════════════════════════════════════════
# LLM Research Prompt
# ═══════════════════════════════════════════════════════════

def _build_research_prompt(name: str, info: dict) -> str:
    """Build the prompt that generates a jurisdiction regulatory profile."""
    topics = ", ".join(info.get("topics", []))
    return f"""You are a gaming law research specialist. Generate a comprehensive regulatory intelligence brief for the following jurisdiction.

JURISDICTION: {info.get("full_name", name)}
REGULATOR: {info.get("regulator", "Unknown")}
CATEGORY: {info.get("category", "unknown")}
KEY TOPICS TO COVER: {topics}

Generate a detailed markdown document covering ALL of the following sections. Be specific — cite actual statute numbers, actual regulatory requirements, actual monetary limits. Do NOT use placeholder values.

# {name} Gaming Regulations — Intelligence Brief

## 1. Regulatory Framework
- Primary governing law (name, number, year)
- Regulatory body and its authority
- License types and requirements
- Application process and fees (if known)

## 2. Gambling Definition & Legal Elements
- How this jurisdiction legally defines "gambling"
- The legal test used (chance-predominance, any-chance, material-element, etc.)
- Key statutory language that triggers or exempts gambling classification
- How "consideration", "chance", and "prize" are each defined

## 3. Game Requirements & Technical Standards
- RTP requirements (minimum/maximum)
- RNG certification requirements (GLI-11, GLI-19, etc.)
- Maximum bet limits
- Spin speed / game pace restrictions
- Autoplay restrictions
- Maximum win multipliers (if any)
- Server-based vs client-based requirements

## 4. Player Protection Requirements
- Responsible gambling tools required (deposit limits, session limits, reality checks)
- Self-exclusion programs
- Age verification requirements
- KYC/AML requirements
- Advertising restrictions

## 5. Exemptions & Carve-Outs
- What types of games are exempt from gambling laws
- Skill game exemptions (if any)
- Amusement/arcade exemptions
- Sweepstakes model legality
- Social/free-to-play exemptions
- Prize limits for exempt categories

## 6. Enforcement & Risk Assessment
- Enforcement posture (aggressive/moderate/lax)
- Recent enforcement actions
- Penalties for non-compliance
- Prosecution trends

## 7. Compliance Checklist for Slot-Style Games
- Specific requirements a slot game must meet to be legal
- Certification path (which test lab, which standard)
- Timeline estimates for approval
- Common rejection reasons

## 8. Recent Changes & Pending Legislation
- Changes in the last 2 years
- Pending bills or regulatory proposals
- Industry trends in this jurisdiction

Be factual. If you're uncertain about a specific number, say "approximately" or "as of [date]". Do NOT invent statute numbers."""


# ═══════════════════════════════════════════════════════════
# Core Bootstrap Logic
# ═══════════════════════════════════════════════════════════

def _get_model() -> str:
    """Get the model to use for research generation."""
    try:
        from config.settings import LLMConfig
        m = LLMConfig.get_llm("research_synthesizer")
        return m.replace("openai/", "")
    except Exception:
        return os.getenv("LLM_HEAVY", "gpt-4.1-mini")


def _generate_profile(name: str, info: dict) -> str:
    """Use GPT to generate a jurisdiction regulatory profile."""
    from openai import OpenAI
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    prompt = _build_research_prompt(name, info)
    model = _get_model()

    logger.info(f"Generating profile for {name} using {model}...")
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=4000,
        temperature=0.3,
    )
    return resp.choices[0].message.content.strip()


def _chunk_text(text: str, chunk_size: int = 600, overlap: int = 150) -> list[str]:
    """Split text into overlapping chunks for embedding."""
    words = text.split()
    chunks = []
    i = 0
    while i < len(words):
        chunk = " ".join(words[i:i + chunk_size])
        if chunk.strip():
            chunks.append(chunk)
        i += chunk_size - overlap
    return chunks


def _embed_batch(texts: list[str], model: str = "text-embedding-3-small") -> list[list[float]]:
    """Generate embeddings for a batch of texts."""
    from openai import OpenAI
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    all_embeddings = []
    # Process in batches of 50 to avoid rate limits
    for i in range(0, len(texts), 50):
        batch = texts[i:i + 50]
        resp = client.embeddings.create(model=model, input=batch)
        all_embeddings.extend([item.embedding for item in resp.data])
        if i + 50 < len(texts):
            time.sleep(0.5)  # Rate limit courtesy

    return all_embeddings


def _ensure_collection(client, collection_name: str):
    """Create Qdrant collection if it doesn't exist."""
    from qdrant_client.models import Distance, VectorParams

    collections = [c.name for c in client.get_collections().collections]
    if collection_name not in collections:
        client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=1536, distance=Distance.COSINE),
        )
        logger.info(f"Created Qdrant collection: {collection_name}")
        return True
    return False


def _get_qdrant_client():
    """Create a Qdrant client from env vars."""
    from qdrant_client import QdrantClient

    url = os.getenv("QDRANT_URL", "")
    key = os.getenv("QDRANT_API_KEY", "")

    if not url:
        raise ValueError("QDRANT_URL not set. Configure in Settings → API Keys.")

    if key:
        return QdrantClient(url=url, api_key=key)
    return QdrantClient(url=url)


def check_status() -> dict:
    """Check Qdrant connectivity and return status."""
    from tools.qdrant_store import JurisdictionStore
    store = JurisdictionStore()
    return store.get_status()


def seed_jurisdiction(name: str, info: dict, client=None, collection: str = None,
                      save_local: bool = True) -> dict:
    """
    Seed a single jurisdiction into Qdrant.

    Returns: {"jurisdiction": name, "chunks": N, "status": "ok"/"error", ...}
    """
    from qdrant_client.models import PointStruct

    collection = collection or os.getenv("QDRANT_COLLECTION", "slot_regulations")
    result = {"jurisdiction": name, "category": info.get("category", "")}

    try:
        # 1. Generate profile via LLM
        profile_text = _generate_profile(name, info)
        result["profile_length"] = len(profile_text)

        # 2. Save locally
        if save_local:
            slug = name.lower().replace(" ", "_")
            category_dir = info.get("category", "general")
            save_dir = Path("data/regulations") / category_dir
            save_dir.mkdir(parents=True, exist_ok=True)
            save_path = save_dir / f"{slug}.md"
            save_path.write_text(profile_text, encoding="utf-8")
            result["local_path"] = str(save_path)

        # 3. Chunk
        chunks = _chunk_text(profile_text)
        result["chunks"] = len(chunks)

        if not chunks:
            result["status"] = "error"
            result["error"] = "No chunks generated"
            return result

        # 4. Embed
        embeddings = _embed_batch(chunks)

        # 5. Upsert to Qdrant
        if client is None:
            client = _get_qdrant_client()
        _ensure_collection(client, collection)

        points = []
        for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
            point_id = int(hashlib.md5(f"{name}:{i}:{chunk[:50]}".encode()).hexdigest()[:15], 16)
            points.append(PointStruct(
                id=point_id,
                vector=embedding,
                payload={
                    "text": chunk,
                    "jurisdiction": name,
                    "source": f"bootstrap_{name.lower().replace(' ', '_')}",
                    "filename": f"{name.lower().replace(' ', '_')}_bootstrap.md",
                    "category": info.get("category", "general"),
                    "document_type": "jurisdiction_profile",
                    "is_us_state": info.get("category", "").startswith("us_"),
                    "regulator": info.get("regulator", ""),
                    "bootstrap_date": datetime.now().isoformat()[:10],
                    "chunk_index": i,
                },
            ))

        # Batch upsert
        for i in range(0, len(points), 100):
            client.upsert(collection_name=collection, points=points[i:i + 100])

        result["status"] = "ok"
        result["vectors_added"] = len(points)
        logger.info(f"✅ {name}: {len(points)} vectors added")

    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
        logger.error(f"❌ {name}: {e}")

    return result


def seed_qdrant(
    jurisdictions: list[str] = None,
    include_international: bool = True,
    include_us_regulated: bool = True,
    include_us_gray_area: bool = True,
    skip_existing: bool = True,
    progress_callback=None,
) -> dict:
    """
    Main bootstrap function. Seeds Qdrant with jurisdiction intelligence.

    Args:
        jurisdictions: Specific jurisdictions to seed (overrides include_* flags)
        include_international: Seed international regulated markets
        include_us_regulated: Seed US regulated states
        include_us_gray_area: Seed US gray-area states
        skip_existing: Skip jurisdictions already in Qdrant
        progress_callback: Optional fn(step, total, name, status) for UI updates

    Returns: Summary dict with per-jurisdiction results
    """
    # Build target list
    targets = {}
    if jurisdictions:
        # Specific jurisdictions requested
        all_known = {**INTERNATIONAL_JURISDICTIONS, **US_REGULATED_JURISDICTIONS, **US_GRAY_AREA_JURISDICTIONS}
        for j in jurisdictions:
            if j in all_known:
                targets[j] = all_known[j]
            else:
                # Unknown jurisdiction — create a minimal entry
                targets[j] = {
                    "full_name": j,
                    "regulator": "Unknown",
                    "category": "custom",
                    "topics": [f"{j} gambling law", f"{j} gaming regulation", f"{j} slot machines"],
                }
    else:
        if include_international:
            targets.update(INTERNATIONAL_JURISDICTIONS)
        if include_us_regulated:
            targets.update(US_REGULATED_JURISDICTIONS)
        if include_us_gray_area:
            targets.update(US_GRAY_AREA_JURISDICTIONS)

    if not targets:
        return {"status": "no_targets", "message": "No jurisdictions to seed"}

    # Check what's already in Qdrant
    existing = set()
    if skip_existing:
        try:
            from tools.qdrant_store import JurisdictionStore
            store = JurisdictionStore()
            existing = set(store.list_jurisdictions())
            logger.info(f"Existing jurisdictions in Qdrant: {existing}")
        except Exception:
            pass

    # Filter out existing
    to_seed = {k: v for k, v in targets.items() if k not in existing}
    skipped = {k for k in targets if k in existing}

    if not to_seed:
        return {
            "status": "all_exist",
            "message": f"All {len(targets)} jurisdictions already in Qdrant",
            "existing": sorted(existing),
            "skipped": sorted(skipped),
        }

    # Connect to Qdrant
    try:
        client = _get_qdrant_client()
        collection = os.getenv("QDRANT_COLLECTION", "slot_regulations")
        _ensure_collection(client, collection)
    except Exception as e:
        return {"status": "connection_error", "error": str(e)}

    # Seed each jurisdiction
    results = []
    total = len(to_seed)
    for i, (name, info) in enumerate(to_seed.items()):
        logger.info(f"[{i+1}/{total}] Seeding: {name}")
        if progress_callback:
            progress_callback(i + 1, total, name, "generating")

        result = seed_jurisdiction(name, info, client=client, collection=collection)
        results.append(result)

        if progress_callback:
            progress_callback(i + 1, total, name, result.get("status", "unknown"))

        # Rate limit courtesy between jurisdictions
        if i < total - 1:
            time.sleep(1)

    # Summary
    ok = [r for r in results if r.get("status") == "ok"]
    failed = [r for r in results if r.get("status") != "ok"]
    total_vectors = sum(r.get("vectors_added", 0) for r in ok)

    summary = {
        "status": "complete",
        "seeded": len(ok),
        "failed": len(failed),
        "skipped": len(skipped),
        "total_vectors_added": total_vectors,
        "results": results,
        "skipped_jurisdictions": sorted(skipped),
        "timestamp": datetime.now().isoformat(),
    }

    logger.info(f"Bootstrap complete: {len(ok)} seeded, {len(failed)} failed, "
                f"{len(skipped)} skipped, {total_vectors} vectors")

    return summary


# ═══════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════

def main():
    import argparse
    from rich.console import Console
    from rich.table import Table
    from rich.progress import Progress

    console = Console()
    parser = argparse.ArgumentParser(description="Bootstrap Qdrant with jurisdiction intelligence")
    parser.add_argument("--jurisdictions", nargs="+", help="Specific jurisdictions to seed")
    parser.add_argument("--international", action="store_true", help="Seed international markets only")
    parser.add_argument("--us-regulated", action="store_true", help="Seed US regulated states only")
    parser.add_argument("--us-priority", action="store_true", help="Seed US gray-area states only")
    parser.add_argument("--all", action="store_true", help="Seed everything (default)")
    parser.add_argument("--check", action="store_true", help="Check Qdrant status without seeding")
    parser.add_argument("--force", action="store_true", help="Re-seed even if jurisdiction exists")
    parser.add_argument("--no-save", action="store_true", help="Don't save .md files locally")
    args = parser.parse_args()

    console.print("\n[bold cyan]⚡ ArkainBrain — Qdrant Bootstrap[/bold cyan]\n")

    # Check mode
    if args.check:
        status = check_status()
        console.print(f"Status: [bold]{status['status']}[/bold]")
        console.print(f"Collection: {status.get('collection', 'N/A')}")
        console.print(f"Total vectors: {status.get('total_vectors', 0)}")
        console.print(f"Jurisdictions: {status.get('jurisdiction_count', 0)}")
        for j in status.get("jurisdictions", []):
            console.print(f"  ✅ {j}")
        return

    # Determine what to seed
    if args.jurisdictions:
        include_int = include_us = include_gray = False
    elif args.international:
        include_int, include_us, include_gray = True, False, False
    elif args.us_regulated:
        include_int, include_us, include_gray = False, True, False
    elif args.us_priority:
        include_int, include_us, include_gray = False, False, True
    else:
        include_int = include_us = include_gray = True

    # Progress display
    def _progress(step, total, name, status):
        icon = "✅" if status == "ok" else "⏳" if status == "generating" else "❌"
        console.print(f"  [{step}/{total}] {icon} {name}")

    console.print("[cyan]Connecting to Qdrant...[/cyan]")

    result = seed_qdrant(
        jurisdictions=args.jurisdictions,
        include_international=include_int,
        include_us_regulated=include_us,
        include_us_gray_area=include_gray,
        skip_existing=not args.force,
        progress_callback=_progress,
    )

    # Display results
    console.print(f"\n[bold green]{'='*50}[/bold green]")
    console.print(f"[bold]Status:[/bold] {result['status']}")
    console.print(f"[bold]Seeded:[/bold] {result.get('seeded', 0)}")
    console.print(f"[bold]Failed:[/bold] {result.get('failed', 0)}")
    console.print(f"[bold]Skipped:[/bold] {result.get('skipped', 0)} (already in Qdrant)")
    console.print(f"[bold]Total vectors:[/bold] {result.get('total_vectors_added', 0)}")

    if result.get("results"):
        console.print("\n[bold]Details:[/bold]")
        table = Table()
        table.add_column("Jurisdiction", style="cyan")
        table.add_column("Category")
        table.add_column("Status")
        table.add_column("Vectors")
        table.add_column("Notes")
        for r in result["results"]:
            status_style = "green" if r.get("status") == "ok" else "red"
            table.add_row(
                r.get("jurisdiction", "?"),
                r.get("category", ""),
                f"[{status_style}]{r.get('status', '?')}[/{status_style}]",
                str(r.get("vectors_added", 0)),
                r.get("error", r.get("local_path", ""))[:50],
            )
        console.print(table)


if __name__ == "__main__":
    main()
