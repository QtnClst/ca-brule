"""Exporte les COMPTAGES de tv.db (jamais de texte) en tv_data.json, puis (option --push) les envoie au serveur du site.
Usage : python export.py [--push]"""
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import topics as T

HERE = os.path.dirname(os.path.abspath(__file__))
# sur le serveur (--out-parent), le fichier est écrit à côté d'app.py, d'où la page le lit ; sur le PC il reste dans tv/
OUT = os.path.join(os.path.dirname(HERE) if "--out-parent" in sys.argv else HERE, "tv_data.json")
REMOTE = "utilisateur@serveur:/chemin/tv_data.json"


def main(push=False):
    c = sqlite3.connect(os.path.join(HERE, "tv.db"))
    c.execute("CREATE TABLE IF NOT EXISTS name_counts(day TEXT, hour INTEGER, name TEXT, n INTEGER, PRIMARY KEY(day, hour, name))")
    data = {
        "updated": datetime.now(ZoneInfo("Europe/Paris")).isoformat(timespec="minutes"),
        "totals": c.execute("select day, hour, passages, words from totals order by day, hour").fetchall(),
        "topics": c.execute("select day, hour, topic, n from topic_counts order by day, hour").fetchall(),
        "families": c.execute("select day, hour, fam, n from family_counts order by day, hour").fetchall(),
        "names": c.execute("select day, hour, name, n from name_counts order by day, hour").fetchall(),
        "names_ctx": c.execute("select day, hour, name, ctx, n from name_context order by day, hour").fetchall(),
        "names_meta": json.load(open(os.path.join(HERE, "noms.json"), encoding="utf-8")),
        "labels": T.TOPIC_LABEL,
        "model": {"train": 330, "accuracy": 82.4, "min_prob": T.MIN_PROB},
        "channel": "franceinfo",
    }
    c.close()
    json.dump(data, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print("tv_data.json :", len(data["totals"]), "lignes d'heures,", sum(r[2] for r in data["totals"]), "passages")
    if push:
        r = subprocess.run(["scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", OUT, REMOTE], capture_output=True, text=True, timeout=120)
        print("envoi au serveur :", "ok" if r.returncode == 0 else "ÉCHEC " + (r.stderr or "")[:200])


if __name__ == "__main__":
    main("--push" in sys.argv)
