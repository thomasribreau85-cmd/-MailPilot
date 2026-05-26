# ============================================================
# app.py — Interface web multi-comptes pour MailPilot
# Supporte plusieurs boîtes mail par compte (Gmail, Outlook, IMAP)
# ============================================================

import sys
import os
import re
import logging
# Autoriser OAuth sur HTTP en développement local
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
import uuid
import json
import time
import secrets
import threading
import subprocess
from pathlib import Path
from urllib.parse import urlencode

import anthropic as anthropic_sdk
import requests as http_requests
from flask import Flask, render_template, request, jsonify, redirect, session
from werkzeug.security import generate_password_hash, check_password_hash
from google_auth_oauthlib.flow import InstalledAppFlow, Flow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

# --- Microsoft OAuth ---
MS_AUTH_URL  = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
MS_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
MS_SCOPES    = "https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/Mail.ReadWrite https://graph.microsoft.com/Mail.Send offline_access"

app = Flask(__name__)

# ── Logger de sécurité ────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SECURITY] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
sec_log = logging.getLogger("mailpilot.security")

# ── Secret key stable ─────────────────────────────────────────
_sk_file = Path(os.environ.get("DATA_DIR", str(Path(__file__).parent))) / ".secret_key"
if not _sk_file.exists():
    _sk_file.write_text(secrets.token_hex(32))
app.secret_key = os.environ.get("SECRET_KEY", _sk_file.read_text().strip())

# ── Session sécurisée ─────────────────────────────────────────
app.config.update(
    SESSION_COOKIE_HTTPONLY  = True,    # JS ne peut pas lire le cookie
    SESSION_COOKIE_SAMESITE  = "Lax",   # Protection CSRF de base
    SESSION_COOKIE_SECURE    = not os.environ.get("DEV_MODE", "1") == "1",
    PERMANENT_SESSION_LIFETIME = 86400 * 7,  # 7 jours
)

# ── Mot de passe admin (hashé) ────────────────────────────────
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Loulou06")
_admin_hash_file = Path(os.environ.get("DATA_DIR", str(Path(__file__).parent))) / ".admin_hash"
if not _admin_hash_file.exists():
    _admin_hash_file.write_text(generate_password_hash(ADMIN_PASSWORD, method="pbkdf2:sha256"))
ADMIN_PASSWORD_HASH = _admin_hash_file.read_text().strip()

# ── Rate limiting ─────────────────────────────────────────────
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=[],
    storage_uri="memory://",
)

from prompts import TOUS_LES_LABELS, LABELS_DEFAUT

# ── Chemins ──────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent
DATA_DIR   = Path(os.environ.get("DATA_DIR", str(BASE_DIR)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
TOKENS_DIR = DATA_DIR / "tokens"
TOKENS_DIR.mkdir(exist_ok=True)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

# ── Base de données SQLite ────────────────────────────────────
import database as db_module
db_module.init_db()

# Migration automatique depuis les anciens fichiers JSON (une seule fois)
_migration_done_flag = DATA_DIR / ".db_migrated"
if not _migration_done_flag.exists():
    db_module.migrate_from_json(DATA_DIR)
    _migration_done_flag.write_text("ok")

# Raccourcis pratiques
from database import (
    charger_comptes, sauver_comptes,
    charger_agenda, sauver_agenda,
    charger_transferts, sauver_transferts_db,
    charger_emails_recents,
    get_setting, set_setting,
    get_config, set_config,
)

# ── État en mémoire ───────────────────────────────────────────
processus       = {}
logs_par_compte = {}
emails_comptes  = {}
oauth_statut    = {}
oauth_flows     = {}
lock            = threading.Lock()
MAX_LOGS        = 150

# ── Agents actifs (SQLite) ────────────────────────────────────
def charger_agents_actifs():
    return db_module.charger_agents_actifs()

def sauver_agents_actifs(agents):
    db_module.sauver_agents_actifs(agents)

def marquer_agent_actif(compte_id, boite_id):
    db_module.marquer_agent_actif_db(compte_id, boite_id)

def marquer_agent_inactif(compte_id, boite_id):
    db_module.marquer_agent_inactif_db(compte_id, boite_id)


# ── Helpers ───────────────────────────────────────────────────

def get_creds_path():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        tmp_path = BASE_DIR / "credentials_tmp.json"
        tmp_path.write_text(creds_json)
        return str(tmp_path)
    return str(BASE_DIR / "credentials.json")


def pkey(compte_id, boite_id):
    """Clé unique pour un process/log/statut."""
    return f"{compte_id}_{boite_id}"


def migrer_compte_vers_boites(c):
    """Migre un compte de l'ancien format (champs directs) vers le format boites[]."""
    if "boites" in c:
        return
    boite = {
        "id":            str(uuid.uuid4())[:8],
        "email":         c.pop("email", ""),
        "provider":      c.pop("provider", "gmail"),
        "token":         c.pop("token", ""),
        "connecte":      c.pop("connecte", False),
        "labels_actifs": c.pop("labels_actifs", LABELS_DEFAUT),
        "intervalle":    c.pop("intervalle", "60"),
        "imap_server":   c.pop("imap_server", ""),
        "imap_port":     c.pop("imap_port", "993"),
        "smtp_server":   c.pop("smtp_server", ""),
        "smtp_port":     c.pop("smtp_port", "465"),
        "imap_password": c.pop("imap_password", ""),
    }
    if not c.get("login_email"):
        c["login_email"] = boite["email"]
    c["boites"] = [boite]


# charger_comptes et sauver_comptes sont importés depuis database.py


def trouver_compte(data, compte_id):
    for c in data["comptes"]:
        if c["id"] == compte_id:
            return c
    return None


def trouver_boite(compte, boite_id):
    for b in compte.get("boites", []):
        if b["id"] == boite_id:
            return b
    return None


def check_access(compte_id):
    if session.get("admin"):
        return True
    if session.get("user_id") != compte_id:
        return False
    # Vérifier que la session correspond à la version du mot de passe
    data = charger_comptes()
    c = trouver_compte(data, compte_id)
    if not c:
        return False
    return session.get("pwd_v", 0) == c.get("password_version", 0)


def get_login_email(c):
    if c.get("login_email"):
        return c["login_email"]
    if c.get("boites"):
        return c["boites"][0].get("email", "")
    return ""


# ── Capture logs ──────────────────────────────────────────────

def capturer_logs(pk, process):
    logs_par_compte.setdefault(pk, [])
    for ligne in iter(process.stdout.readline, ""):
        ligne = ligne.strip()
        if not ligne:
            continue
        with lock:
            logs_par_compte[pk].append(ligne)
            if len(logs_par_compte[pk]) > MAX_LOGS:
                logs_par_compte[pk].pop(0)
            if "Brouillon créé" in ligne:
                emails_comptes[pk] = emails_comptes.get(pk, 0) + 1
    process.wait()
    # Agent terminé — le retirer de la liste des actifs
    parts = pk.split("_", 1)
    if len(parts) == 2:
        marquer_agent_inactif(parts[0], parts[1])


def _get_signature_text(compte_id):
    """Retourne la signature si activée, sinon chaîne vide."""
    sig = get_setting(compte_id, "signature", {"texte": "", "actif": False})
    return sig.get("texte", "") if sig.get("actif") else ""


TEMPLATES_DEFAUT = [
    {"id": "t1", "categorie": "VISITE",        "titre": "Confirmation de visite",      "texte": "Bonjour,\n\nJe vous confirme notre rendez-vous de visite. Je serai présent(e) à l'heure convenue et reste disponible pour toute question d'ici là.\n\nÀ très bientôt,"},
    {"id": "t2", "categorie": "DEVIS",         "titre": "Réponse demande de devis",    "texte": "Bonjour,\n\nMerci pour votre demande. Je reviens vers vous très rapidement avec une estimation détaillée correspondant à votre projet.\n\nCordialement,"},
    {"id": "t3", "categorie": "RELANCE",       "titre": "Relance client",              "texte": "Bonjour,\n\nJe me permets de revenir vers vous suite à notre dernier échange. Avez-vous eu l'occasion d'avancer sur votre projet ? Je reste à votre disposition pour en discuter.\n\nBien cordialement,"},
    {"id": "t4", "categorie": "URGENT",        "titre": "Prise en charge urgente",     "texte": "Bonjour,\n\nJ'ai bien reçu votre message et je comprends l'urgence de la situation. Je traite votre demande en priorité et vous recontacte dans les plus brefs délais.\n\nCordialement,"},
    {"id": "t5", "categorie": "COMMERCIAL",    "titre": "Remerciement contact",        "texte": "Bonjour,\n\nMerci pour l'intérêt que vous portez à nos services. Je serais ravi(e) d'en apprendre davantage sur votre projet et de vous accompagner.\n\nÀ bientôt,"},
]


def _get_templates(compte_id):
    """Retourne la liste des templates du compte (ou les défauts)."""
    return get_setting(compte_id, "templates", TEMPLATES_DEFAUT)


def _get_absence(compte_id, boite_id):
    """Retourne le config absence d'une boîte."""
    return get_setting(compte_id, f"absence_{boite_id}", {"actif": False, "date_retour": "", "message": ""})


def _get_relance_intelligente(compte_id):
    """Retourne la config relance intelligente du compte."""
    return get_setting(compte_id, "relance_intelligente", {"actif": False, "jours": 3})


LANGUES_VALIDES = {"auto", "fr", "en", "es", "de", "it", "pt", "nl", "ar", "zh"}

TONS_VALIDES = {"formel", "neutre", "detendu"}

CAT_CUSTOM_MAX = 10   # max catégories custom par compte
CAT_NOM_MAX    = 40   # max longueur du nom
CAT_DESC_MAX   = 200  # max longueur description

def _get_categories_custom(compte_id):
    """Retourne la liste des catégories personnalisées du compte ([] si aucune)."""
    cats = get_setting(compte_id, "categories_custom", [])
    return cats if isinstance(cats, list) else []

BLACKLIST_MAX      = 100  # max entrées blacklist
BLACKLIST_ITEM_MAX = 120  # max longueur d'une entrée

def _get_blacklist(compte_id):
    """Retourne la liste des domaines/adresses en blacklist ([] si aucune)."""
    bl = get_setting(compte_id, "blacklist_expediteurs", [])
    return bl if isinstance(bl, list) else []

def _get_instructions_globales(compte_id):
    """Retourne les instructions personnalisées globales (chaîne vide si aucune)."""
    return get_setting(compte_id, "instructions_globales", "") or ""

def _get_ton(compte_id):
    """Retourne le ton de rédaction configuré ('neutre' par défaut)."""
    t = get_setting(compte_id, "ton_redaction", "neutre")
    return t if t in TONS_VALIDES else "neutre"

def _get_langue(compte_id):
    """Retourne la langue de réponse configurée ('auto' par défaut)."""
    l = get_setting(compte_id, "langue_reponse", "auto")
    return l if l in LANGUES_VALIDES else "auto"


def _lancer_agent(compte_id, boite_id, c, b, data):
    """Lance le subprocess mailpilot pour une boite donnée."""
    pk       = pkey(compte_id, boite_id)
    provider = b.get("provider", "gmail")

    env = os.environ.copy()
    env.update({
        "AGENT_NOM":           c["nom"],
        "AGENT_AGENCE":        c.get("agence", ""),
        "AGENT_TEL":           c.get("tel", ""),
        "AGENT_EMAIL":         b["email"],
        "AGENT_ZONE":          c.get("zone", ""),
        "ANTHROPIC_API_KEY":   data.get("api_key", ""),
        "CHECK_INTERVAL":      b.get("intervalle", "60"),
        "LABELS_ACTIFS":       ",".join(b.get("labels_actifs", LABELS_DEFAUT)),
        "MAIL_PROVIDER":       provider,
        "COMPTE_ID":           pk,
        "STATS_DIR":           str(DATA_DIR),
        "BILAN_JOUR":          str(b.get("bilan_jour",  "0")),
        "BILAN_HEURE":         str(b.get("bilan_heure", "8")),
        "AGENT_INSTRUCTIONS":  b.get("instructions", ""),
        "AGENDA_ACTIF":        "1" if c.get("agenda_actif", True) else "0",
        "AGENT_SIGNATURE":     _get_signature_text(compte_id),
        "AGENT_TEMPLATES":     json.dumps(_get_templates(compte_id), ensure_ascii=False),
        "AGENT_ABSENCE_ACTIF":         "1" if _get_absence(compte_id, boite_id).get("actif") else "0",
        "AGENT_ABSENCE_DATE":          _get_absence(compte_id, boite_id).get("date_retour", ""),
        "AGENT_ABSENCE_MSG":           _get_absence(compte_id, boite_id).get("message", ""),
        "RELANCE_INTELLIGENTE_ACTIF":  "1" if _get_relance_intelligente(compte_id).get("actif") else "0",
        "RELANCE_INTELLIGENTE_JOURS":  str(_get_relance_intelligente(compte_id).get("jours", 3)),
        "AGENT_LANGUE":                _get_langue(compte_id),
        "AGENT_TON":                   _get_ton(compte_id),
        "AGENT_INSTRUCTIONS_GLOBALES": _get_instructions_globales(compte_id),
        "AGENT_CUSTOM_CATEGORIES":     json.dumps(_get_categories_custom(compte_id), ensure_ascii=False),
        "AGENT_BLACKLIST":             "|".join(_get_blacklist(compte_id)),
        **_nettoyage_env(compte_id),
        "TRANSFERTS_RULES":    "|".join(
            f"{r['categorie']}:{r['to']}"
            for r in charger_transferts(compte_id, boite_id)
        ),
    })
    if provider == "microsoft":
        env.update({
            "MICROSOFT_TOKEN_PATH":    b.get("token", ""),
            "MICROSOFT_CLIENT_ID":     os.environ.get("MICROSOFT_CLIENT_ID", ""),
            "MICROSOFT_CLIENT_SECRET": os.environ.get("MICROSOFT_CLIENT_SECRET", ""),
        })
    elif provider == "imap":
        env.update({
            "IMAP_SERVER":   b.get("imap_server", ""),
            "IMAP_PORT":     str(b.get("imap_port", "993")),
            "SMTP_SERVER":   b.get("smtp_server", ""),
            "SMTP_PORT":     str(b.get("smtp_port", "465")),
            "IMAP_PASSWORD": b.get("imap_password", ""),
        })

    logs_par_compte[pk] = logs_par_compte.get(pk, [])
    emails_comptes[pk]  = emails_comptes.get(pk, 0)

    cmd = [sys.executable, "mailpilot.py"]
    if provider == "gmail":
        cmd += ["--token", b["token"]]

    p = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=str(BASE_DIR),
        env=env,
    )
    processus[pk] = p
    threading.Thread(target=capturer_logs, args=(pk, p), daemon=True).start()
    return p


def auto_restart_agents():
    """Redémarre automatiquement les agents actifs après un reboot Railway."""
    import time as _time
    _time.sleep(5)  # laisser Flask démarrer complètement
    agents = charger_agents_actifs()
    if not agents:
        return
    data      = charger_comptes()
    restarted = 0
    for pk, info in list(agents.items()):
        compte_id = info.get("compte_id")
        boite_id  = info.get("boite_id")
        if not compte_id or not boite_id:
            continue
        c = trouver_compte(data, compte_id)
        b = trouver_boite(c, boite_id) if c else None
        if not c or not b:
            marquer_agent_inactif(compte_id, boite_id)
            continue
        provider = b.get("provider", "gmail")
        if provider == "gmail" and (not b.get("connecte") or not b.get("token")):
            marquer_agent_inactif(compte_id, boite_id)
            continue
        if provider == "microsoft" and not b.get("token"):
            marquer_agent_inactif(compte_id, boite_id)
            continue
        if provider == "imap" and not b.get("imap_server"):
            marquer_agent_inactif(compte_id, boite_id)
            continue
        if b.get("paused"):
            # Boite en pause volontaire — ne pas redémarrer
            marquer_agent_inactif(compte_id, boite_id)
            continue
        try:
            _lancer_agent(compte_id, boite_id, c, b, data)
            restarted += 1
            print(f"🔄 Auto-restart: {b['email']} ({provider})")
        except Exception as e:
            print(f"❌ Auto-restart échec {pk}: {e}")
            marquer_agent_inactif(compte_id, boite_id)
    if restarted:
        print(f"✅ Auto-restart terminé: {restarted} agent(s) relancé(s)")


# ── Security headers ──────────────────────────────────────────
@app.after_request
def set_security_headers(resp):
    resp.headers["X-Content-Type-Options"]  = "nosniff"
    resp.headers["X-Frame-Options"]         = "DENY"
    resp.headers["X-XSS-Protection"]        = "1; mode=block"
    resp.headers["Referrer-Policy"]         = "strict-origin-when-cross-origin"
    resp.headers["Permissions-Policy"]      = "geolocation=(), microphone=(), camera=()"
    # HSTS — force HTTPS en production (Railway)
    if not os.environ.get("DEV_MODE", "1") == "1":
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    # Content Security Policy
    csp = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "font-src 'self'; "
        "frame-ancestors 'none';"
    )
    resp.headers["Content-Security-Policy"] = csp
    return resp

# ── Rate limit error handler ───────────────────────────────────
@app.errorhandler(429)
def trop_de_requetes(e):
    return jsonify({"ok": False, "message": "Trop de tentatives. Réessayez dans quelques minutes."}), 429

# ── Routes ────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute; 30 per hour", methods=["POST"])
def login():
    if request.method == "POST":
        d     = request.json
        email = d.get("email", "").strip().lower()
        pwd   = d.get("password", "")
        ip    = request.remote_addr
        data  = charger_comptes()
        for c in data["comptes"]:
            if get_login_email(c).lower() == email and c.get("password_hash"):
                if check_password_hash(c["password_hash"], pwd):
                    session["user_id"] = c["id"]
                    session["pwd_v"]   = c.get("password_version", 0)
                    session.permanent  = True
                    sec_log.info("LOGIN_OK email=%s compte=%s ip=%s", email, c["id"], ip)
                    return jsonify({"ok": True})
        sec_log.warning("LOGIN_FAIL email=%s ip=%s", email, ip)
        return jsonify({"ok": False, "message": "Email ou mot de passe incorrect"})
    if session.get("admin"):
        return redirect("/admin-dashboard")
    if session.get("user_id"):
        return redirect("/dashboard")
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
@limiter.limit("5 per minute; 20 per hour", methods=["POST"])
def register():
    if request.method == "POST":
        d     = request.json
        nom   = d.get("nom", "").strip()
        email = d.get("email", "").strip().lower()
        pwd   = d.get("password", "")
        if not nom or not email or not pwd:
            return jsonify({"ok": False, "message": "Nom, email et mot de passe sont requis"})
        if len(pwd) < 6:
            return jsonify({"ok": False, "message": "Mot de passe trop court (6 caractères min)"})
        data = charger_comptes()
        for c in data["comptes"]:
            if get_login_email(c).lower() == email:
                return jsonify({"ok": False, "message": "Cet email est déjà utilisé"})

        provider = d.get("provider", "gmail")
        boite = {
            "id":            str(uuid.uuid4())[:8],
            "email":         email,
            "provider":      provider,
            "token":         "",
            "connecte":      provider == "imap",
            "labels_actifs": LABELS_DEFAUT,
            "intervalle":    "60",
            "bilan_jour":    "0",
            "bilan_heure":   "8",
            "instructions":  "",
        }
        if provider == "imap":
            boite.update({
                "imap_server":   d.get("imap_server", ""),
                "imap_port":     d.get("imap_port", "993"),
                "smtp_server":   d.get("smtp_server", ""),
                "smtp_port":     d.get("smtp_port", "465"),
                "imap_password": d.get("imap_password", ""),
            })

        compte = {
            "id":            str(uuid.uuid4())[:8],
            "access_token":  secrets.token_urlsafe(16),
            "password_hash": generate_password_hash(pwd, method="pbkdf2:sha256"),
            "login_email":   email,
            "nom":           nom,
            "agence":        d.get("agence", ""),
            "tel":           d.get("tel", ""),
            "zone":          d.get("zone", ""),
            "boites":        [boite],
        }
        data["comptes"].append(compte)
        sauver_comptes(data)
        session["user_id"] = compte["id"]
        return jsonify({"ok": True})
    if session.get("user_id"):
        return redirect("/dashboard")
    return render_template("register.html")


@app.route("/dashboard")
def dashboard():
    if session.get("admin"):
        return redirect("/admin-dashboard")
    user_id = session.get("user_id")
    if not user_id:
        return redirect("/login")
    data = charger_comptes()
    compte = trouver_compte(data, user_id)
    if not compte:
        session.clear()
        return redirect("/login")
    # Vérifie que les fichiers token Microsoft existent toujours
    modifie = False
    for b in compte.get("boites", []):
        if b.get("provider") == "microsoft" and b.get("token"):
            if not Path(b["token"]).exists():
                b["token"]    = ""
                b["connecte"] = False
                modifie = True
    if modifie:
        sauver_comptes(data)
    return render_template(
        "client.html",
        compte=compte,
        labels_defaut=LABELS_DEFAUT,
        labels_info={k: {"emoji": v["emoji"], "nom": v["nom"]} for k, v in TOUS_LES_LABELS.items()},
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/api/change-password/<compte_id>", methods=["POST"])
def change_password(compte_id):
    if session.get("user_id") != compte_id:
        return jsonify({"ok": False, "message": "Non autorisé"}), 403
    d = request.json
    ancien = d.get("ancien", "")
    nouveau = d.get("nouveau", "")
    if not ancien or not nouveau:
        return jsonify({"ok": False, "message": "Champs manquants"})
    if len(nouveau) < 8:
        return jsonify({"ok": False, "message": "Le nouveau mot de passe doit faire au moins 8 caractères"})
    if not re.search(r"[0-9]", nouveau):
        return jsonify({"ok": False, "message": "Le mot de passe doit contenir au moins un chiffre"})
    data = charger_comptes()
    for c in data["comptes"]:
        if c["id"] == compte_id:
            if not c.get("password_hash") or not check_password_hash(c["password_hash"], ancien):
                sec_log.warning("CHANGE_PWD_FAIL compte=%s ip=%s", compte_id, request.remote_addr)
                return jsonify({"ok": False, "message": "Ancien mot de passe incorrect"})
            c["password_hash"]     = generate_password_hash(nouveau, method="pbkdf2:sha256")
            c["password_version"]  = c.get("password_version", 0) + 1
            sauver_comptes(data)
            # Mettre à jour la session courante avec la nouvelle version
            session["pwd_v"] = c["password_version"]
            sec_log.info("CHANGE_PWD_OK compte=%s ip=%s", compte_id, request.remote_addr)
            return jsonify({"ok": True})
    return jsonify({"ok": False, "message": "Compte introuvable"})


# ── Admin ─────────────────────────────────────────────────────

@app.route("/admin", methods=["GET", "POST"])
@limiter.limit("5 per minute; 15 per hour", methods=["POST"])
def admin_login():
    if request.method == "POST":
        pwd = request.json.get("password", "")
        ip  = request.remote_addr
        if check_password_hash(ADMIN_PASSWORD_HASH, pwd):
            session["admin"] = True
            session.permanent = True
            sec_log.info("ADMIN_LOGIN_OK ip=%s", ip)
            return jsonify({"ok": True})
        sec_log.warning("ADMIN_LOGIN_FAIL ip=%s", ip)
        return jsonify({"ok": False, "message": "Mot de passe incorrect"})
    if session.get("admin"):
        return redirect("/admin-dashboard")
    return render_template("admin_login.html")


@app.route("/admin-dashboard")
def index():
    if not session.get("admin"):
        return redirect("/admin")
    data = charger_comptes()
    return render_template("index.html", data=data)


@app.route("/")
def home():
    if session.get("admin"):
        return redirect("/admin-dashboard")
    if session.get("user_id"):
        return redirect("/dashboard")
    return redirect("/login")


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/sauvegarder_api", methods=["POST"])
def sauvegarder_api():
    if not session.get("admin"):
        return jsonify({"ok": False}), 403
    data = charger_comptes()
    data["api_key"] = request.json.get("api_key", "")
    sauver_comptes(data)
    return jsonify({"ok": True})


@app.route("/ajouter_compte", methods=["POST"])
def ajouter_compte():
    if not session.get("admin"):
        return jsonify({"ok": False}), 403
    d     = request.json
    email = d.get("email", "")
    boite = {
        "id":            str(uuid.uuid4())[:8],
        "email":         email,
        "provider":      "gmail",
        "token":         "",
        "connecte":      False,
        "labels_actifs": LABELS_DEFAUT,
        "intervalle":    d.get("intervalle", "60"),
    }
    pwd = d.get("password", "")
    compte = {
        "id":            str(uuid.uuid4())[:8],
        "access_token":  secrets.token_urlsafe(16),
        "login_email":   email,
        "password_hash": generate_password_hash(pwd, method="pbkdf2:sha256") if pwd else "",
        "nom":           d.get("nom", ""),
        "agence":        d.get("agence", ""),
        "tel":           d.get("tel", ""),
        "zone":          d.get("zone", ""),
        "boites":        [boite],
    }
    data = charger_comptes()
    data["comptes"].append(compte)
    sauver_comptes(data)
    return jsonify({"ok": True, "compte": compte})


@app.route("/modifier_compte/<compte_id>", methods=["POST"])
def modifier_compte(compte_id):
    if not session.get("admin"):
        return jsonify({"ok": False}), 403
    d    = request.json
    data = charger_comptes()
    c    = trouver_compte(data, compte_id)
    if not c:
        return jsonify({"ok": False})
    for champ in ["nom", "agence", "tel", "zone"]:
        if champ in d:
            c[champ] = d[champ]
    if d.get("email") and c.get("boites"):
        c["boites"][0]["email"] = d["email"]
        c["login_email"] = d["email"]
    if d.get("intervalle") and c.get("boites"):
        c["boites"][0]["intervalle"] = d["intervalle"]
    sauver_comptes(data)
    return jsonify({"ok": True})


@app.route("/supprimer_compte/<compte_id>", methods=["POST"])
def supprimer_compte(compte_id):
    if not session.get("admin"):
        return jsonify({"ok": False}), 403
    data = charger_comptes()
    c    = trouver_compte(data, compte_id)
    if c:
        for b in c.get("boites", []):
            pk = pkey(compte_id, b["id"])
            if pk in processus:
                p = processus.pop(pk)
                if p.poll() is None:
                    p.terminate()
    db_module.supprimer_compte_db(compte_id)
    for f in TOKENS_DIR.glob(f"token_{compte_id}_*.json"):
        f.unlink()
    for f in TOKENS_DIR.glob(f"token_ms_{compte_id}_*.json"):
        f.unlink()
    return jsonify({"ok": True})


# ── Gestion des boîtes mail ───────────────────────────────────

@app.route("/ajouter_boite/<compte_id>", methods=["POST"])
def ajouter_boite(compte_id):
    if not check_access(compte_id):
        return jsonify({"ok": False, "message": "Accès refusé"}), 403
    d    = request.json
    data = charger_comptes()
    c    = trouver_compte(data, compte_id)
    if not c:
        return jsonify({"ok": False, "message": "Compte introuvable"})

    provider = d.get("provider", "gmail")
    email    = d.get("email", "").strip().lower()
    if not email:
        return jsonify({"ok": False, "message": "Email requis"})

    for b in c.get("boites", []):
        if b.get("email", "").lower() == email:
            return jsonify({"ok": False, "message": "Cette boîte mail est déjà ajoutée"})

    boite = {
        "id":            str(uuid.uuid4())[:8],
        "email":         email,
        "provider":      provider,
        "token":         "",
        "connecte":      provider == "imap",
        "labels_actifs": LABELS_DEFAUT,
        "intervalle":    "60",
        "bilan_jour":    "0",
        "bilan_heure":   "8",
        "instructions":  "",
    }
    if provider == "imap":
        imap_server = d.get("imap_server", "").strip()
        smtp_server = d.get("smtp_server", "").strip()
        imap_pwd    = d.get("imap_password", "")
        if not imap_server or not smtp_server or not imap_pwd:
            return jsonify({"ok": False, "message": "Serveur IMAP, SMTP et mot de passe requis"})
        boite.update({
            "imap_server":   imap_server,
            "imap_port":     d.get("imap_port", "993"),
            "smtp_server":   smtp_server,
            "smtp_port":     d.get("smtp_port", "465"),
            "imap_password": imap_pwd,
        })

    c.setdefault("boites", []).append(boite)
    sauver_comptes(data)
    return jsonify({"ok": True, "boite": boite})


@app.route("/supprimer_boite/<compte_id>/<boite_id>", methods=["POST"])
def supprimer_boite(compte_id, boite_id):
    if not check_access(compte_id):
        return jsonify({"ok": False, "message": "Accès refusé"}), 403
    data = charger_comptes()
    c    = trouver_compte(data, compte_id)
    if not c:
        return jsonify({"ok": False})
    if len(c.get("boites", [])) <= 1:
        return jsonify({"ok": False, "message": "Impossible de supprimer la dernière boîte mail"})
    pk = pkey(compte_id, boite_id)
    if pk in processus:
        p = processus.pop(pk)
        if p.poll() is None:
            p.terminate()
    db_module.supprimer_boite_db(boite_id)
    return jsonify({"ok": True})


# ── OAuth Gmail ───────────────────────────────────────────────

@app.route("/connecter_gmail/<compte_id>/<boite_id>", methods=["POST"])
def connecter_gmail(compte_id, boite_id):
    if not check_access(compte_id):
        return jsonify({"ok": False, "message": "Accès refusé"}), 403
    pk = pkey(compte_id, boite_id)
    if oauth_statut.get(pk) == "en_cours":
        return jsonify({"ok": False, "message": "Connexion déjà en cours…"})

    creds_path = get_creds_path()
    if not Path(creds_path).exists():
        return jsonify({"ok": False, "message": "credentials.json introuvable !"})

    data = charger_comptes()
    c    = trouver_compte(data, compte_id)
    b    = trouver_boite(c, boite_id) if c else None
    if not b:
        return jsonify({"ok": False, "message": "Boîte introuvable"})

    token_path = str(TOKENS_DIR / f"token_{compte_id}_{boite_id}.json")
    if Path(token_path).exists():
        try:
            creds = Credentials.from_authorized_user_file(token_path, GMAIL_SCOPES)
            if creds.valid:
                b["connecte"] = True
                b["token"]    = token_path
                sauver_comptes(data)
                oauth_statut[pk] = "ok"
                return jsonify({"ok": True, "message": "Déjà connecté !"})
            elif creds.expired and creds.refresh_token:
                creds.refresh(Request())
                Path(token_path).write_text(creds.to_json())
                b["connecte"] = True
                b["token"]    = token_path
                sauver_comptes(data)
                oauth_statut[pk] = "ok"
                return jsonify({"ok": True, "message": "Token rafraîchi !"})
        except Exception:
            pass

    is_local     = request.host.startswith('127') or request.host.startswith('localhost')
    scheme       = 'http' if is_local else 'https'
    redirect_uri = f'{scheme}://{request.host}/oauth/callback'
    flow = Flow.from_client_secrets_file(creds_path, scopes=GMAIL_SCOPES, redirect_uri=redirect_uri)
    auth_url, state = flow.authorization_url(access_type='offline', include_granted_scopes='true', prompt='consent')
    oauth_flows[state] = {"compte_id": compte_id, "boite_id": boite_id, "flow": flow}
    oauth_statut[pk]   = "en_cours"
    return jsonify({"ok": True, "auth_url": auth_url})


@app.route("/oauth/callback")
def oauth_callback():
    state     = request.args.get('state')
    error     = request.args.get('error')
    flow_data = oauth_flows.pop(state, None)
    compte_id = flow_data["compte_id"] if flow_data else None
    boite_id  = flow_data["boite_id"]  if flow_data else None
    pk        = pkey(compte_id, boite_id) if compte_id and boite_id else None

    if error or not flow_data:
        if pk:
            oauth_statut[pk] = "erreur"
        return redirect('/')

    flow = flow_data["flow"]
    try:
        auth_response = request.url
        is_local      = request.host.startswith('127') or request.host.startswith('localhost')
        if not is_local:
            auth_response = auth_response.replace('http://', 'https://')
        flow.fetch_token(authorization_response=auth_response)
        creds      = flow.credentials
        token_path = str(TOKENS_DIR / f"token_{compte_id}_{boite_id}.json")
        Path(token_path).write_text(creds.to_json())

        data = charger_comptes()
        c    = trouver_compte(data, compte_id)
        b    = trouver_boite(c, boite_id) if c else None
        if b:
            b["connecte"] = True
            b["token"]    = token_path
            sauver_comptes(data)
        oauth_statut[pk] = "ok"
        print(f"[OAuth SUCCESS] compte={compte_id} boite={boite_id}", file=sys.stderr)
    except Exception as e:
        print(f"[OAuth Callback ERREUR] {e}", file=sys.stderr)
        if pk:
            oauth_statut[pk] = "erreur"
            with lock:
                logs_par_compte.setdefault(pk, []).append(f"[ERREUR OAuth] {e}")

    return redirect('/admin-dashboard' if session.get("admin") else '/dashboard')


@app.route("/statut_oauth/<compte_id>/<boite_id>")
def statut_oauth(compte_id, boite_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    pk = pkey(compte_id, boite_id)
    return jsonify({"statut": oauth_statut.get(pk, "")})


# ── OAuth Microsoft ────────────────────────────────────────────

@app.route("/connecter_microsoft/<compte_id>/<boite_id>", methods=["POST"])
def connecter_microsoft(compte_id, boite_id):
    if not check_access(compte_id):
        return jsonify({"ok": False, "message": "Accès refusé"}), 403
    pk = pkey(compte_id, boite_id)
    if oauth_statut.get(pk) == "en_cours":
        return jsonify({"ok": False, "message": "Connexion déjà en cours…"})

    client_id = os.environ.get("MICROSOFT_CLIENT_ID", "")
    if not client_id:
        return jsonify({"ok": False, "message": "Microsoft OAuth non configuré (MICROSOFT_CLIENT_ID manquant)"})

    is_local     = request.host.startswith('127') or request.host.startswith('localhost')
    scheme       = 'http' if is_local else 'https'
    redirect_uri = f'{scheme}://{request.host}/oauth/microsoft/callback'

    state = secrets.token_urlsafe(16)
    oauth_flows[state] = {"compte_id": compte_id, "boite_id": boite_id, "provider": "microsoft", "redirect_uri": redirect_uri}
    oauth_statut[pk]   = "en_cours"

    params = {
        "client_id":     client_id,
        "response_type": "code",
        "redirect_uri":  redirect_uri,
        "scope":         MS_SCOPES,
        "state":         state,
        "response_mode": "query",
    }
    return jsonify({"ok": True, "auth_url": MS_AUTH_URL + "?" + urlencode(params)})


@app.route("/oauth/microsoft/callback")
def microsoft_callback():
    state     = request.args.get("state")
    code      = request.args.get("code")
    error     = request.args.get("error")
    flow_data = oauth_flows.pop(state, None)
    compte_id = flow_data["compte_id"] if flow_data else None
    boite_id  = flow_data["boite_id"]  if flow_data else None
    pk        = pkey(compte_id, boite_id) if compte_id and boite_id else None

    if error or not flow_data or not code:
        if pk:
            oauth_statut[pk] = "erreur"
        return redirect("/dashboard")

    try:
        resp       = http_requests.post(MS_TOKEN_URL, data={
            "client_id":     os.environ.get("MICROSOFT_CLIENT_ID", ""),
            "client_secret": os.environ.get("MICROSOFT_CLIENT_SECRET", ""),
            "code":          code,
            "redirect_uri":  flow_data["redirect_uri"],
            "grant_type":    "authorization_code",
        })
        token_data = resp.json()
        if "access_token" not in token_data:
            raise Exception(f"Réponse Microsoft invalide : {token_data}")

        token_path = str(TOKENS_DIR / f"token_ms_{compte_id}_{boite_id}.json")
        with open(token_path, "w") as f:
            json.dump({
                "access_token":  token_data["access_token"],
                "refresh_token": token_data.get("refresh_token", ""),
                "expires_at":    time.time() + token_data.get("expires_in", 3600),
            }, f)

        data = charger_comptes()
        c    = trouver_compte(data, compte_id)
        b    = trouver_boite(c, boite_id) if c else None
        if b:
            b["connecte"] = True
            b["token"]    = token_path
            sauver_comptes(data)
        oauth_statut[pk] = "ok"

    except Exception as e:
        if pk:
            oauth_statut[pk] = "erreur"
            with lock:
                logs_par_compte.setdefault(pk, []).append(f"[ERREUR OAuth Microsoft] {e}")

    return redirect('/admin-dashboard' if session.get("admin") else '/dashboard')


# ── Démarrer / Arrêter ────────────────────────────────────────

@app.route("/demarrer/<compte_id>/<boite_id>", methods=["POST"])
def demarrer(compte_id, boite_id):
    if not check_access(compte_id):
        return jsonify({"ok": False, "message": "Accès refusé"}), 403
    pk = pkey(compte_id, boite_id)
    if pk in processus and processus[pk].poll() is None:
        return jsonify({"ok": False, "message": "Déjà en cours"})

    data = charger_comptes()
    c    = trouver_compte(data, compte_id)
    if not c:
        return jsonify({"ok": False, "message": "Compte introuvable"})
    b = trouver_boite(c, boite_id)
    if not b:
        return jsonify({"ok": False, "message": "Boîte introuvable"})

    provider = b.get("provider", "gmail")
    if provider == "gmail" and (not b.get("connecte") or not b.get("token")):
        return jsonify({"ok": False, "message": "Connecte d'abord Gmail !"})
    elif provider == "microsoft" and not b.get("token"):
        return jsonify({"ok": False, "message": "Connecte d'abord Outlook !"})
    elif provider == "imap" and not b.get("imap_server"):
        return jsonify({"ok": False, "message": "Configuration IMAP incomplète."})

    logs_par_compte[pk] = []
    emails_comptes[pk]  = 0
    _lancer_agent(compte_id, boite_id, c, b, data)
    marquer_agent_actif(compte_id, boite_id)
    return jsonify({"ok": True})


@app.route("/arreter/<compte_id>/<boite_id>", methods=["POST"])
def arreter(compte_id, boite_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    pk = pkey(compte_id, boite_id)
    p  = processus.pop(pk, None)
    if p and p.poll() is None:
        p.terminate()
        with lock:
            logs_par_compte.setdefault(pk, []).append("── Arrêté manuellement ──")
    marquer_agent_inactif(compte_id, boite_id)
    return jsonify({"ok": True})


# ── Pause / Reprise ───────────────────────────────────────────

@app.route("/api/pause/<compte_id>/<boite_id>", methods=["POST"])
def pause_boite(compte_id, boite_id):
    """Met en pause un agent : l'arrête et empêche le redémarrage auto."""
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    pk = pkey(compte_id, boite_id)
    p  = processus.pop(pk, None)
    if p and p.poll() is None:
        p.terminate()
        with lock:
            logs_par_compte.setdefault(pk, []).append("── Mis en pause ──")
    marquer_agent_inactif(compte_id, boite_id)
    data = charger_comptes()
    c    = trouver_compte(data, compte_id)
    b    = trouver_boite(c, boite_id) if c else None
    if b:
        b["paused"] = True
        sauver_comptes(data)
    return jsonify({"ok": True})


@app.route("/api/resume/<compte_id>/<boite_id>", methods=["POST"])
def resume_boite(compte_id, boite_id):
    """Reprend un agent mis en pause et le redémarre."""
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    data = charger_comptes()
    c    = trouver_compte(data, compte_id)
    b    = trouver_boite(c, boite_id) if c else None
    if not b:
        return jsonify({"ok": False}), 404
    b["paused"] = False
    sauver_comptes(data)
    pk = pkey(compte_id, boite_id)
    if pk not in processus or processus[pk].poll() is not None:
        _lancer_agent(compte_id, boite_id, c, b, data)
        marquer_agent_actif(compte_id, boite_id)
    return jsonify({"ok": True})


# ── Statut ────────────────────────────────────────────────────

@app.route("/statut/<compte_id>")
def statut_compte(compte_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    data = charger_comptes()
    c    = trouver_compte(data, compte_id)
    if not c:
        return jsonify({"ok": False}), 404

    boites_statut = []
    for b in c.get("boites", []):
        pk    = pkey(compte_id, b["id"])
        actif = pk in processus and processus[pk].poll() is None
        with lock:
            logs = list(logs_par_compte.get(pk, [])[-25:])
        boites_statut.append({
            "id":             b["id"],
            "email":          b["email"],
            "provider":       b.get("provider", "gmail"),
            "connecte":       b.get("connecte", False),
            "token":          bool(b.get("token")),
            "actif":          actif,
            "paused":         b.get("paused", False),
            "emails_traites": emails_comptes.get(pk, 0),
            "logs":           logs,
            "oauth":          oauth_statut.get(pk, ""),
            "bilan_jour":     b.get("bilan_jour",    "0"),
            "bilan_heure":    b.get("bilan_heure",   "8"),
            "instructions":   b.get("instructions",  ""),
        })
    return jsonify({"boites": boites_statut})


# ── Paramètres bilan ──────────────────────────────────────────

@app.route("/parametres_bilan/<compte_id>/<boite_id>", methods=["POST"])
def parametres_bilan(compte_id, boite_id):
    if not check_access(compte_id):
        return jsonify({"ok": False, "message": "Accès refusé"}), 403
    d    = request.json
    data = charger_comptes()
    c    = trouver_compte(data, compte_id)
    b    = trouver_boite(c, boite_id) if c else None
    if not b:
        return jsonify({"ok": False, "message": "Boîte introuvable"}), 404
    jour  = str(d.get("bilan_jour",  "0"))
    heure = str(d.get("bilan_heure", "8"))
    if jour not in ["-1"] + [str(i) for i in range(7)]:
        return jsonify({"ok": False, "message": "Jour invalide"})
    if heure not in [str(i) for i in range(24)]:
        return jsonify({"ok": False, "message": "Heure invalide"})
    b["bilan_jour"]  = jour
    b["bilan_heure"] = heure
    sauver_comptes(data)
    return jsonify({"ok": True})


# ── Instructions IA ───────────────────────────────────────────

@app.route("/sauvegarder_instructions/<compte_id>/<boite_id>", methods=["POST"])
def sauvegarder_instructions(compte_id, boite_id):
    if not check_access(compte_id):
        return jsonify({"ok": False, "message": "Accès refusé"}), 403
    d    = request.json or {}
    data = charger_comptes()
    c    = trouver_compte(data, compte_id)
    b    = trouver_boite(c, boite_id) if c else None
    if not b:
        return jsonify({"ok": False, "message": "Boîte introuvable"}), 404
    b["instructions"] = d.get("instructions", "").strip()[:2000]  # max 2000 chars
    sauver_comptes(data)
    return jsonify({"ok": True})


# ── Signature email ───────────────────────────────────────────

@app.route("/api/signature/<compte_id>", methods=["GET"])
def get_signature(compte_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    sig = get_setting(compte_id, "signature", {"texte": "", "actif": False})
    return jsonify({"ok": True, **sig})

@app.route("/api/signature/<compte_id>", methods=["POST"])
def sauver_signature(compte_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    d = request.json or {}
    sig = {
        "texte": d.get("texte", "").strip()[:1000],
        "actif": bool(d.get("actif", True)),
    }
    set_setting(compte_id, "signature", sig)
    return jsonify({"ok": True, **sig})


# ── Templates de réponse ──────────────────────────────────────

@app.route("/api/templates/<compte_id>", methods=["GET"])
def get_templates(compte_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    return jsonify({"ok": True, "templates": _get_templates(compte_id)})

@app.route("/api/templates/<compte_id>", methods=["POST"])
def sauver_templates(compte_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    d = request.json or {}
    templates = []
    for t in d.get("templates", [])[:20]:   # max 20 templates
        templates.append({
            "id":        str(t.get("id", uuid.uuid4()))[:36],
            "categorie": str(t.get("categorie", "")).upper()[:30],
            "titre":     str(t.get("titre", "")).strip()[:80],
            "texte":     str(t.get("texte", "")).strip()[:800],
        })
    # Si liste vide → on efface le setting pour retrouver les défauts au prochain GET
    if templates:
        set_setting(compte_id, "templates", templates)
    else:
        set_setting(compte_id, "templates", None)
        templates = TEMPLATES_DEFAUT
    return jsonify({"ok": True, "templates": templates})

# ── Mode absence ──────────────────────────────────────────────

@app.route("/api/absence/<compte_id>/<boite_id>", methods=["GET"])
def get_absence(compte_id, boite_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    return jsonify({"ok": True, **_get_absence(compte_id, boite_id)})

@app.route("/api/absence/<compte_id>/<boite_id>", methods=["POST"])
def sauver_absence(compte_id, boite_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    d = request.json or {}
    absence = {
        "actif":       bool(d.get("actif", False)),
        "date_retour": str(d.get("date_retour", "")).strip()[:20],
        "message":     str(d.get("message", "")).strip()[:500],
    }
    set_setting(compte_id, f"absence_{boite_id}", absence)
    return jsonify({"ok": True, **absence})

# ── Relance intelligente ─────────────────────────────────────

@app.route("/api/relance-intelligente/<compte_id>", methods=["GET"])
def get_relance_intelligente(compte_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    cfg = _get_relance_intelligente(compte_id)
    # Ajouter les stats globales (toutes boites)
    data = charger_comptes()
    c    = trouver_compte(data, compte_id)
    stats_total = {"pending": 0, "sent": 0, "replied": 0}
    if c:
        for b in c.get("boites", []):
            s = db_module.stats_relances(compte_id, b.get("id", ""))
            for k in stats_total:
                stats_total[k] += s.get(k, 0)
    return jsonify({"ok": True, **cfg, "stats": stats_total})

@app.route("/api/relance-intelligente/<compte_id>", methods=["POST"])
def sauver_relance_intelligente(compte_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    d = request.json or {}
    cfg = {
        "actif": bool(d.get("actif", False)),
        "jours": max(1, min(30, int(d.get("jours", 3)))),
    }
    set_setting(compte_id, "relance_intelligente", cfg)
    return jsonify({"ok": True, **cfg})

# ── Langue de réponse ────────────────────────────────────────

@app.route("/api/langue/<compte_id>", methods=["GET"])
def get_langue(compte_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    return jsonify({"ok": True, "langue": _get_langue(compte_id)})

@app.route("/api/langue/<compte_id>", methods=["POST"])
def sauver_langue(compte_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    langue = str((request.json or {}).get("langue", "auto")).lower().strip()
    if langue not in LANGUES_VALIDES:
        return jsonify({"ok": False, "message": "Langue non supportée"}), 400
    set_setting(compte_id, "langue_reponse", langue)
    return jsonify({"ok": True, "langue": langue})

# ── Ton de rédaction ──────────────────────────────────────────

@app.route("/api/ton/<compte_id>", methods=["GET"])
def get_ton(compte_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    return jsonify({"ok": True, "ton": _get_ton(compte_id)})

@app.route("/api/ton/<compte_id>", methods=["POST"])
def sauver_ton(compte_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    ton = str((request.json or {}).get("ton", "neutre")).lower().strip()
    if ton not in TONS_VALIDES:
        return jsonify({"ok": False, "message": "Ton non supporté"}), 400
    set_setting(compte_id, "ton_redaction", ton)
    return jsonify({"ok": True, "ton": ton})

# ── Instructions personnalisées ───────────────────────────────

@app.route("/api/instructions-globales/<compte_id>", methods=["GET"])
def get_instructions_globales(compte_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    return jsonify({"ok": True, "instructions": _get_instructions_globales(compte_id)})

@app.route("/api/instructions-globales/<compte_id>", methods=["POST"])
def sauver_instructions_globales(compte_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    texte = str((request.json or {}).get("instructions", "")).strip()
    if len(texte) > 2000:
        return jsonify({"ok": False, "message": "Trop long (max 2000 caractères)"}), 400
    set_setting(compte_id, "instructions_globales", texte)
    return jsonify({"ok": True, "instructions": texte})

# ── Catégories personnalisées ─────────────────────────────────

@app.route("/api/categories-custom/<compte_id>", methods=["GET"])
def get_categories_custom(compte_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    return jsonify({"ok": True, "categories": _get_categories_custom(compte_id)})

@app.route("/api/categories-custom/<compte_id>", methods=["POST"])
def sauver_categories_custom(compte_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    d    = request.json or {}
    cats = d.get("categories", [])
    if not isinstance(cats, list):
        return jsonify({"ok": False, "message": "Format invalide"}), 400
    validated = []
    seen_ids  = set()
    for c in cats[:CAT_CUSTOM_MAX]:
        nom  = str(c.get("nom", "")).strip()[:CAT_NOM_MAX]
        desc = str(c.get("description", "")).strip()[:CAT_DESC_MAX]
        emoji = str(c.get("emoji", "🏷️")).strip()[:4]
        couleur = str(c.get("couleur", "#4f6ef7"))[:9]
        if not nom:
            continue
        # Construire un ID stable : lettres MAJ + underscore
        import re as _re
        cat_id = _re.sub(r"[^A-Z0-9_]", "_", nom.upper().replace(" ", "_"))[:30]
        if cat_id in seen_ids:
            cat_id = cat_id + "_2"
        seen_ids.add(cat_id)
        validated.append({
            "id":          cat_id,
            "nom":         nom,
            "emoji":       emoji,
            "description": desc,
            "couleur":     couleur,
        })
    set_setting(compte_id, "categories_custom", validated)
    return jsonify({"ok": True, "categories": validated})

# ── Blacklist expéditeurs ─────────────────────────────────────

@app.route("/api/blacklist/<compte_id>", methods=["GET"])
def get_blacklist(compte_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    return jsonify({"ok": True, "blacklist": _get_blacklist(compte_id)})

@app.route("/api/blacklist/<compte_id>", methods=["POST"])
def sauver_blacklist(compte_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    d  = request.json or {}
    bl = d.get("blacklist", [])
    if not isinstance(bl, list):
        return jsonify({"ok": False, "message": "Format invalide"}), 400
    validated = []
    seen = set()
    for item in bl[:BLACKLIST_MAX]:
        entry = str(item).strip().lower()[:BLACKLIST_ITEM_MAX]
        if not entry or entry in seen:
            continue
        # Accepter adresses email ou domaines (optionnel @)
        if not re.match(r'^[@a-z0-9._\-\+]+$', entry):
            continue
        seen.add(entry)
        validated.append(entry)
    set_setting(compte_id, "blacklist_expediteurs", validated)
    return jsonify({"ok": True, "blacklist": validated})

# ── Labels ────────────────────────────────────────────────────

@app.route("/labels/<compte_id>/<boite_id>", methods=["GET", "POST"])
def gerer_labels(compte_id, boite_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    data = charger_comptes()
    c    = trouver_compte(data, compte_id)
    b    = trouver_boite(c, boite_id) if c else None
    if not b:
        return jsonify({"ok": False}), 404
    if request.method == "POST":
        labels = [l for l in request.json.get("labels", []) if l in TOUS_LES_LABELS]
        if "INUTILE" not in labels:
            labels.append("INUTILE")
        b["labels_actifs"] = labels
        sauver_comptes(data)
        return jsonify({"ok": True, "labels": labels})
    actifs = b.get("labels_actifs", LABELS_DEFAUT)
    return jsonify({
        "actifs": actifs,
        "tous": {k: {"emoji": v["emoji"], "description_ui": v["description_ui"]}
                 for k, v in TOUS_LES_LABELS.items()}
    })


# ── Stats ─────────────────────────────────────────────────────

@app.route("/stats/<compte_id>/<boite_id>")
def stats_compte_route(compte_id, boite_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    semaine_courante, historique = db_module.charger_stats_route(compte_id, boite_id)
    return jsonify({"ok": True, "semaine_courante": semaine_courante, "historique": historique})


@app.route("/statut_global")
def statut_global():
    data   = charger_comptes()
    result = {}
    for c in data["comptes"]:
        for b in c.get("boites", []):
            pk = pkey(c["id"], b["id"])
            result[pk] = {
                "actif":          pk in processus and processus[pk].poll() is None,
                "connecte":       b.get("connecte", False),
                "emails_traites": emails_comptes.get(pk, 0),
                "oauth":          oauth_statut.get(pk, ""),
            }
    return jsonify(result)


# ── Règles de transfert  (charger_transferts importé depuis database.py) ──────

@app.route("/api/transferts/<compte_id>/<boite_id>", methods=["GET"])
def get_transferts(compte_id, boite_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    return jsonify({"ok": True, "transferts": charger_transferts(compte_id, boite_id)})

@app.route("/api/transferts/<compte_id>/<boite_id>", methods=["POST"])
def sauver_transferts(compte_id, boite_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    d = request.json or {}
    regles = d.get("transferts", [])
    # Validation basique
    regles_valides = [
        {"categorie": r["categorie"], "to": r["to"].strip(), "filtre": r.get("filtre", "").strip()}
        for r in regles
        if r.get("categorie") and r.get("to", "").strip()
    ]
    sauver_transferts_db(compte_id, boite_id, regles_valides)
    # Encoder pour l'env var de l'agent
    encoded = "|".join(f"{r['categorie']}:{r['to']}" for r in regles_valides)
    # Mettre à jour le processus actif si en cours
    return jsonify({"ok": True, "transferts": regles_valides})


# ── Horaires d'ouverture ──────────────────────────────────────

HORAIRES_DEFAUT = {
    "lundi":    {"ouvert": True,  "debut": "09:00", "fin": "18:00"},
    "mardi":    {"ouvert": True,  "debut": "09:00", "fin": "18:00"},
    "mercredi": {"ouvert": True,  "debut": "09:00", "fin": "18:00"},
    "jeudi":    {"ouvert": True,  "debut": "09:00", "fin": "18:00"},
    "vendredi": {"ouvert": True,  "debut": "09:00", "fin": "18:00"},
    "samedi":   {"ouvert": False, "debut": "09:00", "fin": "12:00"},
    "dimanche": {"ouvert": False, "debut": "09:00", "fin": "12:00"},
}

def charger_horaires(compte_id):
    return get_setting(compte_id, "horaires", HORAIRES_DEFAUT.copy())

def _nettoyage_env(compte_id):
    cfg = get_setting(compte_id, "nettoyage", {"actif": False, "jours": 365, "categories": ["INUTILE"]})
    return {
        "NETTOYAGE_ACTIF": "1" if cfg.get("actif") else "0",
        "NETTOYAGE_JOURS": str(cfg.get("jours", 365)),
        "NETTOYAGE_CATS":  ",".join(cfg.get("categories", ["INUTILE"])),
    }

@app.route("/api/nettoyage/<compte_id>", methods=["GET"])
def get_nettoyage(compte_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    cfg = get_setting(compte_id, "nettoyage", {"actif": False, "jours": 365, "categories": ["INUTILE"]})
    return jsonify({"ok": True, "config": cfg})

@app.route("/api/nettoyage/<compte_id>", methods=["POST"])
def sauver_nettoyage(compte_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    d = request.json or {}
    cfg = {
        "actif":      bool(d.get("actif", False)),
        "jours":      int(d.get("jours", 365)),
        "categories": d.get("categories", ["INUTILE"]),
    }
    set_setting(compte_id, "nettoyage", cfg)
    return jsonify({"ok": True})

@app.route("/api/nettoyage/<compte_id>/maintenant", methods=["POST"])
def nettoyage_maintenant(compte_id):
    """Déclenche immédiatement le nettoyage des vieux emails via Gmail API."""
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403

    cfg = get_setting(compte_id, "nettoyage", None)
    if not cfg:
        return jsonify({"ok": False, "message": "Nettoyage non configuré"}), 400

    jours    = cfg.get("jours", 365)
    cats_cfg = cfg.get("categories", ["INUTILE"])

    data = charger_comptes()
    c = trouver_compte(data, compte_id)
    if not c:
        return jsonify({"ok": False, "message": "Compte introuvable"}), 404

    supprimés = 0
    erreurs = []

    for b in c.get("boites", []):
        provider = b.get("provider", "gmail")
        if provider != "gmail":
            continue
        token_path = b.get("token", "")
        if not token_path or not Path(token_path).exists():
            continue
        try:
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request
            from googleapiclient.discovery import build

            creds = Credentials.from_authorized_user_file(token_path, GMAIL_SCOPES)
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                Path(token_path).write_text(creds.to_json())

            service = build("gmail", "v1", credentials=creds)

            # Déterminer les catégories
            from prompts import TOUS_LES_LABELS
            cats = list(TOUS_LES_LABELS.keys()) if cats_cfg == "ALL" else cats_cfg

            for cat in cats:
                info = TOUS_LES_LABELS.get(cat)
                if not info:
                    continue
                nom_label = info["nom"]
                query = f'label:"{nom_label}" older_than:{jours}d'
                res = service.users().messages().list(
                    userId="me", q=query, maxResults=500
                ).execute()
                messages = res.get("messages", [])
                for m in messages:
                    try:
                        service.users().messages().trash(userId="me", id=m["id"]).execute()
                        supprimés += 1
                    except Exception as e:
                        erreurs.append(str(e))

        except Exception as e:
            erreurs.append(f"Boite {b.get('email','?')}: {str(e)}")

    return jsonify({
        "ok": True,
        "supprimes": supprimés,
        "erreurs": erreurs[:5],
        "message": f"{supprimés} email(s) déplacé(s) en corbeille"
    })


@app.route("/api/toggle_agenda/<compte_id>", methods=["POST"])
def toggle_agenda(compte_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    data = charger_comptes()
    c = trouver_compte(data, compte_id)
    if not c:
        return jsonify({"ok": False, "message": "Compte introuvable"}), 404
    c["agenda_actif"] = not c.get("agenda_actif", True)
    sauver_comptes(data)
    return jsonify({"ok": True, "agenda_actif": c["agenda_actif"]})

@app.route("/api/horaires/<compte_id>", methods=["GET"])
def get_horaires(compte_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    return jsonify({"ok": True, "horaires": charger_horaires(compte_id)})

@app.route("/api/horaires/<compte_id>", methods=["POST"])
def sauver_horaires(compte_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    d = request.json or {}
    horaires = d.get("horaires", {})
    set_setting(compte_id, "horaires", horaires)
    return jsonify({"ok": True})


# ── Agenda ────────────────────────────────────────────────────

def agenda_file(compte_id):
    return DATA_DIR / f"agenda_{compte_id}.json"

def charger_agenda(compte_id):
    f = agenda_file(compte_id)
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:
            return []
    return []

def sauver_agenda(compte_id, rdvs):
    agenda_file(compte_id).write_text(json.dumps(rdvs, indent=2, ensure_ascii=False))

@app.route("/agenda/<compte_id>")
def agenda(compte_id):
    if not check_access(compte_id):
        return redirect("/login")
    data = charger_comptes()
    c    = trouver_compte(data, compte_id)
    if not c:
        return redirect("/login")
    return render_template("agenda.html", compte=c)

@app.route("/api/rdv/<compte_id>", methods=["GET"])
def get_rdvs(compte_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    return jsonify({"ok": True, "rdvs": charger_agenda(compte_id)})

@app.route("/api/rdv/<compte_id>", methods=["POST"])
def creer_rdv(compte_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    d    = request.json or {}
    rdvs = charger_agenda(compte_id)
    rdv  = {
        "id":          str(uuid.uuid4())[:8],
        "titre":       d.get("titre", "Rendez-vous").strip(),
        "client_nom":  d.get("client_nom", "").strip(),
        "client_email":d.get("client_email", "").strip(),
        "adresse":     d.get("adresse", "").strip(),
        "date":        d.get("date", ""),
        "heure_debut": d.get("heure_debut", "09:00"),
        "heure_fin":   d.get("heure_fin",   "10:00"),
        "type":        d.get("type", "autre"),
        "statut":      d.get("statut", "confirme"),
        "notes":       d.get("notes", "").strip(),
        "boite_id":    d.get("boite_id", ""),
        "created_at":  __import__("datetime").datetime.now().isoformat(),
    }
    rdvs.append(rdv)
    sauver_agenda(compte_id, rdvs)
    # Envoi de confirmation si RDV confirmé d'emblée
    if rdv["statut"] == "confirme" and rdv.get("client_email"):
        threading.Thread(target=_tenter_confirmation, args=(compte_id, rdv), daemon=True).start()
    return jsonify({"ok": True, "rdv": rdv})

@app.route("/api/rdv/<compte_id>/<rdv_id>", methods=["PUT"])
def modifier_rdv(compte_id, rdv_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    d    = request.json or {}
    rdvs = charger_agenda(compte_id)
    for rdv in rdvs:
        if rdv["id"] == rdv_id:
            statut_avant = rdv.get("statut")
            for k in ["titre","client_nom","client_email","adresse","date",
                      "heure_debut","heure_fin","type","statut","notes"]:
                if k in d:
                    rdv[k] = d[k]
            sauver_agenda(compte_id, rdvs)
            # Si le RDV vient d'être confirmé et pas encore de confirmation envoyée
            if (statut_avant != "confirme" and rdv.get("statut") == "confirme"
                    and rdv.get("client_email") and not rdv.get("confirmation_envoyee_at")):
                threading.Thread(target=_tenter_confirmation, args=(compte_id, rdv), daemon=True).start()
            return jsonify({"ok": True, "rdv": rdv})
    return jsonify({"ok": False, "message": "RDV introuvable"}), 404

@app.route("/api/rdv/<compte_id>/<rdv_id>", methods=["DELETE"])
def supprimer_rdv(compte_id, rdv_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    rdvs = charger_agenda(compte_id)
    rdvs = [r for r in rdvs if r["id"] != rdv_id]
    sauver_agenda(compte_id, rdvs)
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════
# CHAT IA EMAIL — Emails récents + rafraîchissement brouillon
# ══════════════════════════════════════════════════════════════

@app.route("/api/emails-recents/<compte_id>/<boite_id>")
def get_emails_recents(compte_id, boite_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    emails = charger_emails_recents(compte_id, boite_id)
    return jsonify({"ok": True, "emails": emails})

def _sanitize_chat_input(text, max_len=600):
    """Tronque et neutralise les tentatives d'injection de prompt."""
    if not text:
        return ""
    text = text[:max_len]
    # Bloquer les patterns d'injection courants
    injection_patterns = [
        r"ignore (all |the |previous |)instructions",
        r"disregard (all |the |previous |)instructions",
        r"you are now",
        r"new instructions?:",
        r"system\s*:",
        r"<\s*/?system\s*>",
        r"act as (a|an)",
    ]
    for pattern in injection_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            sec_log.warning("PROMPT_INJECTION compte=%s pattern=%s", "?", pattern)
            return ""
    return text


@app.route("/api/chat-email/<compte_id>/<boite_id>", methods=["POST"])
@limiter.limit("30 per hour; 5 per minute", methods=["POST"])
def chat_email(compte_id, boite_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    d = request.json or {}
    instruction      = _sanitize_chat_input(d.get("instruction", "").strip())
    email_sujet      = d.get("email_sujet", "")[:200]
    email_expediteur = d.get("email_expediteur", "")[:100]
    email_corps      = d.get("email_corps", "")[:3000]
    brouillon_actuel = d.get("brouillon_actuel", "")[:2000]
    if not instruction:
        return jsonify({"ok": False, "message": "Instruction manquante ou refusée"}), 400

    data = charger_comptes()
    c    = trouver_compte(data, compte_id)
    if not c:
        return jsonify({"ok": False, "message": "Compte introuvable"}), 404
    # Clé API : variable d'environnement Railway en priorité, sinon comptes.json
    api_key = os.environ.get("ANTHROPIC_API_KEY") or data.get("api_key", "")
    if not api_key:
        return jsonify({"ok": False, "message": "Clé API Anthropic manquante"}), 400

    agent_nom    = c.get("nom", "L'agent")
    agent_agence = c.get("agence", "")
    agent_zone   = c.get("zone", "")
    signature    = _get_signature_text(compte_id)

    sig_bloc = f"""
--- SIGNATURE À CONSERVER EN FIN D'EMAIL (obligatoire, mot pour mot) ---
{signature}
---""" if signature else ""

    secteur_txt = f" ({agent_zone})" if agent_zone else ""
    langue_cfg  = _get_langue(compte_id)
    _LANGUE_INSTRUCTIONS = {
        "auto": "Détecte la langue de l'email reçu et réponds dans cette même langue.",
        "fr":   "Réponds toujours en français.",
        "en":   "Always respond in English.",
        "es":   "Responde siempre en español.",
        "de":   "Antworte immer auf Deutsch.",
        "it":   "Rispondi sempre in italiano.",
        "pt":   "Responda sempre em português.",
        "nl":   "Antwoord altijd in het Nederlands.",
        "ar":   "أجب دائمًا باللغة العربية.",
        "zh":   "始终用中文回复。",
    }
    langue_instr = _LANGUE_INSTRUCTIONS.get(langue_cfg, _LANGUE_INSTRUCTIONS["auto"])

    prompt = f"""Tu es l'assistant email de {agent_nom}{(' — ' + agent_agence) if agent_agence else ''}{secteur_txt}.
{langue_instr}

Voici l'email reçu d'un client :
---
SUJET : {email_sujet}
EXPÉDITEUR : {email_expediteur}

{email_corps}
---

Voici le brouillon de réponse actuel généré par l'IA :
---
{brouillon_actuel}
---

INSTRUCTION DE L'AGENT : {instruction}
{sig_bloc}
Réécris uniquement le corps de la réponse email en appliquant l'instruction ci-dessus. Conserve un ton professionnel et courtois. Réponds directement avec le texte de l'email, sans commentaire ni introduction."""

    try:
        client_ai = anthropic_sdk.Anthropic(api_key=api_key)
        msg = client_ai.messages.create(
            model="claude-opus-4-7",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        nouveau_brouillon = msg.content[0].text.strip()

        # Mettre à jour le brouillon dans la BDD
        email_id = d.get("email_id")
        if email_id:
            db_module.update_brouillon_email_db(compte_id, boite_id, email_id, nouveau_brouillon)

        return jsonify({"ok": True, "brouillon": nouveau_brouillon})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


# ══════════════════════════════════════════════════════════════
# CONFIRMATION RDV — Envoi automatique à la création / confirmation
# ══════════════════════════════════════════════════════════════

CONFIRMATION_SUJET_DEFAULT = "Confirmation de votre rendez-vous — {titre}"
CONFIRMATION_CORPS_DEFAULT = """\
Bonjour {client_nom},

Votre rendez-vous est confirmé ! Voici le récapitulatif :

📅 Date : {date}
🕐 Heure : {heure_debut} — {heure_fin}
📌 {titre}{adresse_line}

Nous vous attendons avec plaisir. En cas d'empêchement, merci de nous prévenir au plus tôt.

À très bientôt,
{agent_nom}"""

def charger_confirmation_settings(compte_id):
    return get_setting(compte_id, "confirmation", {
        "actif": False, "sujet_template": "", "corps_template": "", "nb_envoyes": 0,
    })

def sauver_confirmation_settings_file(compte_id, settings):
    set_setting(compte_id, "confirmation", settings)

@app.route("/api/confirmation/<compte_id>", methods=["GET"])
def get_confirmation(compte_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    return jsonify({
        "ok": True,
        "sujet_default": CONFIRMATION_SUJET_DEFAULT,
        "corps_default":  CONFIRMATION_CORPS_DEFAULT,
        **charger_confirmation_settings(compte_id),
    })

@app.route("/api/confirmation/<compte_id>", methods=["POST"])
def sauver_confirmation(compte_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    d = request.json or {}
    s = charger_confirmation_settings(compte_id)
    if "actif" in d:
        s["actif"] = bool(d["actif"])
    if "sujet_template" in d:
        s["sujet_template"] = d["sujet_template"]
    if "corps_template" in d:
        s["corps_template"] = d["corps_template"]
    sauver_confirmation_settings_file(compte_id, s)
    return jsonify({"ok": True, **s})

def _construire_email_confirmation(agent_nom, agent_agence, rdv, settings=None):
    vars_map  = _vars_rdv(agent_nom, agent_agence, rdv)
    sujet_tpl = (settings or {}).get("sujet_template") or CONFIRMATION_SUJET_DEFAULT
    corps_tpl = (settings or {}).get("corps_template") or CONFIRMATION_CORPS_DEFAULT
    return _remplir_template(sujet_tpl, vars_map), _remplir_template(corps_tpl, vars_map)

def _tenter_confirmation(compte_id, rdv):
    """Envoie l'email de confirmation d'un RDV si le feature est activé."""
    from datetime import datetime as _dt
    try:
        s = charger_confirmation_settings(compte_id)
        if not s.get("actif"):
            return
        dest = rdv.get("client_email", "").strip()
        if not dest:
            return
        data = charger_comptes()
        c    = trouver_compte(data, compte_id)
        if not c:
            return
        # Première boite connectée
        boite = None
        for b in c.get("boites", []):
            provider = b.get("provider", "gmail")
            if provider == "gmail" and b.get("connecte") and b.get("token"):
                boite = b; break
            if provider == "microsoft" and b.get("token"):
                boite = b; break
            if provider == "imap" and b.get("imap_server") and b.get("imap_password"):
                boite = b; break
        if not boite:
            return
        sujet, corps = _construire_email_confirmation(c.get("nom",""), c.get("agence",""), rdv, s)
        expediteur   = boite.get("email", "")
        provider     = boite.get("provider", "gmail")
        if provider == "gmail":
            _envoyer_relance_gmail(boite["token"], expediteur, dest, sujet, corps)
        elif provider == "microsoft":
            _envoyer_relance_microsoft(boite["token"], expediteur, dest, sujet, corps)
        elif provider == "imap":
            _envoyer_relance_smtp(
                boite.get("smtp_server",""), boite.get("smtp_port","465"),
                boite.get("imap_password",""), expediteur, dest, sujet, corps
            )
        # Marquer confirmation envoyée sur le RDV
        rdvs = charger_agenda(compte_id)
        for r in rdvs:
            if r["id"] == rdv["id"]:
                r["confirmation_envoyee_at"] = _dt.now().isoformat()
                break
        sauver_agenda(compte_id, rdvs)
        s["nb_envoyes"] = s.get("nb_envoyes", 0) + 1
        sauver_confirmation_settings_file(compte_id, s)
        print(f"✅ Confirmation RDV envoyée → {dest} ({rdv.get('titre','')} — {c.get('nom','')})")
    except Exception as e:
        print(f"❌ Confirmation RDV échouée → {rdv.get('client_email','?')}: {e}")


# ══════════════════════════════════════════════════════════════
# RELANCE AUTO — Thread de relance des RDV en attente
# ══════════════════════════════════════════════════════════════

RELANCE_SUJET_DEFAULT = "Rappel : votre rendez-vous du {date} — {titre}"
RELANCE_CORPS_DEFAULT = """\
Bonjour {client_nom},

Je vous contacte au sujet de notre rendez-vous prévu le {date} de {heure_debut} à {heure_fin}.

Rendez-vous : {titre}{adresse_line}

Pourriez-vous confirmer votre disponibilité pour ce rendez-vous ?

Dans l'attente de votre retour, je reste à votre disposition pour toute question.

Cordialement,
{agent_nom}"""

def charger_relance_settings(compte_id):
    return get_setting(compte_id, "relance", {
        "actif": False, "delai_heures": 48,
        "sujet_template": "", "corps_template": "",
        "derniere_exec": None, "nb_envoyees": 0,
    })

def sauver_relance_settings(compte_id, settings):
    set_setting(compte_id, "relance", settings)

@app.route("/api/relance/<compte_id>", methods=["GET"])
def get_relance(compte_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    return jsonify({
        "ok": True,
        "sujet_default": RELANCE_SUJET_DEFAULT,
        "corps_default":  RELANCE_CORPS_DEFAULT,
        **charger_relance_settings(compte_id),
    })

@app.route("/api/relance/<compte_id>", methods=["POST"])
def sauver_relance(compte_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    d = request.json or {}
    s = charger_relance_settings(compte_id)
    if "actif" in d:
        s["actif"] = bool(d["actif"])
    if "delai_heures" in d:
        s["delai_heures"] = int(d["delai_heures"])
    if "sujet_template" in d:
        s["sujet_template"] = d["sujet_template"]
    if "corps_template" in d:
        s["corps_template"] = d["corps_template"]
    sauver_relance_settings(compte_id, s)
    return jsonify({"ok": True, **s})


def _envoyer_relance_gmail(token_path, expediteur, destinataire, sujet, corps):
    """Envoie une relance via l'API Gmail."""
    import base64 as _b64
    from email.mime.text import MIMEText as _MIMEText
    from google.oauth2.credentials import Credentials as _Creds
    from googleapiclient.discovery import build as _build
    GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
    creds = _Creds.from_authorized_user_file(token_path, GMAIL_SCOPES)
    if creds.expired and creds.refresh_token:
        from google.auth.transport.requests import Request as _Req
        creds.refresh(_Req())
    service = _build("gmail", "v1", credentials=creds, cache_discovery=False)
    msg = _MIMEText(corps, "plain", "utf-8")
    msg["From"]    = expediteur
    msg["To"]      = destinataire
    msg["Subject"] = sujet
    raw = _b64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()


def _envoyer_relance_microsoft(token_path, expediteur, destinataire, sujet, corps):
    """Envoie une relance via Microsoft Graph."""
    try:
        token_data = json.loads(Path(token_path).read_text())
    except Exception:
        raise Exception("Token Microsoft introuvable")
    access_token = token_data.get("access_token", "")
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    body = {
        "message": {
            "subject": sujet,
            "body": {"contentType": "Text", "content": corps},
            "toRecipients": [{"emailAddress": {"address": destinataire}}],
        }
    }
    r = http_requests.post("https://graph.microsoft.com/v1.0/me/sendMail", headers=headers, json=body, timeout=15)
    if r.status_code == 401:
        raise Exception("Token Microsoft expiré")


def _envoyer_relance_smtp(smtp_server, smtp_port, imap_password, expediteur, destinataire, sujet, corps):
    """Envoie une relance via SMTP."""
    import smtplib as _smtp
    from email.mime.text import MIMEText as _MIMEText
    msg = _MIMEText(corps, "plain", "utf-8")
    msg["From"]    = expediteur
    msg["To"]      = destinataire
    msg["Subject"] = sujet
    smtp_port = int(smtp_port)
    if smtp_port == 587:
        with _smtp.SMTP(smtp_server, smtp_port, timeout=15) as s:
            s.starttls()
            s.login(expediteur, imap_password)
            s.sendmail(expediteur, destinataire, msg.as_bytes())
    else:
        import ssl as _ssl
        ctx = _ssl.create_default_context()
        with _smtp.SMTP_SSL(smtp_server, smtp_port, context=ctx, timeout=15) as s:
            s.login(expediteur, imap_password)
            s.sendmail(expediteur, destinataire, msg.as_bytes())


def _remplir_template(tpl, vars_map):
    """Remplace les {variables} dans un template."""
    for k, v in vars_map.items():
        tpl = tpl.replace("{" + k + "}", str(v))
    return tpl

def _vars_rdv(agent_nom, agent_agence, rdv):
    """Construit le dictionnaire de variables pour un RDV."""
    from datetime import datetime as _dt
    try:
        date_fr = _dt.strptime(rdv["date"], "%Y-%m-%d").strftime("%d/%m/%Y")
    except Exception:
        date_fr = rdv.get("date", "")
    adr = rdv.get("adresse", "").strip()
    return {
        "client_nom":   rdv.get("client_nom", ""),
        "date":         date_fr,
        "heure_debut":  rdv.get("heure_debut", ""),
        "heure_fin":    rdv.get("heure_fin", ""),
        "titre":        rdv.get("titre", ""),
        "adresse":      adr,
        "adresse_line": ("\nAdresse : " + adr) if adr else "",
        "notes":        rdv.get("notes", ""),
        "agent_nom":    agent_nom + ((" — " + agent_agence) if agent_agence else ""),
        "agent_agence": agent_agence,
    }

def _construire_email_relance(agent_nom, agent_agence, rdv, settings=None):
    """Génère le sujet et corps de la relance (template personnalisé ou défaut)."""
    vars_map = _vars_rdv(agent_nom, agent_agence, rdv)
    sujet_tpl = (settings or {}).get("sujet_template") or RELANCE_SUJET_DEFAULT
    corps_tpl = (settings or {}).get("corps_template") or RELANCE_CORPS_DEFAULT
    return _remplir_template(sujet_tpl, vars_map), _remplir_template(corps_tpl, vars_map)


def thread_relance_auto():
    """Thread de fond : vérifie toutes les 30 min les RDV en attente et envoie des relances."""
    import time as _time
    from datetime import datetime as _dt, timedelta as _td
    _time.sleep(15)  # attendre que tout soit initialisé
    print("🔔 Thread relance auto démarré")
    while True:
        try:
            data = charger_comptes()
            for c in data.get("comptes", []):
                compte_id = c.get("id")
                if not compte_id:
                    continue
                s = charger_relance_settings(compte_id)
                if not s.get("actif"):
                    continue
                delai_h = int(s.get("delai_heures", 48))
                seuil   = _dt.now() - _td(hours=delai_h)
                rdvs    = charger_agenda(compte_id)
                modifie = False

                # Trouver la première boite connectée du compte
                boite = None
                for b in c.get("boites", []):
                    provider = b.get("provider", "gmail")
                    if provider == "gmail" and b.get("connecte") and b.get("token"):
                        boite = b; break
                    if provider == "microsoft" and b.get("token"):
                        boite = b; break
                    if provider == "imap" and b.get("imap_server") and b.get("imap_password"):
                        boite = b; break
                if not boite:
                    continue

                for rdv in rdvs:
                    # Seulement les RDV en attente sans relance déjà envoyée
                    if rdv.get("statut") != "attente":
                        continue
                    if rdv.get("relance_envoyee_at"):
                        continue
                    # Vérifier si le délai est dépassé
                    created_str = rdv.get("created_at", "")
                    if not created_str:
                        continue
                    try:
                        created = _dt.fromisoformat(created_str)
                    except Exception:
                        continue
                    if created > seuil:
                        continue  # pas encore assez vieux

                    # Vérifier qu'il y a bien un email client
                    dest = rdv.get("client_email", "").strip()
                    if not dest:
                        continue

                    expediteur = boite.get("email", "")
                    sujet, corps = _construire_email_relance(c.get("nom",""), c.get("agence",""), rdv, s)
                    provider = boite.get("provider", "gmail")

                    try:
                        if provider == "gmail":
                            _envoyer_relance_gmail(boite["token"], expediteur, dest, sujet, corps)
                        elif provider == "microsoft":
                            _envoyer_relance_microsoft(boite["token"], expediteur, dest, sujet, corps)
                        elif provider == "imap":
                            _envoyer_relance_smtp(
                                boite.get("smtp_server",""), boite.get("smtp_port","465"),
                                boite.get("imap_password",""), expediteur, dest, sujet, corps
                            )
                        rdv["relance_envoyee_at"] = _dt.now().isoformat()
                        modifie = True
                        s["nb_envoyees"] = s.get("nb_envoyees", 0) + 1
                        print(f"✅ Relance envoyée → {dest} (RDV {rdv['id']} — {c['nom']})")
                    except Exception as e:
                        print(f"❌ Relance échouée → {dest}: {e}")

                if modifie:
                    sauver_agenda(compte_id, rdvs)
                    s["derniere_exec"] = _dt.now().isoformat()
                    sauver_relance_settings(compte_id, s)

        except Exception as e:
            print(f"❌ Erreur thread relance: {e}")

        _time.sleep(1800)  # toutes les 30 minutes


# ══════════════════════════════════════════════════════════════
# RAPPEL RDV — Thread de rappel J-1 / H-X avant les RDV confirmés
# ══════════════════════════════════════════════════════════════

RAPPEL_SUJET_DEFAULT = "Rappel de votre rendez-vous — {titre}"
RAPPEL_CORPS_DEFAULT = """\
Bonjour {client_nom},

Nous vous rappelons votre rendez-vous confirmé :

📅 Date : {date}
🕐 Heure : {heure_debut} — {heure_fin}
📌 {titre}{adresse_line}

En cas d'empêchement, n'hésitez pas à nous contacter le plus tôt possible afin de reprogrammer.

À très bientôt,
{agent_nom}"""

def charger_rappel_settings(compte_id):
    return get_setting(compte_id, "rappel", {
        "actif": False, "avance_heures": 24,
        "sujet_template": "", "corps_template": "",
        "derniere_exec": None, "nb_envoyes": 0,
    })

def sauver_rappel_settings(compte_id, settings):
    set_setting(compte_id, "rappel", settings)

@app.route("/api/rappel/<compte_id>", methods=["GET"])
def get_rappel(compte_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    return jsonify({
        "ok": True,
        "sujet_default": RAPPEL_SUJET_DEFAULT,
        "corps_default":  RAPPEL_CORPS_DEFAULT,
        **charger_rappel_settings(compte_id),
    })

@app.route("/api/rappel/<compte_id>", methods=["POST"])
def sauver_rappel(compte_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    d = request.json or {}
    s = charger_rappel_settings(compte_id)
    if "actif" in d:
        s["actif"] = bool(d["actif"])
    if "avance_heures" in d:
        s["avance_heures"] = int(d["avance_heures"])
    if "sujet_template" in d:
        s["sujet_template"] = d["sujet_template"]
    if "corps_template" in d:
        s["corps_template"] = d["corps_template"]
    sauver_rappel_settings(compte_id, s)
    return jsonify({"ok": True, **s})


def _construire_email_rappel(agent_nom, agent_agence, rdv, settings=None):
    """Génère l'email de rappel de RDV confirmé (template personnalisé ou défaut)."""
    vars_map  = _vars_rdv(agent_nom, agent_agence, rdv)
    sujet_tpl = (settings or {}).get("sujet_template") or RAPPEL_SUJET_DEFAULT
    corps_tpl = (settings or {}).get("corps_template") or RAPPEL_CORPS_DEFAULT
    return _remplir_template(sujet_tpl, vars_map), _remplir_template(corps_tpl, vars_map)


def thread_rappel_rdv():
    """Thread de fond : envoie des rappels avant les RDV confirmés."""
    import time as _time
    from datetime import datetime as _dt, timedelta as _td
    _time.sleep(20)
    print("📅 Thread rappel RDV démarré")
    while True:
        try:
            data = charger_comptes()
            now  = _dt.now()
            for c in data.get("comptes", []):
                compte_id = c.get("id")
                if not compte_id:
                    continue
                s = charger_rappel_settings(compte_id)
                if not s.get("actif"):
                    continue
                avance_h = int(s.get("avance_heures", 24))
                rdvs     = charger_agenda(compte_id)
                modifie  = False

                # Première boite connectée
                boite = None
                for b in c.get("boites", []):
                    provider = b.get("provider", "gmail")
                    if provider == "gmail" and b.get("connecte") and b.get("token"):
                        boite = b; break
                    if provider == "microsoft" and b.get("token"):
                        boite = b; break
                    if provider == "imap" and b.get("imap_server") and b.get("imap_password"):
                        boite = b; break
                if not boite:
                    continue

                for rdv in rdvs:
                    if rdv.get("statut") != "confirme":
                        continue
                    if rdv.get("rappel_envoye_at"):
                        continue
                    dest = rdv.get("client_email", "").strip()
                    if not dest:
                        continue

                    # Calculer le datetime du RDV
                    try:
                        rdv_dt = _dt.strptime(f"{rdv['date']} {rdv['heure_debut']}", "%Y-%m-%d %H:%M")
                    except Exception:
                        continue

                    # Fenêtre : dans moins de avance_h heures mais pas déjà passé
                    delta = rdv_dt - now
                    if not (0 <= delta.total_seconds() <= avance_h * 3600):
                        continue

                    expediteur = boite.get("email", "")
                    sujet, corps = _construire_email_rappel(c.get("nom",""), c.get("agence",""), rdv, s)
                    provider = boite.get("provider", "gmail")

                    try:
                        if provider == "gmail":
                            _envoyer_relance_gmail(boite["token"], expediteur, dest, sujet, corps)
                        elif provider == "microsoft":
                            _envoyer_relance_microsoft(boite["token"], expediteur, dest, sujet, corps)
                        elif provider == "imap":
                            _envoyer_relance_smtp(
                                boite.get("smtp_server",""), boite.get("smtp_port","465"),
                                boite.get("imap_password",""), expediteur, dest, sujet, corps
                            )
                        rdv["rappel_envoye_at"] = now.isoformat()
                        modifie = True
                        s["nb_envoyes"] = s.get("nb_envoyes", 0) + 1
                        print(f"📅 Rappel RDV envoyé → {dest} ({rdv['titre']} — {c['nom']})")
                    except Exception as e:
                        print(f"❌ Rappel RDV échoué → {dest}: {e}")

                if modifie:
                    sauver_agenda(compte_id, rdvs)
                    s["derniere_exec"] = now.isoformat()
                    sauver_rappel_settings(compte_id, s)

        except Exception as e:
            print(f"❌ Erreur thread rappel: {e}")

        _time.sleep(1800)


# Démarrage des threads relance + rappel sous Gunicorn (production Railway)
_relance_thread_started = False
def _start_relance_thread():
    global _relance_thread_started
    if not _relance_thread_started:
        _relance_thread_started = True
        threading.Thread(target=thread_relance_auto, daemon=True).start()
        threading.Thread(target=thread_rappel_rdv,  daemon=True).start()

_start_relance_thread()


# ── Lancement ─────────────────────────────────────────────────
if __name__ == "__main__":
    port     = int(os.environ.get("PORT", 5001))
    is_local = port == 5001

    # Auto-restart des agents après reboot (Railway ou local)
    threading.Thread(target=auto_restart_agents, daemon=True).start()

    if is_local:
        import webbrowser
        def ouvrir():
            import time; time.sleep(1.2)
            webbrowser.open(f"http://127.0.0.1:{port}")
        threading.Thread(target=ouvrir, daemon=True).start()
        print("\n🛩️  MailPilot Pro — Interface multi-boîtes")
        print(f"   Ouvre http://127.0.0.1:{port}\n")

    app.run(debug=False, host="0.0.0.0", port=port)
