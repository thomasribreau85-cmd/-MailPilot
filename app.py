# ============================================================
# app.py — Interface web multi-comptes pour MailPilot
# Lance avec : python3 app.py
# Ouvre : http://127.0.0.1:5000
# ============================================================

import sys
import os
import uuid
import json
import threading
import subprocess
from pathlib import Path

from flask import Flask, render_template, request, jsonify, redirect
from google_auth_oauthlib.flow import InstalledAppFlow, Flow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

app = Flask(__name__)

# ── Chemins ──────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
COMPTES_FILE = BASE_DIR / "comptes.json"
TOKENS_DIR   = BASE_DIR / "tokens"
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
        return json.loads(COMPTES_FILE.read_text())
    return {"api_key": "", "comptes": []}

def sauver_comptes(data):
    COMPTES_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))

def trouver_compte(data, compte_id):
    for c in data["comptes"]:
        if c["id"] == compte_id:
            return c
    return None


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

@app.route("/")
def index():
    data = charger_comptes()
    return render_template("index.html", data=data)


@app.route("/sauvegarder_api", methods=["POST"])
def sauvegarder_api():
    data = charger_comptes()
    data["api_key"] = request.json.get("api_key", "")
    sauver_comptes(data)
    return jsonify({"ok": True})


@app.route("/ajouter_compte", methods=["POST"])
def ajouter_compte():
    d = request.json
    compte = {
        "id":       str(uuid.uuid4())[:8],
        "nom":      d.get("nom", ""),
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
    oauth_flows[state] = compte_id
    oauth_statut[compte_id] = "en_cours"
    return jsonify({"ok": True, "auth_url": auth_url})


@app.route("/oauth/callback")
def oauth_callback():
    """Reçoit le code OAuth de Google et enregistre le token."""
    state    = request.args.get('state')
    code     = request.args.get('code')
    error    = request.args.get('error')

    compte_id = oauth_flows.pop(state, None)

    if error or not compte_id:
        if compte_id:
            oauth_statut[compte_id] = "erreur"
        return redirect('/')

    creds_path   = get_creds_path()
    is_local = request.host.startswith('127') or request.host.startswith('localhost')
    scheme = 'http' if is_local else 'https'
    redirect_uri = f'{scheme}://{request.host}/oauth/callback'

    try:
        flow = Flow.from_client_secrets_file(
            creds_path,
            scopes=GMAIL_SCOPES,
            redirect_uri=redirect_uri
        )
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

    except Exception as e:
        import sys
        print(f"[OAuth Callback ERREUR] compte={compte_id} err={e}", file=sys.stderr)
        oauth_statut[compte_id] = "erreur"
        with lock:
            logs_par_compte.setdefault(compte_id, []).append(f"[ERREUR OAuth] {e}")

    return redirect('/')


@app.route("/statut_oauth/<compte_id>")
def statut_oauth(compte_id):
    return jsonify({"statut": oauth_statut.get(compte_id, "")})


@app.route("/demarrer/<compte_id>", methods=["POST"])
def demarrer(compte_id):
    if compte_id in processus and processus[compte_id].poll() is None:
        return jsonify({"ok": False, "message": "Déjà en cours"})

    data = charger_comptes()
    c = trouver_compte(data, compte_id)
    if not c:
        return jsonify({"ok": False, "message": "Compte introuvable"})
    if not c.get("connecte") or not c.get("token"):
        return jsonify({"ok": False, "message": "Connecte d'abord Gmail !"})

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
    })

    logs_par_compte[compte_id] = []
    emails_comptes[compte_id]  = 0

    p = subprocess.Popen(
        [sys.executable, "mailpilot.py", "--token", c["token"]],
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
    p = processus.pop(compte_id, None)
    if p and p.poll() is None:
        p.terminate()
        with lock:
            logs_par_compte.setdefault(compte_id, []).append("── Arrêté manuellement ──")
    return jsonify({"ok": True})


@app.route("/statut/<compte_id>")
def statut_compte(compte_id):
    actif = compte_id in processus and processus[compte_id].poll() is None
    with lock:
        logs = list(logs_par_compte.get(compte_id, [])[-25:])
    return jsonify({
        "actif":          actif,
        "emails_traites": emails_comptes.get(compte_id, 0),
        "logs":           logs,
        "oauth":          oauth_statut.get(compte_id, ""),
    })


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
