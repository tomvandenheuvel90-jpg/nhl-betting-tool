"""
Diagnose-script voor bankroll-saldo.

Run lokaal vanuit /Users/tomvandenheuvel/Documents/BetAnalyzer:
    python3 diag_bankroll.py

Het script maakt geen wijzigingen — het leest alleen Supabase + lokale JSON
en print stap voor stap hoe je huidige saldo wordt opgebouwd.
"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Streamlit secrets laden zonder Streamlit te starten
try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore

_secrets_path = os.path.join(os.path.dirname(__file__), ".streamlit", "secrets.toml")
if os.path.exists(_secrets_path):
    with open(_secrets_path, "rb") as f:
        _secrets = tomllib.load(f)
    for k, v in _secrets.items():
        if isinstance(v, (str, int, float)):
            os.environ.setdefault(k, str(v))

# Streamlit-stub zodat db.py zijn st.secrets call niet crasht
class _SecretsShim(dict):
    def __getattr__(self, k):
        return self.get(k, "")
class _StStub:
    secrets = _SecretsShim(_secrets if os.path.exists(_secrets_path) else {})
    @staticmethod
    def cache_data(*a, **kw):
        def deco(fn): return fn
        return deco if a and callable(a[0]) is False else (a[0] if a else deco)
sys.modules.setdefault("streamlit", _StStub())  # type: ignore

import db  # noqa: E402

# Supabase verbinding initialiseren
_sb_url = os.environ.get("SUPABASE_URL", "")
_sb_key = os.environ.get("SUPABASE_KEY", "")
if _sb_url and _sb_key:
    ok = db.init(_sb_url, _sb_key)
    print(f"[Supabase] verbinding: {'OK' if ok else 'MISLUKT — fallback op lokale JSON'}")
else:
    print("[Supabase] geen credentials gevonden in secrets.toml — gebruikt lokale JSON")

print("=" * 70)
print("BANKROLL DIAGNOSE")
print("=" * 70)

start_bk = float(db.get_setting("start_bankroll") or 0.0)
print(f"\n[1] Startbankroll (settings.start_bankroll)  : € {start_bk:>10.2f}")

# Raw dump van de settings tabel om bug uit te sluiten
try:
    _raw_settings = db._supabase.table("settings").select("*").execute().data
    print(f"    Raw settings rows in Supabase ({len(_raw_settings)}):")
    for r in _raw_settings:
        print(f"      {r}")
except Exception as e:
    print(f"    (kon settings tabel niet lezen: {e})")

mutations = db.load_bankroll_mutations()
print(f"\n[2] Bankroll-mutaties ({len(mutations)} stuks):")
mut_total = 0.0
for m in mutations:
    bedrag = float(m.get("bedrag", 0))
    mut_total += bedrag
    print(f"     {m.get('datum',''):<12} € {bedrag:>+8.2f}  {m.get('omschrijving','')}")
print(f"     {'TOTAAL':<12} € {mut_total:>+8.2f}")

print("\n[3] Resultaten tabel (alle rijen):")
res = db.load_resultaten()
open_rows     = [r for r in res if r.get("uitkomst") == "open"]
settled_rows  = [r for r in res if r.get("uitkomst") in ("gewonnen", "verloren", "void")]
other_rows    = [r for r in res if r.get("uitkomst") not in ("open", "gewonnen", "verloren", "void")]

print(f"\n   OPEN ({len(open_rows)}):")
open_inzet_res = 0.0
for r in open_rows:
    inzet = float(r.get("inzet", 0))
    open_inzet_res += inzet
    print(f"     id={str(r.get('id',''))[:25]:<25} datum={(r.get('datum','') or '')[:10]} inzet=€{inzet:>7.2f} odds={float(r.get('odds') or 0):>5.2f} | {str(r.get('speler',''))[:25]:<25} {str(r.get('bet',''))[:30]}")
print(f"   → open inzet uit resultaten:               € {open_inzet_res:>+10.2f}")

print(f"\n   GESETTLED ({len(settled_rows)}):")
settled_wl = 0.0
for r in settled_rows:
    wl = float(r.get("winst_verlies", 0))
    settled_wl += wl
    print(f"     id={str(r.get('id',''))[:25]:<25} datum={(r.get('datum','') or '')[:10]} {r.get('uitkomst',''):<10} inzet=€{float(r.get('inzet',0)):>7.2f} wl=€{wl:>+8.2f} | {str(r.get('speler',''))[:25]:<25} {str(r.get('bet',''))[:30]}")
print(f"   → totaal P&L gesettlede bets:              € {settled_wl:>+10.2f}")

if other_rows:
    print(f"\n   ANDERE STATUS ({len(other_rows)}) — verdacht!")
    for r in other_rows:
        print(f"     id={r.get('id')} uitkomst={r.get('uitkomst')!r} inzet={r.get('inzet')} wl={r.get('winst_verlies')}")

print("\n[4] Parlays tabel:")
parlays = db.load_parlays()
res_ids = {str(r.get("id","")) for r in res}
open_parlay_extra_inzet = 0.0
print(f"   ({len(parlays)} parlays totaal)")
for p in parlays:
    pid = f"parlay_{p.get('id')}"
    in_res = pid in res_ids
    status = p.get("uitkomst") or "open"
    inzet = float(p.get("inzet") or 0)
    wl = float(p.get("winst_verlies") or 0)
    extra = ""
    if status == "open" and not in_res:
        open_parlay_extra_inzet += inzet
        extra = "  ← telt EXTRA mee als open inzet (niet in resultaten)"
    elif status == "open" and in_res:
        extra = "  (open + ook in resultaten → geen dubbeltelling)"
    elif status in ("gewonnen", "verloren", "void"):
        extra = f"  (gesettled: wl=€{wl:+.2f})"
    print(f"     {pid:<20} status={status:<10} inzet=€{inzet:>7.2f} odds={float(p.get('gecombineerde_odds') or 0):>5.2f} in_res={in_res}{extra}")
print(f"   → open inzet uit parlays (niet in res):    € {open_parlay_extra_inzet:>+10.2f}")

total_open_inzet = open_inzet_res + open_parlay_extra_inzet
saldo = start_bk + mut_total + settled_wl - total_open_inzet

print("\n" + "=" * 70)
print("BEREKENING (zelfde formule als in de app):")
print("=" * 70)
print(f"   Startbankroll                 : € {start_bk:>+10.2f}")
print(f" + Mutaties (storting/opname)    : € {mut_total:>+10.2f}")
print(f" + P&L gesettlede bets           : € {settled_wl:>+10.2f}")
print(f" - Open inzet (resultaten)       : € {open_inzet_res:>+10.2f}")
print(f" - Open inzet (parlays extra)    : € {open_parlay_extra_inzet:>+10.2f}")
print(f"   {'─'*40}")
print(f"   HUIDIG SALDO                  : € {saldo:>+10.2f}")
print("=" * 70)

# Sanity-checks
print("\n[5] Sanity-checks op verdachte rijen:")
issues = []
for r in res:
    inzet = float(r.get("inzet") or 0)
    odds = float(r.get("odds") or 0)
    wl = float(r.get("winst_verlies") or 0)
    uit = r.get("uitkomst")
    rid = r.get("id")
    if uit == "open" and wl != 0:
        issues.append(f"   ⚠️  {rid} is OPEN maar heeft wl=€{wl:+.2f} (zou 0 moeten zijn)")
    if uit == "gewonnen":
        verwacht = round(inzet * (odds - 1), 2)
        if abs(wl - verwacht) > 0.05:
            issues.append(f"   ⚠️  {rid} GEWONNEN: wl=€{wl:+.2f}, verwacht €{verwacht:+.2f} (inzet €{inzet} × (odds {odds}−1))")
    if uit == "verloren":
        verwacht = round(-inzet, 2)
        if abs(wl - verwacht) > 0.05:
            issues.append(f"   ⚠️  {rid} VERLOREN: wl=€{wl:+.2f}, verwacht €{verwacht:+.2f}")
    if uit == "void" and wl != 0:
        issues.append(f"   ⚠️  {rid} VOID maar wl=€{wl:+.2f} (zou 0 moeten zijn)")

# Dubbele entries (zelfde id of parlay-leg ook als losse bet)
ids_seen = {}
for r in res:
    rid = r.get("id")
    ids_seen[rid] = ids_seen.get(rid, 0) + 1
for rid, n in ids_seen.items():
    if n > 1:
        issues.append(f"   ⚠️  id {rid} komt {n}x voor in resultaten (duplicaat)")

if issues:
    for i in issues:
        print(i)
else:
    print("   ✅ Geen verdachte rijen gevonden.")
