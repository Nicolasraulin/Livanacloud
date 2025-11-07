# -*- coding: utf-8 -*-
"""
Config Updater (standalone)
---------------------------
- Écoute Firestore (collection `codesHardware`) en continu.
- À chaque ajout/modification de document, écrit un fichier config par patient :
    <LIVANA_CONFIG_DIR>/<code>.cfg
- Robuste aux pertes réseau (reprise automatique).

ENV attendues :
- FIREBASE_CRED_PATH : chemin du JSON de service Firebase (ex: /home/ubuntu/agent_tel/assets/sophie-medias.json)
- LIVANA_CONFIG_DIR  : dossier de sortie des configs (ex: /home/ubuntu/agent_wifi/configs)

Usage :
    python3 config_updater.py

Optionnel: lancer en service systemd (voir unité proposée dans ta doc d'installation).
"""
from __future__ import annotations
import os
import sys
import time
import socket
import logging
from typing import Optional

import firebase_admin
from firebase_admin import credentials, firestore
from typing import Optional, Any  # si pas déjà importé

_watch: Optional[Any] = None
# ---------------------- LOGGING ----------------------
logger = logging.getLogger("config-updater")
handler = logging.StreamHandler(sys.stdout)
formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.INFO)

# ---------------------- ENV / PATHS ----------------------
FIREBASE_CRED_PATH = os.getenv("FIREBASE_CRED_PATH", os.path.join(os.path.dirname(__file__), "assets", "livana.json"))
CONFIG_DIR = os.getenv("LIVANA_CONFIG_DIR", "/home/ubuntu/agent_tel/configs")
os.makedirs(CONFIG_DIR, exist_ok=True)

# ---------------------- NET CHECK ----------------------
def internet_available(host="8.8.8.8", port=53, timeout=3) -> bool:
    try:
        socket.setdefaulttimeout(timeout)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
        return True
    except socket.error:
        return False

# ---------------------- FIREBASE INIT ----------------------
_init_done = False

def ensure_firebase():
    global _init_done
    if _init_done:
        return firestore.client()
    if not os.path.exists(FIREBASE_CRED_PATH):
        logger.error("FIREBASE_CRED_PATH introuvable: %s", FIREBASE_CRED_PATH)
        sys.exit(1)
    cred = credentials.Certificate(FIREBASE_CRED_PATH)
    firebase_admin.initialize_app(cred)
    _init_done = True
    return firestore.client()
# ---------------------- FORMATTER ----------------------
def has_phone_number(data: dict) -> bool:
    pn = data.get('phone_number')
    return bool(pn) and bool(str(pn).strip())

def safe_code(code: str) -> str:
    return "".join(ch for ch in str(code) if ch.isalnum() or ch in ("-", "_"))

def config_path_for_code(code: str) -> str:
    return os.path.join(CONFIG_DIR, f"{safe_code(code)}.cfg")

def format_config(data: dict) -> str:
    # Conserve les clés/français attendus par tes parseurs
    return f"""Received Data:
    Voix par defaut : {data.get('Voix par defaut')}
    Selected Times: {data.get('Selected Times')}
    Mode heures precises: {data.get('Mode heures precises')}
    Nom Livana: {data.get('Nom Livana')}
    Definition Livana: {data.get('Definition Livana')}
    Interests 3: {data.get('Interests 3')}
    Interests 2: {data.get('Interests 2')}
    Interests 1: {data.get('Interests 1')}
    Age: {data.get('Age')}
    Prenom: {data.get('Prenom')}
    Nom: {data.get('Nom')}
    PhraseDebut: {data.get('PhraseDebut')}
    PhraseFin: {data.get('PhraseFin')}
    phone_number: {data.get('phone_number')}
    code_country: {data.get('code_country')}
"""

def upsert_config_for_code(code: str, data: dict) -> str | None:
    """
    - Si phone_number présent: écrit/maj le fichier et renvoie son chemin
    - Sinon: supprime le fichier s'il existe et renvoie None
    """
    path = config_path_for_code(code)
    if has_phone_number(data):
        content = format_config(data)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info("config mise à jour: %s", path)
        return path
    else:
        if os.path.exists(path):
            os.remove(path)
            logger.info("config supprimée (pas de phone_number): %s", path)
        else:
            logger.info("config ignorée (pas de phone_number) pour code=%s", code)
        return None


# ---------------------- BOOTSTRAP (one-shot sync) ----------------------
def bootstrap_snapshot(db) -> int:
    """Écrit toutes les configs existantes au démarrage (one-shot)."""
    count = 0
    try:
        docs = db.collection('codesHardware').stream()
        for d in docs:
            data = d.to_dict() or {}
            code = data.get('code')
            if not code:
                continue
            if upsert_config_for_code(code, data):
                count += 1
    except Exception as e:
        logger.error("Erreur snapshot initial: %s", e)
    return count


# ---------------------- LISTENER ----------------------
def start_collection_listener(db):
    global _watch
    if _watch is not None:
        logger.info("listener déjà actif")
        return

    def on_snapshot(col_snapshot, changes, read_time):
        for change in changes:
            try:
                data = change.document.to_dict() or {}
                code = data.get('code')
                if not code:
                    continue
                if change.type.name in ("ADDED", "MODIFIED"):
                    upsert_config_for_code(code, data)
            except Exception as e:
                logger.error("Erreur traitement change: %s", e)

    query = db.collection('codesHardware')
    _watch = query.on_snapshot(on_snapshot)   # <-- important
    logger.info("listener Firestore démarré sur collection 'codesHardware'")


# ---------------------- MAIN LOOP ----------------------
def main():
    logger.info("Config Updater démarre. DIR=%s", CONFIG_DIR)
    while True:
        if not internet_available():
            logger.warning("internet indisponible, retry dans 5s…")
            time.sleep(5)
            continue
        try:
            db = ensure_firebase()
            n = bootstrap_snapshot(db)
            logger.info("snapshot initial: %d configs écrites", n)
            start_collection_listener(db)
            # boucle dormante tant que tout va bien
            while internet_available():
                time.sleep(30)
            logger.warning("perte réseau, tentative de reconnexion…")
            # tomber dans l'exception pour relancer proprement
            raise RuntimeError("network down")
        except Exception as e:
            logger.error("boucle principale: %s", e)
            # cleanup listener si besoin
            global _watch
            try:
                if _watch is not None:
                    _watch.unsubscribe()
            except Exception:
                pass
            _watch = None
            time.sleep(5)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Interruption demandée, sortie…")
