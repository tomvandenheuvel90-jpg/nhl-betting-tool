# Bet Analyzer

Analyseert Linemate screenshots en rangschikt de beste bets op basis van hit rate, odds en Expected Value.

## Installatie

```bash
pip3 install anthropic
```

## API Key instellen

Je hebt een Anthropic API key nodig. Maak er een aan op https://console.anthropic.com

Stel de key eenmalig in via de terminal:
```bash
export ANTHROPIC_API_KEY='sk-ant-...'
```

Of geef hem mee bij elke aanroep via `--api-key`.

## Gebruik

### Basis (alleen Linemate screenshot)
```bash
python3 analyze_bets.py screenshot.png
```

### Met Bet365 odds
```bash
python3 analyze_bets.py screenshot.png --bet365 "M. Carcone:1.80" "A. Gritsyuk:1.55"
```

### Met API key direct meegeven
```bash
python3 analyze_bets.py screenshot.png --api-key sk-ant-...
```

## Hoe werkt het?

1. De screenshot wordt geanalyseerd via Claude vision
2. Alle spelers, bet-types, odds en hit rates worden geëxtraheerd
3. Per bet wordt een Expected Value (EV) berekend:
   `EV = hit_rate × (odds - 1) - (1 - hit_rate)`
4. Bets worden gerangschikt van beste naar slechtste
5. Je krijgt een Top 3 aanbeveling

## Disclaimer

Past performance biedt geen garantie voor toekomstige resultaten. Gok verantwoord.
