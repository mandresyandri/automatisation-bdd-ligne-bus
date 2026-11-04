"""
run.py - Mise a jour complete, a lancer a chaque fois (ou en tache planifiee) :

    python run.py

Pour CHAQUE section DATASET_... de config.ini (frequentation ET taux de realisation) :
  1. retelecharge le jeu ODS et regenere data/processed/<service_name>.csv
     (une ligne par annee, une colonne par ligne de bus) ;
  2. met a jour la table hebergee ArcGIS correspondante :
       - premiere fois : la table est creee, partagee, et son URL est ecrite dans layer_url ;
       - nouvelle annee     -> nouvel enregistrement ;
       - nouvelle ligne bus -> nouveau champ (alias = nom de la ligne) ajoute automatiquement ;
       - les donnees sont remplacees, mais l'element, l'URL et les champs existants ne
         changent jamais : l'hyperviseur continue de fonctionner sans retouche.

Securites :
  - si ODS ne repond pas ou si le CSV est inexploitable, la table ArcGIS n'est PAS touchee ;
  - les nouvelles lignes sont ajoutees AVANT de supprimer les anciennes : la table n'est
    jamais vide ; si l'ajout echoue, on revient a l'etat precedent ;
  - l'edition n'est activee que le temps de la MAJ, puis les droits d'origine sont remis.

Connexion : auth=pro (session ArcGIS Pro, rien a saisir) ou auth=builtin (voir config.ini).
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from datetime import datetime, timezone

import pandas as pd
from arcgis.features import FeatureLayerCollection, Table
from arcgis.gis import GIS

from load_data import build_all
from src.get_configs import ROOT, Dataset, arcgis_settings, get_datasets, read_config, set_config_value
from src.logger import get_logger

OUT_FILE = ROOT / "data" / "items.json"
SHARING_LEVELS = {"everyone": "EVERYONE", "org": "ORGANIZATION", "private": "PRIVATE"}
EDIT_CAPS = "Create,Delete,Query,Update,Editing"
RESERVED = {"objectid", "fid", "oid", "shape", "globalid", "annee"}

log = get_logger("run")


# --- Portail ------------------------------------------------------------------
def connect(s: dict) -> GIS:
    if s["auth"] == "pro":
        gis = GIS("pro")
    else:
        gis = GIS(s["url"], s["username"], s["password"], verify_cert=s["verify_cert"])
    if gis.users.me is None:
        raise PermissionError("Connexion anonyme : se connecter au portail dans ArcGIS Pro, ou renseigner username/password")
    return gis


def ensure_folder(gis: GIS, name: str) -> None:
    try:
        gis.content.folders.create(name)          # arcgis >= 2.3
    except AttributeError:
        try:
            gis.content.create_folder(name)
        except Exception:
            pass
    except Exception:
        pass                                      # dossier deja existant


def share(item, level: str) -> None:
    try:
        item.sharing.sharing_level = SHARING_LEVELS[level]          # arcgis >= 2.3
    except AttributeError:
        item.share(everyone=(level == "everyone"), org=(level in ("everyone", "org")))


def find_service(gis: GIS, title: str):
    me = gis.users.me.username
    hits = gis.content.search(query=f'title:"{title}" AND owner:{me}', item_type="Feature Service", max_items=50)
    return next((i for i in hits if i.title == title), None)


def create_table(gis: GIS, ds: Dataset, s: dict):
    """Table hebergee vide (OBJECTID + annee). Les champs des lignes de bus sont ajoutes par sync()."""
    if not gis.content.is_service_name_available(ds.service_name, "featureService"):
        raise ValueError(
            f"Le nom de service '{ds.service_name}' est deja pris sur le portail : "
            "changer service_name dans config.ini ou supprimer l'ancien service."
        )
    item = gis.content.create_service(
        name=ds.service_name,
        service_type="featureService",
        has_static_data=False,
        capabilities="Query",
        folder=s["folder"],
        item_properties={
            "title": ds.title,
            "tags": s["tags"],
            "snippet": f"Source : data.seineouest.fr ({ds.data_name}) - une ligne par annee, un champ par ligne de bus",
        },
    )
    FeatureLayerCollection.fromitem(item).manager.add_to_definition({"tables": [{
        "name": ds.service_name,
        "type": "Table",
        "objectIdField": "OBJECTID",
        "displayField": "annee",
        "hasAttachments": False,
        "indexes": [],
        "templates": [],
        "fields": [
            {"name": "OBJECTID", "type": "esriFieldTypeOID", "alias": "OBJECTID",
             "sqlType": "sqlTypeOther", "nullable": False, "editable": False},
            {"name": "annee", "type": "esriFieldTypeInteger", "alias": "annee",
             "sqlType": "sqlTypeOther", "nullable": True, "editable": True},
        ],
    }]})
    item = gis.content.get(item.id)
    share(item, s["sharing"])
    log.info("%s : table creee (item %s)", ds.service_name, item.id)
    return item


def get_table(gis: GIS, ds: Dataset, s: dict) -> Table:
    if ds.layer_url:
        return Table(ds.layer_url, gis)
    item = find_service(gis, ds.title)
    if item:
        log.info("%s : service existant retrouve (item %s)", ds.service_name, item.id)
    else:
        item = create_table(gis, ds, s)
    lyr = item.tables[0] if item.tables else item.layers[0]
    set_config_value(ds.key, "layer_url", lyr.url)
    ds.layer_url = lyr.url
    log.info("%s : layer_url ecrit dans config.ini (%s)", ds.service_name, lyr.url)
    return Table(lyr.url, gis)


# --- Synchronisation ----------------------------------------------------------
def field_name(label: str, used: set[str]) -> str:
    """Nom de champ valide et unique a partir du nom de la ligne ('469' -> 'l_469')."""
    s = unicodedata.normalize("NFKD", str(label)).encode("ascii", "ignore").decode()
    s = re.sub(r"[^0-9A-Za-z]+", "_", s).strip("_").lower()[:40].rstrip("_")
    if not s or not s[0].isalpha():
        s = "l_" + s
    base, n = s, 2
    while s in used:
        s, n = f"{base}_{n}", n + 1
    used.add(s)
    return s


def sync(gis: GIS, tbl: Table, wide: pd.DataFrame, name: str) -> int:
    props = tbl.properties.fields
    oid = next(f["name"] for f in props if f["type"] == "esriFieldTypeOID")
    by_alias = {f["alias"]: f["name"] for f in props if f["type"] != "esriFieldTypeOID"}
    names = {f["name"].lower(): f["name"] for f in props}
    if "annee" not in names:
        raise ValueError(f"champ 'annee' absent de {tbl.url} : table d'un ancien format, la supprimer (et vider layer_url)")
    year_field = names["annee"]

    # 1. une nouvelle ligne de bus = un nouveau champ ; les champs existants ne bougent pas
    used = set(names) | RESERVED
    lines = [c for c in wide.columns if c != "annee"]
    mapping, new_fields = {}, []
    for label in lines:
        if label in by_alias:
            mapping[label] = by_alias[label]
        else:
            fname = field_name(label, used)
            mapping[label] = fname
            new_fields.append({"name": fname, "type": "esriFieldTypeDouble", "alias": label,
                               "sqlType": "sqlTypeOther", "nullable": True, "editable": True})
    if new_fields:
        tbl.manager.add_to_definition({"fields": new_fields})
        log.info("%s : %d champ(s) ajoute(s) : %s", name, len(new_fields),
                 ", ".join(f"{f['name']} ({f['alias']})" for f in new_fields))
        tbl = Table(tbl.url, gis)
    absent = [a for a in by_alias if a not in mapping and by_alias[a] != year_field]
    if absent:
        log.info("%s : ligne(s) plus presente(s) dans ODS, champ conserve mais vide : %s", name, absent)

    # 2. enregistrements : une ligne par annee
    adds = []
    for _, row in wide.iterrows():
        attrs = {year_field: int(row["annee"])}
        for label in lines:
            v = row[label]
            attrs[mapping[label]] = None if pd.isna(v) else float(v)
        adds.append({"attributes": attrs})

    # 3. ajout puis suppression des anciens (la table n'est jamais vide)
    flc = FeatureLayerCollection(tbl.url.rsplit("/", 1)[0], gis)
    caps = flc.properties.capabilities
    need_edit = not all(c in caps for c in ("Create", "Delete"))
    try:
        if need_edit:
            flc.manager.update_definition({"capabilities": EDIT_CAPS})
        old_ids = tbl.query(where="1=1", return_ids_only=True).get("objectIds") or []

        res = tbl.edit_features(adds=adds)["addResults"]
        added = [r["objectId"] for r in res if r.get("success")]
        if len(added) != len(adds):
            if added:
                tbl.edit_features(deletes=",".join(map(str, added)))
            errors = [r.get("error") for r in res if not r.get("success")]
            raise RuntimeError(f"ajout refuse, donnees precedentes conservees : {errors[:3]}")

        if old_ids:
            tbl.edit_features(deletes=",".join(map(str, old_ids)))
    finally:
        if need_edit:
            flc.manager.update_definition({"capabilities": caps})

    n = tbl.query(where="1=1", return_count_only=True)
    log.info("%s : table ArcGIS a jour, %d annee(s) (%d-%d), %d champ(s) ligne de bus (%s)",
             name, n, int(wide["annee"].min()), int(wide["annee"].max()), len(lines), oid)
    return n


# --- Main ---------------------------------------------------------------------
def main() -> int:
    cfg = read_config()
    datasets = get_datasets(cfg)

    built = build_all(cfg)                       # 1. CSV
    failures = len(datasets) - len(built)

    s = arcgis_settings(cfg)                      # 2. ArcGIS
    gis = connect(s)
    log.info("Connecte a %s en tant que %s", gis.url, gis.users.me.username)
    ensure_folder(gis, s["folder"])

    out = json.loads(OUT_FILE.read_text(encoding="utf-8")) if OUT_FILE.exists() else {}
    for ds in datasets:
        if ds.key not in built:
            log.warning("%s : CSV non regenere -> table ArcGIS laissee telle quelle", ds.service_name)
            continue
        try:
            tbl = get_table(gis, ds, s)
            n = sync(gis, tbl, built[ds.key], ds.service_name)
            out[ds.key] = {
                "service": ds.service_name,
                "layer_url": tbl.url,
                "records": n,
                "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        except Exception:
            failures += 1
            log.exception("Echec de la MAJ ArcGIS de %s", ds.key)

    OUT_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Termine : %d echec(s) sur %d jeu(x). Recap dans %s", failures, len(datasets), OUT_FILE)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
