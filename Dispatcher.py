#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -*- coding: utf-8 -*-
"""
Scheduler Dispatcher (standalone)
---------------------------------
Lit périodiquement les fichiers de config dans LIVANA_CONFIG_DIR, 
identifie les créneaux "Selected Times" et déclenche une dispatch LiveKit
au bon moment.

- Le nom de room et le nom d'agent (affiché) suivent le schéma :
    room_{code}_{prenom}
  Nous passons aussi `agent_display_name` dans metadata pour que l'agent le connaisse.

? IMPORTANT LiveKit:
- `--agent-name` DOIT correspondre au nom d'agent enregistré côté worker (ex: "LivanaOutbound").
  On ne peut pas inventer un nom d'agent au moment du dispatch.
- On garde donc `--agent-name LivanaOutbound` (ou la valeur de l'env `LIVANA_AGENT_NAME`).

ENV attendues :
- LIVANA_CONFIG_DIR      : dossier où se trouvent les *.cfg générées (ex: /home/ubuntu/agent_wifi/configs)
- LIVANA_AGENT_NAME      : (optionnel) nom d'agent enregistré côté worker (defaut: LivanaOutbound)
- LK_BIN                 : (optionnel) binaire de la CLI LiveKit (defaut: lk)
- DISPATCH_DRY_RUN       : (optionnel) si "1", n'exécute pas la commande (log seulement)

Usage :
    python3 scheduler_dispatcher.py

Tu peux ensuite en faire un service systemd.
"""
from __future__ import annotations
import os
import re
import sys
import glob
import json
import time
import shlex
import logging
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional, Tuple

# ---------------- LOGGING ----------------
logger = logging.getLogger("scheduler")
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(handler)
logger.setLevel(logging.INFO)

# ---------------- ENV ----------------
CONFIG_DIR = os.getenv("LIVANA_CONFIG_DIR", "/home/ubuntu/agent_tel/configs")
AGENT_NAME = os.getenv("LIVANA_AGENT_NAME", "LivanaOutbound")  # Doit matcher le worker
LK_BIN = os.getenv("LK_BIN", "lk")
DRY_RUN = os.getenv("DISPATCH_DRY_RUN", "0") == "1"
TZ = ZoneInfo("Europe/Paris")

# ---------------- UTILS ----------------

def read_text_robust(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except UnicodeDecodeError:
        with open(path, "r", encoding="latin-1") as f:
            return f.read()

def parse_kv_from_cfg(text: str) -> Dict[str, str]:
    """Parse très tolérant pour le format 'Received Data:' avec lignes '    Key: Value'"""
    out: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip("\n")
        m = re.match(r"\s*([^:]+)\s*:\s*(.*)$", line)
        if m:
            key = m.group(1).strip()
            val = m.group(2).strip()
            out[key] = val
    return out

def extract_first_nonempty(d: Dict[str, str], keys: List[str]) -> Optional[str]:
    for k in keys:
        v = d.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return None

TIME_PAT = re.compile(r'(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)')

def parse_selected_times(val: Optional[str]) -> List[str]:
    """
    Robust: extrait tous les HH:MM où qu'ils soient (JSON cassé, crochets, CSV, texte).
    Retourne une liste triée et dédoublonnée.
    """
    if not val:
        return []
    # trouve toutes les heures/minutes
    pairs = TIME_PAT.findall(val)
    times = [f"{int(h):02d}:{m}" for (h, m) in pairs]  # normalise 8:05 -> 08:05
    # dédoublonne + tri
    times = sorted(sorted(set(times)), key=lambda t: (int(t.split(":")[0]), int(t.split(":")[1])))
    return times
    
# stocke les minutes déjà déclenchées par code pour éviter les doublons
already_triggered: Dict[str, str] = {}  # code -> "YYYY-MM-DD HH:MM"


def build_room_and_agent_display(code: str, prenom: str) -> str:
    safe_code = "".join(ch for ch in code if ch.isalnum() or ch in ("-", "_"))
    safe_prenom = "".join(ch for ch in prenom if ch.isalnum() or ch in ("-", "_"))
    return f"room_{safe_code}_{safe_prenom}"


def dispatch_call(room_name: str, phone: str, transfer_to: Optional[str], code: str, agent_display_name: str):
    meta = {
        "phone_number": phone,
        "transfer_to": transfer_to or "",
    }
    metadata_json = json.dumps(meta)

    cmd = [
        LK_BIN, "dispatch", "create",
        "--room", room_name,
        "--agent-name", agent_display_name,
        "--metadata", metadata_json,
    ]

    logger.info("Dispatch: %s", shlex.join(cmd))
    if DRY_RUN:
        logger.warning("DRY-RUN actif: dispatch non exécutée")
        return

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        logger.info("Dispatch OK: %s", (res.stdout or res.stderr).strip())
    except subprocess.CalledProcessError as e:
        logger.error("Dispatch ECHEC (code %s): %s\n%s", e.returncode, e.stdout, e.stderr)


def handle_config_file(path: str):
    txt = read_text_robust(path)
    kv = parse_kv_from_cfg(txt)

    # Extractions robustes
    prenom = extract_first_nonempty(kv, ["Prenom", "Prénom", "FirstName"]) or "User"
    code = extract_first_nonempty(kv, ["code", "Code", "Hardware Code"]) or infer_code_from_filename(path)

    # Téléphones
    phone = extract_first_nonempty(kv, [
        "Phone", "Phone Number", "Numero", "Numéro", "telephone", "Téléphone", "phone_number"
    ])
    transfer_to = extract_first_nonempty(kv, [
        "Transfer To", "transfer_to", "Transfer", "Numéro transfert"
    ])
    code_country = extract_first_nonempty(kv, ["code_country"]) or "+33"
    phone=code_country+phone
    times_raw = extract_first_nonempty(kv, ["Selected Times", "Heures", "Times"])
    times = parse_selected_times(times_raw)
    
    # Mode heures précises (si présent)
    mode_precis = (extract_first_nonempty(kv, ["Mode heures precises", "Mode précis", "Precise Mode"]) or "").lower()
    # pour l'instant, on déclenche exactement aux HH:MM listés; sinon on pourrait planifier différemment

    if not code:
        logger.warning("%s: code manquant -> ignore", path)
        return
    if not phone:
        logger.warning("%s: phone manquant -> ignore", path)
        return
    if not times:
        logger.info("%s: pas d'horaires ('Selected Times') -> rien à planifier", path)
        return

    now = datetime.now(TZ)
    now_key = now.strftime("%Y-%m-%d %H:%M")
    hhmm = now.strftime("%H:%M")

    if hhmm in times:
        prev_key = already_triggered.get(code)
        if prev_key == now_key:
            return  # déjà déclenché cette minute pour ce code
        room_name = build_room_and_agent_display(code, prenom)
        print(f"agent lancé chez {room_name}")
        dispatch_call(room_name=room_name, phone=phone, transfer_to=transfer_to, code=code, agent_display_name=AGENT_NAME)
        already_triggered[code] = now_key


def infer_code_from_filename(path: str) -> Optional[str]:
    base = os.path.basename(path)
    m = re.match(r"(.+)\.cfg$", base)
    if m:
        return m.group(1)
    return None

# ---------------- MAIN LOOP ----------------

def main_loop():
    logger.info("Scheduler démarré. Dossier configs: %s | Agent=%s | tz=Europe/Paris", CONFIG_DIR, AGENT_NAME)
    if not os.path.isdir(CONFIG_DIR):
        logger.error("CONFIG_DIR introuvable: %s", CONFIG_DIR)
        sys.exit(1)

    while True:
        try:
            files = glob.glob(os.path.join(CONFIG_DIR, "*.cfg"))
            for f in files:
                handle_config_file(f)
        except Exception as e:
            logger.error("boucle: %s", e)
        # granularité 10s pour attraper la minute en cours
        time.sleep(10)

if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        logger.info("Stop demandé")
