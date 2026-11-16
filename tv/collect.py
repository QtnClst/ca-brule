"""Collecte des sous-titres du direct franceinfo (france.tv), classement immédiat, puis oubli du texte (sauf les passages citant un candidat : conservés en privé 7 jours, voir stocker()).

Chaque passage (toutes les 3 h) :
  1. lit la liste des segments de sous-titres (une requête) et ne télécharge que les segments NOUVEAUX (une requête chacun, avec une pause) ;
  2. découpe en passages d'environ 40 mots, les classe (champs lexicaux de /cadrage + sujets) ;
  3. n'enregistre que des COMPTAGES par jour et par heure dans tv.db ; le texte reste en mémoire ; seuls les passages citant un candidat sont écrits, en privé, pour 7 jours (./tv_textes).
Usage : python collect.py [--max-segments N]"""
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

for _p in ("/opt/tvlib", "/tmp/ytlib"):  # sur le serveur, yt-dlp est installé hors du système
    if os.path.isdir(_p):
        sys.path.insert(0, _p)

import yt_dlp  # noqa: E402

import topics as T  # noqa: E402
import noms as N  # noqa: E402
CTX_ONLY = "--ctx-only" in sys.argv
NAMES_ONLY = "--names-only" in sys.argv or CTX_ONLY

HERE = os.path.dirname(os.path.abspath(__file__))
URL = "https://www.france.tv/franceinfo/direct.html"
TRACK = "qsa"
STATE = os.path.join(HERE, "state.json")
DB = os.path.join(HERE, "tv.db")
PARIS = ZoneInfo("Europe/Paris")
SEG_SECONDS = 7.68
PAUSE = 0.15      # secondes entre deux segments : on reste au rythme d'un lecteur vidéo
PASSAGE_WORDS = 40
UA = {"User-Agent": "ca-brule-comptages/1.0 (analyse de sous-titres, comptages uniquement)"}  # on dit qui on est


import hashlib  # noqa: E402
TEXTES_DB = "./tv_textes/textes.db"
_tdb = None


def stocker(t_iso, noms, text):
    """Conservation PRIVÉE (7 jours, voir tv_purge.py) des seuls passages citant un candidat ; jamais exportée ni publiée."""
    global _tdb
    try:
        if _tdb is None:
            os.makedirs("./tv_textes", mode=0o700, exist_ok=True)
            _tdb = sqlite3.connect(TEXTES_DB, timeout=30)
            _tdb.execute("CREATE TABLE IF NOT EXISTS passages(cle TEXT PRIMARY KEY, t TEXT, noms TEXT, texte TEXT)")
            os.chmod(TEXTES_DB, 0o600)
        cle = hashlib.sha1((t_iso + "|" + text).encode("utf-8")).hexdigest()[:20]
        _tdb.execute("INSERT OR IGNORE INTO passages VALUES (?,?,?,?)", (cle, t_iso, json.dumps(noms, ensure_ascii=False), text))
        _tdb.commit()
    except Exception as e:
        print("stockage texte :", e)


def get(url, tries=3):
    last = None
    for i in range(tries):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20).read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                raise  # on s'arrête : ne pas insister si le site refuse
            last = e
        except Exception as e:
            last = e
        time.sleep(1 + i)
    raise last


def playlist():
    with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "no_warnings": True, "socket_timeout": 20}) as y:
        info = y.extract_info(URL, download=False)
    u = info["subtitles"][TRACK][0]["url"]
    text = get(u)
    base = u.rsplit("/", 1)[0] + "/"
    seq0 = int(re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", text).group(1))
    pdt = re.search(r"#EXT-X-PROGRAM-DATE-TIME:(\S+)", text)
    t0 = datetime.fromisoformat(pdt.group(1).replace("Z", "+00:00")) if pdt else datetime.now(timezone.utc) - timedelta(seconds=SEG_SECONDS * 1875)
    segs = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
    return base, seq0, t0, segs


def cues_of(vtt):
    out = []
    for block in re.split(r"\n\s*\n", vtt):
        lines = [l for l in block.strip().splitlines() if l.strip()]
        for i, l in enumerate(lines):
            if "-->" in l:
                t = " ".join(re.sub(r"<[^>]+>", "", x).strip() for x in lines[i + 1:])
                if t:
                    out.append(t)
                break
    return out


def db():
    c = sqlite3.connect(DB, timeout=30)
    c.executescript("""
        CREATE TABLE IF NOT EXISTS totals(day TEXT, hour INTEGER, passages INTEGER, words INTEGER, PRIMARY KEY(day, hour));
        CREATE TABLE IF NOT EXISTS topic_counts(day TEXT, hour INTEGER, topic TEXT, n INTEGER, PRIMARY KEY(day, hour, topic));
        CREATE TABLE IF NOT EXISTS family_counts(day TEXT, hour INTEGER, fam TEXT, n INTEGER, PRIMARY KEY(day, hour, fam));
        CREATE TABLE IF NOT EXISTS name_counts(day TEXT, hour INTEGER, name TEXT, n INTEGER, PRIMARY KEY(day, hour, name));
        CREATE TABLE IF NOT EXISTS name_context(day TEXT, hour INTEGER, name TEXT, ctx TEXT, n INTEGER, PRIMARY KEY(day, hour, name, ctx));
        CREATE TABLE IF NOT EXISTS runs(at TEXT, segments INTEGER, passages INTEGER, first_seq INTEGER, last_seq INTEGER, note TEXT);
    """)
    return c


def main(max_segments=None):
    t_run = datetime.now(PARIS).isoformat(timespec="seconds")
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    last = state.get("last_seq")
    base, seq0, t0, segs = playlist()
    first_seq, last_seq = seq0, seq0 + len(segs) - 1
    start = (last + 1) if last is not None and last >= first_seq - 1 else first_seq
    if NAMES_ONLY:
        start = first_seq   # repasse sur toute la fenêtre actuelle (environ 3 h), sans rien changer d'autre
    todo = [(s, segs[s - seq0]) for s in range(start, last_seq + 1)]
    if max_segments:
        todo = todo[-max_segments:]
    c = db()
    prev, buf, buf_t = [], [], None
    n_seg = n_pass = 0
    gap = bool(last is not None and last < first_seq - 1)
    errors = 0

    def flush(t):
        nonlocal n_pass
        text = " ".join(buf).strip()
        if not text:
            return
        loc = t.astimezone(PARIS)
        day, hour = loc.strftime("%Y-%m-%d"), loc.hour
        _cx = N.contexts(text)
        _nm = N.match(text)
        if _nm:
            stocker(loc.isoformat(timespec="seconds"), _nm, text)
        for nm in _nm:
            if not CTX_ONLY:
                c.execute("INSERT INTO name_counts VALUES (?,?,?,1) ON CONFLICT(day,hour,name) DO UPDATE SET n=n+1", (day, hour, nm))
            for cx in _cx:
                c.execute("INSERT INTO name_context VALUES (?,?,?,?,1) ON CONFLICT(day,hour,name,ctx) DO UPDATE SET n=n+1", (day, hour, nm, cx))
        if NAMES_ONLY:
            n_pass += 1
            return
        c.execute("INSERT INTO totals VALUES (?,?,1,?) ON CONFLICT(day,hour) DO UPDATE SET passages=passages+1, words=words+excluded.words", (day, hour, len(text.split())))
        tp = T.topic_of(text)
        c.execute("INSERT INTO topic_counts VALUES (?,?,?,1) ON CONFLICT(day,hour,topic) DO UPDATE SET n=n+1", (day, hour, tp))
        for f in T.families_of(text):
            c.execute("INSERT INTO family_counts VALUES (?,?,?,1) ON CONFLICT(day,hour,fam) DO UPDATE SET n=n+1", (day, hour, f))
        n_pass += 1

    for seq, name in todo:
        try:
            vtt = get(base + name)
            errors = 0
        except urllib.error.HTTPError as e:
            print("refus HTTP", e.code, "à la séquence", seq, "- arrêt")
            break
        except Exception as e:
            errors += 1
            print("erreur segment", seq, e)
            if errors >= 5:
                break
            continue
        n_seg += 1
        t_seg = t0 + timedelta(seconds=SEG_SECONDS * (seq - seq0))
        for cue in cues_of(vtt):
            if cue in prev[-3:]:
                continue  # un sous-titre à cheval sur deux segments apparaît deux fois
            prev.append(cue)
            prev = prev[-6:]
            if not buf:
                buf_t = t_seg
            buf.append(cue)
            if len(" ".join(buf).split()) >= PASSAGE_WORDS and re.search(r"[.!?]$", cue):
                flush(buf_t)
                buf, buf_t = [], None
        last_done = seq
        if not NAMES_ONLY:
            state["last_seq"] = last_done
        time.sleep(PAUSE)
    if buf and buf_t:
        flush(buf_t)
    if not NAMES_ONLY:
        c.execute("INSERT INTO runs VALUES (?,?,?,?,?,?)", (t_run, n_seg, n_pass, todo[0][0] if todo else None, state.get("last_seq"), "trou possible" if gap else ""))
    c.commit()
    c.close()
    if not NAMES_ONLY:
        json.dump(state, open(STATE, "w"))
    print("%s : %d segments, %d passages classés%s" % (t_run, n_seg, n_pass, " (trou : le dernier passage date de plus de 4 h)" if gap else ""))


if __name__ == "__main__":
    ms = int(sys.argv[sys.argv.index("--max-segments") + 1]) if "--max-segments" in sys.argv else None
    main(ms)
