#!/usr/bin/env python3
"""
Bet Analyzer — Web interface (alle sporten + Flashscore)
Start: python3 app.py
Open op telefoon: http://<mac-ip>:5001
"""

import os
import sys
import json
import base64
import datetime
import tempfile
from pathlib import Path
from flask import Flask, request, render_template_string, jsonify

sys.path.insert(0, str(Path(__file__).parent))
from sports import nhl, nba, mlb, soccer
from scorer import composite_score, ev, rating

try:
    import anthropic
except ImportError:
    print("Installeer eerst: pip3 install anthropic")
    sys.exit(1)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024

CACHE_FILE = Path(__file__).parent / ".stats_cache.json"
SOCCER_COMPS = {"EPL", "LA LIGA", "LALIGA", "BUNDESLIGA", "SERIE A", "LIGUE 1", "LIGUE1", "VOETBAL", "SOCCER"}

# ─── Prompts ──────────────────────────────────────────────────────────────────

EXTRACT_PROMPT = """
Je ziet één of meerdere screenshots van Linemate en/of Flashscore.

Geef een JSON object terug met twee arrays:

1. "bets": ALLE Linemate spelersprops. Elk item:
   - "player": naam (bijv. "S. Rinzel" of "Connor McDavid")
   - "sport": "NHL", "NBA", "MLB", "EPL", "La Liga", "Bundesliga", "Serie A" of "Ligue 1"
   - "team": teamafkorting indien zichtbaar (bijv. "CHI"), anders null
   - "bet_type": bijv. "Over 1.5 Shots on Goal" of "Anytime Goal Scorer"
   - "linemate_odds": odds als decimaal getal (number)
   - "hit_rate": percentage als decimaal: 100%→1.0, 92.3%→0.923 (number)
   - "sample": bijv. "12/13" (string)
   - "sample_n": totaal aantal wedstrijden als getal (number)

2. "matches": ALLE Flashscore wedstrijden. Elk item:
   - "home_team": naam thuisploeg (string)
   - "away_team": naam uitploeg (string)
   - "home_form": laatste 5 resultaten thuisploeg, bijv. "WWDLW", of null
   - "away_form": idem voor uitploeg, of null
   - "h2h": korte H2H samenvatting indien zichtbaar, bijv. "Arsenal won 3/5", of null
   - "competition": competitienaam (bijv. "Premier League"), of null
   - "date": datum indien zichtbaar (bijv. "2025-03-25"), of null
   - "status": "gepland", "bezig" of "afgelopen"
   - "score": score indien zichtbaar (bijv. "2-1"), of null

Als er geen Linemate screenshots zijn, geef dan een lege array voor "bets".
Als er geen Flashscore screenshots zijn, geef dan een lege array voor "matches".
Geef ALLEEN het JSON object terug, geen andere tekst.
"""

FLASHSCORE_PROMPT = """
Je bent een expert sportsbetting analist. Analyseer de volgende wedstrijden en props in het Nederlands.

## WEDSTRIJDEN (Flashscore)
{matches_json}

## PROPS (Linemate — al gescoord)
{bets_json}

## STAP 2 — FLASHSCORE ANALYSE
Geef een scoretabel voor de wedstrijden:
| Wedstrijd | Thuis vorm | Uit vorm | H2H | Advies |
|---|---|---|---|---|

Daarna: **Top 3 wedstrijden** om op te focussen, met 1-zin uitleg per wedstrijd.

{combo_section}

## STAP 5 — TE VERMIJDEN
- Welke wedstrijden vermijd je en waarom? (max 3 bullets)
- Welke props vermijd je en waarom? (max 3 bullets)

## DISCLAIMER
Dit is een statistische analyse ter ondersteuning van je eigen beslissing. Wedden brengt financiële risico's met zich mee. Speel verantwoord.
"""

COMBO_SECTION = """## STAP 4 — COMBINATIE ADVIES
Koppel de beste props aan de beste wedstrijden:
- Welke speler props passen bij de aanbevolen wedstrijden?
- Geef een definitief **Top 3 advies** met onderbouwing (speler + wedstrijd + motivatie)
"""

# ─── HTML ─────────────────────────────────────────────────────────────────────

HTML = """
<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>Bet Analyzer</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, sans-serif; background: #0f0f1a; color: #e0e0e0; min-height: 100vh; }

  .header { background: linear-gradient(135deg, #1a1a2e, #16213e); padding: 20px; text-align: center; border-bottom: 1px solid #2a2a4a; }
  .header h1 { font-size: 22px; color: #fff; }
  .header p { font-size: 13px; color: #888; margin-top: 4px; }

  .container { max-width: 600px; margin: 0 auto; padding: 20px; }

  .upload-area { border: 2px dashed #3a3a6a; border-radius: 16px; padding: 30px 20px; text-align: center; margin-bottom: 16px; cursor: pointer; transition: border-color 0.2s; background: #1a1a2e; }
  .upload-area:active { border-color: #6c63ff; }
  .upload-area .icon { font-size: 40px; margin-bottom: 10px; }
  .upload-area p { color: #aaa; font-size: 14px; }
  .upload-area .hint { font-size: 12px; color: #666; margin-top: 6px; }
  #fileInput { display: none; }
  #preview { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }
  #preview img { height: 80px; border-radius: 8px; border: 2px solid #3a3a6a; }

  .btn { width: 100%; padding: 16px; border: none; border-radius: 12px; font-size: 16px; font-weight: 600; cursor: pointer; background: linear-gradient(135deg, #6c63ff, #4facfe); color: #fff; transition: opacity 0.2s; }
  .btn:active { opacity: 0.85; }
  .btn:disabled { opacity: 0.4; cursor: not-allowed; }

  .loading { text-align: center; padding: 40px 20px; display: none; }
  .spinner { width: 40px; height: 40px; border: 3px solid #3a3a6a; border-top-color: #6c63ff; border-radius: 50%; animation: spin 0.8s linear infinite; margin: 0 auto 16px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .loading p { color: #aaa; font-size: 14px; }

  .results { margin-top: 24px; }

  /* Flashscore sectie */
  .flashscore-section { background: #1a1a2e; border-radius: 14px; margin-bottom: 20px; border: 1px solid #2a3a5a; overflow: hidden; }
  .flashscore-header { padding: 12px 16px; background: linear-gradient(135deg, #16213e, #1a2a4e); border-bottom: 1px solid #2a3a5a; font-size: 14px; font-weight: 700; color: #4facfe; }
  .flashscore-body { padding: 16px; font-size: 13px; line-height: 1.7; color: #ccc; white-space: pre-wrap; word-break: break-word; }
  .flashscore-body strong, .flashscore-body b { color: #fff; }

  /* Bet cards */
  .section-title { font-size: 13px; font-weight: 700; color: #888; text-transform: uppercase; letter-spacing: 1px; margin: 20px 0 12px; }
  .bet-card { background: #1a1a2e; border-radius: 14px; margin-bottom: 14px; overflow: hidden; border: 1px solid #2a2a4a; }
  .bet-header { padding: 14px 16px; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #2a2a4a; }
  .bet-rank { font-size: 13px; color: #888; }
  .bet-rating { font-size: 14px; font-weight: 700; }
  .rating-strong { color: #4ade80; }
  .rating-matig { color: #facc15; }
  .rating-vermijd { color: #f87171; }
  .bet-body { padding: 14px 16px; }
  .bet-player { font-size: 16px; font-weight: 700; color: #fff; margin-bottom: 2px; }
  .bet-type { font-size: 13px; color: #aaa; margin-bottom: 12px; }
  .bet-ev { font-size: 22px; font-weight: 800; margin-bottom: 12px; }
  .ev-positive { color: #4ade80; }
  .ev-low { color: #facc15; }
  .stats-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 10px; }
  .stat-item { background: #0f0f1a; border-radius: 8px; padding: 8px 10px; }
  .stat-label { font-size: 10px; color: #666; text-transform: uppercase; letter-spacing: 0.5px; }
  .stat-value { font-size: 14px; font-weight: 600; color: #e0e0e0; margin-top: 2px; }
  .progress-bar { height: 6px; background: #2a2a4a; border-radius: 3px; margin-top: 10px; }
  .progress-fill { height: 100%; border-radius: 3px; background: linear-gradient(90deg, #6c63ff, #4facfe); }
  .opp-info { font-size: 12px; color: #888; margin-top: 8px; padding-top: 8px; border-top: 1px solid #2a2a4a; }

  .top3 { background: linear-gradient(135deg, #1a1a2e, #16213e); border-radius: 14px; padding: 16px; margin-bottom: 20px; border: 1px solid #3a3a6a; }
  .top3 h2 { font-size: 16px; color: #fff; margin-bottom: 12px; }
  .top3-item { display: flex; align-items: center; gap: 10px; padding: 8px 0; border-bottom: 1px solid #2a2a4a; }
  .top3-item:last-child { border-bottom: none; }
  .top3-num { width: 24px; height: 24px; border-radius: 50%; background: #6c63ff; color: #fff; font-size: 12px; font-weight: 700; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
  .top3-text { flex: 1; }
  .top3-name { font-size: 14px; font-weight: 600; color: #fff; }
  .top3-sub { font-size: 12px; color: #888; }
  .top3-ev { font-size: 14px; font-weight: 700; color: #4ade80; }

  .disclaimer { font-size: 11px; color: #555; text-align: center; padding: 16px; line-height: 1.5; }
  .error { background: #2a1a1a; border: 1px solid #f87171; border-radius: 12px; padding: 16px; color: #f87171; font-size: 14px; margin-top: 16px; }
</style>
</head>
<body>

<div class="header">
  <h1>🎯 Bet Analyzer</h1>
  <p>Linemate + Flashscore · NHL · NBA · MLB · Voetbal</p>
</div>

<div class="container">
  <div class="upload-area" onclick="document.getElementById('fileInput').click()">
    <div class="icon">📱</div>
    <p>Tik om screenshots te selecteren</p>
    <p class="hint">Linemate en/of Flashscore screenshots</p>
  </div>
  <input type="file" id="fileInput" accept="image/*" multiple onchange="handleFiles(this)">
  <div id="preview"></div>
  <button class="btn" id="analyzeBtn" onclick="analyze()" disabled>Analyseer</button>

  <div class="loading" id="loading">
    <div class="spinner"></div>
    <p id="loadingText">Screenshots analyseren...</p>
  </div>

  <div id="results"></div>
</div>

<script>
let selectedFiles = [];

function handleFiles(input) {
  selectedFiles = Array.from(input.files);
  const preview = document.getElementById('preview');
  preview.innerHTML = '';
  selectedFiles.forEach(file => {
    const img = document.createElement('img');
    img.src = URL.createObjectURL(file);
    preview.appendChild(img);
  });
  document.getElementById('analyzeBtn').disabled = selectedFiles.length === 0;
}

async function analyze() {
  if (selectedFiles.length === 0) return;
  document.getElementById('analyzeBtn').disabled = true;
  document.getElementById('loading').style.display = 'block';
  document.getElementById('results').innerHTML = '';

  const steps = [
    'Screenshots herkennen...',
    'Spelers opzoeken...',
    'Seizoensstats ophalen...',
    'Wedstrijden analyseren...',
    'Score berekenen...'
  ];
  let step = 0;
  const interval = setInterval(() => {
    if (step < steps.length - 1) step++;
    document.getElementById('loadingText').textContent = steps[step];
  }, 3500);

  const formData = new FormData();
  selectedFiles.forEach(f => formData.append('images', f));

  try {
    const resp = await fetch('/analyze', { method: 'POST', body: formData });
    const data = await resp.json();
    clearInterval(interval);
    document.getElementById('loading').style.display = 'none';
    document.getElementById('analyzeBtn').disabled = false;
    if (data.error) {
      document.getElementById('results').innerHTML = `<div class="error">❌ ${data.error}</div>`;
    } else {
      renderResults(data);
    }
  } catch(e) {
    clearInterval(interval);
    document.getElementById('loading').style.display = 'none';
    document.getElementById('analyzeBtn').disabled = false;
    document.getElementById('results').innerHTML = `<div class="error">❌ Verbindingsfout: ${e.message}</div>`;
  }
}

function ratingClass(r) {
  if (r.includes('Sterk')) return 'rating-strong';
  if (r.includes('Matig')) return 'rating-matig';
  return 'rating-vermijd';
}

function simpleMarkdown(text) {
  return text
    .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
    .replace(/^## (.+)$/gm, '<strong style="color:#4facfe;font-size:14px;">$1</strong>')
    .replace(/^### (.+)$/gm, '<strong>$1</strong>')
    .replace(/^- (.+)$/gm, '• $1')
    .replace(/\|(.+)\|/g, (m) => {
      const cells = m.split('|').filter(c => c.trim() && !c.match(/^[\s\-:]+$/));
      if (!cells.length) return '';
      return '<span style="display:block;font-family:monospace;font-size:12px;">' +
        cells.map(c => c.trim().padEnd(18)).join('  ') + '</span>';
    });
}

function renderResults(data) {
  const bets = data.bets || [];
  const top3 = data.top3 || [];
  const flashscore = data.flashscore || '';
  let html = '';

  // Flashscore analyse
  if (flashscore) {
    html += `<div class="flashscore-section">
      <div class="flashscore-header">📺 Flashscore Analyse</div>
      <div class="flashscore-body">${simpleMarkdown(flashscore)}</div>
    </div>`;
  }

  if (bets.length === 0) {
    document.getElementById('results').innerHTML = html;
    return;
  }

  // Top 3 props
  if (top3.length > 0) {
    html += '<div class="top3"><h2>🏆 Top prop aanbevelingen</h2>';
    top3.forEach((b, i) => {
      html += `<div class="top3-item">
        <div class="top3-num">${i+1}</div>
        <div class="top3-text">
          <div class="top3-name">${b.player}</div>
          <div class="top3-sub">${b.bet_type} @ ${b.odds}</div>
        </div>
        <div class="top3-ev">EV ${b.ev >= 0 ? '+' : ''}${b.ev.toFixed(2)}</div>
      </div>`;
    });
    html += '</div>';
  }

  // Alle props
  if (flashscore) {
    html += '<div class="section-title">📊 Linemate Props</div>';
  }
  bets.forEach((bet, i) => {
    const composite = (bet.composite * 100).toFixed(0);
    const evClass = bet.ev >= 0.05 ? 'ev-positive' : 'ev-low';
    const sportIcon = {NHL:'🏒', NBA:'🏀', MLB:'⚾'}[bet.sport] || '⚽';
    html += `
    <div class="bet-card">
      <div class="bet-header">
        <span class="bet-rank">${sportIcon} #${i+1} van ${bets.length}</span>
        <span class="bet-rating ${ratingClass(bet.rating)}">${bet.rating}</span>
      </div>
      <div class="bet-body">
        <div class="bet-player">${bet.player}</div>
        <div class="bet-type">${bet.bet_type} · ${bet.sport}</div>
        <div class="bet-ev ${evClass}">EV ${bet.ev >= 0 ? '+' : ''}${bet.ev.toFixed(3)}</div>
        <div class="stats-grid">
          <div class="stat-item">
            <div class="stat-label">Linemate HR</div>
            <div class="stat-value">${(bet.linemate_hr * 100).toFixed(1)}%</div>
          </div>
          <div class="stat-item">
            <div class="stat-label">Seizoens HR</div>
            <div class="stat-value">${(bet.season_hr * 100).toFixed(1)}%</div>
          </div>
          <div class="stat-item">
            <div class="stat-label">Odds</div>
            <div class="stat-value">${bet.odds}</div>
          </div>
          <div class="stat-item">
            <div class="stat-label">Sample</div>
            <div class="stat-value">${bet.sample}</div>
          </div>
        </div>
        <div class="progress-bar">
          <div class="progress-fill" style="width: ${composite}%"></div>
        </div>
        <div class="opp-info">
          Composite: ${composite}%
          ${bet.opponent ? ` · Speelt vs ${bet.opponent}` : ''}
          ${bet.gaa ? ` · GAA ${bet.gaa}` : ''}
        </div>
      </div>
    </div>`;
  });

  html += '<div class="disclaimer">⚠️ Past performance biedt geen garantie. Gok verantwoord.</div>';
  document.getElementById('results').innerHTML = html;
}
</script>
</body>
</html>
"""

# ─── Cache ────────────────────────────────────────────────────────────────────

def _load_cache():
    today = datetime.date.today().isoformat()
    if CACHE_FILE.exists():
        try:
            data = json.loads(CACHE_FILE.read_text())
            if data.get("date") == today:
                return data.get("players", {})
        except Exception:
            pass
    return {}


def _save_cache(players_dict):
    data = {"date": datetime.date.today().isoformat(), "players": players_dict}
    CACHE_FILE.write_text(json.dumps(data))


# ─── Extractie ────────────────────────────────────────────────────────────────

def extract_bets(client, image_paths):
    content = []
    for path in image_paths:
        ext = Path(path).suffix.lower()
        media_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                     ".png": "image/png", ".webp": "image/webp"}
        media_type = media_map.get(ext, "image/jpeg")
        with open(path, "rb") as f:
            img_data = base64.standard_b64encode(f.read()).decode("utf-8")
        content.append({"type": "image", "source": {"type": "base64",
                                                      "media_type": media_type,
                                                      "data": img_data}})
    content.append({"type": "text", "text": EXTRACT_PROMPT})
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=4096,
        messages=[{"role": "user", "content": content}]
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip().strip("```")
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data, []
        return data.get("bets", []), data.get("matches", [])
    except Exception:
        return [], []


# ─── Flashscore analyse ───────────────────────────────────────────────────────

def analyze_flashscore(client, matches, enriched_bets):
    has_bets = bool(enriched_bets)
    bets_summary = [{
        "player":    b.get("player"),
        "sport":     b.get("sport"),
        "bet_type":  b.get("bet_type"),
        "odds":      b.get("odds"),
        "ev":        b.get("ev"),
        "rating":    b.get("rating"),
        "composite": b.get("composite"),
    } for b in enriched_bets]

    prompt = FLASHSCORE_PROMPT.format(
        matches_json=json.dumps(matches, ensure_ascii=False, indent=2),
        bets_json=json.dumps(bets_summary, ensure_ascii=False, indent=2) if has_bets else "(geen props)",
        combo_section=COMBO_SECTION if has_bets else "",
    )
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text


# ─── Bet verrijken ────────────────────────────────────────────────────────────

def enrich_bet(bet, cache):
    sport       = (bet.get("sport") or "").upper().strip()
    player_name = bet.get("player", "")
    team_hint   = bet.get("team") or ""
    bet_type    = bet.get("bet_type", "")
    sample_n    = bet.get("sample_n") or 5

    player_stats   = {}
    opponent_stats = {}
    opponent_name  = None

    cache_key = f"{sport}::{player_name}"

    if cache_key in cache:
        cached        = cache[cache_key]
        player_stats  = cached.get("player_stats", {})
        opponent_name = cached.get("opponent")
        opponent_stats = cached.get("opponent_stats", {})
    else:
        if sport == "NHL":
            player_id, team = nhl.find_player(player_name)
            if player_id:
                player_stats  = nhl.get_player_stats(player_id)
                opponent_name = nhl.get_opponent(team) if team else None
                if opponent_name:
                    opponent_stats = nhl.get_team_defense(opponent_name)

        elif sport == "NBA":
            player = nba.find_player(player_name)
            if player:
                player_stats = nba.get_player_stats(player["id"])

        elif sport == "MLB":
            player = mlb.find_player(player_name)
            if player:
                pos_code = player.get("primaryPosition", {}).get("code", "")
                pos_type = "pitching" if pos_code == "1" else "hitting"
                player_stats = mlb.get_player_stats(player.get("id"), position_type=pos_type)

        elif sport in SOCCER_COMPS:
            comp = sport if sport != "VOETBAL" else "EPL"
            player = soccer.find_player(player_name, team_hint=team_hint, competition=comp)
            if player:
                player_stats  = soccer.get_player_stats(player.get("id"), player.get("team_id"), comp)
                opponent_stats = soccer.get_team_defense(player.get("team_id")) if player.get("team_id") else {}

        cache[cache_key] = {
            "player_stats":   player_stats,
            "opponent":       opponent_name,
            "opponent_stats": opponent_stats,
        }

    odds  = bet.get("linemate_odds", 1.0)
    score = composite_score(
        linemate_hit_rate=bet.get("hit_rate", 0.5),
        sample_size=sample_n,
        bet_type=bet_type,
        player_stats=player_stats,
        opponent_stats=opponent_stats,
        sport=sport,
    )
    ev_score = ev(score["composite"], odds)
    rat      = rating(ev_score, score["composite"])

    return {
        "player":      player_name,
        "sport":       bet.get("sport", "?"),
        "bet_type":    bet_type,
        "odds":        odds,
        "sample":      bet.get("sample", "?"),
        "linemate_hr": score["linemate_hr"],
        "season_hr":   score["season_hr"],
        "composite":   score["composite"],
        "ev":          ev_score,
        "rating":      rat,
        "opponent":    opponent_name,
        "gaa":         opponent_stats.get("goals_against_avg"),
    }


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/analyze", methods=["POST"])
def analyze():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return jsonify({"error": "Geen API key ingesteld op de Mac."})

    files = request.files.getlist("images")
    if not files:
        return jsonify({"error": "Geen afbeeldingen ontvangen."})

    tmp_paths = []
    try:
        for f in files:
            suffix = Path(f.filename).suffix or ".png"
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
            f.save(tmp.name)
            tmp_paths.append(tmp.name)

        client = anthropic.Anthropic(api_key=api_key)

        # Stap 1: extraheer bets + matches
        bets, matches = extract_bets(client, tmp_paths)
        if not bets and not matches:
            return jsonify({"error": "Geen bets of wedstrijden gevonden in de afbeeldingen."})

        # Stap 2: verrijk props
        cache    = _load_cache()
        enriched = [enrich_bet(bet, cache) for bet in bets]
        _save_cache(cache)
        enriched.sort(key=lambda x: x["ev"], reverse=True)

        # Stap 3: Flashscore analyse
        flashscore_text = ""
        if matches:
            flashscore_text = analyze_flashscore(client, matches, enriched)

        # Top 3 props
        top3 = [b for b in enriched if b["rating"].startswith("✅")][:3]
        if not top3:
            top3 = enriched[:3]
        top3_out = [{"player": b["player"], "bet_type": b["bet_type"],
                     "odds": b["odds"], "ev": b["ev"]} for b in top3]

        return jsonify({
            "bets":       enriched,
            "top3":       top3_out,
            "flashscore": flashscore_text,
        })

    finally:
        for p in tmp_paths:
            try:
                os.unlink(p)
            except Exception:
                pass


# ─── Start ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import socket
    try:
        ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        ip = "onbekend"

    print("\n" + "=" * 50)
    print("  Bet Analyzer — Web interface")
    print("=" * 50)
    print(f"\n  Open op je Mac:      http://localhost:5001")
    print(f"  Open op je telefoon: http://{ip}:5001")
    print(f"\n  (Mac en telefoon op hetzelfde wifi)")
    print("\n  Stop met Ctrl+C")
    print("=" * 50 + "\n")

    app.run(host="0.0.0.0", port=5001, debug=False)
