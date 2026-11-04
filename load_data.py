"""
load_data.py - ODS -> CSV brut -> CSV transforme. Rien d'autre.

    python load_data.py

Pour chaque section DATASET_... de config.ini :
  1. telecharge l'export CSV depuis data.seineouest.fr ;
  2. data/raw/<service_name>.csv       : fichier ODS tel quel (iso source) ;
  3. data/processed/<service_name>.csv : format
        annee;ligne;valeur
        2019;469;0.9894
        2019;ChavilBus;0.9985
        ...

Source avec des colonnes annees (2019, 2020...) -> passees en lignes.
Source deja en lignes (ligne;annee;<chiffre>) -> simplement remise au meme format.
Annees >= min_year (2019 par defaut). Les cases vides de la source sont ignorees.
"""
from __future__ import annotations

import io
import re
import sys
import time

import pandas as pd
import requests

from src.get_configs import ROOT, get_datasets, ods_urls, read_config
from src.logger import get_logger

RAW_DIR = ROOT / "data" / "raw"
PROCESSED_DIR = ROOT / "data" / "processed"
YEAR_COL = re.compile(r"(?:^|[^0-9])((?:19|20)\d{2})$")   # 2019, annee_2019...

log = get_logger("load_data")


def download(url: str, retries: int = 3) -> bytes:
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, timeout=300)
            r.raise_for_status()
            return r.content
        except requests.RequestException as exc:
            if attempt == retries:
                raise
            log.warning("Tentative %d/%d echouee (%s), nouvel essai", attempt, retries, exc)
            time.sleep(5 * attempt)


def year_of(col) -> int | None:
    m = YEAR_COL.search(str(col).strip())
    return int(m.group(1)) if m else None


def transform(df: pd.DataFrame, sec) -> pd.DataFrame:
    """-> annee;ligne;valeur."""
    id_field = sec.get("id_field", fallback="ligne").strip()
    year_field = sec.get("year_field", fallback="annee").strip()
    min_year = sec.getint("min_year", fallback=2019)
    if id_field not in df.columns:
        raise ValueError(f"colonne '{id_field}' absente (colonnes : {list(df.columns)}) : renseigner id_field")

    year_cols = {c: year_of(c) for c in df.columns if c != id_field and year_of(c)}
    if year_cols:
        # annees en colonnes -> en lignes
        long = df.melt(id_vars=[id_field], value_vars=list(year_cols), var_name="annee", value_name="valeur")
        long["annee"] = long["annee"].map(year_cols)
    elif year_field in df.columns:
        # deja en lignes : ligne;annee;<chiffre>
        value_field = sec.get("value_field", fallback="").strip() or next(
            c for c in df.columns if c not in (id_field, year_field))
        long = df[[id_field, year_field, value_field]].copy()
        long.columns = [id_field, "annee", "valeur"]
        long["annee"] = pd.to_numeric(
            long["annee"].astype(str).str.extract(r"((?:19|20)\d{2})", expand=False), errors="coerce")
    else:
        raise ValueError(f"ni colonnes annees ni colonne '{year_field}' (colonnes : {list(df.columns)})")

    long["ligne"] = long[id_field].astype(str).str.strip()
    long["valeur"] = pd.to_numeric(
        long["valeur"].astype(str).str.replace(r"[\s\u00a0]", "", regex=True).str.replace(",", "."),
        errors="coerce")

    long = long.dropna(subset=["annee", "valeur"])
    long = long[long["annee"] >= min_year]
    long["annee"] = long["annee"].astype(int)
    if (long["valeur"] % 1 == 0).all():                     # 794187.0 -> 794187
        long["valeur"] = long["valeur"].astype("Int64")

    order = {l: i for i, l in enumerate(dict.fromkeys(long["ligne"]))}   # ordre de la source
    long = long.assign(_o=long["ligne"].map(order)).sort_values(["annee", "_o"])
    return long[["annee", "ligne", "valeur"]].reset_index(drop=True)


def main() -> int:
    cfg = read_config()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    datasets = get_datasets(cfg)
    failures = 0

    for ds in datasets:
        try:
            _, url = ods_urls(cfg, ds.data_name)
            content = download(url)

            raw_path = RAW_DIR / f"{ds.service_name}.csv"
            raw_path.write_bytes(content)

            raw = pd.read_csv(io.BytesIO(content), sep=";", encoding="utf-8-sig", dtype=str)
            out = transform(raw, cfg[ds.key])
            out_path = PROCESSED_DIR / f"{ds.service_name}.csv"
            out.to_csv(out_path, sep=";", index=False, encoding="utf-8-sig")

            log.info("%s : %s -> %s (%d lignes, annees %d-%d)", ds.service_name, raw_path.name,
                     out_path, len(out), out["annee"].min(), out["annee"].max())
        except Exception:
            failures += 1
            log.exception("Echec de %s (%s)", ds.key, ds.data_name)

    log.info("Termine : %d echec(s) sur %d", failures, len(datasets))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
