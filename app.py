# ============================================================
# app.py — Interface web multi-comptes pour MailPilot
# Supporte plusieurs boîtes mail par compte (Gmail, Outlook, IMAP)
# ============================================================

import sys
import os
import uuid
import json
import time
import secrets
import threading
import subprocess
from pathlib import Path
from urllib.parse import urlencode

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
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "mailpilot2024")

from prompts import TOUS_LES_LABELS, LABELS_DEFAUT

# ── Chemins ──────────────────────────────────────────────────
BASE_DIR     = Path(__file__).parent
DATA_DIR     = Path(os.environ.get("DATA_DIR", str(BASE_DIR)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
COMPTES_FILE = DATA_DIR / "comptes.json"
TOKENS_DIR   = DATA_DIR / "tokens"
TOKENS_DIR.mkdir(exist_ok=True)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

# ── État en mémoire ───────────────────────────────────────────
# Clé = f"{compte_id}_{boite_id}"
processus       = {}
logs_par_compte = {}
emails_comptes  = {}
oauth_statut    = {}
oauth_flows     = {}  # state -> {compte_id, boite_id, ...}
lock            = threading.Lock()
MAX_LOGS        = 150


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


def charger_comptes():
    if COMPTES_FILE.exists():
        data = json.loads(COMPTES_FILE.read_text())
        modifie = False
        for c in data.get("comptes", []):
            if not c.get("access_token"):
                c["access_token"] = secrets.token_urlsafe(16)
                modifie = True
            if "boites" not in c:
                migrer_compte_vers_boites(c)
                modifie = True
        if modifie:
            sauver_comptes(data)
        return data
    return {"api_key": "", "comptes": []}


def sauver_comptes(data):
    COMPTES_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


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
    return session.get("user_id") == compte_id


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


# ── Routes ────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        d     = request.json
        email = d.get("email", "").strip().lower()
        pwd   = d.get("password", "")
        data  = charger_comptes()
        for c in data["comptes"]:
            if get_login_email(c).lower() == email and c.get("password_hash"):
                if check_password_hash(c["password_hash"], pwd):
                    session["user_id"] = c["id"]
                    return jsonify({"ok": True})
        return jsonify({"ok": False, "message": "Email ou mot de passe incorrect"})
    if session.get("admin"):
        return redirect("/admin-dashboard")
    if session.get("user_id"):
        return redirect("/dashboard")
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
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
            "password_hash": generate_password_hash(pwd),
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
    return render_template("client.html", compte=compte)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# ── Admin ─────────────────────────────────────────────────────

@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        pwd = request.json.get("password", "")
        if pwd == ADMIN_PASSWORD:
            session["admin"] = True
            return jsonify({"ok": True})
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
    compte = {
        "id":           str(uuid.uuid4())[:8],
        "access_token": secrets.token_urlsafe(16),
        "login_email":  email,
        "nom":          d.get("nom", ""),
        "agence":       d.get("agence", ""),
        "tel":          d.get("tel", ""),
        "zone":         d.get("zone", ""),
        "boites":       [boite],
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
    data["comptes"] = [c for c in data["comptes"] if c["id"] != compte_id]
    sauver_comptes(data)
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
    c["boites"] = [b for b in c["boites"] if b["id"] != boite_id]
    sauver_comptes(data)
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

    env = os.environ.copy()
    env.update({
        "AGENT_NOM":         c["nom"],
        "AGENT_AGENCE":      c.get("agence", ""),
        "AGENT_TEL":         c.get("tel", ""),
        "AGENT_EMAIL":       b["email"],
        "AGENT_ZONE":        c.get("zone", ""),
        "ANTHROPIC_API_KEY": data.get("api_key", ""),
        "CHECK_INTERVAL":    b.get("intervalle", "60"),
        "LABELS_ACTIFS":     ",".join(b.get("labels_actifs", LABELS_DEFAUT)),
        "MAIL_PROVIDER":     provider,
        "COMPTE_ID":         pk,
        "STATS_DIR":         str(DATA_DIR),
        "BILAN_JOUR":          str(b.get("bilan_jour",  "0")),
        "BILAN_HEURE":         str(b.get("bilan_heure", "8")),
        "AGENT_INSTRUCTIONS":  b.get("instructions", ""),
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

    logs_par_compte[pk] = []
    emails_comptes[pk]  = 0

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
    if jour not in [str(i) for i in range(7)]:
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
    stats_id   = pkey(compte_id, boite_id)
    stats_file = DATA_DIR / f"stats_{stats_id}.json"
    hist_file  = DATA_DIR / f"stats_hist_{stats_id}.json"
    semaine_courante = {}
    historique       = []
    if stats_file.exists():
        try:
            semaine_courante = json.loads(stats_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    if hist_file.exists():
        try:
            historique = json.loads(hist_file.read_text(encoding="utf-8")).get("semaines", [])
        except Exception:
            pass
    return jsonify({"ok": True, "semaine_courante": semaine_courante, "historique": historique[-12:]})


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


# ── Lancement ─────────────────────────────────────────────────
if __name__ == "__main__":
    port     = int(os.environ.get("PORT", 5001))
    is_local = port == 5001

    if is_local:
        import webbrowser
        def ouvrir():
            import time; time.sleep(1.2)
            webbrowser.open(f"http://127.0.0.1:{port}")
        threading.Thread(target=ouvrir, daemon=True).start()
        print("\n🛩️  MailPilot Pro — Interface multi-boîtes")
        print(f"   Ouvre http://127.0.0.1:{port}\n")

    app.run(debug=False, host="0.0.0.0", port=port)
