"""
bootstrap.py - Creation initiale des tables hebergees "lignes de bus" sur atlas-sig.

A lancer UNE fois, avec le Python d'ArcGIS Pro (arcgis, pandas, requests deja embarques) :
    "C:\\Program Files\\ArcGIS\\Pro\\bin\\Python\\scripts\\propy.bat" bootstrap.py

Pour chaque jeu ODS :
  1. telecharge l'export CSV (noms de champs techniques : use_labels=false) ;
  2. l'ajoute au portail comme element CSV ;
  3. le publie en table hebergee SANS geometrie (FeatureServer) ;
  4. le partage et note item_id + URL REST dans config/items.json.

Idempotent : un service deja present (meme titre, meme proprietaire) n'est pas recree.
Les mises a jour suivantes se feront par overwrite (pipeline.py) : l'item_id et l'URL
donnes a l'hyperviseur ne changent pas. Garder le meme nom de fichier CSV (data/<service>.csv),
overwrite l'exige.

Aucun calcul ici : les evolutions sont calculees par l'hyperviseur.
"""
import getpass
import json
import logging
import sys
from pathlib import Path

import requests
from arcgis.gis import GIS

# --- Parametres ---------------------------------------------------------------
PORTAL_URL = "https://atlas-sig.seineouest.fr/portal"
PROFILE = "digit_bus"        # profil arcgis : identifiants stockes chiffres en local
FOLDER = "Hyperviseur_bus"   # dossier de contenu sur le portail
SHARING = "everyone"         # "everyone", "org" ou "private"
TAGS = ["GPSO", "bus", "hyperviseur", "opendata"]

ODS_EXPORT = (
    "https://data.seineouest.fr/api/explore/v2.1/catalog/datasets/{ds}/exports/csv"
    "?delimiter=%3B&use_labels=false"
)

DATASETS = {
    # dataset_id ODS : (nom du service, titre de l'element)
    "frequentation-des-lignes-de-bus-locaux-depuis-2014": (
        "bus_frequentation_lignes",
        "Frequentation des lignes de bus locaux depuis 2014",
    ),
    "taux-de-realisation-des-lignes-de-bus-locaux-depuis-2014": (
        "bus_taux_realisation_lignes",
        "Taux de realisation des lignes de bus locaux depuis 2014",
    ),
}

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
OUT_FILE = ROOT / "config" / "items.json"

for d in (DATA_DIR, LOG_DIR, OUT_FILE.parent):
    d.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "bootstrap.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("bootstrap")


# --- Connexion ----------------------------------------------------------------
def connect() -> GIS:
    """Profil enregistre si dispo, sinon demande les identifiants et cree le profil."""
    try:
        gis = GIS(profile=PROFILE)
        if gis.users.me is not None:
            return gis
    except Exception:
        pass
    log.info("Profil '%s' absent : saisie des identifiants (stockes ensuite dans le profil).", PROFILE)
    user = input("Utilisateur portail : ")
    pwd = getpass.getpass("Mot de passe : ")
    return GIS(PORTAL_URL, user, pwd, profile=PROFILE)


# --- Etapes -------------------------------------------------------------------
def download_csv(dataset_id: str, service_name: str) -> Path:
    url = ODS_EXPORT.format(ds=dataset_id)
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    if not r.content.strip():
        raise ValueError(f"Export vide pour {dataset_id}")
    path = DATA_DIR / f"{service_name}.csv"
    path.write_bytes(r.content)
    header = r.content.decode("utf-8-sig").splitlines()[0]
    log.info("%s : %d octets telecharges, colonnes = %s", dataset_id, len(r.content), header)
    return path


def find_service(gis: GIS, title: str):
    me = gis.users.me.username
    hits = gis.content.search(query=f'title:"{title}" AND owner:{me}', item_type="Feature Service")
    return next((i for i in hits if i.title == title), None)


def ensure_folder(gis: GIS):
    try:
        gis.content.folders.create(FOLDER)           # arcgis >= 2.3
    except AttributeError:
        gis.content.create_folder(FOLDER)            # versions anterieures
    except Exception:
        pass                                         # dossier deja existant


def publish_table(gis: GIS, csv_path: Path, service_name: str, title: str, dataset_id: str):
    props = {
        "type": "CSV",
        "title": title,
        "tags": ",".join(TAGS),
        "snippet": f"Source : data.seineouest.fr ({dataset_id})",
    }
    csv_item = gis.content.add(props, data=str(csv_path), folder=FOLDER)

    pp = gis.content.analyze(item=csv_item.id, file_type="csv", location_type="none")["publishParameters"]
    pp.update({"name": service_name, "locationType": "none", "columnDelimiter": ";"})

    service = csv_item.publish(publish_parameters=pp, file_type="csv")
    log.info("%s publie (item %s)", service_name, service.id)
    return service


def share(item):
    try:
        item.sharing.sharing_level = {"everyone": "EVERYONE", "org": "ORGANIZATION", "private": "PRIVATE"}[SHARING]
    except Exception:
        item.share(everyone=(SHARING == "everyone"), org=(SHARING in ("everyone", "org")))


def table_url(service) -> tuple[str, int]:
    lyr = service.tables[0] if service.tables else service.layers[0]
    return lyr.url, lyr.query(where="1=1", return_count_only=True)


# --- Main ---------------------------------------------------------------------
def main():
    gis = connect()
    log.info("Connecte a %s en tant que %s", gis.url, gis.users.me.username)
    ensure_folder(gis)

    out = json.loads(OUT_FILE.read_text(encoding="utf-8")) if OUT_FILE.exists() else {}

    for dataset_id, (service_name, title) in DATASETS.items():
        try:
            service = find_service(gis, title)
            if service:
                log.info("%s existe deja (item %s) : pas de recreation.", service_name, service.id)
            else:
                csv_path = download_csv(dataset_id, service_name)
                service = publish_table(gis, csv_path, service_name, title, dataset_id)
            share(service)
            url, n = table_url(service)
            out[dataset_id] = {"service": service_name, "item_id": service.id, "url": url, "records": n}
            log.info("%s -> %s (%d enregistrements)", service_name, url, n)
        except Exception:
            log.exception("Echec pour %s", dataset_id)

    OUT_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Recap ecrit dans %s", OUT_FILE)


if __name__ == "__main__":
    main()
