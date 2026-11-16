"""Efface les passages conservés depuis plus de 7 jours, SEULEMENT si la sauvegarde du PC les a déjà copiés (marqueur écrit par le PC)."""
import sqlite3, os, datetime as dt
DB, M = "./tv_textes/textes.db", "./tv_textes/dernier_backup.txt"
if not (os.path.exists(DB) and os.path.exists(M)):
    print(dt.datetime.now().isoformat(timespec="seconds"), "rien à purger (base ou marqueur absent)"); raise SystemExit
sauve = open(M).read().strip()[:19]            # dernier passage copié par le PC (heure de Paris)
lim = (dt.datetime.now() - dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
db = sqlite3.connect(DB, timeout=60)
n0 = db.execute("select count(*) from passages").fetchone()[0]
n = db.execute("select count(*) from passages where substr(t,1,19) < ? and substr(t,1,19) <= ?", (lim, sauve)).fetchone()[0]
db.execute("delete from passages where substr(t,1,19) < ? and substr(t,1,19) <= ?", (lim, sauve)); db.commit(); db.execute("vacuum")
print(dt.datetime.now().isoformat(timespec="seconds"), "purge : %d passages effacés sur %d (limite 7 jours %s, sauvegarde jusqu'à %s)" % (n, n0, lim, sauve))
