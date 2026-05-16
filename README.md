# MailPilot — Assistant Email Automatique pour Agent Immobilier

MailPilot surveille ta boîte Gmail, classe chaque email entrant dans une catégorie métier (URGENT, VISITE, OFFRE, INFO, ADMIN, INUTILE), applique un label coloré et rédige un brouillon de réponse professionnel — le tout automatiquement grâce à l'IA Claude d'Anthropic.

---

## Table des matières

1. [Ce que fait MailPilot](#1-ce-que-fait-mailpilot)
2. [Installer Python sur Mac](#2-installer-python-sur-mac)
3. [Télécharger le projet](#3-télécharger-le-projet)
4. [Créer un environnement virtuel et installer les dépendances](#4-créer-un-environnement-virtuel-et-installer-les-dépendances)
5. [Créer les credentials OAuth Gmail](#5-créer-les-credentials-oauth-gmail)
6. [Configurer le fichier .env](#6-configurer-le-fichier-env)
7. [Lancer MailPilot pour la première fois](#7-lancer-mailpilot-pour-la-première-fois)
8. [Utilisation au quotidien](#8-utilisation-au-quotidien)
9. [Résoudre les erreurs courantes](#9-résoudre-les-erreurs-courantes)
10. [Questions fréquentes](#10-questions-fréquentes)

---

## 1. Ce que fait MailPilot

- **Toutes les 60 secondes** (configurable), MailPilot se connecte à ta boîte Gmail
- Il lit les emails non lus dans ta boîte de réception
- Pour chaque email, il :
  1. Le **classe** dans une catégorie grâce à Claude Haiku (rapide et économique)
  2. **Applique un label Gmail coloré** (rouge = URGENT, bleu = VISITE, vert = OFFRE, jaune = INFO, violet = ADMIN, gris = INUTILE)
  3. **Rédige un brouillon de réponse** professionnel grâce à Claude Sonnet (sauf pour les emails INUTILE)
  4. **Marque l'email comme lu**
- Tu n'as plus qu'à ouvrir Gmail, retrouver le brouillon dans l'onglet "Brouillons" et l'envoyer (ou le modifier si besoin)

**MailPilot ne supprime rien et n'envoie rien automatiquement.** Il prépare seulement les brouillons — tu gardes le contrôle total.

---

## 2. Installer Python sur Mac

### Vérifier si Python est déjà installé

Ouvre l'application **Terminal** (cherche "Terminal" dans Spotlight avec `Cmd + Espace`), puis tape :

```bash
python3 --version
```

Si tu vois quelque chose comme `Python 3.11.4` ou toute version `3.10` ou supérieure, **Python est déjà installé**, passe directement à l'étape 3.

Si tu vois `command not found` ou une version `2.x`, suis les étapes ci-dessous.

### Installer Python via le site officiel (méthode recommandée pour débutants)

1. Ouvre ton navigateur et va sur **https://www.python.org/downloads/mac-osx/**
2. Clique sur le gros bouton jaune **"Download Python 3.x.x"** (prends la version la plus récente)
3. Le fichier `.pkg` se télécharge — double-clique dessus pour l'ouvrir
4. Suis l'assistant d'installation : clique sur "Continuer" à chaque étape, puis "Installer"
5. Une fois l'installation terminée, **ferme et rouvre le Terminal**
6. Vérifie l'installation :

```bash
python3 --version
```

Tu devrais voir `Python 3.x.x`. Python est installé !

---

## 3. Télécharger le projet

Si tu as reçu le projet dans un dossier ZIP, décompresse-le où tu veux (par exemple sur ton Bureau ou dans `Documents`).

Si le projet est déjà dans `/Users/ton-nom/MailPilot`, ouvre le Terminal et navigue jusqu'à ce dossier :

```bash
cd ~/MailPilot
```

> **Astuce :** `cd` signifie "change directory" (changer de dossier). `~` est un raccourci pour ton dossier personnel (`/Users/ton-nom`).

Vérifie que tu es bien dans le bon dossier :

```bash
ls
```

Tu dois voir apparaître les fichiers : `mailpilot.py`, `prompts.py`, `requirements.txt`, `.env.example`, etc.

---

## 4. Créer un environnement virtuel et installer les dépendances

Un **environnement virtuel** est une installation Python isolée pour ce projet. Cela évite les conflits entre différents projets sur ton Mac. C'est une bonne pratique à toujours utiliser.

### Étape 4.1 — Créer l'environnement virtuel

Dans le Terminal, dans le dossier `MailPilot` :

```bash
python3 -m venv venv
```

Cette commande crée un sous-dossier `venv/` dans ton projet. **Ne modifie jamais ce dossier manuellement.**

### Étape 4.2 — Activer l'environnement virtuel

```bash
source venv/bin/activate
```

Tu dois voir `(venv)` apparaître au début de ta ligne de commande, comme ceci :

```
(venv) ton-mac:MailPilot toi$
```

> **Important :** À chaque fois que tu ouvres un nouveau Terminal pour utiliser MailPilot, tu dois réactiver l'environnement avec `source venv/bin/activate`.

### Étape 4.3 — Installer les dépendances

```bash
pip install -r requirements.txt
```

Cette commande installe automatiquement toutes les bibliothèques nécessaires (Anthropic, Google API, etc.). L'installation peut prendre 1 à 2 minutes.

En fin d'installation, tu dois voir quelque chose comme :

```
Successfully installed anthropic-x.x.x google-auth-x.x.x ...
```

---

## 5. Créer les credentials OAuth Gmail

C'est l'étape la plus longue, mais elle n'est à faire qu'**une seule fois**. Suis chaque étape attentivement.

L'objectif : obtenir un fichier `credentials.json` qui permet à MailPilot de se connecter à Gmail en ton nom.

### Étape 5.1 — Créer un projet sur Google Cloud Console

1. Ouvre ton navigateur et va sur **https://console.cloud.google.com/**
2. Connecte-toi avec le **compte Google** dont tu veux gérer les emails
3. En haut à gauche, clique sur le menu déroulant qui affiche le nom d'un projet (ou "Sélectionner un projet")
4. Dans la fenêtre qui s'ouvre, clique sur **"Nouveau projet"** (en haut à droite)
5. Dans "Nom du projet", tape `MailPilot` (ou ce que tu veux)
6. Laisse "Organisation" vide
7. Clique sur **"Créer"**
8. Attends quelques secondes — Google crée ton projet
9. Clique sur la notification en haut à droite ou retourne dans le menu déroulant et **sélectionne ton projet MailPilot**

### Étape 5.2 — Activer l'API Gmail

1. Dans la barre de recherche en haut de la page, tape **"Gmail API"**
2. Clique sur le résultat **"Gmail API"** (avec le logo Gmail)
3. Sur la page qui s'affiche, clique sur le bouton bleu **"Activer"**
4. Attends que l'activation se termine (quelques secondes)

### Étape 5.3 — Configurer l'écran de consentement OAuth

1. Dans le menu de gauche, cherche et clique sur **"API et services"** puis **"Écran de consentement OAuth"**
2. On te demande de choisir le type d'utilisateur :
   - Sélectionne **"Externe"**
   - Clique sur **"Créer"**
3. Remplis le formulaire :
   - **Nom de l'application** : `MailPilot`
   - **Adresse e-mail d'assistance utilisateur** : ton adresse Gmail
   - **Coordonnées du développeur** (en bas de page) : ton adresse Gmail
   - Laisse tout le reste vide
4. Clique sur **"Enregistrer et continuer"**
5. Sur la page "Champs d'application" : clique directement sur **"Enregistrer et continuer"** sans rien ajouter
6. Sur la page "Utilisateurs test" :
   - Clique sur **"+ Add users"**
   - Entre ton adresse Gmail
   - Clique sur **"Ajouter"**
   - Clique sur **"Enregistrer et continuer"**
7. Sur la page récapitulative, clique sur **"Retour au tableau de bord"**

### Étape 5.4 — Créer les identifiants OAuth 2.0

1. Dans le menu de gauche, clique sur **"Identifiants"**
2. En haut, clique sur **"+ Créer des identifiants"**
3. Dans le menu déroulant, sélectionne **"ID client OAuth"**
4. Dans "Type d'application", sélectionne **"Application de bureau"**
5. Dans "Nom", tape `MailPilot Desktop` (ou ce que tu veux)
6. Clique sur **"Créer"**
7. Une fenêtre pop-up s'affiche avec ton Client ID et Client Secret — **clique sur "Télécharger le fichier JSON"**
8. Un fichier nommé `client_secret_XXXX.json` se télécharge dans ton dossier Téléchargements

### Étape 5.5 — Placer le fichier credentials.json dans le projet

1. Ouvre ton dossier `Téléchargements` dans le Finder
2. Trouve le fichier `client_secret_XXXX.json`
3. **Renomme-le exactement** en `credentials.json` (clic droit → Renommer)
4. **Déplace-le** dans ton dossier `MailPilot` (à côté de `mailpilot.py`)

Dans le Terminal, vérifie que le fichier est bien là :

```bash
ls -la | grep credentials
```

Tu dois voir `credentials.json` dans la liste.

> **Sécurité :** Ce fichier contient des secrets. Il est listé dans `.gitignore` — il ne sera jamais envoyé sur GitHub. Ne le partage avec personne.

---

## 6. Configurer le fichier .env

Le fichier `.env` contient ta clé API Anthropic et tes informations personnelles d'agent immobilier.

### Étape 6.1 — Copier le fichier exemple

Dans le Terminal :

```bash
cp .env.example .env
```

### Étape 6.2 — Obtenir ta clé API Anthropic

1. Va sur **https://console.anthropic.com/**
2. Crée un compte ou connecte-toi
3. Dans le menu de gauche, clique sur **"API Keys"**
4. Clique sur **"Create Key"**
5. Donne un nom à ta clé (ex: `MailPilot`)
6. **Copie la clé** qui s'affiche — elle commence par `sk-ant-...` — tu ne pourras plus la voir après avoir fermé cette fenêtre !

> **Note :** L'API Anthropic est payante à l'usage. Pour MailPilot avec un volume normal d'emails, le coût est très faible (moins de 1€ par mois en général). Tu peux configurer un budget limite dans la console Anthropic.

### Étape 6.3 — Modifier le fichier .env

Ouvre le fichier `.env` avec un éditeur de texte. Dans le Terminal :

```bash
open -e .env
```

Cela ouvre le fichier dans TextEdit. Remplace chaque valeur par tes vraies informations :

```
ANTHROPIC_API_KEY=sk-ant-ta-vraie-clé-ici

AGENT_NOM=Thomas RIBREAU
AGENT_AGENCE=MailPilot Démo
AGENT_TEL=06 59 51 27 12
AGENT_EMAIL=mailpilot.contact86@gmail.com
AGENT_ZONE=Châtellerault

CHECK_INTERVAL=60
```

Sauvegarde le fichier (`Cmd + S`) et ferme TextEdit.

> **Important :** Ne mets jamais de guillemets autour des valeurs dans le `.env`. Écris `AGENT_NOM=Thomas RIBREAU` et non `AGENT_NOM="Thomas RIBREAU"`.

---

## 7. Lancer MailPilot pour la première fois

### Étape 7.1 — Ouvrir le Terminal dans le bon dossier

```bash
cd ~/MailPilot
source venv/bin/activate
```

Vérifie que tu vois `(venv)` au début de la ligne.

### Étape 7.2 — Lancer le programme

```bash
python3 mailpilot.py
```

### Étape 7.3 — Autoriser l'accès à Gmail (première fois seulement)

Au premier lancement, un message s'affiche dans le Terminal :

```
Première connexion — ouverture du navigateur pour autoriser l'accès Gmail...
```

Ton navigateur s'ouvre automatiquement sur une page Google. Suis ces étapes :

1. **Choisis ton compte Gmail** (celui dont tu veux gérer les emails)
2. Une page "Google n'a pas vérifié cette application" apparaît — c'est normal car c'est ton propre projet
3. Clique sur **"Avancé"** (ou "Advanced")
4. Clique sur **"Accéder à MailPilot (non sécurisé)"** — c'est ton propre programme, c'est sans danger
5. Clique sur **"Autoriser"** pour donner accès à Gmail
6. Une page blanche avec `The authentication flow has completed` s'affiche — tu peux fermer l'onglet

Retourne dans le Terminal. Tu dois voir :

```
Connexion Gmail réussie.
Connexion Anthropic réussie.
Labels Gmail vérifiés/créés.
--- Nouveau cycle de vérification ---
Lecture de X email(s) non lu(s)...
```

MailPilot est lancé ! Il va maintenant tourner en continu. Laisse le Terminal ouvert.

> **Note :** Le fichier `token.json` est créé automatiquement dans ton dossier. Il garde ta connexion Gmail active. La prochaine fois, le navigateur ne s'ouvrira plus — MailPilot se reconnecte tout seul.

---

## 8. Utilisation au quotidien

### Démarrer MailPilot

Chaque matin (ou quand tu veux), ouvre le Terminal et lance :

```bash
cd ~/MailPilot
source venv/bin/activate
python3 mailpilot.py
```

### Arrêter MailPilot

Dans le Terminal, appuie sur `Ctrl + C`. MailPilot s'arrête proprement.

### Voir les brouillons dans Gmail

1. Ouvre Gmail dans ton navigateur
2. Dans la colonne de gauche, clique sur **"Brouillons"**
3. Tu vois les réponses préparées par MailPilot — une par email traité
4. Clique sur un brouillon pour l'ouvrir, modifie-le si besoin, puis clique sur **"Envoyer"**

### Voir les labels colorés

Dans Gmail, tes emails sont maintenant étiquetés avec des labels colorés :
- 🔴 **MailPilot/URGENT** — rouge
- 🔵 **MailPilot/VISITE** — bleu
- 🟢 **MailPilot/OFFRE** — vert
- 🟡 **MailPilot/INFO** — jaune
- 🟣 **MailPilot/ADMIN** — violet
- ⚫ **MailPilot/INUTILE** — gris

---

## 9. Résoudre les erreurs courantes

### ❌ `ModuleNotFoundError: No module named 'anthropic'`

**Cause :** L'environnement virtuel n'est pas activé.

**Solution :**
```bash
source venv/bin/activate
python3 mailpilot.py
```

---

### ❌ `FileNotFoundError: credentials.json not found`

**Cause :** Le fichier `credentials.json` est absent ou mal nommé.

**Solution :**
1. Vérifie que le fichier est bien dans le dossier `MailPilot` : `ls -la | grep credentials`
2. Vérifie qu'il s'appelle exactement `credentials.json` (pas `credentials (1).json` ou autre)
3. Si absent, retourne à l'[étape 5](#5-créer-les-credentials-oauth-gmail)

---

### ❌ `Error 403: access_denied`

**Cause :** Ton compte Gmail n'est pas dans la liste des "utilisateurs test" de ton projet Google Cloud.

**Solution :**
1. Va sur https://console.cloud.google.com/
2. Sélectionne ton projet MailPilot
3. Va dans "API et services" → "Écran de consentement OAuth"
4. Section "Utilisateurs test" → clique sur "+ Add users"
5. Ajoute ton adresse Gmail
6. Supprime le fichier `token.json` s'il existe : `rm token.json`
7. Relance MailPilot

---

### ❌ `ANTHROPIC_API_KEY manquante`

**Cause :** Le fichier `.env` est absent ou la clé API n'est pas renseignée.

**Solution :**
1. Vérifie que le fichier `.env` existe : `ls -la | grep .env`
2. Ouvre-le et vérifie que `ANTHROPIC_API_KEY=sk-ant-...` est bien renseigné
3. Assure-toi qu'il n'y a pas d'espace autour du `=`

---

### ❌ `google.auth.exceptions.RefreshError`

**Cause :** Le token d'authentification Gmail a expiré ou est invalide.

**Solution :** Supprime le fichier token et reconnecte-toi :
```bash
rm token.json
python3 mailpilot.py
```
Le navigateur s'ouvrira à nouveau pour te demander d'autoriser l'accès.

---

### ❌ `anthropic.APIStatusError: 401 Unauthorized`

**Cause :** La clé API Anthropic est invalide ou expirée.

**Solution :**
1. Va sur https://console.anthropic.com/
2. Vérifie que ta clé API est active (non révoquée)
3. Si besoin, crée une nouvelle clé et mets à jour ton fichier `.env`

---

### ❌ `anthropic.APIStatusError: 429 Too Many Requests`

**Cause :** Tu as dépassé les limites de l'API Anthropic (trop d'appels en peu de temps).

**Solution :** Augmente l'intervalle entre les vérifications dans ton `.env` :
```
CHECK_INTERVAL=120
```
Cela vérifie les emails toutes les 2 minutes au lieu de 1 minute.

---

### ❌ Le programme tourne mais ne traite pas les emails

**Cause possible :** Les emails sont dans un dossier autre que la boîte de réception principale (ex: onglet "Promotions" de Gmail).

**Solution :** Gmail classe automatiquement certains emails dans des onglets. MailPilot ne lit que la boîte de réception principale. Tu peux désactiver les onglets dans Gmail :
1. Dans Gmail, clique sur l'icône de roue dentée (paramètres)
2. "Voir tous les paramètres" → onglet "Boîte de réception"
3. Dans "Type de boîte de réception", sélectionne "Par défaut" et décoche tous les onglets sauf "Principal"

---

### ❌ MailPilot s'arrête tout seul

**Cause :** Erreur inattendue non gérée, ou fermeture du Terminal.

**Solution :** Relis les dernières lignes affichées dans le Terminal pour identifier l'erreur. Si le Terminal est fermé, MailPilot s'arrête — laisse la fenêtre ouverte pendant l'utilisation.

Pour un fonctionnement 24h/24, tu peux utiliser la commande `nohup` :
```bash
nohup python3 mailpilot.py > mailpilot.log 2>&1 &
```
Cela lance MailPilot en arrière-plan et enregistre les logs dans `mailpilot.log`.

---

## 10. Questions fréquentes

**Q : MailPilot peut-il envoyer des emails à ma place ?**
Non. MailPilot crée uniquement des **brouillons** dans Gmail. Il ne peut pas envoyer d'email sans ton accord explicite. Tu restes en contrôle total.

**Q : Mes emails sont-ils lus par Anthropic ?**
Le contenu des emails est envoyé à l'API Anthropic pour la classification et la rédaction. Anthropic indique qu'il n'utilise pas les données de l'API pour entraîner ses modèles. Consulte leur politique de confidentialité sur anthropic.com pour plus de détails.

**Q : Combien ça coûte par mois ?**
Pour un agent immobilier traitant 20 à 50 emails par jour, le coût est estimé entre **0,50€ et 3€ par mois** selon le volume. Tu peux suivre ta consommation sur https://console.anthropic.com/.

**Q : Puis-je changer les catégories ou les prompts ?**
Oui ! Modifie le fichier `prompts.py`. Les prompts de classification et de rédaction y sont clairement séparés et commentés.

**Q : Que se passe-t-il si MailPilot classe mal un email ?**
Il peut faire des erreurs. En cas de doute, il classe en INFO (catégorie par défaut). Vérifie simplement les brouillons avant d'envoyer — tu restes le décideur final.

**Q : Puis-je utiliser MailPilot avec plusieurs boîtes Gmail ?**
Non, dans sa version actuelle, MailPilot gère une seule boîte Gmail à la fois. Il faudrait lancer plusieurs instances avec des `credentials.json` et `token.json` différents.

---

*MailPilot — Développé avec Claude d'Anthropic et l'API Gmail de Google*
