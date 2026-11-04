"""
load_data.py - Chargement des jeux "lignes de bus locaux" depuis data.seineouest.fr.

Tous les parametres (URL ODS, cle API, jeux a charger) sont dans config.ini.
Aucune transformation : les donnees sont recuperees telles que publiees sur ODS.

Usage (Python d'ArcGIS Pro) :
    python load_data.py            # ne retelecharge que les jeux modifies sur ODS
    python load_data.py --force    # retelecharge tout

Pour chaque section DATASET_... de config.ini :
  1. lit les metadonnees ODS (titre, date de modification, nombre de lignes, champs) ;
  2. saute le jeu si la date de modification ODS n'a pas change depuis le dernier chargement ;
  3. telecharge l'export CSV ;
  4. controle le fichier (non vide, colonnes attendues, nombre de lignes) ;
  5. ecrit :
       data/raw/<service_name>.csv      le CSV tel que recu d'ODS
       data/schema/<service_name>.json  les champs ODS et le type Esri correspondant
       data/manifest.json               l'etat du dernier chargement
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

from src.get_configs import ROOT, Dataset, get_datasets, ods_urls, read_config
from src.logger import get_logger

RAW_DIR = ROOT / "data" / "raw"
SCHEMA_DIR = ROOT / "data" / "schema"
MANIFEST = ROOT / "data" / "manifest.json"

# Types ODS -> types de champs Esri (pour la publication)
ODS_TO_ESRI = {
    "text": "esriFieldTypeString",
    "int": "esriFieldTypeInteger",
    "double": "esriFieldTypeDouble",
    "date": "esriFieldTypeDate",
    "datetime": "esriFieldTypeDate",
    "boolean": "esriFieldTypeSmallInteger",
}

log = get_logger("load_data")
session = requests.Session()
session.headers.update({"User-Agent": "GPSO-DIGIT automatisation-bdd-ligne-bus"})


# --- Utilitaires --------------------------------------------------------------
def http_get(url: str, retries: int = 3, timeout: int = 120) -> requests.Response:
    """GET avec nouvelles tentatives (ODS renvoie parfois des 5xx passagers)."""
    for attempt in range(1, retries + 1):
        try:
            r = session.get(url, timeout=timeout)
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
def fetch_metadata(meta_url: str) -> dict:
    meta = http_get(meta_url).json()
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


def check_csv(content: bytes, meta: dict) -> pd.DataFrame:
    """Controles simples, sans rien modifier au fichier."""
    if not content.strip():
        raise ValueError("export CSV vide")
    df = pd.read_csv(io.BytesIO(content), sep=";", encoding="utf-8-sig")
    if df.empty:
        raise ValueError("le CSV ne contient aucune ligne")

    missing = [f["name"] for f in meta["fields"] if f["name"] not in df.columns]
    if missing:
        raise ValueError(f"colonnes absentes du CSV : {missing} (verifier use_labels=false dans sufix)")

    if meta["records_count"] is not None and len(df) != meta["records_count"]:
        log.warning("Nombre de lignes : %d dans le CSV, %d annonce par ODS", len(df), meta["records_count"])
    return df


def describe(service: str, df: pd.DataFrame) -> None:
    """Apercu des colonnes dans le journal, pour fixer le schema."""
    log.info("%s : %d lignes x %d colonnes", service, len(df), len(df.columns))
    for col in df.columns:
        sample = df[col].dropna().unique()[:5].tolist()
        log.info("   - %-30s %-8s %4d valeurs distinctes, ex. %s", col, str(df[col].dtype), df[col].nunique(), sample)


def load_dataset(cfg, ds: Dataset, manifest: dict, force: bool) -> Path:
    meta_url, export_url = ods_urls(cfg, ds.data_name)
    meta = fetch_metadata(meta_url)
    previous = manifest.get(ds.data_name, {})
    csv_path = RAW_DIR / f"{ds.service_name}.csv"

    if not force and csv_path.exists() and previous.get("ods_modified") == meta["modified"]:
        log.info("%s : inchange sur ODS depuis %s, rien a faire.", ds.service_name, meta["modified"])
        return csv_path

    content = http_get(export_url, timeout=300).content
    df = check_csv(content, meta)
    describe(ds.service_name, df)

    csv_path.write_bytes(content)
    (SCHEMA_DIR / f"{ds.service_name}.json").write_text(
        json.dumps({"data_name": ds.data_name, "title": meta["title"], "fields": meta["fields"]}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    manifest[ds.data_name] = {
        "service": ds.service_name,
        "title": meta["title"],
        "ods_modified": meta["modified"],
        "records": len(df),
        "columns": list(df.columns),
        "sha256": hashlib.sha256(content).hexdigest(),
        "file": str(csv_path.relative_to(ROOT)),
        "loaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    log.info("%s : ecrit dans %s", ds.service_name, csv_path)
    return csv_path


def load_all(cfg, force: bool = False) -> dict[str, Path]:
    """Charge tous les jeux de config.ini. Renvoie {section: chemin du CSV} pour ceux qui ont reussi."""
    for d in (RAW_DIR, SCHEMA_DIR):
        d.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest()
    paths = {}
    for ds in get_datasets(cfg):
        try:
            paths[ds.key] = load_dataset(cfg, ds, manifest, force)
        except Exception:
            log.exception("Echec du chargement de %s (%s)", ds.key, ds.data_name)
    save_manifest(manifest)
    return paths


# --- Main ---------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Charge les jeux bus depuis data.seineouest.fr")
    parser.add_argument("--force", action="store_true", help="retelecharger meme si ODS n'a pas change")
    args = parser.parse_args()

    cfg = read_config()
    total = len(get_datasets(cfg))
    paths = load_all(cfg, args.force)
    failures = total - len(paths)
    log.info("Termine : %d jeu(x) en echec sur %d. Etat dans %s", failures, total, MANIFEST)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
