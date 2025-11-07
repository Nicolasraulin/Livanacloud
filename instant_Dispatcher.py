#!/usr/bin/env python3
# coding: utf-8


import os
import json
import logging
import shlex
import subprocess
import sys
from typing import Optional, Dict
from datetime import datetime
import logging
import numpy as np
import firebase_admin
from firebase_admin import credentials, db


# ---------- LOGGING ----------
logger = logging.getLogger("fb-dispatch")
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(handler)
logger.setLevel(logging.INFO)

# ---------- ENV (modifiable) ----------
NOTIF_PATH = os.getenv("LIVANA_FIREBASE_CRED", os.path.join("assets", "livana.json"))
# Si tu utilises plusieurs apps firebase, il faudrait passer databaseURL ici :
# firebase_admin.initialize_app(cred, {'databaseURL': 'https://<YOUR_DB>.firebaseio.com'})
LK_BIN = os.getenv("LK_BIN", "lk")
AGENT_NAME = os.getenv("LIVANA_AGENT_NAME", "LivanaOutbound")
DRY_RUN = os.getenv("DISPATCH_DRY_RUN", "0") == "1"

# ---------- Firebase init ----------
cred = credentials.Certificate(NOTIF_PATH)
try:
    firebase_admin.initialize_app(cred, {
    'databaseURL': 'https://livana-9daae-default-rtdb.firebaseio.com/'
})

except ValueError:
    # déjà initialisé
    pass

ref = db.reference('calls/')

# conserve les calls déjà lancés pour éviter doublons
active_calls = set()




logger = logging.getLogger("dispatcher")

def update_call_status(hardware_code: str, call_id: str, status: str, reason: str | None = None):
    if not hardware_code or not call_id:
        logger.warning("update_call_status appele sans hardware_code ou call_id (%r, %r)", hardware_code, call_id)
        return False

    ref_path = f"calls/{hardware_code}-appel_agent/{call_id}"
    ref = db.reference(ref_path)

    payload = {
        "status": status,
        "updated_at": datetime.utcnow().isoformat(),
    }

    if reason:
        payload["reason"] = reason

    try:
        ref.update(payload)
        logger.info(" Status de %s/%s mis a jour -> %s", hardware_code, call_id, status)
        return True
    except Exception as e:
        logger.exception("? Erreur mise a jour status %s/%s: %s", hardware_code, call_id, e)
        return False




def build_room_and_agent_display(code: str, prenom: str) -> str:
    safe_code = "".join(ch for ch in (code or "") if ch.isalnum() or ch in ("-", "_"))
    safe_prenom = "".join(ch for ch in (prenom or "") if ch.isalnum() or ch in ("-", "_"))
    if not safe_code:
        safe_code = "nocode"
    if not safe_prenom:
        safe_prenom = "nouser"
    return f"room_{safe_code}_{safe_prenom}"

def dispatch_create(room_name: str, phone: str, transfer_to: Optional[str], code: str, agent_display_name: str,call_id):
    meta = {
        "phone_number": phone or "",
        "transfer_to": transfer_to or "",
        "code": code or "",
        "call_id": call_id or "",
    }
    metadata_json = json.dumps(meta)

    cmd = [
        LK_BIN, "dispatch", "create",
        "--room", room_name,
        "--agent-name", agent_display_name,
        "--metadata", metadata_json,
    ]
    logger.info("Dispatch command: %s", shlex.join(cmd))
    if DRY_RUN:
        logger.warning("non execute")
        return
    hardware_code=code
    # lance en asynchrone pour ne pas bloquer le listener
    try:
        update_call_status(hardware_code, call_id, "agent-busy")
    
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        out, err = p.communicate(timeout=15)
    
        if p.returncode == 0:
            logger.info("Dispatch OK: %s", (out or err).strip())
    
            update_call_status(hardware_code, call_id, "en-cours")
    
        else:
            logger.error("Dispatch ECHEC (%s): %s", p.returncode, (out or err).strip())
    
            update_call_status(hardware_code, call_id, "error", reason=f"returncode={p.returncode}")
    
    except subprocess.TimeoutExpired:
        logger.info("Dispatch lance (processus en arridre-plan).")
        update_call_status(hardware_code, call_id, "appel-done")
    
    except FileNotFoundError:
        logger.error("Binaire lk introuvable: %s. Verifie LK_BIN ou installe la CLI LiveKit.", LK_BIN)
        update_call_status(hardware_code, call_id, "error", reason="lk not found")
    
    except Exception as e:
        logger.exception("Erreur lors du dispatch: %s", e)
        update_call_status(hardware_code, call_id, "error", reason=str(e))


def listener(event):
   
    logger.debug("FB event: path=%s data=%r", event.path, event.data)

    path = (event.path or "").strip("/")
    if path == "" or path == "/":
        if isinstance(event.data, dict):
            # parcourir récursivement : chercher toutes les call_id leaves
            for maybe_agent, subtree in event.data.items():
                if isinstance(subtree, dict):
                    for call_id, node in subtree.items():
                        _handle_call_node(call_id, node, agent_node=maybe_agent)
                else:
                    # cas improbable : treat maybe_agent as call_id
                    _handle_call_node(maybe_agent, subtree, agent_node=None)
        return

    parts = path.split("/")  # ex: ["HF00GP-appel_agent", "1761751000161", "status"]
    # On suppose structure calls/<agent_node>/<call_id>/...
    agent_node = parts[0] if len(parts) >= 1 else None
    call_id = parts[1] if len(parts) >= 2 else None

    # si path est plus court, tente de retrouver call_id différemment
    if call_id is None:
        # fallback : last numeric-like part
        for p in reversed(parts):
            if p.isdigit() or len(p) > 6:
                call_id = p
                break
    if call_id is None:
        # ultime fallback
        call_id = parts[-1]

    raw = event.data

    # si on a reçu une string (update direct sur /status)
    if isinstance(raw, str):
        status = raw
        try:
            full_node = db.reference(f"calls/{agent_node}/{call_id}").get() if agent_node else db.reference(f"calls/{call_id}").get()
        except Exception:
            full_node = None
        _handle_call_node(call_id, full_node or {"status": status}, agent_node=agent_node)
        return

    if isinstance(raw, dict):
        # status peut être au niveau racine (calls/.../<call_id>/status) ou dans la dict reçue
        status = raw.get("status") or raw.get("statut")
        if status is not None:
            # raw contient status (node partielle ou entière)
            _handle_call_node(call_id, raw, agent_node=agent_node)
            return

        # sinon, raw peut être le contenu de Data ou de la node entière sans status
        # re-get la node complète pour être sûr
        try:
            full_node = db.reference(f"calls/{agent_node}/{call_id}").get() if agent_node else db.reference(f"calls/{call_id}").get()
        except Exception:
            full_node = None
        _handle_call_node(call_id, full_node or raw, agent_node=agent_node)
        return

    # autres types inattendus
    logger.debug("listener: type inattendu pour event.data: %s", type(raw))

def _handle_call_node(call_id: str, node: dict, agent_node: Optional[str]=None):
    if not node:
        logger.debug("_handle_call_node: node vide pour %s", call_id)
        return

    # extraire status au niveau racine (PAS dans Data)
    status = None
    if isinstance(node, dict):
        status = node.get("status") or node.get("statut")

    # logging sûr
    logger.info("_handle_call_node: call_id=%s status=%r agent_node=%r", call_id, status, agent_node)

    if status == "incoming" and call_id not in active_calls:
        # récupération tolérante des champs utiles (pouvant être dans node ou node.get('Data'))
        def _get_field(klist):
            for k in klist:
                if isinstance(node, dict) and k in node and node[k]:
                    return node[k]
            # fallback : chercher dans Data si présent
            data_block = node.get("Data") if isinstance(node, dict) else None
            if isinstance(data_block, dict):
                for k in klist:
                    if k in data_block and data_block[k]:
                        return data_block[k]
            return None

        prenom = _get_field(["Prenom", "prenom", "FirstName"])
        nom = _get_field(["Nom", "name"])
        code = _get_field(["code", "Code", "hardwareCode", "HardwareCode"])
        phone = _get_field(["phone_number"])
        code_country = _get_field(["code_country"])
        transfer_to = _get_field(["transfer_to", "Transfer To"])
        phone = code_country + phone
        print(phone)
        room_name = build_room_and_agent_display(code or call_id, prenom or nom or "User")
        agent_display_name = AGENT_NAME

        active_calls.add(call_id)
        try:
            dispatch_create(room_name=room_name, phone=phone, transfer_to=transfer_to, code=code or call_id, agent_display_name=agent_display_name,call_id=call_id)
            print(f"CALL LANCE POUR {phone}")
        except Exception:
            logger.exception("Erreur dispatch pour %s", call_id)
            active_calls.discard(call_id)

    elif status and str(status).lower() in ("ended", "finished", "cancelled", "completed", "closed"):
        if call_id in active_calls:
            logger.info("Call %s termine (%s) -> nettoyage", call_id, status)
            active_calls.discard(call_id)


# ---------- start ----------
def start_listener():
    logger.info("Demarrage du listener Firebase sur /calls/")
    ref.listen(listener)


if __name__ == "__main__":
    start_listener()
