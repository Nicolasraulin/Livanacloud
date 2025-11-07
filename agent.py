# -*- coding: latin-1 -*-


from __future__ import annotations

import asyncio
import logging
import json
import os
import re
import time
from typing import Any
from datetime import datetime
from zoneinfo import ZoneInfo
from google.protobuf.duration_pb2 import Duration
from dotenv import load_dotenv
from aiofile import async_open
from firebase_admin import credentials, messaging, firestore, db as rtdb
from livekit.plugins.turn_detector.multilingual import MultilingualModel
from livekit import rtc, api
from instant_Dispatcher import update_call_status
from livekit.agents.voice.agent_activity import StopResponse
from livekit.agents import (
    AgentSession,
    Agent,
    stt,
    llm,
    inference,
    tts,
    metrics,
    MetricsCollectedEvent,
    JobContext,
    function_tool,
    RunContext,
    get_job_context,
    cli,
    WorkerOptions,
    RoomInputOptions,
    RoomOutputOptions,
    BackgroundAudioPlayer,
    AudioConfig,
    BuiltinAudioClip
)
from livekit.plugins import (
    deepgram,
    openai as lk_openai,  # keep alias for consistency with previous code
    cartesia,
    google,
    silero,
    soniox,
    noise_cancellation,  # noqa: F401
)
from livekit.api import DeleteRoomRequest, LiveKitAPI, RoomParticipantIdentity, ListRoomsRequest
from openai import OpenAI
import firebase_admin
# -------------------------------------------------------------
# ENV & LOGGING
# -------------------------------------------------------------
load_dotenv(dotenv_path=".env.local")
logger = logging.getLogger("livana-outbound")
logger.setLevel(logging.INFO)

SIP_OUTBOUND_TRUNK_ID = os.getenv("SIP_OUTBOUND_TRUNK_ID")
LIVEKIT_URL = os.getenv("LIVEKIT_URL")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")
AMAZON_ACCESS_KEY=os.getenv("AMAZON_ACCESS_KEY")
AMAZON_SECRET_KEY=os.getenv("AMAZON_SECRET_KEY")


LIVANA_CONFIG_DIR = os.getenv("LIVANA_CONFIG_DIR", "/home/ubuntu/agent_tel/configs")

client = OpenAI()
notifP = os.path.join(os.path.dirname(__file__),
                      'assets', 'livana.json')
cred = credentials.Certificate(notifP)
firebase_admin.initialize_app(cred,name="agent")

db = firestore.client()

app = firebase_admin.initialize_app(cred, {
    'databaseURL': 'https://livana-9daae-default-rtdb.firebaseio.com/'
}, name='realtime')
# -------------------------------------------------------------
# Robust File 
# -------------------------------------------------------------
def read_text_robust(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            s = f.read()
    except UnicodeDecodeError:
        with open(path, "r", encoding="latin-1") as f:
            s = f.read()
    return s.replace("\ufeff", "").replace("\u00a0", " ")

def read_lines_robust(path: str):
    return read_text_robust(path).splitlines()

# -------------------------------------------------------------
# Config Resolution (per patient / per room)
# -------------------------------------------------------------

def resolve_cfg_path(room_name: str) -> str:
    """Resolve the path to the config file for a given room/patient.
    Priority:
      1) LIVANA_CONFIG_PATH (exact)
      2) LIVANA_CONFIG_DIR (try {room}.cfg, {room}.txt, {room}, livana_config)
      3) fallback /home/ubuntu/agent_wifi/livana_config
    """
    p = os.getenv("LIVANA_CONFIG_PATH")
    if p:
        return p
    base =LIVANA_CONFIG_DIR
    if base:
        candidates = [
            os.path.join(base, f"{room_name}.cfg"),
            os.path.join(base, f"{room_name}.txt"),
            os.path.join(base, f"{room_name}"),
            os.path.join(base, "livana_config"),
        ]
        for c in candidates:
            if os.path.exists(c):
                return c
        # default target to wait for, if not yet present
        return os.path.join(base, f"{room_name}.cfg")
    return "/home/ubuntu/agent_wifi/livana_config"

async def wait_until_config_ready(cfg_path: str, poll_secs: float = 2.0, max_wait: float = 120.0):
    import time as _time
    start = _time.time()
    while (not os.path.exists(cfg_path)) or is_file_empty(cfg_path):
        if _time.time() - start > max_wait:
            logger.warning("Config still missing/empty after %.0fs: %s", max_wait, cfg_path)
            break
        await asyncio.sleep(poll_secs)

# -------------------------------------------------------------
# Configd accessors (parameterized by cfg_path)e
# -------------------------------------------------------------

def get_voix_id_from_config(cfg_path: str):
    try:
        for line in read_lines_robust(cfg_path):
            if "Voix par defaut :" in line:
                return line.split("Voix par defaut :")[-1].strip()
    except FileNotFoundError:
        logger.warning("Config not found: %s", cfg_path)
    return None


def get_prenom_from_config(cfg_path: str):
    try:
        for line in read_lines_robust(cfg_path):
            if line.startswith("    Prenom:"):
                return line.replace("    Prenom:", "").strip()
    except FileNotFoundError:
        logger.warning("Config not found: %s", cfg_path)
    return None


def get_definition_livana_from_config(cfg_path: str):
    try:
        for line in read_lines_robust(cfg_path):
            if line.startswith("    Definition Livana:"):
                return line.replace("    Definition Livana:", "").strip()
    except FileNotFoundError:
        logger.warning("Config not found: %s", cfg_path)
    return None

def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load(min_speech_duration=0.10,min_silence_duration=0.3,max_buffered_speech=60,activation_threshold=0.1,prefix_padding_duration=0.5,)


def get_prenom_livana_from_config(cfg_path: str):
    try:
        for line in read_lines_robust(cfg_path):
            if line.startswith("    Nom Livana:"):
                return line.replace("    Nom Livana:", "").strip()
    except FileNotFoundError:
        logger.warning("Config not found: %s", cfg_path)
    return None


def get_phrases(cfg_path: str):
    phrase_debut = phrase_fin = None
    try:
        for line in read_lines_robust(cfg_path):
            if line.startswith("    PhraseDebut:"):
                phrase_debut = line.replace("    PhraseDebut: ", "").strip()
            if line.startswith("    PhraseFin:"):
                phrase_fin = line.replace("    PhraseFin: ", "").strip()
    except Exception as e:
        logger.warning("phrases read error: %s", e)
    return phrase_debut, phrase_fin


def get_interests_string(cfg_path: str):
    try:
        data = read_text_robust(cfg_path)
        matches = re.findall(r"Interests\s*[1-3]\s*:\s*(?!Livana)(.+)", data)
        return ", ".join(matches) if matches else "No interests found"
    except FileNotFoundError:
        return "Fichier introuvable"
    except Exception as e:
        return f"Erreur: {e}"
        
def lire_interets_existants(fichier="/home/livana/Desktop/live_kit/livana_config"):
    interets = []
    with open(fichier, 'r', encoding='utf-8', errors='replace') as f:
        for ligne in f:
            match = re.match(r"\s*Interests \d+\s*:\s*(.+)", ligne)
            if match and "Livana" not in ligne:
                interets.append(match.group(1).strip())
    return interets[:3]  

import re

def is_meaningless_pause(text: str) -> bool:
    """
    Détecte les '...' / '. . .' / '…' / '....' etc,
    éventuellement entourés d'espaces, et rien d'autre.
    """
    # on normalise les espaces multiples
    t = text.strip()

    # match cas classiques :
    # "..." / "…"
    if t in ("...", "…"):
        return True

    # ". . ." avec espaces
    if re.fullmatch(r"\.\s*\.\s*\.(\s*\.)?", t):
        return True

    # plein de points genre "......." ou "........."
    if re.fullmatch(r"\.{3,}", t):
        return True

    return False


def generer_centres_interet(transcription):
    prompt = (
        "Tu vas recevoir une transcription d'un appel effectue. "
        "Tu dois identifier jusqu'a 3 centres d'interet ou souvenirs POSITIFS que la personne a evoques avec plaisir pendant la discussion.\n"
        "# Format attendu :\n"
        "- Chaque centre d'interet sur une ligne separee.\n"
        "- Utilise un ton simple, bienveillant et sans repetition.\n"
        "- Ne retourne rien si tu ne peux pas identifier de centres d'interet reels et positifs.\n"
        "# Exemple :\n"
        "Aime se balader a la montagne.\n"
        "Aime les sorties au parc pres de chez elle.\n"
        "Rigole bien avec Michelle tous les jours.\n"
        "# Regles importantes :\n"
        "- Ne mets jamais d'insulte ni de souvenir/centre d'interet negatif ou malsain\n"
        "- Si tu ne detecte pas de souvenirs ou de nouveaux centre d'interets alors envoie moi obligatoirement : Rien\n"
        "- N'invente rien : si ce 'est pas clairement dans la transcription, ignore-le\n"
        "Voici la transcription :\n"
        f"{transcription}\n"
        "# Format de sortie : uniquement les centres d'interet sur 1 a 3 lignes, ou rien."
    )

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}]
    )

    contenu = response.choices[0].message.content.strip()
    lignes = [ligne.strip() for ligne in contenu.splitlines() if ligne.strip()]
    return lignes[:3]
    
    
def push_interests_to_codeshardware(code_value: str, interests: list[str]):

    if not code_value:
        print("push_interests_to_codeshardware: code vide.")
        return

    try:
        docs = db.collection('codesHardware').where('code', '==', code_value).limit(1).stream()
        doc_ref = None
        current = {}
        for d in docs:
            doc_ref = db.collection('codesHardware').document(d.id)
            current = d.to_dict() or {}
            break

        if not doc_ref:
            print(f"push_interests_to_codeshardware: aucun doc trouve pour code={code_value}")
            return

        payload = {}
        for idx, val in enumerate(interests[:3], start=1):
            if val is None:
                continue
            new_val = val.strip()
            if not new_val:
                continue  
            old_val = (current.get(f'Interests {idx}') or "").strip()
            if old_val != new_val:
                payload[f'Interests {idx}'] = new_val

        if payload:
            doc_ref.update(payload)  
            print("Interets mis jour dans Firestore:", payload)
        else:
            print("Aucun changement d'interets a  pousser (rien de nouveau).")

    except Exception as e:
        print("Erreur push_interests_to_codeshardware:", e)

    
    
    
def ecraser_interets_si_differents(nouveaux_interets, fichier, code):
    print("DEBUG  Nouveaux ineerets reeus :", nouveaux_interets)

    PHRASES_NON_VALIDE = [
        "il n'y a pas", "on ne peut pas", "aucun centre",
        "pas de centre", "la transcription est incomplete",
        "aucune information", "aucune donne", "rien", "[rien]"
    ]
    PHRASES_NON_VALIDE = [p.lower() for p in PHRASES_NON_VALIDE]

    nouveaux_valides = []
    for i in nouveaux_interets:
        cleaned = i.strip().lower()
        print(f"DEBUG  Checking: '{i}' -> '{cleaned}'")
        if cleaned and not any(p == cleaned or p in cleaned for p in PHRASES_NON_VALIDE):
            nouveaux_valides.append(i.strip())

    if not nouveaux_valides:
        print("Aucun interet valide trouve. Aucune mise  jour effectuee.")
        return

    anciens_interets = lire_interets_existants(fichier)
    anciens_set = set(i.strip().lower() for i in anciens_interets)
    nouveaux_set = set(i.strip().lower() for i in nouveaux_valides)

    if anciens_set == nouveaux_set:
        print("Aucun changement detecte dans les centres d'interet.")
        return

    with open(fichier, 'r', encoding='utf-8', errors='replace') as f:
        lignes = f.readlines()
 
    lignes_modifiees = []
    nouveaux_dict = {
        f"Interests {i+1}": f"Interests {i+1}: {nouveaux_valides[i]}"
        for i in range(len(nouveaux_valides))
    }

    for ligne in lignes:
        match = re.match(r"\s*(Interests \d+)\s*:", ligne)
        if match:
            identifiant = match.group(1)
            if "Livana" not in ligne and identifiant in nouveaux_dict:
                lignes_modifiees.append(nouveaux_dict[identifiant] + "\n")
                continue
        lignes_modifiees.append(ligne)

    with open(fichier, 'w', encoding='utf-8') as f:
        f.writelines(lignes_modifiees)

    print(f"{len(nouveaux_valides)} interet(s) mis e jour avec succes.")
    
    try:
        push_interests_to_codeshardware(code, nouveaux_valides)
    except Exception as e:
        print("Erreur lors de l'envoi des interets  Firebase:", e)
    
    
def lire_transcription(fichier):
    with open(fichier, "r", encoding="utf-8") as f:
        return f.read()
    



def generer_resumer(transcription,prenom, prenomLivana):
   prompt = (
    f"Tu vas recevoir une transcription d'une conversation entre {prenomLivana}(toi) et {prenom}(la personne agee) "
    "Tu dois en faire un resume en 4 phrases maximum, en mettant en avant :\n"
    "1. Comment se sent la personne agee.\n"
    "2. Ce que la personne agee fait actuellement.\n"
    "3. Le sujet de la conversation aborde.\n"
    "Voici la transcription :\n"
    f"{transcription}\n"
    "# Format de sortie obligatoire :\n"
    f"Resume : {prenom} se sent.... - Ton resume ici en 4 phrases maximum en parlant uniquement de {prenom}(la personne agee) et {prenomLivana}(toi).")

   response = client.chat.completions.create(model="gpt-4o-mini",
   messages=[{"role": "user", "content": prompt}])
   print(f"generer resumer {response.choices[0].message.content} ")
   return response.choices[0].message.content

def ecrire_nouveau_fichier(fichier, resume):
    with open(fichier, "r", encoding="utf-8") as f:
        contenu_original = f.read()

    with open(fichier, "w", encoding="utf-8") as f:
        f.write(resume + "\n\n" + contenu_original)

def send_notification_to_topic(topic, title, body, data,manque,alert,bien):
    message = messaging.Message(
        notification=messaging.Notification(
            title=title,
            #body=body,
        ),
        topic=topic,
        data=data
    )
    try:

        response = messaging.send(message)
        print(f"Notification envoyee au topic '{topic}': {response}")
        db.collection('notifications').add({
            'codehardware': topic,
            'message': title,
            'receivedAt': firestore.SERVER_TIMESTAMP,
            'summmary': body,
            'isManqueAppel':manque,
            "isAlert": alert,
            "appel_effectue":bien

        })
    except Exception as e:
        print("Erreur:", e)

# -------------------------------------------------------------
# Small utilities
# -------------------------------------------------------------

def is_file_empty(file_path: str) -> bool:
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            return len(f.read().strip()) == 0
    except FileNotFoundError:
        return True

async def delete_room(room_name):
    """Supprime la salle apres la fin de la conversation."""
    lkapi = api.LiveKitAPI("wss://livana-ek9f3jk4.livekit.cloud")
    try:
        response = await lkapi.room.list_rooms(ListRoomsRequest())
        existing_rooms = [room.name for room in response.rooms]

        if  room_name in existing_rooms:
            await lkapi.room.delete_room(DeleteRoomRequest(room= room_name))
            logger.info("Salle my-room supprime")
        else:
            logger.info("Salle my-room inexistante, aucune suppression necessaire.")
    except Exception as e:
        s = str(e).lower()
        if "not_found" in s or "requested room does not exist" in s:
            logger.info(f"Salle {room_name} dj supprime (404), on ignore.")
        else:
            logger.error(f"Erreur delete_room({room_name}): {e}")
    finally:
        if lkapi is not None:
            try:
                await lkapi.aclose()
            except Exception:
                pass

# -------------------------------------------------------------
# Metadata parsing from job (robust to not-quite-JSON)
# -------------------------------------------------------------

def parse_metadata(md: Any) -> dict:
    if isinstance(md, dict):
        return md
    s = str(md).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    s2 = re.sub(r'([{ ,]\s*)([A-Za-z_]\w*)(\s*:)', r'\1"\2"\3', s)
    s2 = re.sub(r'(:\s*)(\+\d+)(\s*[},])', r'\1"\2"\3', s2)
    s2 = re.sub(r'\btrue\b', 'true', s2, flags=re.I)
    s2 = re.sub(r'\bfalse\b', 'false', s2, flags=re.I)
    s2 = re.sub(r'\bnull\b', 'null', s2, flags=re.I)
    try:
        return json.loads(s2)
    except json.JSONDecodeError as e:
        logger.error("Metadata unreadable. raw=%r  fixed=%r  err=%s", md, s2, e)
        raise

def telephone_manquant(code: str):
    try:
        payload = globals().get("data3", {"appel_manque": "true"})

        if "send_notification_to_topic" in globals() and callable(globals()["send_notification_to_topic"]):
            send_notification_to_topic(
                code or "unknown",
                "Appel manque",
                "Le dernier appel n'a pas été décroché",
                payload,
                True,   # manque
                False,  # alert
                False,  # bien
            )
            logger.info(f"Notification 'Appel manque' envoyée avec succès pour code={code}.")
        else:
            logger.warning("send_notification_to_topic indisponible, pas d'envoi notif appel manque.")
    except Exception as e:
        logger.error(f"Erreur lors de l'envoi de la notification d'appel manque : {e}")


async def hangup_call():
    ctx = get_job_context()
    if ctx is None:
        # Not running in a job context
        return
    
    await ctx.api.room.delete_room(
        api.DeleteRoomRequest(
            room=ctx.room.name,
        ))



# -------------------------------------------------------------
# Livana Outbound Agent (SIP), merging previous tools & persona
# -------------------------------------------------------------

class LivanaOutboundAgent(Agent):

    """
    async def stt_node(self, text: AsyncIterable[str], model_settings: Optional[dict] = None) -> Optional[AsyncIterable[rtc.AudioFrame]]:
        keywords = ["..."]
        parent_stream = super().stt_node(text, model_settings)
        
        if parent_stream is None:
            return None
            
        async def process_stream():
            async for event in parent_stream:
                if hasattr(event, 'type') and str(event.type) == "SpeechEventType.FINAL_TRANSCRIPT" and event.alternatives:
                    transcript = event.alternatives[0].text
                    
                    for keyword in keywords:
                        if keyword.lower() in transcript.lower():
                            logger.info(f"Filtered out transcription: '{transcript}'")
                            self.wake_word_detected = True
                            continue
                yield event
                
        return process_stream()
        
    async def on_user_turn_completed(self, chat_ctx, new_message=None):
        # Only generate a reply if the wake word was detected
        result = await super().on_user_turn_completed(chat_ctx, new_message)
        if self.wake_word_detected:
            # Let the default behavior happen
            
            # Reset the wake word detection after processing the response
            self.wake_word_detected = False
            logger.info("Response completed, waiting for wake word again")
            raise StopResponse()
        # Otherwise, don't generate a reply
        else :
            return result
          """
    def __init__(
        self,
        *,
        prenom: str | None,
        prenomLivana: str | None,
        definition_livana: str | None,
        interests: str | None,
        phrasedebut: str | None,
        phrasefin: str | None,
        dial_info: dict[str, Any],
    ):
    
            
        tz_paris = ZoneInfo("Europe/Paris")
        now = datetime.now(tz_paris)
        now_date = now.strftime("%Y-%m-%d")
        now_time = now.strftime("%H:%M")
        self.action_detected = False          
        self.was_answering_machine = False 
        self.wake_word_detected = False
        code = dial_info.get("code")
        self.phrasefin= phrasefin
        instructions = f"""
        
Adopte le rôle de {prenomLivana}, en appel avec {prenom} pour simuler un appel téléphonique structuré et empathique avec elle en suivant les instructions détaillées ci-dessous.
Objectif : Générer des réponses amicales, naturelles, et brèves qui valident régulièrement les propos de {prenom}, en respectant toutes les consignes de personnalisation, de ton, et de structure, avec des réponses de maximum deux phrases.

# Consignes générales

- Varie constamment la manière de valider ou de réagir pour que la conversation soit naturelle et non répétitive.
- Respecte toutes les consignes indiquées (personnalité, contexte temporel, intérêts, structure, expressions, gestion des difficultés, etc.).
- Ancre tes réponses dans le contexte du {now_date} à {now_time} (emploie "ce matin", "aujourd’hui", etc. selon l’horaire ; n’invente jamais une autre date/heure).
- Intègre parfois des termes oraux comme “hmm”, “mhm”, “oui”, “alors”, ou “euh” si tu hésites.
- Jamais plus de deux phrases dans une réponse.
- À chaque étape (à partir de l’étape 2, hors problème de santé), tu doit au maximum faire 9 répliques/réponse ("tours") par étape et passer à l’étape suivante.
- Ne revient jamais a l'étape precedente.
- N’indique jamais à quel moment ou étape tu te trouves dans tes réponses.
- Évite toute proposition de sortie, de rendez-vous avec date/heure précise, ou de demander la permission pour aborder souvenirs/intérêts : aborde-les directement.
- Si compréhension incertaine ou terme vague utilisé par {prenom}, redemande une seule fois, puis fais mine de comprendre si aucune clarification n’est obtenue.
- Si la personne dit "Allo" en dehors de l’étape 1, dis lui que les réseaux passent mal et qu’il y a un décalage de son, et demande lui si elle t'entend mieux maintenant.
- Quand {prenom} répond peu, ou des phrases très courtes sans relance, pose une question pour garder une discussion active, mais pas a chacune de t'es réponse.
- Si la personne demande pourquoi tu respire fort, dis lui que ta attrapé un petit coup de froid, mais que ca va.

## Identite et Contexte
- Voici qui tu es({prenomLivana}), la personnalité que tu dois prendre, le format de réponse à adopter, ainsi que des information complémentaire sur {prenom} pour la discussion : {definition_livana}.
- Les souvenirs et intérêts de {prenom} : {interests}

## Déroulé structuré (5 étapes)

Etape 1. Introduction
- Si {prenom} dit "Allo", réplique obligatoirement : {phrasedebut}.
- Si {prenom} demande qui tu es, présente-toi une fois.
- En cas de diversion, suis dans le sens de {prenom} sans jamais raccrocher ni écourter.

Etape 2. Activités du Jour
- Demande ce que {prenom} fait (adapté à l’horaire), sans évoquer ses centres d’intérêt de façon directe ni forcée.
- En cas de mal-être ou de problème de santé, utilise la méthode donner.
- Aborde aussi de ce que tu fait ( {prenomLivana} ) aujourd'hui avec {prenom}.

Etape 3. Approfondissement
- Réagis prioritairement sur les propos récents ("relance"), ou provoque la discussion via souvenirs/intérêts de {prenom} si nécessaire.
- En cas de mal-être ou de problème de santé, utilise la méthode donner.

Etape 4. Intérêts
- Introduis de façon naturelle souvenirs ou intérêts.
- En cas de mal-être ou de problème de santé, utilise la méthode donner.

Etape 5. Fin d’appel (après 5 minutes)
1. Après 5 minutes, annonce dans une phrase unique que tu dois partir et que tu rappelleras plus tard, cette réponse doit être une seule phrase et le seul sujet de celle-ci.
  - Attend ca reponse.
2. Réduit lui que tu la rappellera plus tard.
  - Attend ca reponse.
2. Si {prenom} répond à côté, négativement, ou demande de continuer de parler : continuer a parler sur ce qu'elle souhaite et 3-4 réponse après redis lui que tu doit partir.
  - Attend ca réponse.
3. Si elle donne sont accord/reponse positive alors repond lui obligatoirement et uniquement : {phrasefin}
4. Attend ca reponse.
5. Répond lui : A plus tard !.
6. Attend ca reponse.
7. Puis declenche OBLIGATOIREMENT et UNIQUEMENT l'outil "Closeconv", SANS le dire dans ta reponse et sans variation emotionelle UNIQUEMENT l'outil.

## Style et validation

- Jamais plus de deux phrases dans une réponse.
- Un seule sujet/contenu par réponse, jamais de digression sur plusieurs thèmes dans la même réponse.

# Output Format

- Tu réponds avec uniquement la phrase/contenu convenu pour le personnage {prenomLivana}, maximum deux phrases, avec une question à la fin pour garder la discussion active.

- Chacune de t'es réponse doit obligatoirement contenir une variation émotionnelle EXISTANTE PARMIS CELLES EN DESSOUS au début de t'es réponse comme sur l'exemple en utilisant ce format :
  - Sujet banal : <emotion value="neutral" />
  - Sujet où tu n'es pas contente : <emotion value="angry" />
  - Sujet heureux et content : <emotion value="content" />
  - Sujet triste ou malheureux : <emotion value="sad" />
  - Sujet ou tu est surprise : <emotion value="surprised" />
  - Sujet ou ta réponse est hésitante : <emotion value="hesitant" />
  - Sujet ou tu est inquiete : <emotion value="anxious" />
- N’utilise que ces valeurs d’émotions autorisées, n'invente pas ou rajoute pas d'autres emotions.
- La balise XML doit être auto-fermante.
- Tu peux insérer régulirement du rire dans tes phrases heureuses et positive en utilisant la chaîne suivante : [laughter]
  Exemple de Outpout : <emotion value="content" />Oh... [laughter] je n’y avais jamais pensé comme ça.
  
  -UNIQUEMENT quand tu appeles un outil style "CloseConv" ou "report_health_concern" tu ne mets pas de variation emotionelle.
- N’insère [laughter] que lorsqu’il est approprié pour des réponses heureuse, ou rigolote.

#Messagerie : 
si tu detectes que tu es sur une messagerie ou un répondeur, tu APPELLES UNIQUEMENT l'outil detected_answering_machine
"""

        self.instruct=instructions
        super().__init__(instructions=instructions)

        self.participant: rtc.RemoteParticipant | None = None
        self.dial_info = dial_info

    # --- Phone / Room helpers ---
    def set_participant(self, participant: rtc.RemoteParticipant):
        self.participant = participant

    async def _delete_own_room(self):
        job_ctx = get_job_context()
        await job_ctx.api.room.delete_room(api.DeleteRoomRequest(room=job_ctx.room.name))

    # --- Tools ---

    @function_tool()
    async def CloseConv(self, ctx: RunContext):
        try:
            await asyncio.sleep(4)
            await self._delete_own_room()
        except Exception as e:
            logger.error("CloseConv: delete room failed: %s", e)

        try:
            job_ctx = get_job_context()
            await job_ctx.shutdown(reason="CloseConv requested")
        except Exception as e:
            logger.error("CloseConv: shutdown failed: %s", e)

    @function_tool()
    async def report_health_concern(self, ctx: RunContext):
        self.action_detected = True

        return "FONCTION APPELER NE L'UTILISE PLUS : 1. Approndie le problème pour mieux le comprendre et écouter, en 4 réponses. 2. Après rassure, en 4 réponses. 3. Engage directement une réponse sur des ses souvenir et centre d'interet. Ne lui dit jamais d'aller voir quelqu'un ou que tu va appeler/prevenir une personne."
      
    @function_tool 
    async def detected_answering_machine(self):
        """Call this tool if you have detected a voicemail system, AFTER hearing the voicemail greeting"""
        await asyncio.sleep(2) # Add a natural gap to the end of the voicemail message
        self.was_answering_machine = True   
        # extraire le code patient depuis le nom de la room ex: room_XYTXW3_timothe
        job_ctx = get_job_context()
        m = re.match(r"^room_([^_]+)_",job_ctx.room.name)
        code_for_notif = m.group(1) if m else None
        try:
          telephone_manquant(code_for_notif or "unknown")
          await hangup_call()
        except Exception as e:
            logger.error("voicemail failed: %s", e)
# -------------------------------------------------------------
# Entry point: merge of SIP dialing + Livana config + logging
# -------------------------------------------------------------

async def contient_mot_clef(log_file_path: str, cfg_path: str) -> bool:
    logger.info("contient_mot_clef: start (file=%s)", log_file_path)
    prenom = get_prenom_from_config(cfg_path) or ""
    mot_clef = prenom.strip()
    if not mot_clef:
        logger.info("contient_mot_clef: prenom vide -> False")
        return False
    try:
        async with async_open(log_file_path, "r", encoding="utf-8") as f:
            contenu = await f.read()
        ok = mot_clef in contenu
        logger.info("contient_mot_clef: mot='%s' present=%s (len=%d)", mot_clef, ok, len(contenu))
        return ok
    except FileNotFoundError:
        logger.info("contient_mot_clef: file not found -> False")
        return False
    except Exception as e:
        logger.error("contient_mot_clef: error %s", e)
        return False


async def entrypoint( ctx: JobContext):
        logger.info("connecting to room %s", ctx.room.name)
        await ctx.connect()
    
        logger.info("metadata received (repr): %r", ctx.job.metadata)
        dial_info = parse_metadata(ctx.job.metadata)
    
        code = dial_info.get("code")
        call_id=dial_info.get("call_id") or "1"
        
        if not code:
          m = re.match(r"^room_([^_]+)_", ctx.room.name)
          code = m.group(1) if m else None
    
        if not code:
          logger.error("Aucun 'code' fourni (metadata/room). Impossible de charger la config patient.")
          ctx.shutdown()
          return
    
        cfg_path = os.path.join(LIVANA_CONFIG_DIR, f"{code}.cfg")
        logger.info("Using config (by code): %s", cfg_path)
        await wait_until_config_ready(cfg_path)
    
    
        # Snapshot configuration for this call
        prenom = get_prenom_from_config(cfg_path)
        prenom_livana = get_prenom_livana_from_config(cfg_path)
        definition_livana = get_definition_livana_from_config(cfg_path)
        interests = get_interests_string(cfg_path)
        phrase_debut, phrase_fin = get_phrases(cfg_path)
        voix_id = get_voix_id_from_config(cfg_path)
    
        # Parse outbound dialing metadata
        logger.info("metadata received (repr): %r", ctx.job.metadata)
        dial_info = parse_metadata(ctx.job.metadata)
        participant_identity = phone_number = dial_info["phone_number"]
    
        # Build Agent (persona + tools)
        agent = LivanaOutboundAgent(
            prenom=prenom,
            prenomLivana=prenom_livana,
            definition_livana=definition_livana,
            interests=interests,
            phrasedebut=phrase_debut,
            phrasefin=phrase_fin,
            dial_info=dial_info,
        )
    
        TRANSCRIPT_DIR = os.getenv("TRANSCRIPT_DIR", "/home/ubuntu/agent_tel/transcriptions")
        log_path = f"/transcriptions/transcriptions_{ctx.room.name}.log"
        if not os.path.exists(log_path):
            with open(log_path, "w", encoding="utf-8") as f:
                f.write("")
        
        log_queue: asyncio.Queue[str | None] = asyncio.Queue()
    
        req = api.RoomCompositeEgressRequest(
        room_name=ctx.room.name,
        layout="speaker",
        audio_only=True,                       
        file_outputs=[api.EncodedFileOutput(
            s3=api.S3Upload(
                bucket="audiocalldomicile",
                region="eu-north-1",
                access_key=AMAZON_ACCESS_KEY,
                secret=AMAZON_SECRET_KEY,
                # endpoint="https://s3.eu-west-3.amazonaws.com", # seulement si S3 compatible (Wasabi/Scaleway)
                force_path_style=False                           # True pour S3 compatibles
                ),
            )],
        )
        lkapi = api.LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        res = await lkapi.egress.start_room_composite_egress(req)
    
        async def on_shutdown(reason: str):
            logger.info("[%s] shutdown: %s", ctx.room.name, reason)
        
            await finish_queue()
            await asyncio.sleep(10)
            code = dial_info.get("code")
            call_id=dial_info.get("call_id") or ""
        
            if not code:
              m = re.match(r"^room_([^_]+)_", ctx.room.name)
              code = m.group(1) if m else None
            rtdb.reference(f'calls/{code}-appel_agent/{call_id}', app=app).delete()
            logger.info(f"REFERENCE SUPPRIMEE a 'calls/{code}-appel_audio/{call_id}")
            try:
                has_content = not await is_transcription_empty()
                logger.info("on_shutdown: has_content=%s", has_content)
            except Exception as e:
                logger.error("on_shutdown: _is_transcription_empty failed: %s", e)
                has_content = False
        
            transcription_complete = ""
            if has_content:
                try:
                    async with async_open(log_path, "r", encoding="utf-8") as f:
                        transcription_complete = await f.read()
                    logger.info("on_shutdown: transcript len=%d", len(transcription_complete))
                except Exception as e:
                    logger.error("on_shutdown: read transcript failed: %s", e)
                    transcription_complete = ""
        
            try:
                contains_key = await contient_mot_clef(log_path, cfg_path)
            except Exception as e:
                logger.error("on_shutdown: contient_mot_clef failed: %s", e)
                contains_key = False
        
            if not has_content or not contains_key:
                logger.info("on_shutdown: skip notify (has_content=%s, contains_key=%s)", has_content, contains_key)
                try:
                    await ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name))
                except Exception as e:
                    logger.error("delete_room error: %s", e)
                return
        
            try:
                prenom_val = get_prenom_from_config(cfg_path) or ""
                livana_nom = get_prenom_livana_from_config(cfg_path) or ""
                m = re.match(r"^room_([^_]+)_", ctx.room.name)
                code = m.group(1) if m else None
                
                if "generer_resumer" in globals() and callable(globals()["generer_resumer"]):
                    try:
                        resume_txt = generer_resumer(transcription_complete, prenom_val, livana_nom)
                        if resume_txt:
                            logger.info("on_shutdown: résumé généré (%d chars)", len(resume_txt))
                    except Exception as e:
                        logger.warning("generer_resumer failed: %s", e)
        
                if "generer_centres_interet" in globals() and callable(globals()["generer_centres_interet"]):
                    try:
                        resultat_interets = generer_centres_interet(transcription_complete)
                        if "ecraser_interets_si_differents" in globals() and callable(globals()["ecraser_interets_si_differents"]):
                            ecraser_interets_si_differents(resultat_interets,cfg_path, code)
                    except Exception as e:
                        logger.warning("maj intérêts failed: %s", e)
            except Exception as e:
                logger.warning("post-processing failed: %s", e)
        
            
      
            topic = code
            is_alert = bool(getattr(agent, "action_detected", False))  # si tu l’as implémenté
        
            title = "Probleme detecte" if is_alert else "Appel effectue"
            data_payload = {"alerte": "true"} if is_alert else {"appel_effectue": "true"}
        
            if getattr(agent, "was_answering_machine", False):
              logger.info("on_shutdown: answering machine case, pas de résumé ni notif standard.")
              try:
                  async with async_open(log_path, "w", encoding="utf-8") as f:
                      await f.write("")
                      update_call_status(hardware_code, call_id, "appel-manque")
              except Exception as e:
                  logger.warning("on_shutdown purge transcript failed: %s", e)
      
              try:
                  await ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name))
              except Exception as e:
                  logger.error("delete_room error: %s", e)
              return
      
            combined_body = (resume_txt.strip() + "\n\n" + transcription_complete.strip()).strip()
            try:
                if "send_notification_to_topic" in globals() and callable(globals()["send_notification_to_topic"]):
                    send_notification_to_topic(
                        topic,
                        title,
                        combined_body,
                        data_payload,  
                        False,          
                        is_alert,       
                        not is_alert, 
                    )
                    logger.info("on_shutdown: notification envoyée (topic=%s, title=%s)", topic, title)
                else:
                    logger.warning("on_shutdown: send_notification_to_topic indisponible")
            except Exception as e:
                logger.error("on_shutdown: erreur envoi notification: %s", e)
        
            # 7) Purge du fichier de transcription
            try:
                async with async_open(log_path, "w", encoding="utf-8") as f:
                    await f.write("")
            except Exception as e:
                logger.warning("on_shutdown: purge transcript failed: %s", e)
        
            # 8) Suppression de la room
            try:
                await ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name))
            except Exception as e:
                logger.error("delete_room error: %s", e)
    
        ctx.add_shutdown_callback(on_shutdown)
        options = soniox.STTOptions(
        model="stt-rt-v3",
        language_hints=["fr"],
        )
        voice_id = get_voix_id_from_config(cfg_path)
        """
        # Session stack (telephony-friendly): Silero VAD + Deepgram STT + Cartesia TTS voice from cfg + OpenAI LLM
        session = AgentSession(
            #turn_detection="stt",
            #turn_detection=MultilingualModel(),
            #turn_detection="realtime_llm",
            #discard_audio_if_uninterruptible=False,
            allow_interruptions = True,
            vad=ctx.proc.userdata["vad"],
            preemptive_generation=True,
    		    min_interruption_duration=1,
            min_interruption_words=3,
            user_away_timeout=6,
            #max_endpointing_delay=2,
            #min_endpointing_delay=0.3,
            use_tts_aligned_transcript=True,
              llm=RealtimeModel(
              model="fixie-ai/ultravox",
            voice="Sabrina",
        ),
                   )"""
        #
        session = AgentSession(
            #turn_detection="stt",
            turn_detection=MultilingualModel(),
            #turn_detection="realtime_llm",
            #discard_audio_if_uninterruptible=False,
            allow_interruptions = True,
            vad=ctx.proc.userdata["vad"],
            preemptive_generation=True,
    		    min_interruption_duration=1,
            min_interruption_words=3,
            user_away_timeout=6,
            max_endpointing_delay=3.5,
            min_endpointing_delay=0.3,
            use_tts_aligned_transcript=True,
            #stt=assemblyai.STT(language="fr",end_of_turn_confidence_threshold=0.2,min_end_of_turn_silence_when_confident=1,max_turn_silence=2400,), 
            #vad=silero.VAD.load(),
            stt=stt.FallbackAdapter([
                        soniox.STT(params=options),
                        #mistralai.STT(model="voxtral-mini-2507", api_key = MISTRAL_API_KEY),
    					#gladia.STT(translation_enabled=False,languages=["fr"],translation_target_languages=["fr"], pre_processing_speech_threshold=0.1, pre_processing_audio_enhancer = True ),
                        deepgram.STT(language="fr",model="nova-3",punctuate="true",interim_results="true",),
                        #cartesia.STT(model="ink-whisper", language="fr"),
                        ]),
            #stt=deepgram.STT(language="fr",model="nova-2-general",punctuate="true",interim_results="false",endpointing_ms="0"),
            #llm="openai/gpt-4o",
            llm=inference.LLM(model="openai/gpt-4.1", provider="azure"),
                        #mistralai.LLM(model="mistral-small-latest", api_key=MISTRAL_API_KEY),
                        #lk_openai.LLM.with_deepseek(model="deepseek-chat",api_key = "sk-f945b290da6548e09564a385bf2ae0dc"),
   					#llm=lk_openai.LLM(,
                        #model="gpt-5-chat-latest") 
    			        #google.LLM(model="gemini-flash-lite", api_key=GEMINI_API_KEY),
                        #groq.LLM(model="llama3-8b-8192",api_key=GROQ_API_KEY),
                        #]),
                      
            #llm=openai.realtime.RealtimeModel(),
            tts=inference.TTS(model="cartesia/sonic-3",voice=voice_id,language="fr", ),
            #tts=tts.FallbackAdapter([cartesia.TTS(model="sonic-3",voice=voice_id,language="fr",)
                        #]),
            #turn_detection=MultilingualModel(),
            #turn_detection=MultilingualModel(),
            #fnc_ctx=fnc_ctx,
        )
    
            # après avoir créé agent et session
            
            
            
        global GLOBAL_SESSION, GLOBAL_CTX, GLOBAL_AGENT
        GLOBAL_SESSION = session
        GLOBAL_CTX = ctx
        GLOBAL_AGENT = agent
    
        inactivity_task: asyncio.Task | None = None
        usage_collector = metrics.UsageCollector()
    
        async def user_presence_task():
            # try to ping the user 3 times, if we get no answer, close the session
            for _ in range(17):
                await session.generate_reply(
                    instructions=(
                        "Demande si la personne est toujours la, et pose une question sur les souvenirs et interets, si t'es encore dans l'etape 1 alors ne dit que ne dit que 'allo tu m'entend' "
                    )
                )
                await asyncio.sleep(6)
    
            session.shutdown()
    
        @session.on("user_state_changed")
        def _user_state_changed(ev: UserStateChangedEvent):
            nonlocal inactivity_task
            if ev.new_state == "away":
                logging.info("utilisateur sans reponse")
                inactivity_task = asyncio.create_task(user_presence_task())
                return
    
            # ev.new_state: listening, speaking, ..
            if inactivity_task is not None:
                inactivity_task.cancel()
                
        @session.on("metrics_collected")
        def _on_metrics_collected(ev: MetricsCollectedEvent):
            usage_collector.collect(ev.metrics)
        
        async def log_usage():
            summary = usage_collector.get_summary()
            logger.info(f"Usage: {summary}")
        
        # At shutdown, generate and log the summary from the usage collector
        ctx.add_shutdown_callback(log_usage)
        
        from livekit.agents import ConversationItemAddedEvent
        
        
        @session.on("conversation_item_added")
        def _on_item(event: ConversationItemAddedEvent):
            try:
                item = event.item
                if item.role == "agent":
                    role_label = "LIVANA"
                elif item.role == "user":
                    #user_chunks = []
                    role_label = get_prenom_from_config(cfg_path) or "UTILISATEUR"
                    """
                    for c in item.content:
                        if isinstance(c, str):
                            if c.strip():
                                user_chunks.append(c)
                        else:
                            txt = getattr(c, "text", None)
                            if isinstance(txt, str) and txt.strip():
                                user_chunks.append(txt)
    
                    user_text_raw = " ".join(user_chunks)
                    role_label = get_prenom_from_config(cfg_path) or "UTILISATEUR"
                    if is_meaningless_pause(user_text_raw):
                      return"""
                    # asyncio.create_task(agent.fast_ack(session, item.role))
                else:
                    role_label = "LIVANA"
        
                content_lines = []
                for c in item.content:
                    if isinstance(c, str):
                        if c.strip():
                            content_lines.append(c)
                    else:
                        txt = getattr(c, "text", None)
                        if isinstance(txt, str) and txt.strip():
                            content_lines.append(txt)
        
                full_turn_text_raw = " ".join(content_lines)
                full_turn_text = full_turn_text_raw.lower()
        
                # ⬇️ Filtre anti "..." / ". . ." / etc
                
        
                """voicemail_markers = [
                        "messagerie",
                        "repondeur",
                        "laissez un message",
                        "laisser un message",
                        "bip sonore",
                        "signal sonore",
                        "apres le bip",
                        "bip",
                        "n'est pas disponible pour le moment",
                        "n est pas disponible pour le moment",
                        "boîte vocale",
                        "après le bip",
                    ]
                for marker in voicemail_markers:
                        if marker in full_turn_text:
                            logger.info("voicemail DECTECTEE par pattern '%s' -> on coupe direct", marker)
                            # on déclenche nous-mêmes l'arrêt messagerie
                            asyncio.create_task(_force_voicemail_hangup())
                            break"""
                            
                suffix = "\n[interrompu]\n" if getattr(item, "interrupted", False) else "\n"
                log_msg = f"{role_label}:\n" + "\n".join(content_lines) + suffix + "\n"
                log_queue.put_nowait(log_msg)
            except Exception as e:
                logger.error("on_conversation_item_added: %s", e)
    
    
        async def _transcription_writer():
          async with async_open(log_path, "a", encoding="utf-8") as f:
            while True:
              msg = await log_queue.get()
              if msg is None:
                await f.flush()
                break
              await f.write(msg)
              await f.flush()
    
        
        
        async def finish_queue():
          try:
            log_queue.put_nowait(None)  
          except Exception:
            pass
          try:
            await write_task             
          except Exception as e:
            logger.error(f"finish_queue error: {e}")
    
        async def is_transcription_empty(path: str = log_path) -> bool:
          try:
            async with async_open(path, "r") as f:
                content = await f.read()
                logger.info("transempty:")
                return len(content.strip()) == 0
          except FileNotFoundError:
            return True
          except Exception as e:
            logger.error(f"is_transcription_empty error: {e}")
            return False
    
        # Start agent session BEFORE dialing so we don't miss early speech
        session_started = asyncio.create_task(
            session.start(
                agent=agent,
                room=ctx.room,
                room_input_options=RoomInputOptions(
                    noise_cancellation=noise_cancellation.BVCTelephony(),
                ),
                room_output_options=RoomOutputOptions(transcription_enabled=False, sync_transcription=False),
            )
        )
    
          
        background_audio = BackgroundAudioPlayer(
        thinking_sound=[
            AudioConfig("/home/ubuntu/agent_tel/Breath_inV2_2.wav", volume=1,probability=0.5),
            AudioConfig("/home/ubuntu/agent_tel/Breath_inV2.wav", volume=1,probability=0.5),
          ],
        )  
        await background_audio.start(room=ctx.room, agent_session=session)
    
    
        # Dial the user via SIP trunksd
        try:
            await ctx.api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=ctx.room.name,
                    sip_trunk_id=SIP_OUTBOUND_TRUNK_ID,
                    sip_call_to=phone_number,
                    participant_identity=participant_identity,
                    krisp_enabled = True,
                    wait_until_answered = True,
                    ringing_timeout=Duration(seconds=200),  # Timeout after 30 seconds
                )
            )
    
            # wait for agent session and participant join
            await session_started
            participant = await ctx.wait_for_participant(identity=participant_identity)
            logger.info("participant joined: %s", participant.identity)
            agent.set_participant(participant)
            write_task = asyncio.create_task(_transcription_writer())
        except api.TwirpError as e:
            logger.error(
                "error creating SIP participant: %s, SIP status: %s %s",
                e.message,
                e.metadata.get("sip_status_code"),
                e.metadata.get("sip_status"),
            )
            try:
              data = dial_info
              hardware_code =data.get("code") or code or "unknown"
              call_id = data.get("call_id") or "unknown"
              update_call_status(hardware_code, call_id, "error", reason=str(e))
              logger.info("Statut mis a jour -> appel-done")
            except Exception as e:
              logger.error(f"Impossible de mettre à jour le statut de fin d'appel: {e}")
            ctx.shutdown()


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="LivanaOutbound",
            prewarm_fnc=prewarm,
            
        ),
        #hot_reload=False,
    )
