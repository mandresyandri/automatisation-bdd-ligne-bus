"""
load_data.py - Genere les CSV "lignes de bus locaux" depuis data.seineouest.fr.

AUCUNE connexion au portail ArcGIS : ce script ne fait que produire des CSV.

Usage (Python d'ArcGIS Pro) :
    python load_data.py

Pour chaque section DATASET_... de config.ini :
  - data/raw/<service_name>.csv        : export CSV ODS tel quel ;
  - data/processed/<service_name>.csv  : une ligne par annee, une colonne par ligne de bus
        annee;469;ChavilBus;SUBB;TIM;...
        2019;0.9894;0.9985;0.9842;...
    * seulement les valeurs renseignees ;
    * annees de min_year (2019 par defaut) a la derniere annee qui a une valeur ;
    * une annee sans aucune valeur et une ligne de bus sans aucune valeur sont retirees.

Fonctionne que la source soit en colonnes (ligne;2014;2015;...) ou en lignes
(ligne;annee;nombre_de_voyages).

Cles facultatives d'une section DATASET_... :
    min_year    = 2019     premiere annee gardee
    id_field    = ligne    colonne qui contient le nom de la ligne de bus
    year_field  = annee    source en lignes : colonne de l'annee
    value_field =          source en lignes : colonne de la valeur (deduite si une seule autre colonne)
"""
from __future__ import annotations

import io
import json
import re
import sys
import time
from datetime import datetime, timezone

import pandas as pd
import requests

from src.get_configs import ROOT, get_datasets, ods_urls, read_config
from src.logger import get_logger

RAW_DIR = ROOT / "data" / "raw"
PROCESSED_DIR = ROOT / "data" / "processed"
MANIFEST = ROOT / "data" / "manifest.json"
YEAR_COL = re.compile(r"(?:^|[^0-9])((?:19|20)\d{2})$")   # 2019, annee_2019, an2019...

log = get_logger("load_data")
session = requests.Session()
session.headers.update({"User-Agent": "GPSO-DIGIT automatisation-bdd-ligne-bus"})


def http_get(url: str, retries: int = 3, timeout: int = 300) -> requests.Response:
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


def to_number(s: pd.Series) -> pd.Series:
    """Texte -> nombre (virgule decimale et espaces acceptes). Vide -> NaN."""
    s = s.astype(str).str.replace("\u00a0", "", regex=False).str.replace(" ", "", regex=False)
    return pd.to_numeric(s.str.replace(",", ".", regex=False), errors="coerce")


def year_of(col) -> int | None:
    m = YEAR_COL.search(str(col).strip())
    return int(m.group(1)) if m else None


def to_long(df: pd.DataFrame, sec) -> pd.DataFrame:
    """Format commun : colonnes ligne / annee / valeur."""
    id_field = sec.get("id_field", fallback="ligne").strip()
    if id_field not in df.columns:
        raise ValueError(f"colonne '{id_field}' absente (colonnes : {list(df.columns)}) : renseigner id_field")

    year_cols = {c: year_of(c) for c in df.columns if c != id_field and year_of(c) is not None}
    if year_cols:  # source en colonnes : ligne;2014;2015;...
        long = df.melt(id_vars=[id_field], value_vars=list(year_cols), var_name="annee", value_name="valeur")
        long["annee"] = long["annee"].map(year_cols)
    else:          # source en lignes : ligne;annee;valeur
        year_field = sec.get("year_field", fallback="annee").strip()
        if year_field not in df.columns:
            raise ValueError(f"colonne annee '{year_field}' absente (colonnes : {list(df.columns)}) : renseigner year_field")
        value_field = sec.get("value_field", fallback="").strip()
        if not value_field:
            others = [c for c in df.columns if c not in (id_field, year_field)]
            if len(others) != 1:
                raise ValueError(f"plusieurs colonnes possibles pour la valeur {others} : renseigner value_field")
            value_field = others[0]
        long = df[[id_field, year_field, value_field]].copy()
        long.columns = [id_field, "annee", "valeur"]
        # annee peut etre une date (2019-01-01) : on garde l'annee
        long["annee"] = long["annee"].astype(str).str.extract(r"((?:19|20)\d{2})", expand=False)

    long = long.rename(columns={id_field: "ligne"})
    long["ligne"] = long["ligne"].astype(str).str.strip()
    long["annee"] = pd.to_numeric(long["annee"], errors="coerce")
    long["valeur"] = to_number(long["valeur"])
    return long.dropna(subset=["annee", "valeur"]).astype({"annee": int})


def transpose(long: pd.DataFrame, min_year: int, name: str) -> pd.DataFrame:
    """Une ligne par annee (min_year -> derniere annee renseignee), une colonne par ligne de bus."""
    long = long[long["annee"] >= min_year]
    if long.empty:
        raise ValueError(f"aucune valeur renseignee a partir de {min_year}")

    dup = long.duplicated(subset=["ligne", "annee"], keep=False)
    if dup.any():
        raise ValueError(f"plusieurs valeurs pour une meme ligne et une meme annee :\n{long[dup].head(10)}")

    order = list(dict.fromkeys(long["ligne"]))                  # ordre de la source
    wide = long.pivot(index="annee", columns="ligne", values="valeur").sort_index()
    wide = wide[order].dropna(how="all").dropna(axis=1, how="all")
    for c in wide.columns:                                      # 794187.0 -> 794187
        vals = wide[c].dropna()
        if (vals % 1 == 0).all():
            wide[c] = wide[c].astype("Int64")
    wide.index.name = "annee"
    wide.columns.name = None

    log.info("%s : %d annees (%d-%d) x %d lignes de bus", name, len(wide), wide.index.min(), wide.index.max(), wide.shape[1])
    empty = int(wide.isna().sum().sum())
    if empty:
        log.info("%s : %d case(s) vide(s) (pas de valeur ODS pour cette ligne cette annee)", name, empty)
    return wide.reset_index()


def main() -> int:
    cfg = read_config()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    manifest, failures = {}, 0

    for ds in get_datasets(cfg):
        try:
            _, export_url = ods_urls(cfg, ds.data_name)
            content = http_get(export_url).content
            raw_path = RAW_DIR / f"{ds.service_name}.csv"
            raw_path.write_bytes(content)
            raw = pd.read_csv(io.BytesIO(content), sep=";", encoding="utf-8-sig", dtype=str)
            log.info("%s : brut -> %s (%d lignes)", ds.service_name, raw_path, len(raw))

            sec = cfg[ds.key]
            wide = transpose(to_long(raw, sec), sec.getint("min_year", fallback=2019), ds.service_name)
            out_path = PROCESSED_DIR / f"{ds.service_name}.csv"
            wide.to_csv(out_path, sep=";", index=False, encoding="utf-8-sig")
            log.info("%s : traite -> %s, colonnes %s", ds.service_name, out_path, list(wide.columns))

            manifest[ds.data_name] = {
                "raw_file": str(raw_path.relative_to(ROOT)),
                "file": str(out_path.relative_to(ROOT)),
                "columns": list(map(str, wide.columns)),
                "years": [int(wide["annee"].min()), int(wide["annee"].max())],
                "loaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        except Exception:
            failures += 1
            log.exception("Echec de %s (%s)", ds.key, ds.data_name)

    MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Termine : %d echec(s). CSV dans %s", failures, PROCESSED_DIR)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
