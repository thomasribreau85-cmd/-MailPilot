# MailPilot Mobile — Guide de setup

## Prérequis (à installer une seule fois)

```bash
# 1. Node.js (si pas déjà installé)
brew install node

# 2. CocoaPods pour iOS
sudo gem install cocoapods

# 3. Xcode — depuis le Mac App Store (gratuit, ~14 Go)
# Après installation, accepter la licence :
sudo xcodebuild -license accept

# 4. Android Studio (optionnel, pour Android)
brew install --cask android-studio
```

## Installation du projet

```bash
cd /Users/th0m4zz/MailPilot/mobile

# Installer les dépendances npm
npm install

# Générer les icônes PNG
python3 gen_icons.py

# Générer toutes les tailles d'icônes iOS/Android
npm run icons

# Ajouter les plateformes
npx cap add ios
npx cap add android

# Synchroniser
npx cap sync
```

## Lancer sur iOS (simulateur)

```bash
cd /Users/th0m4zz/MailPilot/mobile
npx cap open ios
# → Xcode s'ouvre
# → Choisir "iPhone 15" dans la liste des simulateurs
# → Cliquer ▶ Run
```

## Lancer sur ton iPhone (physique)

1. Connecte ton iPhone au Mac via USB
2. Dans Xcode → Signing & Capabilities → ajouter ton Apple ID
3. Choisir ton iPhone dans la liste → cliquer ▶ Run
4. Sur iPhone → Réglages → Général → Gestion de l'appareil → faire confiance

## Publier sur l'App Store

1. Apple Developer Program : https://developer.apple.com/programs/ (99€/an)
2. Dans Xcode → Product → Archive
3. Window → Organizer → Distribute App → App Store Connect
4. Sur https://appstoreconnect.apple.com → créer la fiche de l'app

## Lancer sur Android (simulateur)

```bash
npx cap open android
# → Android Studio s'ouvre
# → Tools → AVD Manager → créer un émulateur Pixel 7
# → Cliquer ▶ Run
```

## Publier sur Google Play

1. Google Play Console : https://play.google.com/console (25$ une seule fois)
2. Dans Android Studio → Build → Generate Signed Bundle / APK
3. Créer une keystore (à garder précieusement !)
4. Upload le .aab sur Play Console

## Mise à jour de l'app

Bonne nouvelle : comme l'app charge depuis Railway, **aucune mise à jour de l'app store n'est nécessaire** quand tu modifies le backend Flask ! Les changements sont visibles immédiatement.

Tu n'as besoin de re-publier sur les stores que si tu changes :
- Le nom ou l'icône de l'app
- Les permissions natives (caméra, notifications...)
- La version Capacitor
