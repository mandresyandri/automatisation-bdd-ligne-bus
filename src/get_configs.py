"""Lecture de config.ini (a la racine du projet)."""
from __future__ import annotations

import configparser
import getpass
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "config.ini"


@dataclass
class Dataset:
    key: str           # nom de la section, ex. DATASET_FREQUENTATION
    data_name: str     # identifiant du jeu sur ODS
    service_name: str  # nom du service / du fichier local
    title: str         # titre de l'element sur le portail
    layer_url: str     # vide tant que la table n'est pas creee (rempli par run.py)


def read_config(path: Path = CONFIG_FILE) -> configparser.ConfigParser:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} introuvable : copier config.ini.example en config.ini et le remplir."
        )
    # interpolation=None : les URL contiennent des % (ex. %3B)
    cfg = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#", ";"))
    cfg.read(path, encoding="utf-8")
    return cfg


def get_datasets(cfg: configparser.ConfigParser) -> list[Dataset]:
    datasets = []
    for sec in cfg.sections():
        if not sec.upper().startswith("DATASET_"):
            continue
        s = cfg[sec]
        datasets.append(
            Dataset(
                key=sec,
                data_name=s["data_name"].strip(),
                service_name=s["service_name"].strip(),
                title=s.get("title", s["service_name"]).strip(),
                layer_url=s.get("layer_url", "").strip(),
            )
        )
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


def arcgis_settings(cfg: configparser.ConfigParser) -> dict:
    """Parametres ArcGIS.

    auth=pro     : session ArcGIS Pro active (aucun identifiant, aucune saisie) ;
                   valeur par defaut quand username est vide.
    auth=builtin : username + mot de passe (config.ini, sinon ARCGIS_PASSWORD, sinon saisie).
    """
    s = cfg["ARCGIS_ACCESS"]
    username = s.get("username", "").strip()
    auth = s.get("auth", "").strip().lower() or ("builtin" if username else "pro")
    settings = {
        "auth": auth,
        "url": s.get("url", "").strip(),
        "username": "",
        "password": "",
        "verify_cert": s.getboolean("verify_cert", fallback=True),
        "folder": s.get("folder", "Hyperviseur_bus").strip(),
        "sharing": s.get("sharing", "everyone").strip().lower(),
        "tags": s.get("tags", "GPSO,bus").strip(),
    }
    if auth == "pro":
        return settings
    if not username:
        raise ValueError("auth=builtin mais username vide dans config.ini (ou mettre auth=pro)")
    settings["username"] = username
    settings["password"] = (
        s.get("password", "").strip()
        or os.environ.get("ARCGIS_PASSWORD", "")
        or getpass.getpass(f"Mot de passe de {username} : ")
    )
    return settings


def set_config_value(section: str, key: str, value: str, path: Path = CONFIG_FILE) -> None:
    """Ecrit key=value dans une section de config.ini sans toucher aux commentaires."""
    lines = path.read_text(encoding="utf-8").splitlines()
    in_section, section_end, replaced = False, None, False

    for i, line in enumerate(lines):
        st = line.strip()
        if st.startswith("[") and st.endswith("]"):
            if in_section:
                section_end = i
                break
            in_section = st[1:-1].strip() == section
            continue
        if in_section and "=" in st and not st.startswith((";", "#")):
            if st.split("=", 1)[0].strip().lower() == key.lower():
                lines[i] = f"{key}={value}"
                replaced = True
                break

    if not replaced:
        if in_section and section_end is not None:
            lines.insert(section_end, f"{key}={value}")
        elif in_section:
            lines.append(f"{key}={value}")
        else:
            lines += ["", f"[{section}]", f"{key}={value}"]

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
