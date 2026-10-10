# Relais

Bourse aux dossards officielle pour organisateurs de courses à pied. Un coureur qui ne peut plus courir revend son dossard ; l'acheteur est vérifié ; l'argent est bloqué (autorisation PayPal) jusqu'à ce que l'organisateur ait changé le nom ; la commission est partagée entre la plateforme et l'organisateur.

Projet pour le PayPal AI Hackathon (Devpost). PayPal en **sandbox uniquement**. Repo public, licence MIT.

## Moi
Je suis seul sur le projet, à l'aise en Python, débutant en développement logiciel. Explique brièvement ce que tu fais et pourquoi, fais des petits pas, et lance les tests après chaque changement.

## Stack
- Web : FastAPI + Jinja2 + HTMX
- Base : SQLite en local, Postgres Neon en prod, via SQLModel
- PayPal serveur : paypal-server-sdk + httpx pour Payouts
- PayPal front : JS SDK v6
- IA : Claude (API Anthropic)
- Dashboard orga : AG Grid Community, chargé en script
- Hébergement : Render
- Gestion Python : uv ; tests : pytest

## Règles métier
- Circuit de l'argent : La plateforme encaisse, puis Payouts
- Commission : 8 % (5 % plateforme, 3 % organisateur), payée par l'acheteur en frais de service. Configurable par course.
- Attribution : Liste d'attente, offre au suivant
- Blocage des fonds : Dès l'achat (autorisation). Autorisation valable 29 jours, garantie 3 jours : réautoriser au-delà.
- Justificatifs : PPS ou licence FFA + nom. Fichiers supprimés après analyse, seul le verdict est stocké.
- Validation du transfert : Auto si l'IA est au vert, sinon l'orga
- Prix de revente plafonné selon le règlement de la course.
- Courses de démo : 2 ou 3 courses fictives

## Architecture
- app/transfers/ : machine à états (fonction pure), orchestre le reste
- app/marketplace/ : annonces, liste d'attente, offres
- app/payments/ : commande AUTHORIZE, autorisation, capture, annulation (void), payouts, journal comptable
- app/ai/rules_reader.py : PDF du règlement vers RaceRules (Pydantic), chaque champ avec sa citation source, null si absent
- app/ai/verifier.py : justificatif vers verdict vert / orange / rouge + explication
- app/registry_mock/ : base d'inscrits simulée de l'organisateur (lister, changer un nom)
- app/routers/ : runner.py, organizer.py, webhooks.py
- app/templates/, app/static/
- scripts/sandbox_cycle.py : cycle de paiement complet sans interface
- seed/ : règlements PDF et données de démo

## États d'un transfert
EN_VENTE → RÉSERVÉ → AUTORISÉ → VÉRIFIÉ → NOM_CHANGÉ → ENCAISSÉ → VERSÉ
Échecs : REFUSÉ (autorisation annulée, acheteur jamais débité) ; OFFRE_EXPIRÉE (remis en vente au suivant).

## Conventions
- Secrets uniquement dans .env, jamais dans le code ; .env.example tenu à jour.
- Montants en centimes entiers, jamais en float.
- Chaque appel PayPal qui crée ou modifie quelque chose envoie un header PayPal-Request-Id (idempotence).
- Chaque mouvement d'argent écrit une ligne dans le journal comptable.
- Tests obligatoires pour la machine à états et le calcul des commissions.
- Webhooks PayPal : vérifier la signature avant de traiter.

## Bonus
Agent orga branché sur PayPal Agent Toolkit (paypal-agent-toolkit) pour répondre aux questions sur les paiements, si le temps le permet.
