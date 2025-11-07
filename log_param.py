import os, sys, logging
from logtail import LogtailHandler

def setup_logtail(service_name: str = "voice-agent", level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(service_name)
    logger.setLevel(level)
    logger.propagate = False  

    if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(level)
        ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logger.addHandler(ch)

    token = os.getenv("LOGTAIL_TOKEN")
    if token:
        if not any(isinstance(h, LogtailHandler) for h in logger.handlers):
            lh = LogtailHandler(source_token=token, host = "s1528873.eu-nbg-2.betterstackdata.com")
            lh.setLevel(level)
            lh.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
            logger.addHandler(lh)
    else:
        logger.warning("LOGTAIL_TOKEN manquant: logs uniquement en console.")

    # router les warnings Python vers logging
    logging.captureWarnings(True)
    return logger

def read_unique_code(CODE_FILE):
    if os.path.exists(CODE_FILE):
        with open(CODE_FILE, "r") as f:
            return f.read().strip()
    return None


def get_prenom_from_config(livana_file):
    try:
        with open(livana_file, "r") as file:
            for line in file:
                if line.startswith("    Prenom:"):
                    return line.replace("    Prenom:", "").strip()
    except FileNotFoundError:
        print("Le fichier livana_config n'existe pas.")
        return None

