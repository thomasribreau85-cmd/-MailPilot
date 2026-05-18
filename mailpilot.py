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
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

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


def boucle_principale():
    """
    Boucle infinie qui surveille les emails toutes les N secondes.
    Initialise les connexions une seule fois, puis tourne indéfiniment.
    """
    intervalle = int(os.getenv("CHECK_INTERVAL", "60"))

    logger.info("=" * 60)
    logger.info("  MailPilot — Démarrage")
    logger.info(f"  Agent    : {os.getenv('AGENT_NOM')}")
    logger.info(f"  Agence   : {os.getenv('AGENT_AGENCE')}")
    logger.info(f"  Zone     : {os.getenv('AGENT_ZONE')}")
    logger.info(f"  Email    : {os.getenv('AGENT_EMAIL')}")
    logger.info(f"  Intervalle : toutes les {intervalle}s")
    logger.info("=" * 60)

    # --- Connexion Gmail ---
    logger.info("Connexion à Gmail...")
    try:
        service = authentifier_gmail()
        logger.info("✓ Gmail connecté")
    except Exception as e:
        logger.critical(f"Impossible de se connecter à Gmail : {e}")
        raise

    # --- Création/vérification des labels ---
    logger.info("Vérification des labels Gmail...")
    try:
        label_ids = obtenir_ou_creer_labels(service)
        logger.info(f"✓ {len(label_ids)} labels prêts")
    except Exception as e:
        logger.critical(f"Impossible de créer les labels : {e}")
        raise

    # --- Connexion Claude (Anthropic) ---
    logger.info("Connexion à l'API Claude...")
    try:
        client_anthropic = anthropic.Anthropic(
            api_key=os.getenv("ANTHROPIC_API_KEY")
        )
        logger.info("✓ Claude connecté")
    except Exception as e:
        logger.critical(f"Impossible de se connecter à Claude : {e}")
        raise

    logger.info("\n🚀 MailPilot actif ! Surveillance en cours...\n")

    # --- Boucle infinie ---
    while True:
        try:
            logger.info(f"🔍 Vérification des emails non lus...")

            emails = lire_emails_non_lus(service)

            if not emails:
                logger.info("  Aucun email non lu.")
            else:
                logger.info(f"  {len(emails)} email(s) à traiter.")
                stats = {"traites": 0, "brouillons": 0, "categories": {}}
                for email in emails:
                    try:
                        resultat = traiter_email(service, client_anthropic, email, label_ids)
                        if resultat:
                            stats["traites"] += 1
                            cat = resultat.get("categorie", "inconnu")
                            stats["categories"][cat] = stats["categories"].get(cat, 0) + 1
                            if resultat.get("brouillon_cree"):
                                stats["brouillons"] += 1
                    except Exception as e:
                        # Une erreur sur un email n'arrête pas le programme
                        logger.error(
                            f"  ✗ Erreur inattendue sur l'email "
                            f"'{email.get('sujet', '?')}' : {e}"
                        )
                if stats["traites"] > 0:
                    email_agent = os.getenv("AGENT_EMAIL")
                    envoyer_notification(service, email_agent, stats)

        except Exception as e:
            # Une erreur générale ne stoppe pas la boucle
            logger.error(f"Erreur dans le cycle de vérification : {e}")

        # Attente avant le prochain cycle
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
