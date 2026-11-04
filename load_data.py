"""
load_data.py - Chargement des jeux "lignes de bus locaux" depuis data.seineouest.fr.

Tous les parametres (URL ODS, cle API, jeux a charger) sont dans config.ini.

Usage (Python d'ArcGIS Pro) :
    python load_data.py            # ne retelecharge que les jeux modifies sur ODS
    python load_data.py --force    # retelecharge tout

Pour chaque section DATASET_... de config.ini :
  1. lit les metadonnees ODS (titre, date de modification, nombre de lignes, champs) ;
  2. telecharge l'export CSV si la date de modification ODS a change (ou --force)
     et l'ecrit tel quel dans data/raw/<service_name>.csv ;
  3. traite le CSV brut (toujours, meme si ODS n'a pas change) :
       - une ligne par ligne de bus, une colonne par annee (annee_2019, annee_2020, ...),
         que la source soit en lignes (ligne / annee / valeur) ou deja en colonnes ;
       - ne garde que les valeurs renseignees ;
       - annees de min_year (2019 par defaut) a la derniere annee qui a une valeur ;
       - supprime les lignes de bus sans aucune valeur sur la periode ;
     et l'ecrit dans data/processed/<service_name>.csv (c'est ce fichier qui est publie) ;
  4. ecrit data/schema/<service_name>.json (champs du fichier traite et type Esri)
     et data/manifest.json (etat du dernier chargement).

Cles facultatives d'une section DATASET_... :
    min_year     = 2019      premiere annee gardee
    year_prefix  = annee_    prefixe des colonnes annee (un champ Esri ne peut pas commencer par un chiffre)
    year_field   = annee     source en lignes : colonne de l'annee
    value_field  =           source en lignes : colonne de la valeur (deduite si une seule colonne numerique)
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

from src.get_configs import ROOT, Dataset, get_datasets, ods_urls, read_config
from src.logger import get_logger

RAW_DIR = ROOT / "data" / "raw"
PROCESSED_DIR = ROOT / "data" / "processed"
SCHEMA_DIR = ROOT / "data" / "schema"
MANIFEST = ROOT / "data" / "manifest.json"

# Colonne "annee" d'un jeu deja en colonnes : nom qui finit par une annee (2019, _2019, an_2019...)
YEAR_IN_NAME = re.compile(r"(?:^|[^0-9])((?:19|20)\d{2})$")

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


def read_ods_csv(content: bytes) -> pd.DataFrame:
    return pd.read_csv(io.BytesIO(content), sep=";", encoding="utf-8-sig")


def year_of(col) -> int | None:
    m = YEAR_IN_NAME.search(str(col))
    return int(m.group(1)) if m else None


def to_number(s: pd.Series) -> pd.Series:
    """Valeurs numeriques ; accepte la virgule decimale et les espaces. Vide -> NaN."""
    if not pd.api.types.is_numeric_dtype(s):
        s = s.astype(str).str.replace("\u00a0", "", regex=False).str.replace(" ", "", regex=False)
        s = s.str.replace(",", ".", regex=False)
    return pd.to_numeric(s, errors="coerce")


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
    """Controles simples sur le CSV recu d'ODS."""
    if not content.strip():
        raise ValueError("export CSV vide")
    df = read_ods_csv(content)
    if df.empty:
        raise ValueError("le CSV ne contient aucune ligne")

    missing = [f["name"] for f in meta["fields"] if f["name"] not in df.columns]
    if missing:
        raise ValueError(f"colonnes absentes du CSV : {missing} (verifier use_labels=false dans sufix)")

    if meta["records_count"] is not None and len(df) != meta["records_count"]:
        log.warning("Nombre de lignes : %d dans le CSV, %d annonce par ODS", len(df), meta["records_count"])
    return df


def to_wide(df: pd.DataFrame, sec, service: str) -> tuple[pd.DataFrame, list[str]]:
    """Une ligne par ligne de bus, une colonne par annee, de min_year a la derniere annee renseignee."""
    min_year = sec.getint("min_year", fallback=2019)
    prefix = sec.get("year_prefix", fallback="annee_").strip()

    year_cols = {c: year_of(c) for c in df.columns if year_of(c) is not None}
    if year_cols:
        # Source deja en colonnes (ex. taux de realisation) : on repasse par le format long pour filtrer
        id_cols = [c for c in df.columns if c not in year_cols]
        long = df.melt(id_vars=id_cols, value_vars=list(year_cols), var_name="_col", value_name="_val")
        long["_annee"] = long["_col"].map(year_cols)
        long = long.drop(columns="_col")
    else:
        # Source en lignes (ex. frequentation : ligne / annee / nombre_de_voyages)
        year_field = sec.get("year_field", fallback="annee").strip()
        if year_field not in df.columns:
            raise ValueError(f"colonne annee '{year_field}' absente (colonnes : {list(df.columns)}) : renseigner year_field")
        value_field = sec.get("value_field", fallback="").strip()
        if not value_field:
            nums = [c for c in df.columns if c != year_field and to_number(df[c]).notna().any()]
            if len(nums) != 1:
                raise ValueError(f"colonne valeur ambigue {nums} : renseigner value_field dans config.ini")
            value_field = nums[0]
        id_cols = [c for c in df.columns if c not in (year_field, value_field)]
        long = df.rename(columns={year_field: "_annee", value_field: "_val"})

    if not id_cols:
        raise ValueError("aucune colonne d'identifiant (ligne) trouvee")

    long["_val"] = to_number(long["_val"])
    long["_annee"] = pd.to_numeric(long["_annee"], errors="coerce")
    long = long.dropna(subset=["_val", "_annee"])               # que les valeurs renseignees
    long["_annee"] = long["_annee"].astype(int)
    long = long[long["_annee"] >= min_year]
    if long.empty:
        raise ValueError(f"aucune valeur renseignee a partir de {min_year}")

    dup = long.duplicated(subset=id_cols + ["_annee"], keep=False)
    if dup.any():
        raise ValueError(f"plusieurs valeurs pour une meme ligne et une meme annee :\n{long[dup].head(10)}")

    first, last = int(long["_annee"].min()), int(long["_annee"].max())
    wide = long.pivot(index=id_cols, columns="_annee", values="_val")
    wide = wide.reindex(columns=range(first, last + 1))          # annees continues, de first a last
    wide.columns = [f"{prefix}{y}" for y in wide.columns]
    year_names = list(wide.columns)
    wide = wide.reset_index()

    for c in year_names:                                        # 794187.0 -> 794187
        vals = wide[c].dropna()
        if not vals.empty and (vals % 1 == 0).all():
            wide[c] = wide[c].astype("Int64")

    kept = set(map(tuple, wide[id_cols].astype(str).values))
    dropped = sorted(set(map(tuple, df[id_cols].astype(str).values)) - kept)
    log.info("%s : %d lignes de bus x %d annees (%d-%d)", service, len(wide), len(year_names), first, last)
    if dropped:
        log.info("%s : ecartees (aucune valeur depuis %d) : %s", service, min_year, [" / ".join(d) for d in dropped])
    empty = int(wide[year_names].isna().sum().sum())
    if empty:
        log.info("%s : %d case(s) vide(s) restante(s) (pas de valeur ODS pour cette ligne et cette annee)", service, empty)
    return wide, year_names


def schema_fields(wide: pd.DataFrame, year_names: list[str], ods_fields: list[dict]) -> list[dict]:
    by_name = {f["name"]: f for f in ods_fields}
    fields = []
    for c in wide.columns:
        if c in year_names:
            is_int = str(wide[c].dtype) == "Int64"
            fields.append({
                "name": c, "label": c.replace("_", " ").capitalize(),
                "type": "int" if is_int else "double",
                "esri_type": "esriFieldTypeInteger" if is_int else "esriFieldTypeDouble",
            })
        else:
            fields.append(by_name.get(c, {"name": c, "label": c, "type": "text", "esri_type": "esriFieldTypeString"}))
    return fields


def load_dataset(cfg, ds: Dataset, manifest: dict, force: bool) -> Path:
    meta_url, export_url = ods_urls(cfg, ds.data_name)
    meta = fetch_metadata(meta_url)
    previous = manifest.get(ds.data_name, {})
    raw_path = RAW_DIR / f"{ds.service_name}.csv"
    out_path = PROCESSED_DIR / f"{ds.service_name}.csv"

    # 1. Brut : ODS tel quel
    if not force and raw_path.exists() and previous.get("ods_modified") == meta["modified"]:
        log.info("%s : inchange sur ODS depuis %s, brut conserve.", ds.service_name, meta["modified"])
        content = raw_path.read_bytes()
        raw = read_ods_csv(content)
    else:
        content = http_get(export_url, timeout=300).content
        raw = check_csv(content, meta)
        raw_path.write_bytes(content)
        log.info("%s : brut ecrit dans %s (%d lignes)", ds.service_name, raw_path, len(raw))

    # 2. Traite : une colonne par annee, filtre
    wide, year_names = to_wide(raw, cfg[ds.key], ds.service_name)
    out = wide.to_csv(sep=";", index=False).encode("utf-8")
    out_path.write_bytes(out)

    fields = schema_fields(wide, year_names, meta["fields"])
    (SCHEMA_DIR / f"{ds.service_name}.json").write_text(
        json.dumps({"data_name": ds.data_name, "title": meta["title"], "fields": fields}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    manifest[ds.data_name] = {
        "service": ds.service_name,
        "title": meta["title"],
        "ods_modified": meta["modified"],
        "raw_file": str(raw_path.relative_to(ROOT)),
        "raw_records": len(raw),
        "raw_sha256": hashlib.sha256(content).hexdigest(),
        "file": str(out_path.relative_to(ROOT)),
        "records": len(wide),
        "columns": list(wide.columns),
        "sha256": hashlib.sha256(out).hexdigest(),
        "loaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    log.info("%s : traite ecrit dans %s, colonnes %s", ds.service_name, out_path, list(wide.columns))
    return out_path


def load_all(cfg, force: bool = False) -> dict[str, Path]:
    """Charge tous les jeux de config.ini. Renvoie {section: chemin du CSV traite} pour ceux qui ont reussi."""
    for d in (RAW_DIR, PROCESSED_DIR, SCHEMA_DIR):
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
