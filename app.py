# ============================================================
# app.py — Interface web multi-comptes pour MailPilot
# Lance avec : python3 app.py
# Ouvre : http://127.0.0.1:5000
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
MS_AUTH_URL   = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
MS_TOKEN_URL  = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
MS_SCOPES     = "https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/Mail.ReadWrite https://graph.microsoft.com/Mail.Send offline_access"

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
ADMIN_PASSWORD  = os.environ.get("ADMIN_PASSWORD", "mailpilot2024")

# Labels disponibles (importés depuis prompts.py)
from prompts import TOUS_LES_LABELS, LABELS_DEFAUT

# ── Chemins ──────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
# DATA_DIR peut être monté sur un volume persistant (Railway)
DATA_DIR     = Path(os.environ.get("DATA_DIR", str(BASE_DIR)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
COMPTES_FILE = DATA_DIR / "comptes.json"
TOKENS_DIR   = DATA_DIR / "tokens"
TOKENS_DIR.mkdir(exist_ok=True)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

# ── État en mémoire ───────────────────────────────────────────
processus       = {}   # compte_id -> subprocess
logs_par_compte = {}   # compte_id -> [lignes]
emails_comptes  = {}   # compte_id -> int
oauth_statut    = {}   # compte_id -> 'en_cours' | 'ok' | 'erreur'
oauth_flows     = {}   # state -> compte_id
lock            = threading.Lock()
MAX_LOGS        = 150


# ── Credentials helper ───────────────────────────────────────

def get_creds_path():
    """Retourne le chemin vers credentials.json (depuis fichier ou env var)."""
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        tmp_path = BASE_DIR / "credentials_tmp.json"
        tmp_path.write_text(creds_json)
        return str(tmp_path)
    return str(BASE_DIR / "credentials.json")


# ── Persistence (comptes.json) ────────────────────────────────

def charger_comptes():
    if COMPTES_FILE.exists():
        data = json.loads(COMPTES_FILE.read_text())
        # Migration : ajoute access_token aux comptes existants
        modifie = False
        for c in data.get("comptes", []):
            if not c.get("access_token"):
                c["access_token"] = secrets.token_urlsafe(16)
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

def check_access(compte_id):
    """Retourne True si admin connecté ou si l'utilisateur connecté est le propriétaire du compte."""
    if session.get("admin"):
        return True
    return session.get("user_id") == compte_id


# ── Capture logs subprocess ───────────────────────────────────

def capturer_logs(compte_id, process):
    logs_par_compte.setdefault(compte_id, [])
    for ligne in iter(process.stdout.readline, ""):
        ligne = ligne.strip()
        if not ligne:
            continue
        with lock:
            logs_par_compte[compte_id].append(ligne)
            if len(logs_par_compte[compte_id]) > MAX_LOGS:
                logs_par_compte[compte_id].pop(0)
            if "Brouillon créé" in ligne:
                emails_comptes[compte_id] = emails_comptes.get(compte_id, 0) + 1
    process.wait()


# ── Routes ────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    """Connexion client (email + mot de passe)."""
    if request.method == "POST":
        d = request.json
        email = d.get("email", "").strip().lower()
        pwd   = d.get("password", "")
        data  = charger_comptes()
        for c in data["comptes"]:
            if c.get("email", "").lower() == email and c.get("password_hash"):
                if check_password_hash(c["password_hash"], pwd):
                    session["user_id"] = c["id"]
                    return jsonify({"ok": True})
        return jsonify({"ok": False, "message": "Email ou mot de passe incorrect"})
    # Si déjà connecté, redirige directement
    if session.get("admin"):
        return redirect("/admin-dashboard")
    if session.get("user_id"):
        return redirect("/dashboard")
    return render_template("login.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    """Inscription d'un nouveau client."""
    if request.method == "POST":
        d   = request.json
        nom = d.get("nom", "").strip()
        email = d.get("email", "").strip().lower()
        pwd = d.get("password", "")
        if not nom or not email or not pwd:
            return jsonify({"ok": False, "message": "Nom, email et mot de passe sont requis"})
        if len(pwd) < 6:
            return jsonify({"ok": False, "message": "Mot de passe trop court (6 caractères min)"})
        data = charger_comptes()
        for c in data["comptes"]:
            if c.get("email", "").lower() == email:
                return jsonify({"ok": False, "message": "Cet email est déjà utilisé"})
        provider = d.get("provider", "gmail")
        compte = {
            "id":            str(uuid.uuid4())[:8],
            "access_token":  secrets.token_urlsafe(16),
            "password_hash": generate_password_hash(pwd),
            "nom":           nom,
            "agence":        d.get("agence", ""),
            "tel":           d.get("tel", ""),
            "email":         email,
            "zone":          d.get("zone", ""),
            "intervalle":    "60",
            "provider":      provider,
            "connecte":      provider == "imap",    # IMAP : connecté dès l'inscription, Microsoft/Gmail nécessitent OAuth
            "token":         "",
            "labels_actifs": LABELS_DEFAUT,
        }
        if provider == "imap":
            compte["imap_server"]   = d.get("imap_server", "")
            compte["imap_port"]     = d.get("imap_port", "993")
            compte["smtp_server"]   = d.get("smtp_server", "")
            compte["smtp_port"]     = d.get("smtp_port", "465")
            compte["imap_password"] = d.get("imap_password", "")
        data["comptes"].append(compte)
        sauver_comptes(data)
        session["user_id"] = compte["id"]
        return jsonify({"ok": True})
    if session.get("user_id"):
        return redirect("/dashboard")
    return render_template("register.html")

@app.route("/dashboard")
def dashboard():
    """Page principale du client connecté."""
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
    # Pour Microsoft : si le fichier token n'existe plus, on remet à zéro
    if compte.get("provider") == "microsoft" and compte.get("token"):
        if not Path(compte["token"]).exists():
            compte["token"]    = ""
            compte["connecte"] = False
            sauver_comptes(data)
    return render_template("client.html", compte=compte)

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

# ── Admin ─────────────────────────────────────────────────────

@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    """Page de connexion admin (Thomas uniquement)."""
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
    d = request.json
    compte = {
        "id":           str(uuid.uuid4())[:8],
        "access_token": secrets.token_urlsafe(16),
        "nom":          d.get("nom", ""),
        "labels_actifs": LABELS_DEFAUT,
        "agence":   d.get("agence", ""),
        "tel":      d.get("tel", ""),
        "email":    d.get("email", ""),
        "zone":     d.get("zone", ""),
        "intervalle": d.get("intervalle", "60"),
        "connecte": False,
        "token":    "",
    }
    data = charger_comptes()
    data["comptes"].append(compte)
    sauver_comptes(data)
    return jsonify({"ok": True, "compte": compte})


@app.route("/modifier_compte/<compte_id>", methods=["POST"])
def modifier_compte(compte_id):
    if not session.get("admin"):
        return jsonify({"ok": False}), 403
    d = request.json
    data = charger_comptes()
    c = trouver_compte(data, compte_id)
    if not c:
        return jsonify({"ok": False})
    for champ in ["nom", "agence", "tel", "email", "zone", "intervalle"]:
        if champ in d:
            c[champ] = d[champ]
    sauver_comptes(data)
    return jsonify({"ok": True})


@app.route("/supprimer_compte/<compte_id>", methods=["POST"])
def supprimer_compte(compte_id):
    if not session.get("admin"):
        return jsonify({"ok": False}), 403
    # Arrêter le processus s'il tourne
    if compte_id in processus:
        p = processus.pop(compte_id)
        if p.poll() is None:
            p.terminate()
    data = charger_comptes()
    data["comptes"] = [c for c in data["comptes"] if c["id"] != compte_id]
    sauver_comptes(data)
    # Supprimer le token
    token_path = TOKENS_DIR / f"token_{compte_id}.json"
    if token_path.exists():
        token_path.unlink()
    return jsonify({"ok": True})


@app.route("/connecter_gmail/<compte_id>", methods=["POST"])
def connecter_gmail(compte_id):
    """Lance le flux OAuth Gmail (web redirect)."""
    if not check_access(compte_id):
        return jsonify({"ok": False, "message": "Accès refusé"}), 403
    if oauth_statut.get(compte_id) == "en_cours":
        return jsonify({"ok": False, "message": "Connexion déjà en cours…"})

    creds_path = get_creds_path()
    if not Path(creds_path).exists():
        return jsonify({"ok": False, "message": "credentials.json introuvable !"})

    # Vérifie si un token valide existe déjà
    token_path = str(TOKENS_DIR / f"token_{compte_id}.json")
    if Path(token_path).exists():
        try:
            creds = Credentials.from_authorized_user_file(token_path, GMAIL_SCOPES)
            if creds.valid:
                data = charger_comptes()
                c = trouver_compte(data, compte_id)
                if c:
                    c["connecte"] = True
                    c["token"] = token_path
                    sauver_comptes(data)
                oauth_statut[compte_id] = "ok"
                return jsonify({"ok": True, "message": "Déjà connecté !"})
            elif creds.expired and creds.refresh_token:
                creds.refresh(Request())
                Path(token_path).write_text(creds.to_json())
                data = charger_comptes()
                c = trouver_compte(data, compte_id)
                if c:
                    c["connecte"] = True
                    c["token"] = token_path
                    sauver_comptes(data)
                oauth_statut[compte_id] = "ok"
                return jsonify({"ok": True, "message": "Token rafraîchi !"})
        except Exception:
            pass

    # Construit l'URL OAuth web
    is_local = request.host.startswith('127') or request.host.startswith('localhost')
    scheme = 'http' if is_local else 'https'
    redirect_uri = f'{scheme}://{request.host}/oauth/callback'
    flow = Flow.from_client_secrets_file(
        creds_path,
        scopes=GMAIL_SCOPES,
        redirect_uri=redirect_uri
    )
    auth_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        prompt='consent'
    )
    # Stocke le flow ENTIER (avec code_verifier PKCE) + compte_id
    oauth_flows[state] = {"compte_id": compte_id, "flow": flow}
    oauth_statut[compte_id] = "en_cours"
    return jsonify({"ok": True, "auth_url": auth_url})


@app.route("/oauth/callback")
def oauth_callback():
    """Reçoit le code OAuth de Google et enregistre le token."""
    state    = request.args.get('state')
    error    = request.args.get('error')

    flow_data = oauth_flows.pop(state, None)
    compte_id = flow_data["compte_id"] if flow_data else None

    if error or not flow_data:
        if compte_id:
            oauth_statut[compte_id] = "erreur"
        return redirect('/')

    # Réutilise le flow original (conserve le code_verifier PKCE)
    flow = flow_data["flow"]

    try:
        # Reconstruit l'URL de réponse en forçant https sur Railway
        auth_response = request.url
        is_local = request.host.startswith('127') or request.host.startswith('localhost')
        if not is_local:
            auth_response = auth_response.replace('http://', 'https://')
        flow.fetch_token(authorization_response=auth_response)
        creds = flow.credentials

        token_path = str(TOKENS_DIR / f"token_{compte_id}.json")
        Path(token_path).write_text(creds.to_json())

        data = charger_comptes()
        c = trouver_compte(data, compte_id)
        if c:
            c["connecte"] = True
            c["token"]    = token_path
            sauver_comptes(data)

        oauth_statut[compte_id] = "ok"
        import sys
        print(f"[OAuth SUCCESS] compte={compte_id} connecte=True token={token_path}", file=sys.stderr)

    except Exception as e:
        import sys
        print(f"[OAuth Callback ERREUR] compte={compte_id} err={e}", file=sys.stderr)
        oauth_statut[compte_id] = "erreur"
        with lock:
            logs_par_compte.setdefault(compte_id, []).append(f"[ERREUR OAuth] {e}")

    # Redirige vers la bonne page selon le type d'utilisateur
    if session.get("admin"):
        return redirect('/admin-dashboard')
    else:
        return redirect('/dashboard')


@app.route("/statut_oauth/<compte_id>")
def statut_oauth(compte_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    return jsonify({"statut": oauth_statut.get(compte_id, "")})


# ── OAuth Microsoft ────────────────────────────────────────────

@app.route("/connecter_microsoft/<compte_id>", methods=["POST"])
def connecter_microsoft(compte_id):
    """Lance le flux OAuth Microsoft."""
    if not check_access(compte_id):
        return jsonify({"ok": False, "message": "Accès refusé"}), 403
    if oauth_statut.get(compte_id) == "en_cours":
        return jsonify({"ok": False, "message": "Connexion déjà en cours…"})

    client_id = os.environ.get("MICROSOFT_CLIENT_ID", "")
    if not client_id:
        return jsonify({"ok": False, "message": "Microsoft OAuth non configuré (MICROSOFT_CLIENT_ID manquant dans Railway)"})

    is_local   = request.host.startswith('127') or request.host.startswith('localhost')
    scheme     = 'http' if is_local else 'https'
    redirect_uri = f'{scheme}://{request.host}/oauth/microsoft/callback'

    state = secrets.token_urlsafe(16)
    oauth_flows[state] = {"compte_id": compte_id, "provider": "microsoft", "redirect_uri": redirect_uri}
    oauth_statut[compte_id] = "en_cours"

    params = {
        "client_id":     client_id,
        "response_type": "code",
        "redirect_uri":  redirect_uri,
        "scope":         MS_SCOPES,
        "state":         state,
        "response_mode": "query",
    }
    auth_url = MS_AUTH_URL + "?" + urlencode(params)
    return jsonify({"ok": True, "auth_url": auth_url})


@app.route("/oauth/microsoft/callback")
def microsoft_callback():
    """Reçoit le code OAuth de Microsoft et enregistre le token."""
    state = request.args.get("state")
    code  = request.args.get("code")
    error = request.args.get("error")

    flow_data = oauth_flows.pop(state, None)
    compte_id = flow_data["compte_id"] if flow_data else None

    if error or not flow_data or not code:
        if compte_id:
            oauth_statut[compte_id] = "erreur"
        return redirect("/dashboard")

    redirect_uri = flow_data["redirect_uri"]
    try:
        resp = http_requests.post(MS_TOKEN_URL, data={
            "client_id":     os.environ.get("MICROSOFT_CLIENT_ID", ""),
            "client_secret": os.environ.get("MICROSOFT_CLIENT_SECRET", ""),
            "code":          code,
            "redirect_uri":  redirect_uri,
            "grant_type":    "authorization_code",
        })
        token_data = resp.json()
        if "access_token" not in token_data:
            raise Exception(f"Réponse Microsoft invalide : {token_data}")

        token_path = str(TOKENS_DIR / f"token_ms_{compte_id}.json")
        with open(token_path, "w") as f:
            json.dump({
                "access_token":  token_data["access_token"],
                "refresh_token": token_data.get("refresh_token", ""),
                "expires_at":    time.time() + token_data.get("expires_in", 3600),
            }, f)

        data = charger_comptes()
        c = trouver_compte(data, compte_id)
        if c:
            c["connecte"] = True
            c["token"]    = token_path
            sauver_comptes(data)

        oauth_statut[compte_id] = "ok"

    except Exception as e:
        oauth_statut[compte_id] = "erreur"
        with lock:
            logs_par_compte.setdefault(compte_id, []).append(f"[ERREUR OAuth Microsoft] {e}")

    if session.get("admin"):
        return redirect("/admin-dashboard")
    return redirect("/dashboard")


@app.route("/demarrer/<compte_id>", methods=["POST"])
def demarrer(compte_id):
    if not check_access(compte_id):
        return jsonify({"ok": False, "message": "Accès refusé"}), 403
    if compte_id in processus and processus[compte_id].poll() is None:
        return jsonify({"ok": False, "message": "Déjà en cours"})

    data = charger_comptes()
    c = trouver_compte(data, compte_id)
    if not c:
        return jsonify({"ok": False, "message": "Compte introuvable"})
    provider = c.get("provider", "gmail")
    if provider == "gmail" and (not c.get("connecte") or not c.get("token")):
        return jsonify({"ok": False, "message": "Connecte d'abord Gmail !"})
    elif provider == "microsoft" and not c.get("token"):
        return jsonify({"ok": False, "message": "Connecte d'abord Outlook ! (bouton 🔗 Connecter Outlook)"})
    elif provider == "imap" and not c.get("imap_server"):
        return jsonify({"ok": False, "message": "Configuration IMAP incomplète."})

    # Prépare les variables d'environnement pour ce compte
    env = os.environ.copy()
    env.update({
        "AGENT_NOM":         c["nom"],
        "AGENT_AGENCE":      c["agence"],
        "AGENT_TEL":         c["tel"],
        "AGENT_EMAIL":       c["email"],
        "AGENT_ZONE":        c["zone"],
        "ANTHROPIC_API_KEY": data.get("api_key", ""),
        "CHECK_INTERVAL":    c.get("intervalle", "60"),
        "LABELS_ACTIFS":     ",".join(c.get("labels_actifs", list(TOUS_LES_LABELS.keys())[:7])),
        "MAIL_PROVIDER":     provider,
        "COMPTE_ID":         compte_id,
        "STATS_DIR":         str(DATA_DIR),
    })
    if provider == "microsoft":
        env.update({
            "MICROSOFT_TOKEN_PATH":   c.get("token", ""),
            "MICROSOFT_CLIENT_ID":    os.environ.get("MICROSOFT_CLIENT_ID", ""),
            "MICROSOFT_CLIENT_SECRET": os.environ.get("MICROSOFT_CLIENT_SECRET", ""),
        })
    elif provider == "imap":
        env.update({
            "IMAP_SERVER":   c.get("imap_server", ""),
            "IMAP_PORT":     str(c.get("imap_port", "993")),
            "SMTP_SERVER":   c.get("smtp_server", ""),
            "SMTP_PORT":     str(c.get("smtp_port", "465")),
            "IMAP_PASSWORD": c.get("imap_password", ""),
        })

    logs_par_compte[compte_id] = []
    emails_comptes[compte_id]  = 0

    cmd = [sys.executable, "mailpilot.py"]
    if provider == "gmail":
        cmd += ["--token", c["token"]]

    p = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=str(BASE_DIR),
        env=env,
    )
    processus[compte_id] = p
    threading.Thread(target=capturer_logs, args=(compte_id, p), daemon=True).start()

    return jsonify({"ok": True})


@app.route("/arreter/<compte_id>", methods=["POST"])
def arreter(compte_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    p = processus.pop(compte_id, None)
    if p and p.poll() is None:
        p.terminate()
        with lock:
            logs_par_compte.setdefault(compte_id, []).append("── Arrêté manuellement ──")
    return jsonify({"ok": True})


@app.route("/statut/<compte_id>")
def statut_compte(compte_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    actif = compte_id in processus and processus[compte_id].poll() is None
    with lock:
        logs = list(logs_par_compte.get(compte_id, [])[-25:])
    data = charger_comptes()
    c = trouver_compte(data, compte_id)
    connecte = c.get("connecte", False) if c else False
    return jsonify({
        "actif":          actif,
        "connecte":       connecte,
        "emails_traites": emails_comptes.get(compte_id, 0),
        "logs":           logs,
        "oauth":          oauth_statut.get(compte_id, ""),
        "provider":       c.get("provider", "gmail") if c else "gmail",
    })


@app.route("/labels/<compte_id>", methods=["GET", "POST"])
def gerer_labels(compte_id):
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    data = charger_comptes()
    c = trouver_compte(data, compte_id)
    if not c:
        return jsonify({"ok": False}), 404
    if request.method == "POST":
        labels = request.json.get("labels", [])
        # Valide que les labels existent
        labels = [l for l in labels if l in TOUS_LES_LABELS]
        if "INUTILE" not in labels:
            labels.append("INUTILE")  # INUTILE toujours actif
        c["labels_actifs"] = labels
        sauver_comptes(data)
        return jsonify({"ok": True, "labels": labels})
    # GET : retourne les labels actifs et tous les labels disponibles
    actifs = c.get("labels_actifs", LABELS_DEFAUT)
    return jsonify({
        "actifs": actifs,
        "tous": {k: {"emoji": v["emoji"], "description_ui": v["description_ui"]}
                 for k, v in TOUS_LES_LABELS.items()}
    })


@app.route("/stats/<compte_id>")
def stats_compte_route(compte_id):
    """Retourne les stats de la semaine courante + historique pour un compte."""
    if not check_access(compte_id):
        return jsonify({"ok": False}), 403
    stats_file = DATA_DIR / f"stats_{compte_id}.json"
    hist_file  = DATA_DIR / f"stats_hist_{compte_id}.json"
    semaine_courante = {}
    historique = []
    if stats_file.exists():
        try:
            with open(stats_file, encoding="utf-8") as f:
                semaine_courante = json.load(f)
        except Exception:
            pass
    if hist_file.exists():
        try:
            with open(hist_file, encoding="utf-8") as f:
                historique = json.load(f).get("semaines", [])
        except Exception:
            pass
    return jsonify({"ok": True, "semaine_courante": semaine_courante, "historique": historique[-12:]})


@app.route("/statut_global")
def statut_global():
    data = charger_comptes()
    result = {}
    for c in data["comptes"]:
        cid = c["id"]
        actif = cid in processus and processus[cid].poll() is None
        result[cid] = {
            "actif":          actif,
            "connecte":       c.get("connecte", False),
            "emails_traites": emails_comptes.get(cid, 0),
            "oauth":          oauth_statut.get(cid, ""),
        }
    return jsonify(result)


# ── Lancement ─────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    is_local = port == 5001

    if is_local:
        import webbrowser
        def ouvrir():
            import time
            time.sleep(1.2)
            webbrowser.open(f"http://127.0.0.1:{port}")
        threading.Thread(target=ouvrir, daemon=True).start()
        print("\n🛩️  MailPilot Pro — Interface multi-comptes")
        print(f"   Ouvre http://127.0.0.1:{port}\n")

    app.run(debug=False, host="0.0.0.0", port=port)
