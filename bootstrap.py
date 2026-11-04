"""
bootstrap.py - Creation initiale des tables hebergees "lignes de bus" sur le portail.

Tous les parametres sont dans config.ini (copier config.ini.example).

Usage (Python d'ArcGIS Pro) :
    python bootstrap.py

Etapes :
  1. charge les CSV depuis ODS (load_data.py -> data/raw/) ;
  2. se connecte au portail avec [ARCGIS_ACCESS] (url / username / password) ;
  3. pour chaque section DATASET_... dont layer_url est vide :
       - ajoute le CSV au portail, le publie en table hebergee SANS geometrie,
       - le partage selon `sharing`,
       - ecrit l'URL de la table dans layer_url de config.ini ;
  4. ecrit le recapitulatif dans data/items.json.

Relancable : un jeu dont layer_url est rempli est saute ; un service deja present
(meme titre, meme proprietaire) n'est pas recree, son URL est simplement reportee.

Aucun calcul : les evolutions sont calculees par l'hyperviseur.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from arcgis.gis import GIS

from load_data import load_all
from src.get_configs import ROOT, Dataset, arcgis_settings, get_datasets, read_config, set_config_value
from src.logger import get_logger

OUT_FILE = ROOT / "data" / "items.json"
SHARING_LEVELS = {"everyone": "EVERYONE", "org": "ORGANIZATION", "private": "PRIVATE"}

log = get_logger("bootstrap")


# --- Portail ------------------------------------------------------------------
def connect(s: dict) -> GIS:
    gis = GIS(s["url"], s["username"], s["password"], verify_cert=s["verify_cert"])
    if gis.users.me is None:
        raise PermissionError("Connexion anonyme : verifier username / password dans config.ini")
    return gis


def ensure_folder(gis: GIS, name: str) -> None:
    try:
        gis.content.create_folder(name)
    except Exception:
        pass  # dossier deja existant


def find_service(gis: GIS, title: str):
    me = gis.users.me.username
    hits = gis.content.search(query=f'title:"{title}" AND owner:{me}', item_type="Feature Service", max_items=50)
    return next((i for i in hits if i.title == title), None)


def publish_table(gis: GIS, csv_path: Path, ds: Dataset, s: dict):
    if not gis.content.is_service_name_available(ds.service_name, "featureService"):
        raise ValueError(
            f"Le nom de service '{ds.service_name}' est deja pris sur le portail par un autre element : "
            "changer service_name dans config.ini ou supprimer l'ancien service."
        )
    props = {
        "type": "CSV",
        "title": ds.title,
        "tags": s["tags"],
        "snippet": f"Source : data.seineouest.fr ({ds.data_name})",
    }
    csv_item = gis.content.add(props, data=str(csv_path), folder=s["folder"])

    pp = gis.content.analyze(item=csv_item.id, file_type="csv", location_type="none")["publishParameters"]
    pp.update({"name": ds.service_name, "locationType": "none", "columnDelimiter": ";"})

    service = csv_item.publish(publish_parameters=pp, file_type="csv")
    log.info("%s publie (item %s)", ds.service_name, service.id)
    return service


def share(item, level: str) -> None:
    try:
        item.sharing.sharing_level = SHARING_LEVELS[level]          # arcgis >= 2.3
    except AttributeError:
        item.share(everyone=(level == "everyone"), org=(level in ("everyone", "org")))


def table_info(service) -> tuple[str, int]:
    lyr = service.tables[0] if service.tables else service.layers[0]
    return lyr.url, lyr.query(where="1=1", return_count_only=True)


# --- Main ---------------------------------------------------------------------
def main() -> int:
    cfg = read_config()
    datasets = get_datasets(cfg)
    pending = [d for d in datasets if not d.layer_url]
    if not pending:
        log.info("Tous les jeux ont deja un layer_url dans config.ini : rien a publier.")
        return 0

    paths = load_all(cfg)

    s = arcgis_settings(cfg)
    gis = connect(s)
    log.info("Connecte a %s en tant que %s", s["url"], gis.users.me.username)
    ensure_folder(gis, s["folder"])

    out = json.loads(OUT_FILE.read_text(encoding="utf-8")) if OUT_FILE.exists() else {}
    failures = 0

    for ds in pending:
        try:
            if ds.key not in paths:
                raise RuntimeError("CSV non charge depuis ODS (voir logs/load_data.log)")

            service = find_service(gis, ds.title)
            if service:
                log.info("%s existe deja (item %s) : pas de recreation.", ds.service_name, service.id)
            else:
                service = publish_table(gis, paths[ds.key], ds, s)

            share(service, s["sharing"])
            url, n = table_info(service)
            set_config_value(ds.key, "layer_url", url)
            out[ds.key] = {"service": ds.service_name, "item_id": service.id, "layer_url": url, "records": n}
            log.info("%s -> %s (%d enregistrements) : layer_url ecrit dans config.ini", ds.service_name, url, n)
        except Exception:
            failures += 1
            log.exception("Echec de la publication de %s", ds.key)

    OUT_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Termine : %d echec(s) sur %d. Recap dans %s", failures, len(pending), OUT_FILE)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
