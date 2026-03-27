-- Bet Analyzer tabellen setup

-- Geschiedenis tabel (uitgebreid)
CREATE TABLE IF NOT EXISTS geschiedenis (
  id TEXT PRIMARY KEY,
  datum TEXT,
  tijd TEXT,
  top5_json TEXT,
  alle_props_json TEXT,
  parlay_suggesties_json TEXT
);

-- Favorieten tabel
CREATE TABLE IF NOT EXISTS favorieten (
  id TEXT PRIMARY KEY,
  datum TEXT,
  speler TEXT,
  bet TEXT,
  odds REAL,
  ev_score REAL,
  sport TEXT,
  bet365_status TEXT
);

-- Resultaten tabel
CREATE TABLE IF NOT EXISTS resultaten (
  id TEXT PRIMARY KEY,
  datum TEXT,
  speler TEXT,
  bet TEXT,
  odds REAL,
  inzet REAL,
  uitkomst TEXT,
  winst_verlies REAL,
  sport TEXT,
  ev_score REAL,
  is_parlay BOOLEAN DEFAULT FALSE
);

-- Parlays tabel
CREATE TABLE IF NOT EXISTS parlays (
  id TEXT PRIMARY KEY,
  datum TEXT,
  props_json TEXT,
  gecombineerde_odds REAL,
  hit_kans REAL,
  ev_score REAL,
  inzet REAL,
  uitkomst TEXT DEFAULT 'open',
  winst_verlies REAL DEFAULT 0,
  legs_json TEXT
);
