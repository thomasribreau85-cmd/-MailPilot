# ============================================================
# database.py — Couche SQLite pour MailPilot
# Remplace tous les fichiers JSON épars par une BDD unique.
# Thread-safe + multi-process grâce au mode WAL.
# ============================================================

import json
import os
import sqlite3
import threading
from pathlib import Path

# ── Chemin de la BDD ─────────────────────────────────────────
DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH  = DATA_DIR / "mailpilot.db"

# Connexions thread-locales (Flask est multi-thread)
_local = threading.local()


def _conn():
    """Retourne une connexion SQLite thread-locale."""
    if getattr(_local, "conn", None) is None:
        c = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA synchronous=NORMAL")
        _local.conn = c
    return _local.conn


# ── Initialisation du schéma ──────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS comptes (
    id               TEXT PRIMARY KEY,
    access_token     TEXT NOT NULL DEFAULT '',
    login_email      TEXT NOT NULL DEFAULT '',
    password_hash    TEXT NOT NULL DEFAULT '',
    password_version INTEGER NOT NULL DEFAULT 0,
    nom              TEXT NOT NULL DEFAULT '',
    agence           TEXT NOT NULL DEFAULT '',
    tel              TEXT NOT NULL DEFAULT '',
    zone             TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS boites (
    id            TEXT PRIMARY KEY,
    compte_id     TEXT NOT NULL REFERENCES comptes(id) ON DELETE CASCADE,
    email         TEXT NOT NULL DEFAULT '',
    provider      TEXT NOT NULL DEFAULT 'gmail',
    token_path    TEXT NOT NULL DEFAULT '',
    connecte      INTEGER NOT NULL DEFAULT 0,
    labels_actifs TEXT NOT NULL DEFAULT '[]',
    intervalle    TEXT NOT NULL DEFAULT '60',
    bilan_jour    TEXT NOT NULL DEFAULT '0',
    bilan_heure   TEXT NOT NULL DEFAULT '8',
    instructions  TEXT NOT NULL DEFAULT '',
    imap_server   TEXT NOT NULL DEFAULT '',
    imap_port     TEXT NOT NULL DEFAULT '993',
    smtp_server   TEXT NOT NULL DEFAULT '',
    smtp_port     TEXT NOT NULL DEFAULT '465',
    imap_password TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS agents_actifs (
    compte_id TEXT NOT NULL,
    boite_id  TEXT NOT NULL,
    PRIMARY KEY (compte_id, boite_id)
);

CREATE TABLE IF NOT EXISTS emails_recents (
    id               TEXT NOT NULL,
    compte_id        TEXT NOT NULL,
    boite_id         TEXT NOT NULL,
    sujet            TEXT NOT NULL DEFAULT '',
    expediteur       TEXT NOT NULL DEFAULT '',
    corps            TEXT NOT NULL DEFAULT '',
    categorie        TEXT NOT NULL DEFAULT '',
    brouillon        TEXT NOT NULL DEFAULT '',
    brouillon_modifie INTEGER NOT NULL DEFAULT 0,
    traite_at        TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (id, compte_id, boite_id)
);

CREATE TABLE IF NOT EXISTS agenda (
    id                     TEXT NOT NULL,
    compte_id              TEXT NOT NULL,
    titre                  TEXT NOT NULL DEFAULT '',
    client_nom             TEXT NOT NULL DEFAULT '',
    client_email           TEXT NOT NULL DEFAULT '',
    client_tel             TEXT NOT NULL DEFAULT '',
    adresse                TEXT NOT NULL DEFAULT '',
    date                   TEXT NOT NULL DEFAULT '',
    heure_debut            TEXT NOT NULL DEFAULT '09:00',
    heure_fin              TEXT NOT NULL DEFAULT '10:00',
    type                   TEXT NOT NULL DEFAULT 'autre',
    statut                 TEXT NOT NULL DEFAULT 'confirme',
    notes                  TEXT NOT NULL DEFAULT '',
    boite_id               TEXT NOT NULL DEFAULT '',
    confirmation_envoyee_at TEXT,
    created_at             TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (id, compte_id)
);

CREATE TABLE IF NOT EXISTS stats (
    compte_id TEXT NOT NULL,
    boite_id  TEXT NOT NULL,
    semaine   INTEGER NOT NULL,
    annee     INTEGER NOT NULL,
    traites   INTEGER NOT NULL DEFAULT 0,
    brouillons INTEGER NOT NULL DEFAULT 0,
    categories TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (compte_id, boite_id, semaine, annee)
);

CREATE TABLE IF NOT EXISTS stats_hist (
    compte_id  TEXT NOT NULL,
    boite_id   TEXT NOT NULL,
    semaine    INTEGER NOT NULL,
    annee      INTEGER NOT NULL,
    traites    INTEGER NOT NULL DEFAULT 0,
    brouillons INTEGER NOT NULL DEFAULT 0,
    categories TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (compte_id, boite_id, semaine, annee)
);

CREATE TABLE IF NOT EXISTS transferts (
    compte_id TEXT NOT NULL,
    boite_id  TEXT NOT NULL,
    regles    TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (compte_id, boite_id)
);

CREATE TABLE IF NOT EXISTS settings (
    compte_id TEXT NOT NULL,
    cle       TEXT NOT NULL,
    valeur    TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (compte_id, cle)
);

CREATE TABLE IF NOT EXISTS relances_intelligentes (
    id          TEXT PRIMARY KEY,
    compte_id   TEXT NOT NULL,
    boite_id    TEXT NOT NULL,
    email_id    TEXT NOT NULL,
    thread_id   TEXT NOT NULL,
    expediteur  TEXT NOT NULL,
    sujet       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    relance_at  TEXT,
    statut      TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_relances_pending
    ON relances_intelligentes(compte_id, boite_id, statut);
"""


def init_db():
    """Crée toutes les tables si elles n'existent pas encore."""
    db = _conn()
    db.executescript(SCHEMA)
    db.commit()


# ══════════════════════════════════════════════════════════════
# CONFIG (api_key globale, etc.)
# ══════════════════════════════════════════════════════════════

def get_config(key, default=""):
    row = _conn().execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default

def set_config(key, value):
    _conn().execute(
        "INSERT INTO config(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value)
    )
    _conn().commit()


# ══════════════════════════════════════════════════════════════
# COMPTES + BOITES  (interface identique à comptes.json)
# ══════════════════════════════════════════════════════════════

def _boite_row_to_dict(row):
    d = dict(row)
    d["connecte"]      = bool(d["connecte"])
    d["labels_actifs"] = json.loads(d.get("labels_actifs") or "[]")
    d["token"]         = d.pop("token_path", "")   # compatibilité champ "token"
    return d

def _compte_rows_to_dict(c_row, boite_rows):
    c = dict(c_row)
    c["boites"] = [_boite_row_to_dict(b) for b in boite_rows]
    return c


def charger_comptes():
    """Retourne le même dict que l'ancien comptes.json."""
    db   = _conn()
    api_key  = get_config("api_key", "")
    comptes_rows = db.execute("SELECT * FROM comptes").fetchall()
    comptes = []
    for cr in comptes_rows:
        boites = db.execute("SELECT * FROM boites WHERE compte_id=?", (cr["id"],)).fetchall()
        comptes.append(_compte_rows_to_dict(cr, boites))
    return {"api_key": api_key, "comptes": comptes}


def sauver_comptes(data):
    """Sauvegarde le dict complet (interface identique à l'ancienne)."""
    db = _conn()
    set_config("api_key", data.get("api_key", ""))
    for c in data.get("comptes", []):
        db.execute("""
            INSERT INTO comptes(id,access_token,login_email,password_hash,
                password_version,nom,agence,tel,zone)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                access_token=excluded.access_token,
                login_email=excluded.login_email,
                password_hash=excluded.password_hash,
                password_version=excluded.password_version,
                nom=excluded.nom, agence=excluded.agence,
                tel=excluded.tel, zone=excluded.zone
        """, (
            c["id"], c.get("access_token",""), c.get("login_email",""),
            c.get("password_hash",""), c.get("password_version",0),
            c.get("nom",""), c.get("agence",""), c.get("tel",""), c.get("zone",""),
        ))
        for b in c.get("boites", []):
            token_path = b.get("token", b.get("token_path", ""))
            db.execute("""
                INSERT INTO boites(id,compte_id,email,provider,token_path,connecte,
                    labels_actifs,intervalle,bilan_jour,bilan_heure,instructions,
                    imap_server,imap_port,smtp_server,smtp_port,imap_password)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    compte_id=excluded.compte_id, email=excluded.email,
                    provider=excluded.provider, token_path=excluded.token_path,
                    connecte=excluded.connecte, labels_actifs=excluded.labels_actifs,
                    intervalle=excluded.intervalle, bilan_jour=excluded.bilan_jour,
                    bilan_heure=excluded.bilan_heure, instructions=excluded.instructions,
                    imap_server=excluded.imap_server, imap_port=excluded.imap_port,
                    smtp_server=excluded.smtp_server, smtp_port=excluded.smtp_port,
                    imap_password=excluded.imap_password
            """, (
                b["id"], c["id"], b.get("email",""), b.get("provider","gmail"),
                token_path, 1 if b.get("connecte") else 0,
                json.dumps(b.get("labels_actifs", []), ensure_ascii=False),
                b.get("intervalle","60"), b.get("bilan_jour","0"),
                b.get("bilan_heure","8"), b.get("instructions",""),
                b.get("imap_server",""), b.get("imap_port","993"),
                b.get("smtp_server",""), b.get("smtp_port","465"),
                b.get("imap_password",""),
            ))
    db.commit()


def supprimer_compte_db(compte_id):
    db = _conn()
    db.execute("DELETE FROM comptes WHERE id=?", (compte_id,))
    db.commit()


def supprimer_boite_db(boite_id):
    db = _conn()
    db.execute("DELETE FROM boites WHERE id=?", (boite_id,))
    db.commit()


# ══════════════════════════════════════════════════════════════
# AGENTS ACTIFS
# ══════════════════════════════════════════════════════════════

def charger_agents_actifs():
    rows = _conn().execute("SELECT compte_id, boite_id FROM agents_actifs").fetchall()
    return {f"{r['compte_id']}_{r['boite_id']}": True for r in rows}

def sauver_agents_actifs(agents):
    db = _conn()
    db.execute("DELETE FROM agents_actifs")
    for pk in agents:
        parts = pk.split("_", 1)
        if len(parts) == 2:
            db.execute(
                "INSERT OR REPLACE INTO agents_actifs(compte_id,boite_id) VALUES(?,?)",
                (parts[0], parts[1])
            )
    db.commit()

def marquer_agent_actif_db(compte_id, boite_id):
    _conn().execute(
        "INSERT OR IGNORE INTO agents_actifs(compte_id,boite_id) VALUES(?,?)",
        (compte_id, boite_id)
    )
    _conn().commit()

def marquer_agent_inactif_db(compte_id, boite_id):
    _conn().execute(
        "DELETE FROM agents_actifs WHERE compte_id=? AND boite_id=?",
        (compte_id, boite_id)
    )
    _conn().commit()


# ══════════════════════════════════════════════════════════════
# EMAILS RÉCENTS  (app.py + mailpilot.py subprocess)
# ══════════════════════════════════════════════════════════════

def charger_emails_recents(compte_id, boite_id):
    rows = _conn().execute("""
        SELECT * FROM emails_recents
        WHERE compte_id=? AND boite_id=?
        ORDER BY traite_at DESC LIMIT 30
    """, (compte_id, boite_id)).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["brouillon_modifie"] = bool(d["brouillon_modifie"])
        result.append(d)
    return result

def sauver_email_recent_db(compte_id, boite_id, email_dict):
    """Upsert un email récent (appelé par mailpilot subprocess)."""
    e  = email_dict
    eid = e.get("id", "")
    db = _conn()
    db.execute("""
        INSERT INTO emails_recents(id,compte_id,boite_id,sujet,expediteur,corps,
            categorie,brouillon,brouillon_modifie,traite_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id,compte_id,boite_id) DO UPDATE SET
            sujet=excluded.sujet, expediteur=excluded.expediteur,
            corps=excluded.corps, categorie=excluded.categorie,
            brouillon=excluded.brouillon,
            brouillon_modifie=excluded.brouillon_modifie,
            traite_at=excluded.traite_at
    """, (
        eid, compte_id, boite_id,
        e.get("sujet",""), e.get("expediteur",""),
        (e.get("corps","") or "")[:2000],
        e.get("categorie",""), e.get("brouillon",""),
        1 if e.get("brouillon_modifie") else 0,
        e.get("traite_at",""),
    ))
    # Garder max 30 par boite
    db.execute("""
        DELETE FROM emails_recents WHERE compte_id=? AND boite_id=?
        AND id NOT IN (
            SELECT id FROM emails_recents
            WHERE compte_id=? AND boite_id=?
            ORDER BY traite_at DESC LIMIT 30
        )
    """, (compte_id, boite_id, compte_id, boite_id))
    db.commit()

def update_brouillon_email_db(compte_id, boite_id, email_id, brouillon):
    _conn().execute("""
        UPDATE emails_recents SET brouillon=?, brouillon_modifie=1
        WHERE id=? AND compte_id=? AND boite_id=?
    """, (brouillon, email_id, compte_id, boite_id))
    _conn().commit()


# ══════════════════════════════════════════════════════════════
# AGENDA (RDV)
# ══════════════════════════════════════════════════════════════

def _rdv_row_to_dict(row):
    return dict(row)

def charger_agenda(compte_id):
    rows = _conn().execute(
        "SELECT * FROM agenda WHERE compte_id=? ORDER BY date, heure_debut",
        (compte_id,)
    ).fetchall()
    return [_rdv_row_to_dict(r) for r in rows]

def sauver_agenda(compte_id, rdvs):
    """Remplace tous les RDV d'un compte (interface identique à l'ancienne)."""
    db = _conn()
    db.execute("DELETE FROM agenda WHERE compte_id=?", (compte_id,))
    for rdv in rdvs:
        db.execute("""
            INSERT INTO agenda(id,compte_id,titre,client_nom,client_email,client_tel,
                adresse,date,heure_debut,heure_fin,type,statut,notes,boite_id,
                confirmation_envoyee_at,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            rdv["id"], compte_id, rdv.get("titre",""),
            rdv.get("client_nom",""), rdv.get("client_email",""),
            rdv.get("client_tel",""), rdv.get("adresse",""),
            rdv.get("date",""), rdv.get("heure_debut","09:00"),
            rdv.get("heure_fin","10:00"), rdv.get("type","autre"),
            rdv.get("statut","confirme"), rdv.get("notes",""),
            rdv.get("boite_id",""), rdv.get("confirmation_envoyee_at"),
            rdv.get("created_at",""),
        ))
    db.commit()

def creer_rdv_db(compte_id, rdv):
    db = _conn()
    db.execute("""
        INSERT INTO agenda(id,compte_id,titre,client_nom,client_email,client_tel,
            adresse,date,heure_debut,heure_fin,type,statut,notes,boite_id,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        rdv["id"], compte_id, rdv.get("titre",""),
        rdv.get("client_nom",""), rdv.get("client_email",""),
        rdv.get("client_tel",""), rdv.get("adresse",""),
        rdv.get("date",""), rdv.get("heure_debut","09:00"),
        rdv.get("heure_fin","10:00"), rdv.get("type","autre"),
        rdv.get("statut","confirme"), rdv.get("notes",""),
        rdv.get("boite_id",""), rdv.get("created_at",""),
    ))
    db.commit()

def modifier_rdv_db(compte_id, rdv_id, updates):
    """Met à jour les champs fournis dans updates."""
    allowed = {"titre","client_nom","client_email","client_tel","adresse","date",
               "heure_debut","heure_fin","type","statut","notes",
               "confirmation_envoyee_at"}
    sets = {k: v for k, v in updates.items() if k in allowed}
    if not sets:
        return None
    sql = "UPDATE agenda SET " + ", ".join(f"{k}=?" for k in sets)
    sql += " WHERE id=? AND compte_id=?"
    _conn().execute(sql, (*sets.values(), rdv_id, compte_id))
    _conn().commit()
    row = _conn().execute(
        "SELECT * FROM agenda WHERE id=? AND compte_id=?", (rdv_id, compte_id)
    ).fetchone()
    return _rdv_row_to_dict(row) if row else None

def supprimer_rdv_db(compte_id, rdv_id):
    _conn().execute("DELETE FROM agenda WHERE id=? AND compte_id=?", (rdv_id, compte_id))
    _conn().commit()


# ══════════════════════════════════════════════════════════════
# STATS  (mailpilot.py subprocess + app.py)
# ══════════════════════════════════════════════════════════════

def charger_stats_semaine_db(compte_id, boite_id, semaine, annee):
    row = _conn().execute("""
        SELECT * FROM stats WHERE compte_id=? AND boite_id=? AND semaine=? AND annee=?
    """, (compte_id, boite_id, semaine, annee)).fetchone()
    if row:
        d = dict(row)
        d["categories"] = json.loads(d.get("categories") or "{}")
        return d
    return {"semaine": semaine, "annee": annee, "traites": 0, "brouillons": 0, "categories": {}}

def sauver_stats_semaine_db(compte_id, boite_id, stats):
    _conn().execute("""
        INSERT INTO stats(compte_id,boite_id,semaine,annee,traites,brouillons,categories)
        VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(compte_id,boite_id,semaine,annee) DO UPDATE SET
            traites=excluded.traites,
            brouillons=excluded.brouillons,
            categories=excluded.categories
    """, (
        compte_id, boite_id,
        stats.get("semaine", 0), stats.get("annee", 0),
        stats.get("traites", 0), stats.get("brouillons", 0),
        json.dumps(stats.get("categories", {}), ensure_ascii=False),
    ))
    _conn().commit()

def archiver_stats_hist_db(compte_id, boite_id, stats):
    _conn().execute("""
        INSERT INTO stats_hist(compte_id,boite_id,semaine,annee,traites,brouillons,categories)
        VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(compte_id,boite_id,semaine,annee) DO UPDATE SET
            traites=excluded.traites,
            brouillons=excluded.brouillons,
            categories=excluded.categories
    """, (
        compte_id, boite_id,
        stats.get("semaine", 0), stats.get("annee", 0),
        stats.get("traites", 0), stats.get("brouillons", 0),
        json.dumps(stats.get("categories", {}), ensure_ascii=False),
    ))
    # Garder max 12 semaines dans l'historique
    _conn().execute("""
        DELETE FROM stats_hist WHERE compte_id=? AND boite_id=?
        AND rowid NOT IN (
            SELECT rowid FROM stats_hist
            WHERE compte_id=? AND boite_id=?
            ORDER BY annee DESC, semaine DESC LIMIT 12
        )
    """, (compte_id, boite_id, compte_id, boite_id))
    _conn().commit()

def charger_stats_route(compte_id, boite_id):
    """Pour la route /stats — retourne semaine courante + historique."""
    from datetime import date
    s = date.today().isocalendar()[1]
    a = date.today().year
    courante = charger_stats_semaine_db(compte_id, boite_id, s, a)
    rows = _conn().execute("""
        SELECT * FROM stats_hist WHERE compte_id=? AND boite_id=?
        ORDER BY annee DESC, semaine DESC LIMIT 12
    """, (compte_id, boite_id)).fetchall()
    historique = []
    for r in rows:
        d = dict(r)
        d["categories"] = json.loads(d.get("categories") or "{}")
        historique.append(d)
    return courante, historique


# ══════════════════════════════════════════════════════════════
# TRANSFERTS
# ══════════════════════════════════════════════════════════════

def charger_transferts(compte_id, boite_id):
    row = _conn().execute(
        "SELECT regles FROM transferts WHERE compte_id=? AND boite_id=?",
        (compte_id, boite_id)
    ).fetchone()
    return json.loads(row["regles"]) if row else []

def sauver_transferts_db(compte_id, boite_id, regles):
    _conn().execute("""
        INSERT INTO transferts(compte_id,boite_id,regles) VALUES(?,?,?)
        ON CONFLICT(compte_id,boite_id) DO UPDATE SET regles=excluded.regles
    """, (compte_id, boite_id, json.dumps(regles, ensure_ascii=False)))
    _conn().commit()


# ══════════════════════════════════════════════════════════════
# SETTINGS  (horaires, nettoyage, confirmation, relance, rappel)
# ══════════════════════════════════════════════════════════════

def get_setting(compte_id, cle, default=None):
    row = _conn().execute(
        "SELECT valeur FROM settings WHERE compte_id=? AND cle=?",
        (compte_id, cle)
    ).fetchone()
    if row:
        try:
            return json.loads(row["valeur"])
        except Exception:
            return default
    return default

def set_setting(compte_id, cle, valeur):
    _conn().execute("""
        INSERT INTO settings(compte_id,cle,valeur) VALUES(?,?,?)
        ON CONFLICT(compte_id,cle) DO UPDATE SET valeur=excluded.valeur
    """, (compte_id, cle, json.dumps(valeur, ensure_ascii=False)))
    _conn().commit()


# ══════════════════════════════════════════════════════════════
# RELANCES INTELLIGENTES
# ══════════════════════════════════════════════════════════════

import uuid as _uuid

def ajouter_relance(compte_id, boite_id, email_id, thread_id, expediteur, sujet):
    """Enregistre un email à relancer si le client ne répond pas."""
    from datetime import datetime
    # On évite les doublons sur (email_id, statut=pending)
    existing = _conn().execute(
        "SELECT id FROM relances_intelligentes WHERE email_id=? AND statut='pending'",
        (email_id,)
    ).fetchone()
    if existing:
        return existing["id"]
    rid = str(_uuid.uuid4())
    _conn().execute("""
        INSERT INTO relances_intelligentes
        (id, compte_id, boite_id, email_id, thread_id, expediteur, sujet, created_at, statut)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (rid, compte_id, boite_id, email_id, thread_id, expediteur, sujet,
          __import__('datetime').datetime.now().isoformat(), 'pending'))
    _conn().commit()
    return rid

def get_relances_pending(compte_id, boite_id, older_than_seconds):
    """Retourne les relances en attente depuis plus de X secondes."""
    from datetime import datetime, timedelta
    cutoff = (datetime.now() - timedelta(seconds=older_than_seconds)).isoformat()
    rows = _conn().execute("""
        SELECT * FROM relances_intelligentes
        WHERE compte_id=? AND boite_id=? AND statut='pending' AND created_at < ?
        ORDER BY created_at
        LIMIT 20
    """, (compte_id, boite_id, cutoff)).fetchall()
    return [dict(r) for r in rows]

def marquer_relance(relance_id, statut):
    """Met à jour le statut d'une relance (sent / replied / cancelled)."""
    from datetime import datetime
    _conn().execute(
        "UPDATE relances_intelligentes SET statut=?, relance_at=? WHERE id=?",
        (statut, datetime.now().isoformat(), relance_id)
    )
    _conn().commit()

def stats_relances(compte_id, boite_id):
    """Retourne les compteurs (pending, sent, replied) pour une boîte."""
    rows = _conn().execute("""
        SELECT statut, COUNT(*) as n
        FROM relances_intelligentes
        WHERE compte_id=? AND boite_id=?
        GROUP BY statut
    """, (compte_id, boite_id)).fetchall()
    return {r["statut"]: r["n"] for r in rows}


# ══════════════════════════════════════════════════════════════
# MIGRATION  depuis les anciens fichiers JSON
# ══════════════════════════════════════════════════════════════

def migrate_from_json(data_dir: Path):
    """
    Importe les données existantes des fichiers JSON vers SQLite.
    Appelé une seule fois au démarrage si la BDD est vide.
    """
    db = _conn()

    # ── comptes.json ──
    comptes_file = data_dir / "comptes.json"
    if comptes_file.exists():
        try:
            raw = json.loads(comptes_file.read_text(encoding="utf-8"))
            sauver_comptes(raw)
            print("✅ Migration comptes.json → SQLite")
        except Exception as e:
            print(f"⚠️  Migration comptes.json échouée: {e}")

    # ── agents_actifs.json ──
    aa_file = data_dir / "agents_actifs.json"
    if aa_file.exists():
        try:
            agents = json.loads(aa_file.read_text())
            sauver_agents_actifs(agents)
            print("✅ Migration agents_actifs.json → SQLite")
        except Exception as e:
            print(f"⚠️  Migration agents_actifs.json échouée: {e}")

    # ── emails_recents_*.json ──
    for f in data_dir.glob("emails_recents_*.json"):
        try:
            pk    = f.stem.replace("emails_recents_", "")
            parts = pk.split("_", 1)
            if len(parts) != 2:
                continue
            cid, bid = parts
            emails = json.loads(f.read_text(encoding="utf-8"))
            for em in reversed(emails):   # reversed = plus ancien en premier
                sauver_email_recent_db(cid, bid, em)
            print(f"✅ Migration {f.name} → SQLite")
        except Exception as e:
            print(f"⚠️  Migration {f.name} échouée: {e}")

    # ── agenda_*.json ──
    for f in data_dir.glob("agenda_*.json"):
        try:
            compte_id = f.stem.replace("agenda_", "")
            rdvs = json.loads(f.read_text(encoding="utf-8"))
            sauver_agenda(compte_id, rdvs)
            print(f"✅ Migration {f.name} → SQLite")
        except Exception as e:
            print(f"⚠️  Migration {f.name} échouée: {e}")

    # ── stats_*.json ──
    for f in data_dir.glob("stats_*.json"):
        if "hist" in f.name:
            continue
        try:
            pk    = f.stem.replace("stats_", "")
            parts = pk.split("_", 1)
            if len(parts) != 2:
                continue
            cid, bid = parts
            data = json.loads(f.read_text(encoding="utf-8"))
            sauver_stats_semaine_db(cid, bid, data)
            print(f"✅ Migration {f.name} → SQLite")
        except Exception as e:
            print(f"⚠️  Migration {f.name} échouée: {e}")

    # ── stats_hist_*.json ──
    for f in data_dir.glob("stats_hist_*.json"):
        try:
            pk    = f.stem.replace("stats_hist_", "")
            parts = pk.split("_", 1)
            if len(parts) != 2:
                continue
            cid, bid = parts
            raw = json.loads(f.read_text(encoding="utf-8"))
            for s in raw.get("semaines", []):
                archiver_stats_hist_db(cid, bid, s)
            print(f"✅ Migration {f.name} → SQLite")
        except Exception as e:
            print(f"⚠️  Migration {f.name} échouée: {e}")

    # ── transferts_*.json ──
    for f in data_dir.glob("transferts_*.json"):
        try:
            pk    = f.stem.replace("transferts_", "")
            parts = pk.split("_", 1)
            if len(parts) != 2:
                continue
            cid, bid = parts
            regles = json.loads(f.read_text(encoding="utf-8"))
            sauver_transferts_db(cid, bid, regles)
            print(f"✅ Migration {f.name} → SQLite")
        except Exception as e:
            print(f"⚠️  Migration {f.name} échouée: {e}")

    # ── settings : horaires, nettoyage, confirmation, relance, rappel ──
    for prefix in ("horaires", "nettoyage", "confirmation", "relance", "rappel"):
        for f in data_dir.glob(f"{prefix}_*.json"):
            try:
                compte_id = f.stem.replace(f"{prefix}_", "")
                valeur = json.loads(f.read_text(encoding="utf-8"))
                set_setting(compte_id, prefix, valeur)
                print(f"✅ Migration {f.name} → SQLite")
            except Exception as e:
                print(f"⚠️  Migration {f.name} échouée: {e}")

    print("🎉 Migration JSON → SQLite terminée")
