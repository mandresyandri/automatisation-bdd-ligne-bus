"""
load_data.py - Chargement des donnees "lignes de bus locaux" depuis data.seineouest.fr.

Constitue le bootstrap des donnees, en local, avant toute publication SIG.
Aucune transformation : les donnees sont recuperees telles que publiees sur ODS.

A lancer avec le Python d'ArcGIS Pro (pandas et requests deja embarques) :
    "C:\\Program Files\\ArcGIS\\Pro\\bin\\Python\\scripts\\propy.bat" load_data.py
    ... load_data.py --force     # retelecharge meme si ODS n'a pas change

Pour chaque jeu :
  1. lit les metadonnees ODS (titre, date de modification, nombre de lignes, champs) ;
  2. saute le jeu si la date de modification ODS n'a pas change depuis le dernier chargement ;
  3. telecharge l'export CSV complet (noms de champs techniques : use_labels=false) ;
  4. controle le fichier avec pandas (non vide, colonnes attendues, nombre de lignes) ;
  5. ecrit :
       data/raw/<service>.csv       le CSV tel que recu d'ODS
       data/schema/<service>.json   les champs ODS et le type Esri correspondant
       data/manifest.json           l'etat du dernier chargement (date ODS, nb lignes, sha256)
"""
import argparse
import hashlib
import io
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

# --- Parametres ---------------------------------------------------------------
ODS_BASE = "https://data.seineouest.fr/api/explore/v2.1/catalog/datasets"
EXPORT_PARAMS = {"delimiter": ";", "use_labels": "false", "lang": "fr", "timezone": "Europe/Paris"}

DATASETS = {
    # dataset_id ODS : nom du service (= nom du fichier local, puis de la table SIG)
    "frequentation-des-lignes-de-bus-locaux-depuis-2014": "bus_frequentation_lignes",
    "taux-de-realisation-des-lignes-de-bus-locaux-depuis-2014": "bus_taux_realisation_lignes",
}

# Correspondance des types ODS vers les types de champs Esri (pour la publication)
ODS_TO_ESRI = {
    "text": "esriFieldTypeString",
    "int": "esriFieldTypeInteger",
    "double": "esriFieldTypeDouble",
    "date": "esriFieldTypeDate",
    "datetime": "esriFieldTypeDate",
    "boolean": "esriFieldTypeSmallInteger",
    "geo_point_2d": "esriFieldTypeString",
    "geo_shape": "esriFieldTypeString",
    "file": "esriFieldTypeString",
}

ROOT = Path(__file__).resolve().parent
RAW_DIR = ROOT / "data" / "raw"
SCHEMA_DIR = ROOT / "data" / "schema"
MANIFEST = ROOT / "data" / "manifest.json"
LOG_DIR = ROOT / "logs"

for d in (RAW_DIR, SCHEMA_DIR, LOG_DIR):
    d.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "load_data.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("load_data")

session = requests.Session()
session.headers.update({"User-Agent": "GPSO-DIGIT automatisation-bdd-ligne-bus"})


# --- Utilitaires --------------------------------------------------------------
def get(url: str, params: dict | None = None, retries: int = 3, timeout: int = 120) -> requests.Response:
    """GET avec quelques nouvelles tentatives (ODS renvoie parfois des 5xx passagers)."""
    for attempt in range(1, retries + 1):
        try:
            r = session.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r
        except requests.RequestException as exc:
            if attempt == retries:
                raise
            log.warning("Tentative %d/%d echouee (%s), nouvel essai dans %ds", attempt, retries, exc, 5 * attempt)
            time.sleep(5 * attempt)


def load_manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {}


def save_manifest(manifest: dict) -> None:
    MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


# --- Etapes -------------------------------------------------------------------
def fetch_metadata(dataset_id: str) -> dict:
    """Metadonnees ODS : titre, date de modification, nombre de lignes, champs."""
    meta = get(f"{ODS_BASE}/{dataset_id}").json()
    default = meta.get("metas", {}).get("default", {})
    return {
        "title": default.get("title"),
        "modified": default.get("modified") or default.get("data_processed"),
        "records_count": default.get("records_count"),
        "fields": [
            {
                "name": f["name"],
                "label": f.get("label"),
                "type": f.get("type"),
                "esri_type": ODS_TO_ESRI.get(f.get("type"), "esriFieldTypeString"),
            }
            for f in meta.get("fields", [])
        ],
    }


def download_csv(dataset_id: str) -> bytes:
    content = get(f"{ODS_BASE}/{dataset_id}/exports/csv", params=EXPORT_PARAMS, timeout=300).content
    if not content.strip():
        raise ValueError("export CSV vide")
    return content


def check_csv(content: bytes, meta: dict) -> pd.DataFrame:
    """Controles simples, sans rien modifier au fichier."""
    df = pd.read_csv(io.BytesIO(content), sep=";", encoding="utf-8-sig")
    if df.empty:
        raise ValueError("le CSV ne contient aucune ligne")

    expected = [f["name"] for f in meta["fields"]]
    missing = [c for c in expected if c not in df.columns]
    if missing:
        raise ValueError(f"colonnes absentes du CSV : {missing}")

    if meta["records_count"] is not None and len(df) != meta["records_count"]:
        log.warning("Nombre de lignes : %d dans le CSV, %d annonce par ODS", len(df), meta["records_count"])
    return df


def describe(service: str, df: pd.DataFrame) -> None:
    """Apercu dans le log, pour fixer le schema a la premiere execution."""
    log.info("%s : %d lignes x %d colonnes", service, len(df), len(df.columns))
    for col in df.columns:
        sample = df[col].dropna().unique()[:5].tolist()
        log.info("   - %-30s %-8s %4d valeurs distinctes, ex. %s", col, str(df[col].dtype), df[col].nunique(), sample)


def load_dataset(dataset_id: str, service: str, manifest: dict, force: bool) -> None:
    meta = fetch_metadata(dataset_id)
    previous = manifest.get(dataset_id, {})
    csv_path = RAW_DIR / f"{service}.csv"

    if not force and csv_path.exists() and previous.get("ods_modified") == meta["modified"]:
        log.info("%s : inchange sur ODS depuis %s, rien a faire.", service, meta["modified"])
        return

    content = download_csv(dataset_id)
    df = check_csv(content, meta)
    describe(service, df)

    csv_path.write_bytes(content)
    (SCHEMA_DIR / f"{service}.json").write_text(
        json.dumps({"dataset_id": dataset_id, "title": meta["title"], "fields": meta["fields"]}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    manifest[dataset_id] = {
        "service": service,
        "title": meta["title"],
        "ods_modified": meta["modified"],
        "records": len(df),
        "columns": list(df.columns),
        "sha256": hashlib.sha256(content).hexdigest(),
        "file": str(csv_path.relative_to(ROOT)),
        "loaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    log.info("%s : ecrit dans %s", service, csv_path)


# --- Main ---------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Charge les jeux bus depuis data.seineouest.fr")
    parser.add_argument("--force", action="store_true", help="retelecharger meme si ODS n'a pas change")
    args = parser.parse_args()

    manifest = load_manifest()
    failures = 0
    for dataset_id, service in DATASETS.items():
        try:
            load_dataset(dataset_id, service, manifest, args.force)
        except Exception:
            failures += 1
            log.exception("Echec du chargement de %s", dataset_id)

    save_manifest(manifest)
    log.info("Termine : %d jeu(x) en echec sur %d. Etat dans %s", failures, len(DATASETS), MANIFEST)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
