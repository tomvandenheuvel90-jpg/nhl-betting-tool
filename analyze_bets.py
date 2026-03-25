#!/usr/bin/env python3
"""
Bet Analyzer — Linemate screenshot → beste bets op basis van hit rate + odds
Gebruik: python3 analyze_bets.py <pad-naar-screenshot> [--api-key <key>]
"""

import sys
import os
import base64
import json
import argparse
from pathlib import Path

try:
    import anthropic
except ImportError:
    print("Installeer eerst: pip3 install anthropic")
    sys.exit(1)


EXTRACT_PROMPT = """
Je ziet een screenshot van de Linemate app met sportsweddenschappen statistieken.

Extraheer ALLE zichtbare weddenschappen en geef ze terug als een JSON array.
Elk item heeft deze velden:
- "player": naam van de speler (string)
- "sport": sport indien zichtbaar, anders "onbekend" (string)
- "bet_type": het type weddenschap, bijv. "Over 1.5 Shots on Goal" (string)
- "linemate_odds": de odds die zichtbaar zijn in de app, als decimaal getal (number, bijv. 1.77)
- "hit_rate": het percentage als decimaal, bijv. 100% → 1.0, 92.3% → 0.923 (number)
- "sample": aantal gewonnen / totaal, bijv. "6/6" (string)

Geef ALLEEN de JSON array terug, geen andere tekst.
"""

SCORE_PROMPT = """
Je bent een sportsweddenschappen analist. Hier zijn de geëxtraheerde bet-statistieken:

{data}

{bet365_context}

Analyseer deze bets en geef een gestructureerde aanbeveling:

1. **Score** elke bet op basis van:
   - Hit rate (hoe consistenter, hoe beter)
   - Sample size (meer wedstrijden = betrouwbaarder)
   - Odds-waarde (hogere odds bij hoge hit rate = meer waarde)
   - Expected Value (EV) = hit_rate × (odds - 1) - (1 - hit_rate)

2. **Rangschik** van beste naar slechtste bet

3. Geef per bet:
   - Naam + bet type
   - Hit rate + sample size
   - Odds (Linemate{bet365_label})
   - EV score
   - Korte motivatie (1 zin)
   - Aanbeveling: ✅ Sterk / ⚠️ Matig / ❌ Vermijd

4. Sluit af met een **Top 3** die je vandaag zou overwegen

Schrijf in het Nederlands. Voeg altijd een disclaimer toe dat past performance geen garantie biedt.
"""


def image_to_base64(path: str) -> tuple[str, str]:
    """Converteer afbeelding naar base64 en detecteer mediatype."""
    ext = Path(path).suffix.lower()
    media_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}
    media_type = media_map.get(ext, "image/jpeg")
    with open(path, "rb") as f:
        return base64.standard_b64encode(f.read()).decode("utf-8"), media_type


def extract_bets(client: anthropic.Anthropic, image_path: str) -> list[dict]:
    """Gebruik Claude vision om bets uit de screenshot te extraheren."""
    print("📷  Afbeelding analyseren...")
    img_data, media_type = image_to_base64(image_path)

    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=2048,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64",
                                              "media_type": media_type,
                                              "data": img_data}},
                {"type": "text", "text": EXTRACT_PROMPT}
            ]
        }]
    )

    raw = response.content[0].text.strip()
    # Verwijder eventuele markdown code blocks
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip().strip("```")

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        print("⚠️  Kon JSON niet parsen, probeer ruwe output:")
        print(raw)
        return []


def analyze_bets(client: anthropic.Anthropic, bets: list,
                 bet365_odds=None) -> str:
    """Laat Claude de bets analyseren en rangschikken."""
    print("🧠  Bets analyseren en rangschikken...")

    # Voeg EV toe aan elk bet
    for bet in bets:
        odds = bet.get("linemate_odds", 1.0)
        hr = bet.get("hit_rate", 0.0)
        bet["ev"] = round(hr * (odds - 1) - (1 - hr), 4)
        if bet365_odds and bet.get("player") in bet365_odds:
            bet["bet365_odds"] = bet365_odds[bet["player"]]

    data_str = json.dumps(bets, ensure_ascii=False, indent=2)

    if bet365_odds:
        b365_ctx = f"\nDe gebruiker heeft deze Bet365-odds opgegeven: {json.dumps(bet365_odds, ensure_ascii=False)}\nGebruik Bet365-odds waar beschikbaar voor de EV-berekening."
        b365_label = " / Bet365"
    else:
        b365_ctx = "\nGeen aparte Bet365-odds opgegeven — gebruik de Linemate-odds."
        b365_label = ""

    prompt = SCORE_PROMPT.format(
        data=data_str,
        bet365_context=b365_ctx,
        bet365_label=b365_label
    )

    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text


def main():
    parser = argparse.ArgumentParser(
        description="Analyseer Linemate screenshots en rangschik de beste bets")
    parser.add_argument("image", help="Pad naar de Linemate screenshot")
    parser.add_argument("--api-key", help="Anthropic API key (of stel ANTHROPIC_API_KEY in)")
    parser.add_argument("--bet365", nargs="*",
                        help='Bet365 odds als "Speler:1.85" paren, bijv. --bet365 "M. Carcone:1.80"')
    args = parser.parse_args()

    # API key ophalen
    api_key = args.api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("❌  Geen API key gevonden.")
        print("   Stel in via: export ANTHROPIC_API_KEY='sk-ant-...'")
        print("   Of gebruik: --api-key sk-ant-...")
        sys.exit(1)

    if not Path(args.image).exists():
        print(f"❌  Bestand niet gevonden: {args.image}")
        sys.exit(1)

    # Bet365 odds parsen indien opgegeven
    bet365_odds = None
    if args.bet365:
        bet365_odds = {}
        for item in args.bet365:
            if ":" in item:
                speler, odds = item.rsplit(":", 1)
                try:
                    bet365_odds[speler.strip()] = float(odds.strip())
                except ValueError:
                    print(f"⚠️  Ongeldige odds genegeerd: {item}")

    client = anthropic.Anthropic(api_key=api_key)

    # Stap 1: extraheer bets
    bets = extract_bets(client, args.image)
    if not bets:
        print("❌  Geen bets gevonden in de afbeelding.")
        sys.exit(1)

    print(f"✅  {len(bets)} bets gevonden\n")

    # Stap 2: analyseer
    analysis = analyze_bets(client, bets, bet365_odds)

    print("=" * 60)
    print(analysis)
    print("=" * 60)


if __name__ == "__main__":
    main()
