"""Lecture de config.ini (a la racine du projet)."""
from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "config.ini"


@dataclass
class Dataset:
    key: str           # nom de la section, ex. DATASET_FREQUENTATION
    data_name: str     # identifiant du jeu sur ODS
    service_name: str  # nom des fichiers CSV produits


def read_config(path: Path = CONFIG_FILE) -> configparser.ConfigParser:
    if not path.exists():
        raise FileNotFoundError(f"{path} introuvable : copier config.ini.example en config.ini.")
    # interpolation=None : les URL contiennent des % (ex. %3B)
    cfg = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#", ";"))
    cfg.read(path, encoding="utf-8")
    return cfg


def get_datasets(cfg: configparser.ConfigParser) -> list[Dataset]:
    datasets = [
        Dataset(key=sec, data_name=cfg[sec]["data_name"].strip(), service_name=cfg[sec]["service_name"].strip())
        for sec in cfg.sections() if sec.upper().startswith("DATASET_")
    ]
    if not datasets:
        raise ValueError("Aucune section DATASET_... dans config.ini")
    return datasets


def ods_urls(cfg: configparser.ConfigParser, data_name: str) -> tuple[str, str]:
    """Renvoie (URL des metadonnees, URL de l'export CSV) d'un jeu ODS."""
    s = cfg["ODS_ACCESS"]
    base = s["portal_url"].strip().rstrip("/") + "/" + s["prefix"].strip().strip("/") + "/" + data_name
    export = base + "/" + s["sufix"].strip().lstrip("/")
    meta = base
    apikey = s.get("apikey", "").strip()
    if apikey:
        meta += f"?apikey={quote(apikey)}"
        export += ("&" if "?" in export else "?") + f"apikey={quote(apikey)}"
    return meta, export
