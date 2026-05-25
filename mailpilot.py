#!/usr/bin/env python3
# ============================================================
# mailpilot.py — Assistant IA de gestion des emails
# pour un agent immobilier français
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

from prompts import build_classification_prompt, DRAFTING_PROMPTS, LABELS_DEFAUT, TOUS_LES_LABELS

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
            "id": message_id,
            "thread_id": message["threadId"],
            "sujet": sujet,
            "expediteur": expediteur,
            "corps": corps[:4000],  # Limite pour économiser les tokens
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
        logger.warning(f"Aucun prompt de rédaction pour la catégorie {categorie}")
        return None

    # Injecte les variables de l'agent
    prompt = prompt_base.format(
        nom=os.getenv("AGENT_NOM", "L'agent"),
        agence=os.getenv("AGENT_AGENCE", "L'agence"),
        tel=os.getenv("AGENT_TEL", ""),
        email=os.getenv("AGENT_EMAIL", ""),
        zone=os.getenv("AGENT_ZONE", "la région"),
    )

    # Ajoute les instructions personnalisées du client si définies
    instructions = os.getenv("AGENT_INSTRUCTIONS", "").strip()
    if instructions:
        prompt += f"\n\n--- CONSIGNES PERSONNALISÉES (à respecter impérativement) ---\n{instructions}\n---"

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

def _stats_paths():
    """Retourne les chemins des fichiers de stats pour ce compte."""
    stats_dir = os.getenv("STATS_DIR", os.path.dirname(os.path.abspath(__file__)))
    compte_id  = os.getenv("COMPTE_ID", "default")
    return (
        os.path.join(stats_dir, f"stats_{compte_id}.json"),
        os.path.join(stats_dir, f"stats_hist_{compte_id}.json"),
    )

def charger_stats_semaine():
    """Charge les stats de la semaine courante."""
    stats_file, _ = _stats_paths()
    semaine_courante = date.today().isocalendar()[1]
    annee_courante   = date.today().year
    vide = {"semaine": semaine_courante, "annee": annee_courante, "traites": 0, "brouillons": 0, "categories": {}}
    if os.path.exists(stats_file):
        try:
            with open(stats_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("semaine") != semaine_courante or data.get("annee") != annee_courante:
                return vide
            return data
        except Exception:
            pass
    return vide

def sauver_stats_semaine(stats):
    """Sauvegarde les stats de la semaine courante."""
    stats_file, _ = _stats_paths()
    try:
        with open(stats_file, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Erreur sauvegarde stats semaine : {e}")

def archiver_semaine_dans_historique(stats):
    """Ajoute les stats de la semaine terminée à l'historique (12 semaines max)."""
    _, hist_file = _stats_paths()
    try:
        historique = []
        if os.path.exists(hist_file):
            with open(hist_file, "r", encoding="utf-8") as f:
                historique = json.load(f).get("semaines", [])
        # Évite les doublons
        historique = [s for s in historique if not (s.get("semaine") == stats["semaine"] and s.get("annee") == stats["annee"])]
        historique.append({
            "semaine":    stats["semaine"],
            "annee":      stats["annee"],
            "label":      f"Sem. {stats['semaine']}",
            "traites":    stats["traites"],
            "brouillons": stats["brouillons"],
            "categories": stats.get("categories", {}),
        })
        historique = historique[-12:]  # garder les 12 dernières semaines
        with open(hist_file, "w", encoding="utf-8") as f:
            json.dump({"semaines": historique}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Erreur archivage historique stats : {e}")

def accumuler_stats_semaine(stats_cycle):
    """Ajoute les stats du cycle aux stats hebdomadaires."""
    data = charger_stats_semaine()
    data["traites"]   += stats_cycle.get("traites", 0)
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


def detecter_rdv(client_anthropic, email, categorie):
    """
    Si l'email contient une demande de RDV (VISITE, REUNION, etc.),
    extrait les infos et les ajoute dans l'agenda en statut 'attente'.
    """
    CATEGORIES_RDV = {"VISITE", "RENDEZ_VOUS", "DEVIS", "URGENT", "PROSPECT"}
    if categorie not in CATEGORIES_RDV:
        return

    stats_dir  = os.getenv("STATS_DIR", os.path.dirname(os.path.abspath(__file__)))
    compte_id  = os.getenv("COMPTE_ID", "default")
    # COMPTE_ID est au format "compte_boite" — on prend juste la partie compte
    compte_part = compte_id.split("_")[0] if "_" in compte_id else compte_id
    agenda_file = os.path.join(stats_dir, f"agenda_{compte_part}.json")

    prompt = f"""Analyse cet email et réponds UNIQUEMENT en JSON valide (sans markdown).
Si l'email contient une demande de rendez-vous, extrais ces informations.
Sinon, réponds : {{"rdv": false}}

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
CORPS : {email['corps'][:1000]}"""

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

        if not data.get("rdv"):
            return

        # Charger l'agenda existant
        rdvs = []
        if os.path.exists(agenda_file):
            try:
                rdvs = json.loads(open(agenda_file, encoding="utf-8").read())
            except Exception:
                rdvs = []

        # Créer le RDV
        today = datetime.now().strftime("%Y-%m-%d")
        rdv = {
            "id":           str(uuid.uuid4())[:8],
            "titre":        data.get("titre", f"RDV - {email['expediteur'][:30]}"),
            "client_nom":   data.get("client_nom", ""),
            "client_email": data.get("client_email", email.get("expediteur_email", "")),
            "adresse":      data.get("adresse", ""),
            "date":         data.get("date") or today,
            "heure_debut":  data.get("heure_debut", "09:00"),
            "heure_fin":    data.get("heure_fin", "10:00"),
            "type":         data.get("type", "visite"),
            "statut":       "attente",
            "notes":        data.get("notes", f"Détecté automatiquement depuis email : {email['sujet']}"),
            "boite_id":     "",
            "created_at":   datetime.now().isoformat(),
        }
        rdvs.append(rdv)

        with open(agenda_file, "w", encoding="utf-8") as f:
            json.dump(rdvs, f, indent=2, ensure_ascii=False)

        logger.info(f"  📅 RDV détecté et ajouté à l'agenda : {rdv['titre']} ({rdv['date']} {rdv['heure_debut']})")

    except Exception as e:
        logger.error(f"  ✗ Erreur détection RDV : {e}")


def traiter_email(service, client_anthropic, email, label_ids):
    """
    Traite un email complet : classification, label, brouillon, marquage.
    Retourne un dict {"categorie": ..., "brouillon_cree": bool}
    """
    email_resume = f"'{email['sujet'][:40]}' de {email['expediteur'][:30]}"
    logger.info(f"\n📧 Traitement : {email_resume}")
    brouillon_cree = False

    # --- Étape 1 : Classification ---
    try:
        categorie = classifier_email(client_anthropic, email)
        logger.info(f"  → Catégorie : {categorie}")
    except Exception as e:
        logger.error(f"  ✗ Erreur classification : {e}")
        categorie = "INFO"

    # --- Étape 1b : Détection RDV ---
    try:
        detecter_rdv(client_anthropic, email, categorie)
    except Exception as e:
        logger.error(f"  ✗ Erreur détection RDV : {e}")

    # --- Étape 2 : Application du label Gmail ---
    try:
        label_id = label_ids.get(categorie)
        if label_id:
            appliquer_label(service, email["id"], label_id)
            logger.info(f"  → Label appliqué : {LABEL_NOMS[categorie]}")
    except Exception as e:
        logger.error(f"  ✗ Erreur label : {e}")

    # --- Étape 3 : Rédaction du brouillon (sauf INUTILE) ---
    if categorie != "INUTILE":
        try:
            texte_reponse = rediger_reponse(client_anthropic, email, categorie)
            if texte_reponse:
                creer_brouillon(service, email, texte_reponse, categorie)
                brouillon_cree = True
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

    return {"categorie": categorie, "brouillon_cree": brouillon_cree}


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
    url = f"{MS_GRAPH}/me/messages?$filter=isRead eq false&$top=50&$select=id,subject,from,body,receivedDateTime"
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
            "id":         m["id"],
            "sujet":      m.get("subject", ""),
            "expediteur": m.get("from", {}).get("emailAddress", {}).get("address", ""),
            "corps":      corps.strip(),
            "message_id": m["id"],
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

    dernier_bilan_date = None
    bilan_jour  = int(os.getenv("BILAN_JOUR",  "-1"))  # -1=désactivé, 0=lundi … 6=dimanche
    bilan_heure = int(os.getenv("BILAN_HEURE", "8"))   # heure 0-23

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
                logger.info(f"  {len(emails)} email(s) à traiter.")
                stats = {"traites": 0, "brouillons": 0, "categories": {}}

                for em in emails:
                    try:
                        if mail_provider == "gmail":
                            resultat = traiter_email(service, client_anthropic, em, label_ids)
                        elif mail_provider == "microsoft":
                            # --- Pipeline Microsoft Graph ---
                            categorie = classifier_email(client_anthropic, em)
                            logger.info(f"  → Catégorie : {categorie}")
                            try:
                                detecter_rdv(client_anthropic, em, categorie)
                            except Exception as e:
                                logger.error(f"  ✗ Erreur détection RDV : {e}")
                            appliquer_label_microsoft(em["id"], categorie)
                            marquer_comme_lu_microsoft(em["id"])
                            brouillon_cree = False
                            if categorie != "INUTILE":
                                texte = rediger_reponse(client_anthropic, em, categorie)
                                if texte:
                                    brouillon_cree = creer_brouillon_microsoft(em, texte)
                            resultat = {"categorie": categorie, "brouillon_cree": brouillon_cree}
                        else:
                            # --- Pipeline IMAP ---
                            categorie = classifier_email(client_anthropic, em)
                            logger.info(f"  → Catégorie : {categorie}")
                            try:
                                detecter_rdv(client_anthropic, em, categorie)
                            except Exception as e:
                                logger.error(f"  ✗ Erreur détection RDV : {e}")
                            appliquer_label_imap(imap_conn, em["uid_imap"], categorie)
                            marquer_comme_lu_imap(imap_conn, em["uid_imap"])
                            brouillon_cree = False
                            if categorie != "INUTILE":
                                texte = rediger_reponse(client_anthropic, em, categorie)
                                if texte:
                                    brouillon_cree = creer_brouillon_imap(imap_conn, em, texte)
                            resultat = {"categorie": categorie, "brouillon_cree": brouillon_cree}

                        if resultat:
                            stats["traites"] += 1
                            cat = resultat.get("categorie", "inconnu")
                            stats["categories"][cat] = stats["categories"].get(cat, 0) + 1
                            if resultat.get("brouillon_cree"):
                                stats["brouillons"] += 1
                    except Exception as e:
                        logger.error(f"  ✗ Erreur sur '{em.get('sujet','?')}' : {e}")

                if stats["traites"] > 0:
                    accumuler_stats_semaine(stats)

        except Exception as e:
            logger.error(f"Erreur dans le cycle de vérification : {e}")

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
