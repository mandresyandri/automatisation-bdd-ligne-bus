"""Journal commun : logs/<nom>.log + console."""
import logging
import sys
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


def get_logger(name: str) -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    for handler in (
        logging.FileHandler(LOG_DIR / f"{name}.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ):
        handler.setFormatter(fmt)
        logger.addHandler(handler)
    # pas de remontee vers le logger racine (evite les lignes en double avec arcgis)
    logger.propagate = False
    return logger
