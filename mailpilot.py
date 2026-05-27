#!/usr/bin/env python3
# ============================================================
# mailpilot.py — Assistant IA de gestion des emails
# pour tout type d'entreprise ou professionnel
#
# Fonctionnement :
#   1. Lit les nouveaux emails Gmail non lus
#   2. Classe chaque email avec Claude Haiku
#   3. Applique un label Gmail selon la catégorie
#   4. Rédige un brouillon de réponse avec Claude Sonnet (si pas INUTILE)
#   5. Crée le brouillon dans le bon fil Gmail
#   6. Marque l'email comme lu
#   7. Recommence toutes les 60 secondes
# ============================================================

import os
import sys
import time
import base64
import logging
import json
import argparse
import imaplib
import smtplib
import ssl
import email as email_lib
import requests as http_requests
from datetime import datetime, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header

import uuid
import anthropic
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from prompts import build_classification_prompt, DRAFTING_PROMPTS, LABEL_DESCRIPTIONS, LABELS_DEFAUT, TOUS_LES_LABELS

# --- Base de données SQLite ---
import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import database as db_module
db_module.init_db()

# --- Argument --token pour support multi-comptes ---
parser = argparse.ArgumentParser()
parser.add_argument("--token", default="token.json", help="Chemin vers le fichier token Gmail")
args, _ = parser.parse_known_args()
TOKEN_PATH = args.token

# --- Chargement des variables d'environnement ---
load_dotenv()

# --- Configuration du logging ---
# Les logs permettent de voir ce que fait le programme en temps réel
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# --- Permissions Gmail nécessaires ---
# "modify" permet de lire, étiqueter, créer des brouillons
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

# TOUS_LES_LABELS est importé depuis prompts.py

# Labels actifs pour ce compte (depuis variable d'environnement)
_labels_env = os.environ.get("LABELS_ACTIFS", "")
LABELS_ACTIFS = _labels_env.split(",") if _labels_env else LABELS_DEFAUT

# Filtre : seulement les labels actifs et connus
LABELS_ACTIFS = [l for l in LABELS_ACTIFS if l in TOUS_LES_LABELS]
if not LABELS_ACTIFS:
    LABELS_ACTIFS = LABELS_DEFAUT

# Mapping simplifié pour compatibilité
LABEL_NOMS = {k: v["nom"] for k, v in TOUS_LES_LABELS.items()}

# ── Catégories personnalisées ─────────────────────────────────
_custom_cats_raw = os.getenv("AGENT_CUSTOM_CATEGORIES", "").strip()
if _custom_cats_raw:
    try:
        for _cat in json.loads(_custom_cats_raw):
            _cat_id = _cat.get("id", "").upper()
            if not _cat_id:
                continue
            TOUS_LES_LABELS[_cat_id] = {
                "nom":     f"MailPilot - {_cat['nom']}",
                "couleur": {"backgroundColor": _cat.get("couleur", "#4f6ef7"), "textColor": "#ffffff"},
                "emoji":   _cat.get("emoji", "🏷️"),
                "description_ui": _cat.get("description", _cat["nom"]),
            }
            LABEL_DESCRIPTIONS[_cat_id] = _cat.get("description") or _cat["nom"]
            LABEL_NOMS[_cat_id]         = f"MailPilot - {_cat['nom']}"
            if _cat_id not in LABELS_ACTIFS:
                LABELS_ACTIFS.append(_cat_id)
    except Exception as _e:
        logger.warning(f"AGENT_CUSTOM_CATEGORIES invalide : {_e}")

# ── Blacklist expéditeurs ─────────────────────────────────────
_blacklist_raw = os.getenv("AGENT_BLACKLIST", "").strip()
BLACKLIST = [e.strip().lower() for e in _blacklist_raw.split("|") if e.strip()] if _blacklist_raw else []


def est_blackliste(expediteur: str) -> bool:
    """
    Retourne True si l'expéditeur correspond à une entrée blacklist.
    Règles (par ordre de priorité) :
      - Adresse exacte  : entry = 'user@domain.com'   → match exact
      - Domaine         : entry = 'domain.com'         → match tout @domain.com ou @sub.domain.com
      - Partie locale   : entry = 'noreply'            → match noreply@<n'importe quoi>
    """
    if not BLACKLIST or not expediteur:
        return False
    import re as _re
    exp = expediteur.lower()
    m   = _re.search(r'<([^>]+)>', exp)
    addr   = m.group(1).strip() if m else exp.strip()
    local  = addr.split("@")[0]  if "@" in addr else addr
    domain = addr.split("@")[-1] if "@" in addr else ""
    for entry in BLACKLIST:
        entry = entry.lstrip("@")
        if addr == entry:                             return True  # adresse exacte
        if domain and domain == entry:                return True  # domaine exact
        if domain and domain.endswith("." + entry):   return True  # sous-domaine
        if "." not in entry and local == entry:       return True  # partie locale (noreply, mailer-daemon…)
    return False

# ── Détection de sentiment (mots-clés de mécontentement) ─────
_MOTS_MECONTENT = [
    # Français
    "inacceptable", "scandaleux", "honte", "déçu", "décevant", "inadmissible",
    "remboursement", "procès", "avocat", "tribunal", "plainte", "arnaque",
    "escroquerie", "furieux", "en colère", "j'exige", "je exige", "catastrophique",
    "pas normal", "très mécontent", "très déçu", "insatisfait", "inadmissible",
    "intolérable", "lamentable", "jamais revenu", "mauvaise expérience",
    "c'est une honte", "c'est inadmissible", "je vais porter plainte",
    # Anglais
    "unacceptable", "disappointed", "refund", "lawsuit", "lawyer", "furious",
    "angry", "disgusted", "terrible service", "worst", "outrageous", "scam",
]

def detecter_sentiment(email: dict) -> str:
    """
    Détecte si l'email contient des signaux de mécontentement client.
    Retourne 'mecontent' (2+ mots), 'alerte' (1 mot), ou 'neutre'.
    Coût zéro — basé uniquement sur des mots-clés.
    """
    texte = (email.get("sujet", "") + " " + email.get("corps", "")).lower()
    score = sum(1 for mot in _MOTS_MECONTENT if mot in texte)
    if score >= 2: return "mecontent"
    if score == 1: return "alerte"
    return "neutre"


# ── Calcul des créneaux disponibles depuis l'emploi du temps ──
def _creneaux_dispo_texte(edt: dict, nb_jours: int = 7) -> str:
    """
    Calcule le texte des créneaux libres pour les N prochains jours,
    en soustrayant les blocs occupés des blocs de travail.
    """
    from datetime import date as _date, timedelta as _td
    creneaux = edt.get("creneaux", [])
    if not creneaux:
        return ""

    JOURS_FR  = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
    JOURS_ABR = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]

    def to_min(t: str) -> int:
        h, m = t.split(":")
        return int(h) * 60 + int(m)

    def to_str(m: int) -> str:
        return f"{m // 60:02d}h{m % 60:02d}" if m % 60 else f"{m // 60:02d}h"

    lignes = []
    today  = _date.today()
    for i in range(1, nb_jours + 1):
        d       = today + _td(days=i)
        jour_id = d.weekday()

        travail = [c for c in creneaux if c.get("type") == "travail" and jour_id in c.get("jours", [])]
        if not travail:
            continue
        occupes = sorted(
            [(to_min(c["debut"]), to_min(c["fin"])) for c in creneaux
             if c.get("type") != "travail" and jour_id in c.get("jours", [])],
            key=lambda x: x[0]
        )

        dispos = []
        for t in travail:
            cursor = to_min(t["debut"])
            fin_t  = to_min(t["fin"])
            for ob, of in occupes:
                if ob >= fin_t or of <= cursor:
                    continue
                if cursor < ob:
                    dispos.append(f"{to_str(cursor)}-{to_str(ob)}")
                cursor = max(cursor, of)
            if cursor < fin_t:
                dispos.append(f"{to_str(cursor)}-{to_str(fin_t)}")

        if dispos:
            lignes.append(f"  • {JOURS_FR[jour_id]} {d.day}/{d.month} : {', '.join(dispos)}")

    return ("Créneaux disponibles pour rendez-vous :\n" + "\n".join(lignes)) if lignes else ""


# --- Modèles Claude à utiliser ---
MODEL_CLASSIFICATION = "claude-haiku-4-5-20251001"  # Rapide et économique pour classer
MODEL_REDACTION      = "claude-sonnet-4-6"           # Plus puissant pour rédiger


# ============================================================
# AUTHENTIFICATION GMAIL
# ============================================================

def authentifier_gmail():
    """
    Authentifie l'application auprès de l'API Gmail via OAuth 2.0.

    - Si un token valide existe dans token.json, l'utilise directement.
    - Si le token est expiré, le renouvelle automatiquement.
    - Sinon, ouvre un navigateur pour la connexion Google.

    Retourne un objet service Gmail prêt à l'emploi.
    """
    creds = None

    # Charge les identifiants existants s'ils existent
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, GMAIL_SCOPES)

    # Si pas d'identifiants valides, lance le flux OAuth
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            # Renouvelle le token automatiquement
            creds.refresh(Request())
            logger.info("Token Gmail renouvelé automatiquement.")
        else:
            # Première connexion : ouvre le navigateur
            if not os.path.exists("credentials.json"):
                raise FileNotFoundError(
                    "Fichier credentials.json introuvable ! "
                    "Télécharge-le depuis Google Cloud Console et place-le "
                    "dans le dossier MailPilot. Consulte le README pour les étapes."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                "credentials.json", GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
            logger.info("Authentification Gmail réussie !")

        # Sauvegarde le token pour les prochaines exécutions
        os.makedirs(os.path.dirname(TOKEN_PATH) if os.path.dirname(TOKEN_PATH) else ".", exist_ok=True)
        with open(TOKEN_PATH, "w") as token_file:
            token_file.write(creds.to_json())

    # Construit et retourne le service Gmail
    service = build("gmail", "v1", credentials=creds)
    return service


# ============================================================
# GESTION DES LABELS GMAIL
# ============================================================

def obtenir_ou_creer_labels(service):
    """
    Récupère les IDs des labels MailPilot dans Gmail.
    Crée automatiquement les labels manquants.

    Retourne un dictionnaire {catégorie: label_id}.
    """
    # Récupère tous les labels existants
    resultat = service.users().labels().list(userId="me").execute()
    labels_existants = {
        label["name"]: label["id"]
        for label in resultat.get("labels", [])
    }

    label_ids = {}

    for categorie in LABELS_ACTIFS:
        info = TOUS_LES_LABELS[categorie]
        nom_label = info["nom"]
        if nom_label in labels_existants:
            label_ids[categorie] = labels_existants[nom_label]
        else:
            corps_label = {
                "name": nom_label,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
                "color": info["couleur"],
            }
            nouveau_label = (
                service.users()
                .labels()
                .create(userId="me", body=corps_label)
                .execute()
            )
            label_ids[categorie] = nouveau_label["id"]
            logger.info(f"Label créé : {nom_label}")

    return label_ids


# ============================================================
# LECTURE DES EMAILS
# ============================================================

def lire_emails_non_lus(service):
    """
    Récupère la liste des emails non lus dans la boîte de réception.
    Retourne une liste de dictionnaires avec les informations essentielles.
    """
    try:
        # Cherche les emails non lus dans INBOX
        resultat = service.users().messages().list(
            userId="me",
            q="is:unread in:inbox",
            maxResults=50,  # Traite jusqu'à 50 emails par cycle
        ).execute()

        messages = resultat.get("messages", [])
        emails = []

        for msg in messages:
            email_data = lire_email_complet(service, msg["id"])
            if email_data:
                emails.append(email_data)

        return emails

    except HttpError as e:
        logger.error(f"Erreur lors de la lecture des emails : {e}")
        return []


def lire_email_complet(service, message_id):
    """
    Lit le contenu complet d'un email par son ID.
    Extrait : sujet, expéditeur, corps du texte, thread_id.
    """
    try:
        message = service.users().messages().get(
            userId="me",
            id=message_id,
            format="full",
        ).execute()

        headers = {h["name"]: h["value"] for h in message["payload"]["headers"]}
        sujet = headers.get("Subject", "(Sans sujet)")
        expediteur = headers.get("From", "(Expéditeur inconnu)")

        # Extraction du corps du texte
        corps = extraire_corps_email(message["payload"])

        return {
            "id":            message_id,
            "thread_id":     message["threadId"],
            "internal_date": int(message.get("internalDate", "0")),
            "sujet":         sujet,
            "expediteur":    expediteur,
            "corps":         corps[:4000],  # Limite pour économiser les tokens
        }

    except HttpError as e:
        logger.error(f"Erreur lecture email {message_id} : {e}")
        return None


def extraire_corps_email(payload):
    """
    Extrait récursivement le texte d'un email (gère les emails multipart).
    Préfère le texte brut, sinon retire les balises HTML basiquement.
    """
    corps = ""

    if "parts" in payload:
        for part in payload["parts"]:
            corps += extraire_corps_email(part)
    else:
        mime_type = payload.get("mimeType", "")
        data = payload.get("body", {}).get("data", "")

        if data:
            texte = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
            if mime_type == "text/plain":
                corps += texte
            elif mime_type == "text/html" and not corps:
                # Retire grossièrement les balises HTML si pas de texte brut
                import re
                corps += re.sub(r"<[^>]+>", " ", texte)

    return corps.strip()


# ============================================================
# CLASSIFICATION AVEC CLAUDE HAIKU
# ============================================================

def classifier_email(client_anthropic, email):
    """
    Envoie l'email à Claude Haiku pour classification.
    Retourne l'une des 6 catégories : URGENT, VISITE, OFFRE, INFO, ADMIN, INUTILE.
    """
    # Construction du contenu à classifier
    contenu_email = f"""SUJET : {email['sujet']}
EXPÉDITEUR : {email['expediteur']}

CORPS :
{email['corps']}"""

    try:
        prompt_classif = build_classification_prompt(LABELS_ACTIFS)
        reponse = client_anthropic.messages.create(
            model=MODEL_CLASSIFICATION,
            max_tokens=10,  # On attend juste un mot-clé
            messages=[
                {
                    "role": "user",
                    "content": f"{prompt_classif}\n\n{contenu_email}",
                }
            ],
        )

        # Extrait et nettoie la réponse
        categorie = reponse.content[0].text.strip().upper()

        # Vérifie que la catégorie est valide parmi les labels actifs
        if categorie not in LABELS_ACTIFS:
            fallback = "INFO" if "INFO" in LABELS_ACTIFS else LABELS_ACTIFS[-1]
            logger.warning(f"Catégorie inconnue '{categorie}', fallback sur {fallback}")
            categorie = fallback

        return categorie

    except Exception as e:
        logger.error(f"Erreur classification Claude : {e}")
        return "INFO"  # Catégorie par défaut en cas d'erreur


# ============================================================
# RÉDACTION DU BROUILLON AVEC CLAUDE SONNET
# ============================================================

def rediger_reponse(client_anthropic, email, categorie):
    """
    Utilise Claude Sonnet pour rédiger un brouillon de réponse adapté.
    Retourne le texte du brouillon, ou None si erreur.
    """
    # Récupère le prompt pour cette catégorie
    prompt_base = DRAFTING_PROMPTS.get(categorie)
    if not prompt_base:
        # Catégorie personnalisée — prompt générique
        cat_info = TOUS_LES_LABELS.get(categorie, {})
        cat_nom  = cat_info.get("nom", categorie).replace("MailPilot - ", "")
        logger.info(f"  → Prompt générique pour catégorie custom : {categorie}")
        prompt_base = f"""Tu es {{nom}}, travaillant chez {{agence}} ({{zone}}).
Ton email : {{email}} | Ton téléphone : {{tel}}

Tu reçois un email classifié « {cat_nom} ». Rédige une réponse professionnelle et adaptée au contexte.

RÈGLES :
- Réponds directement à la demande ou au sujet de l'email
- Sois professionnel, clair et concis (maximum 6 lignes)
- Guide naturellement vers la prochaine étape appropriée

Bien cordialement,
{{nom}} — {{agence}} — {{tel}}

Email reçu :
"""

    # Injecte les variables de l'agent
    prompt = prompt_base.format(
        nom=os.getenv("AGENT_NOM", "L'agent"),
        agence=os.getenv("AGENT_AGENCE", "L'agence"),
        tel=os.getenv("AGENT_TEL", ""),
        email=os.getenv("AGENT_EMAIL", ""),
        zone=os.getenv("AGENT_ZONE", ""),
    )

    # Ajoute les instructions personnalisées du client si définies
    instructions = os.getenv("AGENT_INSTRUCTIONS", "").strip()
    if instructions:
        prompt += f"\n\n--- CONSIGNES PERSONNALISÉES (à respecter impérativement) ---\n{instructions}\n---"

    # Ton de rédaction
    _TON_INSTRUCTIONS = {
        "formel":   "Adopte un ton professionnel et formel : phrases complètes, vouvoiement, formules de politesse soignées (\"Veuillez agréer…\", \"Je vous prie de…\").",
        "neutre":   "Adopte un ton professionnel et neutre : clair, direct, poli, sans être ni trop formel ni trop familier.",
        "detendu":  "Adopte un ton amical et détendu : phrases simples, formules chaleureuses, naturel et accessible tout en restant professionnel.",
    }
    ton = os.getenv("AGENT_TON", "neutre")
    ton_instr = _TON_INSTRUCTIONS.get(ton, _TON_INSTRUCTIONS["neutre"])
    prompt += f"\n\n--- TON ---\n{ton_instr}\n---"

    # Instructions personnalisées globales
    instructions_globales = os.getenv("AGENT_INSTRUCTIONS_GLOBALES", "").strip()
    if instructions_globales:
        prompt += f"\n\n--- CONSIGNES SPÉCIFIQUES ---\nApplique impérativement ces consignes à chaque réponse :\n{instructions_globales}\n---"

    # Emploi du temps — créneaux disponibles (RENDEZ_VOUS uniquement)
    if categorie == "RENDEZ_VOUS":
        _edt_raw = os.getenv("AGENT_EDT", "").strip()
        if _edt_raw:
            try:
                _edt_texte = _creneaux_dispo_texte(json.loads(_edt_raw))
                if _edt_texte:
                    prompt += f"\n\n--- DISPONIBILITÉS (propose des créneaux parmi ceux-ci) ---\n{_edt_texte}\n---"
            except Exception as _e:
                logger.warning(f"  ✗ EDT parse error : {_e}")

    # Instruction de langue
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
    langue = os.getenv("AGENT_LANGUE", "auto")
    langue_instr = _LANGUE_INSTRUCTIONS.get(langue, _LANGUE_INSTRUCTIONS["auto"])
    prompt += f"\n\n--- LANGUE ---\n{langue_instr}\n---"

    # Modèle de réponse pour cette catégorie (suggestion, pas copie exacte)
    templates_raw = os.getenv("AGENT_TEMPLATES", "")
    if templates_raw:
        try:
            templates = json.loads(templates_raw)
            match = next((t for t in templates if t.get("categorie") == categorie and t.get("texte", "").strip()), None)
            if match:
                prompt += f"\n\n--- MODÈLE DE BASE SUGGÉRÉ (adapte-le au contexte de l'email reçu, ne le copie pas mot pour mot) ---\n{match['texte']}\n---"
        except Exception:
            pass

    # Mode absence — génère une réponse d'absence automatique
    if os.getenv("AGENT_ABSENCE_ACTIF", "0") == "1":
        date_retour = os.getenv("AGENT_ABSENCE_DATE", "").strip()
        msg_perso   = os.getenv("AGENT_ABSENCE_MSG", "").strip()
        prompt += "\n\n--- MODE ABSENCE ACTIVÉ ---"
        retour_str = (" jusqu'au " + date_retour) if date_retour else ""
        prompt += f"\nL'agent est actuellement absent{retour_str}."
        prompt += "\nRédige une réponse d'absence professionnelle et chaleureuse en informant l'expéditeur de l'absence."
        if msg_perso:
            prompt += f"\nMessage personnalisé à intégrer : {msg_perso}"
        prompt += "\n---"

    # Ajoute la signature si configurée
    signature = os.getenv("AGENT_SIGNATURE", "").strip()
    if signature:
        prompt += f"\n\n--- SIGNATURE À AJOUTER EN FIN D'EMAIL (obligatoire, mot pour mot) ---\n{signature}\n---"

    # Ajoute le contenu de l'email reçu
    contenu_email = f"""SUJET : {email['sujet']}
EXPÉDITEUR : {email['expediteur']}

CORPS :
{email['corps']}"""

    try:
        reponse = client_anthropic.messages.create(
            model=MODEL_REDACTION,
            max_tokens=600,
            messages=[
                {
                    "role": "user",
                    "content": f"{prompt}\n\n{contenu_email}",
                }
            ],
        )

        return reponse.content[0].text.strip()

    except Exception as e:
        logger.error(f"Erreur rédaction Claude : {e}")
        return None


# ============================================================
# ACTIONS SUR GMAIL
# ============================================================

def appliquer_label(service, message_id, label_id):
    """Applique un label Gmail à un email."""
    try:
        service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"addLabelIds": [label_id]},
        ).execute()
    except HttpError as e:
        logger.error(f"Erreur application label : {e}")


def marquer_comme_lu(service, message_id):
    """Supprime le label UNREAD pour marquer l'email comme lu."""
    try:
        service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"removeLabelIds": ["UNREAD"]},
        ).execute()
    except HttpError as e:
        logger.error(f"Erreur marquage comme lu : {e}")


def creer_brouillon(service, email, texte_reponse, categorie):
    """
    Crée un brouillon de réponse dans le bon fil de discussion Gmail.

    Le brouillon est créé avec :
    - Destinataire = expéditeur de l'email original
    - Sujet = "Re: <sujet original>"
    - Corps = texte rédigé par Claude
    - threadId = même fil que l'email original
    """
    try:
        # Construction du sujet de réponse
        sujet_original = email["sujet"]
        if not sujet_original.lower().startswith("re:"):
            sujet_reponse = f"Re: {sujet_original}"
        else:
            sujet_reponse = sujet_original

        # Création du message MIME
        message_mime = MIMEMultipart()
        message_mime["To"] = email["expediteur"]
        message_mime["From"] = os.getenv("AGENT_EMAIL", "")
        message_mime["Subject"] = sujet_reponse

        # Corps du message en texte brut
        message_mime.attach(MIMEText(texte_reponse, "plain", "utf-8"))

        # Encodage en base64 pour l'API Gmail
        raw_message = base64.urlsafe_b64encode(
            message_mime.as_bytes()
        ).decode("utf-8")

        # Création du brouillon dans le bon fil
        corps_brouillon = {
            "message": {
                "raw": raw_message,
                "threadId": email["thread_id"],
            }
        }

        brouillon = (
            service.users()
            .drafts()
            .create(userId="me", body=corps_brouillon)
            .execute()
        )

        logger.info(
            f"  → Brouillon créé (ID: {brouillon['id']}) "
            f"| Catégorie: {categorie} | Sujet: {sujet_original[:50]}"
        )
        return brouillon

    except HttpError as e:
        logger.error(f"Erreur création brouillon : {e}")
        return None


# ============================================================
# BOUCLE PRINCIPALE
# ============================================================

def envoyer_notification(service, email_destinataire, stats):
    """
    Envoie un email de résumé au client via l'API Gmail.
    stats = {"traites": N, "brouillons": N, "categories": {"URGENT": 2, ...}}
    """
    try:
        nb = stats["traites"]
        nb_brouillons = stats["brouillons"]
        categories = stats.get("categories", {})

        # Corps du mail
        lignes_categories = ""
        for cat, count in sorted(categories.items(), key=lambda x: -x[1]):
            info = TOUS_LES_LABELS.get(cat, {})
            emoji = info.get("emoji", "•")
            nom = info.get("nom", cat)
            lignes_categories += f"  {emoji} {nom} : {count} email(s)\n"

        corps = f"""Bonjour,

MailPilot vient de traiter votre boîte mail.

📊 Résumé du cycle :
  • {nb} email(s) traité(s)
  • {nb_brouillons} brouillon(s) prêt(s) à valider

📂 Répartition par catégorie :
{lignes_categories}
👉 Consultez vos brouillons Gmail pour valider les réponses.

---
MailPilot — Assistant email IA
"""

        sujet = f"MailPilot — {nb} email(s) traité(s), {nb_brouillons} brouillon(s) prêt(s)"

        message_mime = MIMEMultipart()
        message_mime["To"] = email_destinataire
        message_mime["From"] = email_destinataire
        message_mime["Subject"] = sujet
        message_mime.attach(MIMEText(corps, "plain", "utf-8"))

        raw = base64.urlsafe_b64encode(message_mime.as_bytes()).decode("utf-8")
        service.users().messages().send(
            userId="me",
            body={"raw": raw}
        ).execute()

        logger.info(f"✉️  Notification envoyée à {email_destinataire}")

    except Exception as e:
        logger.error(f"Erreur envoi notification : {e}")


# ============================================================
# STATS HEBDOMADAIRES
# ============================================================

def _get_compte_boite_ids():
    """Retourne (compte_id, boite_id) depuis les variables d'environnement."""
    pk = os.getenv("COMPTE_ID", "default")
    parts = pk.split("_", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (pk, "")

def sauver_email_recent(email, categorie, brouillon):
    """Persiste l'email traité + son brouillon IA dans SQLite."""
    compte_id, boite_id = _get_compte_boite_ids()
    entree = {
        "id":         email.get("id", str(uuid.uuid4())[:8]),
        "sujet":      email.get("sujet", ""),
        "expediteur": email.get("expediteur", ""),
        "corps":      (email.get("corps") or "")[:2000],
        "categorie":  categorie,
        "brouillon":  brouillon or "",
        "traite_at":  datetime.now().isoformat(),
    }
    try:
        db_module.sauver_email_recent_db(compte_id, boite_id, entree)
    except Exception as ex:
        logger.warning(f"Impossible de sauver emails_recents: {ex}")

def charger_stats_semaine():
    """Charge les stats de la semaine courante depuis SQLite."""
    compte_id, boite_id = _get_compte_boite_ids()
    semaine = date.today().isocalendar()[1]
    annee   = date.today().year
    return db_module.charger_stats_semaine_db(compte_id, boite_id, semaine, annee)

def sauver_stats_semaine(stats):
    """Sauvegarde les stats de la semaine courante dans SQLite."""
    compte_id, boite_id = _get_compte_boite_ids()
    try:
        db_module.sauver_stats_semaine_db(compte_id, boite_id, stats)
    except Exception as e:
        logger.error(f"Erreur sauvegarde stats semaine : {e}")

def archiver_semaine_dans_historique(stats):
    """Ajoute les stats de la semaine terminée à l'historique dans SQLite."""
    compte_id, boite_id = _get_compte_boite_ids()
    try:
        entry = {
            "semaine":    stats["semaine"],
            "annee":      stats["annee"],
            "label":      f"Sem. {stats['semaine']}",
            "traites":    stats["traites"],
            "brouillons": stats["brouillons"],
            "categories": stats.get("categories", {}),
        }
        db_module.archiver_stats_hist_db(compte_id, boite_id, entry)
    except Exception as e:
        logger.error(f"Erreur archivage historique stats : {e}")

def accumuler_stats_semaine(stats_cycle):
    """Ajoute les stats du cycle aux stats hebdomadaires."""
    data = charger_stats_semaine()
    data["traites"]    += stats_cycle.get("traites", 0)
    data["brouillons"] += stats_cycle.get("brouillons", 0)
    for cat, nb in stats_cycle.get("categories", {}).items():
        data["categories"][cat] = data["categories"].get(cat, 0) + nb
    sauver_stats_semaine(data)
    return data

def envoyer_bilan_hebdo(service, email_destinataire):
    """Envoie le bilan hebdomadaire au client via Gmail API."""
    try:
        stats = charger_stats_semaine()
        if stats["traites"] == 0:
            logger.info("📅 Bilan hebdo : aucun email traité cette semaine, pas d'envoi.")
            return

        nb = stats["traites"]
        nb_brouillons = stats["brouillons"]
        semaine = stats["semaine"]
        annee = stats["annee"]
        categories = stats.get("categories", {})

        lignes_categories = ""
        for cat, count in sorted(categories.items(), key=lambda x: -x[1]):
            info = TOUS_LES_LABELS.get(cat, {})
            emoji = info.get("emoji", "•")
            nom = info.get("nom", cat)
            lignes_categories += f"  {emoji} {nom} : {count} email(s)\n"

        corps = f"""Bonjour,

Voici votre bilan MailPilot pour la semaine {semaine} ({annee}).

📊 Résumé de la semaine :
  • {nb} email(s) traité(s) au total
  • {nb_brouillons} brouillon(s) généré(s)

📂 Répartition par catégorie :
{lignes_categories}
💡 Astuce : validez vos brouillons Gmail régulièrement pour que vos clients reçoivent leurs réponses rapidement.

---
MailPilot — Assistant email IA
"""
        sujet = f"MailPilot — Bilan semaine {semaine} : {nb} email(s) traité(s)"

        message_mime = MIMEMultipart()
        message_mime["To"] = email_destinataire
        message_mime["From"] = email_destinataire
        message_mime["Subject"] = sujet
        message_mime.attach(MIMEText(corps, "plain", "utf-8"))
        raw = base64.urlsafe_b64encode(message_mime.as_bytes()).decode("utf-8")
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        logger.info(f"📅 Bilan hebdomadaire envoyé à {email_destinataire} (semaine {semaine})")

        # Archive et remet à zéro
        archiver_semaine_dans_historique(stats)
        semaine_nouvelle = date.today().isocalendar()[1]
        sauver_stats_semaine({"semaine": semaine_nouvelle, "annee": annee, "traites": 0, "brouillons": 0, "categories": {}})

    except Exception as e:
        logger.error(f"Erreur envoi bilan hebdo : {e}")


def nettoyer_emails_anciens(service=None, imap_conn=None, mail_provider="gmail"):
    """
    Supprime (corbeille) les emails plus vieux que NETTOYAGE_JOURS
    pour les catégories NETTOYAGE_CATS. Tourne une fois par jour.
    """
    if os.getenv("NETTOYAGE_ACTIF", "0") != "1":
        return

    # Vérifier si déjà exécuté aujourd'hui
    stats_dir   = os.getenv("STATS_DIR", os.path.dirname(os.path.abspath(__file__)))
    compte_id   = os.getenv("COMPTE_ID", "default")
    flag_file   = os.path.join(stats_dir, f"nettoyage_last_{compte_id}.txt")
    today       = datetime.now().strftime("%Y-%m-%d")
    if os.path.exists(flag_file):
        try:
            if open(flag_file).read().strip() == today:
                return  # Déjà fait aujourd'hui
        except Exception:
            pass

    jours     = int(os.getenv("NETTOYAGE_JOURS", "365"))
    cats_env  = os.getenv("NETTOYAGE_CATS", "INUTILE")
    cats      = list(TOUS_LES_LABELS.keys()) if cats_env == "ALL" else cats_env.split(",")

    logger.info(f"🗑️  Nettoyage auto : emails > {jours} jours, catégories : {cats}")
    supprimés = 0

    try:
        if mail_provider == "gmail" and service:
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
                        logger.error(f"  ✗ Erreur suppression {m['id']} : {e}")

        elif mail_provider == "microsoft":
            cutoff = (datetime.now() - __import__("datetime").timedelta(days=jours)).strftime("%Y-%m-%dT00:00:00Z")
            for cat in cats:
                url = f"{MS_GRAPH}/me/mailFolders/MailPilot/childFolders?$filter=displayName eq '{cat}'"
                r = http_requests.get(url, headers=ms_headers())
                folders = r.json().get("value", [])
                for folder in folders:
                    msgs_url = f"{MS_GRAPH}/me/mailFolders/{folder['id']}/messages?$filter=receivedDateTime lt {cutoff}&$select=id&$top=100"
                    msgs = http_requests.get(msgs_url, headers=ms_headers()).json().get("value", [])
                    for m in msgs:
                        try:
                            http_requests.delete(f"{MS_GRAPH}/me/messages/{m['id']}", headers=ms_headers())
                            supprimés += 1
                        except Exception as e:
                            logger.error(f"  ✗ Erreur suppression Microsoft : {e}")

        elif mail_provider == "imap" and imap_conn:
            from datetime import timedelta
            cutoff_date = (datetime.now() - timedelta(days=jours)).strftime("%d-%b-%Y")
            for cat in cats:
                try:
                    imap_conn.select(f"MailPilot/{cat}")
                    _, data = imap_conn.uid("search", None, f'BEFORE {cutoff_date}')
                    uids = data[0].split()
                    for uid in uids:
                        try:
                            imap_conn.uid("store", uid, "+FLAGS", "\\Deleted")
                            supprimés += 1
                        except Exception:
                            pass
                    imap_conn.expunge()
                except Exception as e:
                    logger.error(f"  ✗ Erreur nettoyage IMAP {cat} : {e}")

        logger.info(f"  ✓ Nettoyage terminé : {supprimés} email(s) supprimé(s)")
        # Marquer comme fait aujourd'hui
        with open(flag_file, "w") as f:
            f.write(today)

    except Exception as e:
        logger.error(f"  ✗ Erreur nettoyage : {e}")


def transferer_email(email, categorie, service=None, imap_conn=None, mail_provider="gmail"):
    """
    Transfère l'email vers les adresses configurées pour cette catégorie.
    Les règles sont encodées dans TRANSFERTS_RULES sous la forme CAT:email|CAT2:email2
    """
    regles_raw = os.getenv("TRANSFERTS_RULES", "").strip()
    if not regles_raw:
        return

    # Parser les règles
    regles = []
    for r in regles_raw.split("|"):
        if ":" in r:
            cat, dest = r.split(":", 1)
            regles.append({"categorie": cat.strip(), "to": dest.strip()})

    # Filtrer les règles : catégorie + filtre optionnel (expéditeur ou sujet)
    expediteur = email.get("expediteur", "").lower()
    sujet      = email.get("sujet", "").lower()
    corps      = email.get("corps", "").lower()

    destinataires = []
    for r in regles:
        if r["categorie"] != categorie:
            continue
        filtre = r.get("filtre", "").strip().lower()
        if filtre:
            if filtre not in expediteur:
                continue  # filtre ne correspond pas → on skip
        destinataires.append(r["to"])

    if not destinataires:
        return

    sujet_fwd = f"Fwd: {email.get('sujet', '')}"
    corps_fwd = f"""---------- Message transféré ----------
De : {email.get('expediteur', '')}
Sujet : {email.get('sujet', '')}

{email.get('corps', '')[:3000]}

---------- Transféré par MailPilot ----------"""

    expediteur_agent = os.getenv("AGENT_EMAIL", "")

    for dest in destinataires:
        try:
            if mail_provider == "gmail" and service:
                msg = MIMEMultipart()
                msg["To"]      = dest
                msg["From"]    = expediteur_agent
                msg["Subject"] = sujet_fwd
                msg.attach(MIMEText(corps_fwd, "plain", "utf-8"))
                raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
                service.users().messages().send(userId="me", body={"raw": raw}).execute()
                logger.info(f"  📤 Transféré ({categorie}) → {dest}")

            elif mail_provider == "microsoft":
                body = {
                    "message": {
                        "subject": sujet_fwd,
                        "body": {"contentType": "Text", "content": corps_fwd},
                        "toRecipients": [{"emailAddress": {"address": dest}}],
                    }
                }
                http_requests.post(f"{MS_GRAPH}/me/sendMail", headers=ms_headers(), json=body)
                logger.info(f"  📤 Transféré ({categorie}) → {dest}")

            elif mail_provider == "imap":
                smtp_server = os.getenv("SMTP_SERVER")
                smtp_port   = int(os.getenv("SMTP_PORT", "465"))
                password    = os.getenv("IMAP_PASSWORD")
                msg = MIMEMultipart()
                msg["To"]      = dest
                msg["From"]    = expediteur_agent
                msg["Subject"] = sujet_fwd
                msg.attach(MIMEText(corps_fwd, "plain", "utf-8"))
                if smtp_port == 587:
                    with smtplib.SMTP(smtp_server, smtp_port) as s:
                        s.starttls(); s.login(expediteur_agent, password)
                        s.sendmail(expediteur_agent, dest, msg.as_bytes())
                else:
                    with smtplib.SMTP_SSL(smtp_server, smtp_port) as s:
                        s.login(expediteur_agent, password)
                        s.sendmail(expediteur_agent, dest, msg.as_bytes())
                logger.info(f"  📤 Transféré ({categorie}) → {dest}")

        except Exception as e:
            logger.error(f"  ✗ Erreur transfert vers {dest} : {e}")


def detecter_rdv(client_anthropic, email, categorie):
    """
    Si l'email contient une demande de RDV (VISITE, REUNION, etc.),
    extrait les infos et les ajoute dans l'agenda en statut 'attente'.
    """
    CATEGORIES_RDV = {"VISITE", "RENDEZ_VOUS", "DEVIS", "URGENT", "PROSPECT"}
    if categorie not in CATEGORIES_RDV:
        logger.info(f"  ⏭ Pas de détection RDV pour catégorie '{categorie}' (hors périmètre)")
        return
    logger.info(f"  📅 Analyse RDV en cours pour email : {email.get('sujet','')[:50]}")

    compte_id   = os.getenv("COMPTE_ID", "default")
    compte_part = compte_id.split("_")[0] if "_" in compte_id else compte_id

    # Charger les horaires d'ouverture depuis SQLite
    horaires_str = ""
    try:
        from database import get_setting
        horaires = get_setting(compte_part, "horaires")
        if horaires:
            jours_map = {"lundi":"Lundi","mardi":"Mardi","mercredi":"Mercredi",
                         "jeudi":"Jeudi","vendredi":"Vendredi","samedi":"Samedi","dimanche":"Dimanche"}
            lignes = []
            for jour, info in horaires.items():
                if info.get("ouvert"):
                    lignes.append(f"{jours_map.get(jour, jour)} : {info.get('debut','09:00')}–{info.get('fin','18:00')}")
                else:
                    lignes.append(f"{jours_map.get(jour, jour)} : fermé")
            horaires_str = "\n".join(lignes)
    except Exception:
        pass

    horaires_bloc = f"""
HORAIRES D'OUVERTURE DE L'ENTREPRISE (à respecter impérativement) :
{horaires_str if horaires_str else "Lundi–Vendredi : 09:00–18:00 / Samedi–Dimanche : fermé"}
→ Si l'email ne précise pas d'heure, propose un créneau dans ces horaires.
→ Ne jamais proposer d'heure en dehors de ces plages ou un jour fermé.
""" if True else ""

    today_for_prompt = datetime.now().strftime("%Y-%m-%d")
    today_weekday_fr = ["lundi","mardi","mercredi","jeudi","vendredi","samedi","dimanche"][datetime.now().weekday()]

    prompt = f"""Analyse cet email et réponds UNIQUEMENT en JSON valide (sans markdown).
Si l'email contient une demande de rendez-vous, extrais ces informations.
Sinon, réponds : {{"rdv": false}}

IMPORTANT — Date du jour : {today_for_prompt} ({today_weekday_fr}).
Toutes les dates mentionnées dans l'email sont FUTURES par rapport à aujourd'hui.
Si l'email dit "jeudi 5 juin" ou "vendredi prochain", utilise l'année {datetime.now().year} ou {datetime.now().year + 1} selon la date la plus proche dans le futur.
Ne jamais renvoyer une date passée. Si la date est déjà passée cette année, utilise l'année suivante.

Format attendu si RDV détecté :
{{
  "rdv": true,
  "titre": "Visite - Prénom Nom",
  "client_nom": "Prénom Nom",
  "client_email": "email@exemple.com ou vide",
  "adresse": "adresse du bien ou lieu de RDV ou vide",
  "date": "YYYY-MM-DD ou vide si non précisée",
  "heure_debut": "HH:MM ou 09:00 par défaut",
  "heure_fin": "HH:MM (heure_debut + 1h par défaut)",
  "type": "visite ou reunion ou appel ou autre",
  "notes": "infos supplémentaires utiles"
}}

Email à analyser :
SUJET : {email['sujet']}
EXPÉDITEUR : {email['expediteur']}
CORPS : {email['corps'][:1000]}
{horaires_bloc}"""

    today = datetime.now().strftime("%Y-%m-%d")
    # Extraire l'email expéditeur brut (entre < >) si dispo
    exp_raw = email.get("expediteur", "")
    import re as _re
    m = _re.search(r"<([^>]+)>", exp_raw)
    exp_email = m.group(1) if m else exp_raw.strip()

    data = {}
    try:
        rep = client_anthropic.messages.create(
            model=MODEL_CLASSIFICATION,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = rep.content[0].text.strip()
        # Nettoyer si markdown
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw.strip())
        logger.info(f"  📋 Réponse IA détection RDV : rdv={data.get('rdv')} titre={data.get('titre','')}")
    except Exception as e:
        logger.error(f"  ✗ Erreur appel IA détection RDV : {e}")
        # Si l'IA échoue mais que l'email est déjà classifié comme RDV → créer quand même
        data = {"rdv": True}

    # Si l'IA dit non-RDV ET la catégorie n'est pas une catégorie forte → ignorer
    # Mais si catégorie est RENDEZ_VOUS ou VISITE → on force la création même sans confirmation IA
    force_creation = categorie in {"RENDEZ_VOUS", "VISITE"}
    if not data.get("rdv") and not force_creation:
        logger.info(f"  ⏭ IA n'a pas détecté de RDV (rdv:false) et catégorie non prioritaire")
        return

    try:
        rdv = {
            "id":           str(uuid.uuid4())[:8],
            "titre":        data.get("titre") or f"RDV — {exp_raw[:40]}",
            "client_nom":   data.get("client_nom", ""),
            "client_email": data.get("client_email") or exp_email,
            "adresse":      data.get("adresse", ""),
            "date":         data.get("date") or today,
            "heure_debut":  data.get("heure_debut", "09:00"),
            "heure_fin":    data.get("heure_fin", "10:00"),
            "type":         data.get("type", "visite"),
            "statut":       "attente",
            "notes":        data.get("notes") or f"Détecté depuis email : {email.get('sujet','(sans sujet)')}",
            "boite_id":     "",
            "created_at":   datetime.now().isoformat(),
        }
        # Vérifier doublon avant création
        if db_module.rdv_doublon_existe(compte_part, rdv["client_email"], rdv["date"], rdv["heure_debut"]):
            logger.info(f"  ⏭ RDV doublon ignoré : {rdv['titre']} ({rdv['date']} {rdv['heure_debut']}) — déjà dans l'agenda")
            return
        db_module.creer_rdv_db(compte_part, rdv)
        logger.info(f"  📅 RDV créé dans l'agenda : {rdv['titre']} ({rdv['date']} {rdv['heure_debut']})")
    except Exception as e:
        logger.error(f"  ✗ Erreur création RDV en DB : {e}")


def traiter_email(service, client_anthropic, email, label_ids):
    """
    Traite un email complet : classification, label, brouillon, marquage.
    Retourne un dict {"categorie": ..., "brouillon_cree": bool}
    """
    email_resume = f"'{email['sujet'][:40]}' de {email['expediteur'][:30]}"
    logger.info(f"\n📧 Traitement : {email_resume}")
    brouillon_cree = False

    # --- Étape 0 : Vérification blacklist (avant tout appel API) ---
    if est_blackliste(email.get("expediteur", "")):
        logger.info(f"  🚫 Blacklisté — classé INUTILE sans appel API")
        try:
            label_id = label_ids.get("INUTILE")
            if label_id:
                appliquer_label(service, email["id"], label_id)
            marquer_comme_lu(service, email["id"])
        except Exception as e:
            logger.error(f"  ✗ Erreur blacklist label/lu : {e}")
        return {"categorie": "INUTILE", "brouillon_cree": False, "brouillon_texte": ""}

    # --- Étape 1 : Classification ---
    try:
        categorie = classifier_email(client_anthropic, email)
        logger.info(f"  → Catégorie : {categorie}")
    except Exception as e:
        logger.error(f"  ✗ Erreur classification : {e}")
        categorie = "INFO"

    # --- Étape 1a : Détection de sentiment (mots-clés, sans appel API) ---
    _sentiment = detecter_sentiment(email)
    if _sentiment == "mecontent":
        logger.warning(f"  😤 CLIENT MÉCONTENT détecté — traiter en priorité !")
    elif _sentiment == "alerte":
        logger.info(f"  ⚠️  Signal mécontentement détecté dans l'email")

    # --- Étape 1b : Détection RDV → crée une entrée "attente" dans l'agenda ---
    logger.info(f"  🔍 Vérification RDV pour catégorie : {categorie}")
    try:
        detecter_rdv(client_anthropic, email, categorie)
    except Exception as e:
        logger.error(f"  ✗ Erreur détection RDV : {e}")

    # --- Étape 1c : Transfert automatique ---
    try:
        transferer_email(email, categorie, service=service, mail_provider="gmail")
    except Exception as e:
        logger.error(f"  ✗ Erreur transfert : {e}")

    # --- Étape 2 : Application du label Gmail ---
    try:
        label_id = label_ids.get(categorie)
        if label_id:
            appliquer_label(service, email["id"], label_id)
            logger.info(f"  → Label appliqué : {LABEL_NOMS[categorie]}")
    except Exception as e:
        logger.error(f"  ✗ Erreur label : {e}")

    # --- Étape 3 : Rédaction du brouillon (sauf INUTILE) ---
    texte_reponse = ""
    if categorie != "INUTILE":
        try:
            texte_reponse = rediger_reponse(client_anthropic, email, categorie) or ""
            if texte_reponse:
                creer_brouillon(service, email, texte_reponse, categorie)
                brouillon_cree = True
                # Enregistrer pour relance intelligente (Gmail uniquement)
                if os.getenv("RELANCE_INTELLIGENTE_ACTIF", "0") == "1" and email.get("thread_id"):
                    try:
                        compte_id, boite_id = _get_compte_boite_ids()
                        db_module.ajouter_relance(
                            compte_id, boite_id,
                            email["id"], email["thread_id"],
                            email["expediteur"], email["sujet"],
                        )
                        logger.info("  → Relance intelligente programmée")
                    except Exception as e:
                        logger.warning(f"  ✗ Relance non enregistrée : {e}")
            else:
                logger.warning("  ✗ Brouillon non créé (réponse vide)")
        except Exception as e:
            logger.error(f"  ✗ Erreur rédaction/brouillon : {e}")
    else:
        logger.info("  → Email INUTILE : pas de brouillon créé")

    # --- Étape 4 : Marquage comme lu ---
    try:
        marquer_comme_lu(service, email["id"])
        logger.info("  → Email marqué comme lu ✓")
    except Exception as e:
        logger.error(f"  ✗ Erreur marquage lu : {e}")

    return {"categorie": categorie, "brouillon_cree": brouillon_cree, "brouillon_texte": texte_reponse}


# ============================================================
# ADAPTATEUR MICROSOFT GRAPH API (Outlook / Microsoft 365)
# ============================================================

MS_GRAPH = "https://graph.microsoft.com/v1.0"

def ms_get_token():
    """Retourne un access token Microsoft valide (rafraîchi si nécessaire)."""
    token_path = os.getenv("MICROSOFT_TOKEN_PATH", "")
    if not token_path or not os.path.exists(token_path):
        raise Exception("Token Microsoft introuvable")
    with open(token_path) as f:
        data = json.load(f)
    # Rafraîchit si expiré (avec 5 min de marge)
    if data.get("expires_at", 0) < time.time() + 300:
        resp = http_requests.post(
            "https://login.microsoftonline.com/common/oauth2/v2.0/token",
            data={
                "client_id":     os.getenv("MICROSOFT_CLIENT_ID", ""),
                "client_secret": os.getenv("MICROSOFT_CLIENT_SECRET", ""),
                "refresh_token": data["refresh_token"],
                "grant_type":    "refresh_token",
            }
        )
        new_token = resp.json()
        if "access_token" not in new_token:
            raise Exception(f"Refresh Microsoft échoué : {new_token}")
        data["access_token"] = new_token["access_token"]
        data["expires_at"]   = time.time() + new_token.get("expires_in", 3600)
        if "refresh_token" in new_token:
            data["refresh_token"] = new_token["refresh_token"]
        with open(token_path, "w") as f:
            json.dump(data, f)
    return data["access_token"]

def ms_headers():
    return {"Authorization": f"Bearer {ms_get_token()}", "Content-Type": "application/json"}

def lire_emails_non_lus_microsoft():
    """Lit les emails non lus via Microsoft Graph API."""
    url = f"{MS_GRAPH}/me/messages?$filter=isRead eq false&$top=50&$select=id,subject,from,body,receivedDateTime,conversationId"
    resp = http_requests.get(url, headers=ms_headers())
    resp.raise_for_status()
    messages = resp.json().get("value", [])
    emails = []
    for m in messages:
        corps = m.get("body", {}).get("content", "")
        # Nettoie le HTML basique
        import re
        corps = re.sub(r'<[^>]+>', ' ', corps)[:3000]
        emails.append({
            "id":              m["id"],
            "thread_id":       m.get("conversationId", m["id"]),
            "received_at":     m.get("receivedDateTime", ""),
            "sujet":           m.get("subject", ""),
            "expediteur":      m.get("from", {}).get("emailAddress", {}).get("address", ""),
            "corps":           corps.strip(),
            "message_id":      m["id"],
        })
    return emails

def appliquer_label_microsoft(msg_id, categorie):
    """Déplace l'email dans un dossier MailPilot/catégorie via Graph API."""
    # Crée le dossier si besoin
    try:
        http_requests.post(f"{MS_GRAPH}/me/mailFolders", headers=ms_headers(),
                           json={"displayName": "MailPilot"})
    except Exception:
        pass
    # Récupère l'ID du dossier MailPilot
    r = http_requests.get(f"{MS_GRAPH}/me/mailFolders?$filter=displayName eq 'MailPilot'",
                          headers=ms_headers())
    dossiers = r.json().get("value", [])
    if not dossiers:
        return
    dossier_id = dossiers[0]["id"]
    # Crée le sous-dossier catégorie
    try:
        http_requests.post(f"{MS_GRAPH}/me/mailFolders/{dossier_id}/childFolders",
                           headers=ms_headers(), json={"displayName": categorie})
    except Exception:
        pass
    r2 = http_requests.get(f"{MS_GRAPH}/me/mailFolders/{dossier_id}/childFolders?$filter=displayName eq '{categorie}'",
                           headers=ms_headers())
    sous_dossiers = r2.json().get("value", [])
    if not sous_dossiers:
        return
    sous_id = sous_dossiers[0]["id"]
    http_requests.post(f"{MS_GRAPH}/me/messages/{msg_id}/move",
                       headers=ms_headers(), json={"destinationId": sous_id})

def marquer_comme_lu_microsoft(msg_id):
    """Marque l'email comme lu via Graph API."""
    http_requests.patch(f"{MS_GRAPH}/me/messages/{msg_id}",
                        headers=ms_headers(), json={"isRead": True})

def creer_brouillon_microsoft(email_data, texte_reponse):
    """Crée un brouillon de réponse via Graph API."""
    body = {
        "subject": f"Re: {email_data['sujet']}",
        "body": {"contentType": "Text", "content": texte_reponse},
        "toRecipients": [{"emailAddress": {"address": email_data["expediteur"]}}],
        "isDraft": True,
    }
    resp = http_requests.post(f"{MS_GRAPH}/me/messages", headers=ms_headers(), json=body)
    if resp.status_code in (200, 201):
        logger.info("  ✓ Brouillon Microsoft créé")
        return True
    logger.error(f"  ✗ Erreur brouillon Microsoft : {resp.text}")
    return False

def envoyer_notification_microsoft(sujet, corps):
    """Envoie un email de notification via Graph API."""
    email_agent = os.getenv("AGENT_EMAIL")
    body = {
        "message": {
            "subject": sujet,
            "body": {"contentType": "Text", "content": corps},
            "toRecipients": [{"emailAddress": {"address": email_agent}}],
        }
    }
    try:
        http_requests.post(f"{MS_GRAPH}/me/sendMail", headers=ms_headers(), json=body)
        logger.info(f"✉️  Notification Microsoft envoyée à {email_agent}")
    except Exception as e:
        logger.error(f"Erreur notification Microsoft : {e}")


# ============================================================
# ADAPTATEUR IMAP / SMTP (OVH, Orange, Free, etc.)
# ============================================================

def _decode_header_str(valeur):
    """Décode un header email encodé (ex: =?utf-8?b?...?=)."""
    if not valeur:
        return ""
    parties = decode_header(valeur)
    resultat = ""
    for partie, charset in parties:
        if isinstance(partie, bytes):
            resultat += partie.decode(charset or "utf-8", errors="replace")
        else:
            resultat += partie
    return resultat

def authentifier_imap():
    """Connexion IMAP SSL avec les variables d'environnement."""
    ctx = ssl.create_default_context()
    conn = imaplib.IMAP4_SSL(
        os.getenv("IMAP_SERVER"),
        int(os.getenv("IMAP_PORT", "993")),
        ssl_context=ctx
    )
    conn.login(os.getenv("AGENT_EMAIL"), os.getenv("IMAP_PASSWORD"))
    logger.info(f"✓ IMAP connecté ({os.getenv('IMAP_SERVER')})")
    return conn

def lire_emails_non_lus_imap(imap_conn):
    """Lit les emails non lus via IMAP et retourne une liste de dicts."""
    imap_conn.select("INBOX")
    _, data = imap_conn.uid("search", None, "UNSEEN")
    uids = data[0].split()
    emails = []
    for uid in uids:
        try:
            _, msg_data = imap_conn.uid("fetch", uid, "(RFC822)")
            raw = msg_data[0][1]
            msg = email_lib.message_from_bytes(raw)
            sujet      = _decode_header_str(msg.get("Subject", ""))
            expediteur = _decode_header_str(msg.get("From", ""))
            message_id = msg.get("Message-ID", uid.decode())

            # Extraction du corps texte
            corps = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        charset = part.get_content_charset() or "utf-8"
                        corps = part.get_payload(decode=True).decode(charset, errors="replace")
                        break
            else:
                charset = msg.get_content_charset() or "utf-8"
                corps = msg.get_payload(decode=True).decode(charset, errors="replace")

            emails.append({
                "uid_imap":   uid,
                "id":         uid.decode(),
                "sujet":      sujet,
                "expediteur": expediteur,
                "corps":      corps[:3000],
                "message_id": message_id,
            })
        except Exception as e:
            logger.error(f"  ✗ Erreur lecture email IMAP uid={uid} : {e}")
    return emails

def appliquer_label_imap(imap_conn, uid, categorie):
    """Copie l'email dans un dossier MailPilot/catégorie via IMAP."""
    try:
        dossier = f"MailPilot/{categorie}"
        imap_conn.create(dossier)
    except Exception:
        pass  # dossier existe déjà
    try:
        imap_conn.uid("copy", uid, f"MailPilot/{categorie}")
    except Exception as e:
        logger.error(f"  ✗ Erreur dossier IMAP : {e}")

def marquer_comme_lu_imap(imap_conn, uid):
    """Marque l'email comme lu via IMAP."""
    imap_conn.uid("store", uid, "+FLAGS", "\\Seen")

def creer_brouillon_imap(imap_conn, email_data, texte_reponse):
    """Sauvegarde un brouillon dans le dossier Drafts via IMAP."""
    msg = MIMEMultipart()
    msg["To"]      = email_data["expediteur"]
    msg["From"]    = os.getenv("AGENT_EMAIL")
    msg["Subject"] = f"Re: {email_data['sujet']}"
    if email_data.get("message_id"):
        msg["In-Reply-To"] = email_data["message_id"]
    msg.attach(MIMEText(texte_reponse, "plain", "utf-8"))

    raw_bytes = msg.as_bytes()
    # Essai des noms courants pour le dossier Brouillons
    for dossier in ["Drafts", "Draft", "Brouillons", "[Gmail]/Drafts", "INBOX.Drafts"]:
        try:
            imap_conn.append(dossier, "\\Draft", None, raw_bytes)
            logger.info(f"  ✓ Brouillon IMAP créé dans '{dossier}'")
            return True
        except Exception:
            continue
    logger.error("  ✗ Impossible de créer le brouillon IMAP (dossier Drafts introuvable)")
    return False

def envoyer_notification_smtp(sujet, corps):
    """Envoie un email de notification via SMTP."""
    email_agent = os.getenv("AGENT_EMAIL")
    smtp_server = os.getenv("SMTP_SERVER")
    smtp_port   = int(os.getenv("SMTP_PORT", "465"))
    password    = os.getenv("IMAP_PASSWORD")

    msg = MIMEMultipart()
    msg["To"]      = email_agent
    msg["From"]    = email_agent
    msg["Subject"] = sujet
    msg.attach(MIMEText(corps, "plain", "utf-8"))

    try:
        if smtp_port == 587:
            with smtplib.SMTP(smtp_server, smtp_port) as server:
                server.starttls()
                server.login(email_agent, password)
                server.sendmail(email_agent, email_agent, msg.as_bytes())
        else:
            with smtplib.SMTP_SSL(smtp_server, smtp_port) as server:
                server.login(email_agent, password)
                server.sendmail(email_agent, email_agent, msg.as_bytes())
        logger.info(f"✉️  Notification SMTP envoyée à {email_agent}")
    except Exception as e:
        logger.error(f"Erreur envoi notification SMTP : {e}")


# ============================================================
# RELANCE INTELLIGENTE
# ============================================================

def rediger_relance_intelligente(client_anthropic, sujet_orig, expediteur):
    """Génère un court email de relance pour un client qui n'a pas répondu."""
    nom   = os.getenv("AGENT_NOM", "l'équipe")
    agence = os.getenv("AGENT_AGENCE", "")
    signature = os.getenv("AGENT_SIGNATURE", "").strip()

    prompt = f"""Tu es {nom}{(' chez ' + agence) if agence else ''}.
Tu as envoyé une réponse il y a quelques jours à un client ({expediteur}) concernant : "{sujet_orig}".
Le client n'a pas répondu. Rédige un email de relance court et chaleureux (3-4 lignes maximum).
Règles :
- Rappelle brièvement l'objet du message
- Demande si le client a bien reçu ta réponse et si tu peux l'aider
- Ton professionnel mais humain, pas insistant
- Commence directement par "Bonjour," sans objet ni préambule
- Ne signe pas à la fin (la signature sera ajoutée automatiquement)"""

    try:
        r = client_anthropic.messages.create(
            model="claude-haiku-4-5",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        texte = r.content[0].text.strip()
        if signature:
            texte += f"\n\n{signature}"
        return texte
    except Exception as e:
        logger.error(f"Erreur rédaction relance intelligente : {e}")
        return None


def envoyer_relance_intelligente_gmail(service, relance, texte):
    """Envoie l'email de relance via Gmail dans le thread d'origine."""
    sujet = relance["sujet"]
    if not sujet.lower().startswith("re:"):
        sujet = f"Re: {sujet}"

    msg = MIMEMultipart()
    msg["To"]      = relance["expediteur"]
    msg["From"]    = os.getenv("AGENT_EMAIL", "")
    msg["Subject"] = sujet
    msg.attach(MIMEText(texte, "plain", "utf-8"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    service.users().messages().send(
        userId="me",
        body={"raw": raw, "threadId": relance["thread_id"]},
    ).execute()


def verifier_relances_intelligentes(service, client_anthropic):
    """
    Vérifie les brouillons sans réponse et envoie une relance si besoin.
    Appelé périodiquement depuis la boucle principale (Gmail uniquement).
    """
    if os.getenv("RELANCE_INTELLIGENTE_ACTIF", "0") != "1":
        return
    if os.getenv("MAIL_PROVIDER", "gmail") != "gmail":
        return

    jours = int(os.getenv("RELANCE_INTELLIGENTE_JOURS", "3"))
    compte_id, boite_id = _get_compte_boite_ids()
    pending = db_module.get_relances_pending(compte_id, boite_id, jours * 86400)

    if not pending:
        return

    logger.info(f"🔔 Relances intelligentes : {len(pending)} à vérifier…")

    for relance in pending:
        try:
            # Vérifier si le client a répondu dans le thread
            thread = service.users().threads().get(
                userId="me", id=relance["thread_id"], format="metadata",
                metadataHeaders=["From", "Date"],
            ).execute()

            messages = thread.get("messages", [])
            created_dt = datetime.fromisoformat(relance["created_at"])

            from email.utils import parseaddr
            expediteur_email = parseaddr(relance["expediteur"])[1].lower()

            client_a_repondu = False
            for msg in messages:
                headers = {h["name"]: h["value"]
                           for h in msg.get("payload", {}).get("headers", [])}
                sender = parseaddr(headers.get("From", ""))[1].lower()
                # Date du message en secondes → datetime
                ts = int(msg.get("internalDate", 0)) / 1000
                msg_dt = datetime.fromtimestamp(ts)
                if sender == expediteur_email and msg_dt > created_dt:
                    client_a_repondu = True
                    break

            if client_a_repondu:
                db_module.marquer_relance(relance["id"], "replied")
                logger.info(f"  ✅ {relance['expediteur']} a répondu — relance annulée")
            else:
                texte = rediger_relance_intelligente(client_anthropic, relance["sujet"], relance["expediteur"])
                if texte:
                    envoyer_relance_intelligente_gmail(service, relance, texte)
                    db_module.marquer_relance(relance["id"], "sent")
                    logger.info(f"  📤 Relance envoyée à {relance['expediteur']}")
                else:
                    logger.warning(f"  ✗ Relance non générée pour {relance['expediteur']}")

        except Exception as e:
            logger.error(f"  ✗ Erreur relance {relance['id']} : {e}")


def boucle_principale():
    """
    Boucle infinie qui surveille les emails toutes les N secondes.
    Initialise les connexions une seule fois, puis tourne indéfiniment.
    """
    intervalle = int(os.getenv("CHECK_INTERVAL", "60"))

    mail_provider = os.getenv("MAIL_PROVIDER", "gmail")

    logger.info("=" * 60)
    logger.info("  MailPilot — Démarrage")
    logger.info(f"  Agent      : {os.getenv('AGENT_NOM')}")
    logger.info(f"  Agence     : {os.getenv('AGENT_AGENCE')}")
    logger.info(f"  Zone       : {os.getenv('AGENT_ZONE')}")
    logger.info(f"  Email      : {os.getenv('AGENT_EMAIL')}")
    logger.info(f"  Provider   : {mail_provider.upper()}")
    logger.info(f"  Intervalle : toutes les {intervalle}s")
    logger.info("=" * 60)

    # --- Connexion au service mail ---
    service    = None  # Gmail API (mode gmail)
    imap_conn  = None  # Connexion IMAP (mode imap)
    label_ids  = {}

    if mail_provider == "gmail":
        logger.info("Connexion à Gmail...")
        try:
            service = authentifier_gmail()
            logger.info("✓ Gmail connecté")
        except Exception as e:
            logger.critical(f"Impossible de se connecter à Gmail : {e}")
            raise
        logger.info("Vérification des labels Gmail...")
        try:
            label_ids = obtenir_ou_creer_labels(service)
            logger.info(f"✓ {len(label_ids)} labels prêts")
        except Exception as e:
            logger.critical(f"Impossible de créer les labels : {e}")
            raise
    elif mail_provider == "microsoft":
        logger.info("Vérification token Microsoft...")
        try:
            ms_get_token()
            logger.info("✓ Microsoft Graph connecté")
        except Exception as e:
            logger.critical(f"Impossible de se connecter à Microsoft Graph : {e}")
            raise
    else:
        logger.info(f"Connexion IMAP ({os.getenv('IMAP_SERVER')})...")
        try:
            imap_conn = authentifier_imap()
        except Exception as e:
            logger.critical(f"Impossible de se connecter en IMAP : {e}")
            raise

    # --- Connexion Claude (Anthropic) ---
    logger.info("Connexion à l'API Claude...")
    try:
        client_anthropic = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        logger.info("✓ Claude connecté")
    except Exception as e:
        logger.critical(f"Impossible de se connecter à Claude : {e}")
        raise

    logger.info("\n🚀 MailPilot actif ! Surveillance en cours...\n")

    dernier_bilan_date     = None
    bilan_jour             = int(os.getenv("BILAN_JOUR",  "-1"))
    bilan_heure            = int(os.getenv("BILAN_HEURE", "8"))
    _cycle_count           = 0          # compteur pour les tâches périodiques
    _relance_check_interval = max(1, 600 // max(1, int(os.getenv("CHECK_INTERVAL", "60"))))  # ~10 min

    # --- Boucle infinie ---
    while True:
        try:
            maintenant  = datetime.now()
            email_agent = os.getenv("AGENT_EMAIL")

            # --- Bilan hebdomadaire (jour/heure configurables) ---
            if bilan_jour >= 0 and maintenant.weekday() == bilan_jour and maintenant.hour == bilan_heure:
                jour_actuel = maintenant.date()
                if dernier_bilan_date != jour_actuel:
                    if mail_provider == "gmail":
                        envoyer_bilan_hebdo(service, email_agent)
                    else:
                        stats_h = charger_stats_semaine()
                        if stats_h["traites"] > 0:
                            nb = stats_h["traites"]; nb_b = stats_h["brouillons"]
                            semaine = stats_h["semaine"]
                            lignes = "".join(
                                f"  {TOUS_LES_LABELS.get(c,{}).get('emoji','•')} "
                                f"{TOUS_LES_LABELS.get(c,{}).get('nom',c)} : {n} email(s)\n"
                                for c, n in sorted(stats_h.get("categories",{}).items(), key=lambda x: -x[1])
                            )
                            corps = (f"Bonjour,\n\nVoici votre bilan MailPilot semaine {semaine}.\n\n"
                                     f"📊 {nb} email(s) traité(s), {nb_b} brouillon(s)\n\n{lignes}\n"
                                     f"---\nMailPilot — Assistant email IA\n")
                            sujet_bilan = f"MailPilot — Bilan semaine {semaine}"
                            if mail_provider == "microsoft":
                                envoyer_notification_microsoft(sujet_bilan, corps)
                            else:
                                envoyer_notification_smtp(sujet_bilan, corps)
                            archiver_semaine_dans_historique(stats_h)
                            sauver_stats_semaine({"semaine": semaine, "annee": maintenant.year,
                                                  "traites": 0, "brouillons": 0, "categories": {}})
                    dernier_bilan_date = jour_actuel

            # --- Nettoyage automatique (1x/jour) ---
            nettoyer_emails_anciens(service=service, imap_conn=imap_conn, mail_provider=mail_provider)

            logger.info("🔍 Vérification des emails non lus...")

            # --- Lecture des emails selon le provider ---
            if mail_provider == "gmail":
                emails = lire_emails_non_lus(service)
            elif mail_provider == "microsoft":
                emails = lire_emails_non_lus_microsoft()
            else:
                # Reconnexion IMAP si la session a expiré
                try:
                    imap_conn.noop()
                except Exception:
                    logger.info("  Reconnexion IMAP...")
                    imap_conn = authentifier_imap()
                emails = lire_emails_non_lus_imap(imap_conn)

            if not emails:
                logger.info("  Aucun email non lu.")
            else:
                # ── Déduplication par thread (Gmail + Microsoft) ────────
                # Si plusieurs emails non lus appartiennent au même thread,
                # on ne traite que le plus récent et on marque les autres lus.
                if mail_provider in ("gmail", "microsoft"):
                    _threads: dict = {}
                    for _em in emails:
                        _tid = _em.get("thread_id", _em["id"])
                        # Pour Gmail on a internal_date (ms), pour Microsoft received_at (ISO)
                        _date = _em.get("internal_date") or _em.get("received_at") or ""
                        if _tid not in _threads or str(_date) > str(_threads[_tid].get("internal_date") or _threads[_tid].get("received_at") or ""):
                            _threads[_tid] = _em
                    _keep_ids = {_em["id"] for _em in _threads.values()}
                    _dupes = [_em for _em in emails if _em["id"] not in _keep_ids]
                    if _dupes:
                        logger.info(f"  🔗 {len(_dupes)} email(s) dupliqué(s) dans un thread déjà traité — marqué(s) comme lus")
                        for _dup in _dupes:
                            try:
                                if mail_provider == "gmail":
                                    marquer_comme_lu(service, _dup["id"])
                                else:
                                    marquer_comme_lu_microsoft(_dup["id"])
                            except Exception as _e:
                                logger.warning(f"  ✗ Impossible de marquer dupliqué lu : {_e}")
                    emails = list(_threads.values())
                # ─────────────────────────────────────────────────────────

                logger.info(f"  {len(emails)} email(s) à traiter.")
                stats = {"traites": 0, "brouillons": 0, "categories": {}}

                for em in emails:
                    try:
                        if mail_provider == "gmail":
                            resultat = traiter_email(service, client_anthropic, em, label_ids)
                        elif mail_provider == "microsoft":
                            # --- Pipeline Microsoft Graph ---
                            if est_blackliste(em.get("expediteur", "")):
                                logger.info(f"  🚫 Blacklisté — classé INUTILE sans appel API")
                                appliquer_label_microsoft(em["id"], "INUTILE")
                                marquer_comme_lu_microsoft(em["id"])
                                resultat = {"categorie": "INUTILE", "brouillon_cree": False, "brouillon_texte": ""}
                                if resultat:
                                    stats["traites"] += 1
                                    stats["categories"]["INUTILE"] = stats["categories"].get("INUTILE", 0) + 1
                                continue
                            categorie = classifier_email(client_anthropic, em)
                            logger.info(f"  → Catégorie : {categorie}")
                            _s = detecter_sentiment(em)
                            if _s == "mecontent": logger.warning(f"  😤 CLIENT MÉCONTENT détecté — traiter en priorité !")
                            elif _s == "alerte":  logger.info(f"  ⚠️  Signal mécontentement détecté dans l'email")
                            try:
                                detecter_rdv(client_anthropic, em, categorie)
                            except Exception as e:
                                logger.error(f"  ✗ Erreur détection RDV : {e}")
                            try:
                                transferer_email(em, categorie, mail_provider="microsoft")
                            except Exception as e:
                                logger.error(f"  ✗ Erreur transfert : {e}")
                            appliquer_label_microsoft(em["id"], categorie)
                            marquer_comme_lu_microsoft(em["id"])
                            brouillon_cree = False
                            texte_ms = ""
                            if categorie != "INUTILE":
                                texte_ms = rediger_reponse(client_anthropic, em, categorie) or ""
                                if texte_ms:
                                    brouillon_cree = creer_brouillon_microsoft(em, texte_ms)
                            resultat = {"categorie": categorie, "brouillon_cree": brouillon_cree, "brouillon_texte": texte_ms}
                        else:
                            # --- Pipeline IMAP ---
                            if est_blackliste(em.get("expediteur", "")):
                                logger.info(f"  🚫 Blacklisté — classé INUTILE sans appel API")
                                appliquer_label_imap(imap_conn, em["uid_imap"], "INUTILE")
                                marquer_comme_lu_imap(imap_conn, em["uid_imap"])
                                stats["traites"] += 1
                                stats["categories"]["INUTILE"] = stats["categories"].get("INUTILE", 0) + 1
                                continue
                            categorie = classifier_email(client_anthropic, em)
                            logger.info(f"  → Catégorie : {categorie}")
                            _s = detecter_sentiment(em)
                            if _s == "mecontent": logger.warning(f"  😤 CLIENT MÉCONTENT détecté — traiter en priorité !")
                            elif _s == "alerte":  logger.info(f"  ⚠️  Signal mécontentement détecté dans l'email")
                            try:
                                detecter_rdv(client_anthropic, em, categorie)
                            except Exception as e:
                                logger.error(f"  ✗ Erreur détection RDV : {e}")
                            try:
                                transferer_email(em, categorie, imap_conn=imap_conn, mail_provider="imap")
                            except Exception as e:
                                logger.error(f"  ✗ Erreur transfert : {e}")
                            appliquer_label_imap(imap_conn, em["uid_imap"], categorie)
                            marquer_comme_lu_imap(imap_conn, em["uid_imap"])
                            brouillon_cree = False
                            texte_imap = ""
                            if categorie != "INUTILE":
                                texte_imap = rediger_reponse(client_anthropic, em, categorie) or ""
                                if texte_imap:
                                    brouillon_cree = creer_brouillon_imap(imap_conn, em, texte_imap)
                            resultat = {"categorie": categorie, "brouillon_cree": brouillon_cree, "brouillon_texte": texte_imap}

                        if resultat:
                            stats["traites"] += 1
                            cat = resultat.get("categorie", "inconnu")
                            stats["categories"][cat] = stats["categories"].get(cat, 0) + 1
                            if resultat.get("brouillon_cree"):
                                stats["brouillons"] += 1
                            # Persister l'email + brouillon pour le chat IA
                            try:
                                sauver_email_recent(em, cat, resultat.get("brouillon_texte", ""))
                            except Exception:
                                pass
                    except Exception as e:
                        logger.error(f"  ✗ Erreur sur '{em.get('sujet','?')}' : {e}")

                if stats["traites"] > 0:
                    accumuler_stats_semaine(stats)

        except Exception as e:
            logger.error(f"Erreur dans le cycle de vérification : {e}")

        # --- Relances intelligentes (toutes les ~10 min, Gmail uniquement) ---
        _cycle_count += 1
        if service and _cycle_count % _relance_check_interval == 0:
            try:
                verifier_relances_intelligentes(service, client_anthropic)
            except Exception as e:
                logger.error(f"Erreur relances intelligentes : {e}")

        logger.info(f"\n⏳ Prochain cycle dans {intervalle} secondes...\n")
        time.sleep(intervalle)


# ============================================================
# POINT D'ENTRÉE
# ============================================================

if __name__ == "__main__":
    # Vérifie que les variables essentielles sont configurées
    variables_requises = [
        "ANTHROPIC_API_KEY",
        "AGENT_NOM",
        "AGENT_AGENCE",
        "AGENT_EMAIL",
    ]
    manquantes = [v for v in variables_requises if not os.getenv(v)]
    if manquantes:
        logger.critical(
            f"Variables d'environnement manquantes dans .env : {', '.join(manquantes)}\n"
            "Copie le fichier .env.example en .env et remplis les valeurs."
        )
        exit(1)

    # Lance la boucle principale
    boucle_principale()
