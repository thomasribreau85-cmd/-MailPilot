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

from flask import Flask, render_template, request, jsonify
from google_auth_oauthlib.flow import InstalledAppFlow
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
lock            = threading.Lock()
MAX_LOGS        = 150


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
    """Lance le flux OAuth Gmail pour ce compte dans un thread."""
    if oauth_statut.get(compte_id) == "en_cours":
        return jsonify({"ok": False, "message": "Connexion déjà en cours…"})

    creds_path = BASE_DIR / "credentials.json"
    if not creds_path.exists():
        return jsonify({"ok": False, "message": "credentials.json introuvable !"})

    oauth_statut[compte_id] = "en_cours"

    def lancer_oauth():
        try:
            token_path = str(TOKENS_DIR / f"token_{compte_id}.json")
            creds = None

            # Vérifie si un token existe déjà
            if Path(token_path).exists():
                creds = Credentials.from_authorized_user_file(token_path, GMAIL_SCOPES)

            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                else:
                    flow = InstalledAppFlow.from_client_secrets_file(
                        str(creds_path), GMAIL_SCOPES
                    )
                    creds = flow.run_local_server(port=0)

                Path(token_path).write_text(creds.to_json())

            # Marque comme connecté
            data = charger_comptes()
            c = trouver_compte(data, compte_id)
            if c:
                c["connecte"] = True
                c["token"] = token_path
                sauver_comptes(data)

            oauth_statut[compte_id] = "ok"

        except Exception as e:
            oauth_statut[compte_id] = "erreur"
            with lock:
                logs_par_compte.setdefault(compte_id, []).append(
                    f"[ERREUR OAuth] {e}"
                )

    threading.Thread(target=lancer_oauth, daemon=True).start()
    return jsonify({"ok": True, "message": "Navigateur ouvert — autorise l'accès Gmail…"})


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
    import webbrowser

    def ouvrir():
        import time
        time.sleep(1.2)
        webbrowser.open("http://127.0.0.1:5001")

    threading.Thread(target=ouvrir, daemon=True).start()
    print("\n🛩️  MailPilot Pro — Interface multi-comptes")
    print("   Ouvre http://127.0.0.1:5001\n")
    app.run(debug=False, port=5001)
