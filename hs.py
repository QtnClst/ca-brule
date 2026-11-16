"""Tri des articles de « ça brûle » : hors-sujet, rétrospectives, comptes rendus tardifs.

Utilisé par app.py (ingest) :
    verdict(title, pub_ts) -> (action, detail)
      ("ok", None)                 : à garder au jour de publication
      ("retro", "YYYY-MM-DD")      : compte rendu d'un événement passé -> rangé à la date de l'événement
      ("reject", "ancien"|"hs")    : à écarter (rétrospective / commémoration, ou hors-sujet) ; detail = motif
Le classifieur (hs_model.json, régression logistique sur mots) ne travaille que sur les titres SANS preuve nette de
contestation : un titre qui contient « manifestants », « cortège », « lycéens », « grève », « préfet »… n'est jamais écarté.
"""
import json
import math
import os
import re
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")
HERE = os.path.dirname(os.path.abspath(__file__))
TH = float(os.environ.get("HS_THRESHOLD", "0.8"))
MIN_DAY = "2026-09-30"  # premier jour de l'historique

# ---------- preuve nette de contestation (protège un titre) ----------
EVID = re.compile(r"manifestant|manifestent|manifester|manifestation|\bmanifs?\b|cort[eè]ge|d[ée]fil[ée]|lyc[ée]en|[ée]tudiants?|blocus|blocage des|bloqu[ée]e?s? par|"
                  r"syndicat|intersyndicale|\bcgt\b|\bfsu\b|\bfo\b|solidaires|gr[eè]ve|pr[ée]fe[ct]|interdit|interpell|casseur|[ée]meute|heurts|lacrymo|\bcrs\b|"
                  r"forces de l.ordre|pillage|pilleur|piquet|gilets? jaunes?|acte (?:i{1,3}|iv|v|\d)\b|vie ch[eè]re|attentat|racis|antifa|opposants?|contre[- ]rassemblement|"
                  r"mouvement (?:social|lyc[ée]en|des lyc)|mobilisation (?:du \d|des (?:lyc|[ée]tud|prof|agri)|lyc[ée]|[ée]tud|sociale|syndicale)|colère (?:sociale|des (?:lyc|agri|prof|soignants))|"
                  r"droit de manifester|couvre-feu|violences (?:du|urbaines)|occupation (?:de l|du|des) (?:universit|facult|lyc|campus|b[âa]timent)", re.I)


RALLY = re.compile(r"rassembl|marche|hommage", re.I)  # un rassemblement d'aujourd'hui qui commémore un événement ancien reste une actualité


def has_evidence(title):
    return bool(EVID.search((title or "").replace("’", "'")))


# ---------- date de l'événement ----------
MONTHS = {"janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6, "juillet": 7,
          "août": 8, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12}
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
_MRE = "|".join(sorted(MONTHS, key=len, reverse=True))
EXPL = re.compile(r"(?<![\w/.])(\d{1,2})(?:er)?\s+(%s)(?:\s+(\d{4}))?(?![\w])" % _MRE, re.I)
HIER = re.compile(r"\bhier\b", re.I)
AVANT_HIER = re.compile(r"\bavant-hier\b", re.I)
JOUR_DERNIER = re.compile(r"\b(lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)\s+dernier\b", re.I)
MAX_BACK = 45


def event_day(title, pub_ts):
    """(jour 'YYYY-MM-DD' | None, motif) ; motif 'old' = date d'une autre année ou trop ancienne."""
    t = (title or "").replace("’", "'").replace("\xa0", " ")
    pub = datetime.fromtimestamp(pub_ts, PARIS).date()  # même fuseau que day_of() d'app.py
    found, oldyear = [], False
    for m in EXPL.finditer(t):
        dd, mo = int(m.group(1)), MONTHS[m.group(2).lower()]
        yr = int(m.group(3)) if m.group(3) else None
        if yr and yr < pub.year and not (pub.month <= 2 and yr == pub.year - 1 and mo >= 11):
            oldyear = True  # « 7 octobre 2023 », « 17 octobre 1961 » : l'année vise l'événement commémoré
            continue
        year = yr or pub.year
        try:
            d = datetime(year, mo, dd).date()
        except ValueError:
            continue
        if not yr and d > pub + timedelta(days=200):
            d = datetime(year - 1, mo, dd).date()
        found.append(d)
    if oldyear and not found:
        return None, "old-year"
    if found:
        if any(d >= pub for d in found):
            return None, ""
        d = max(found)
        if (pub - d).days <= MAX_BACK:
            return d.isoformat(), "date"
        return None, "old"
    if AVANT_HIER.search(t):
        return (pub - timedelta(days=2)).isoformat(), "avant-hier"
    if HIER.search(t):
        return (pub - timedelta(days=1)).isoformat(), "hier"
    m = JOUR_DERNIER.search(t)
    if m:
        delta = (pub.weekday() - JOURS.index(m.group(1).lower())) % 7 or 7
        return (pub - timedelta(days=delta)).isoformat(), "jour-dernier"
    return None, ""


# ---------- classifieur hors-sujet ----------
STOP = set("le la les de des du un une et en au aux a à pour par sur dans avec ce cette ces qui que est sont ont son sa ses leur leurs plus pas ne se il elle ils elles nous vous on ou mais donc or ni car y d l s c j n m qu t".split())


def _norm(s):
    s = unicodedata.normalize("NFD", s.lower().replace("’", "'"))
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def feats(title):
    t = _norm(title)
    toks = [w for w in re.findall(r"[a-z]+", t) if len(w) > 1]
    f = set()
    for w in toks:
        if w not in STOP:
            f.add(w)
    for a, b in zip(toks, toks[1:]):
        if not (a in STOP and b in STOP):
            f.add(a + "_" + b)
    if re.search(r"\b(19|20)\d\d\b", t) and not re.search(r"2026", t):
        f.add("__autre_annee")
    return f


_MODEL = {"b": 0.0, "w": {}}
try:
    with open(os.path.join(HERE, "hs_model.json"), encoding="utf-8") as _f:
        _MODEL = json.load(_f)
except Exception:
    pass


def hs_score(title):
    z = _MODEL["b"] + sum(_MODEL["w"].get(k, 0.0) for k in feats(title))
    return 1 / (1 + math.exp(-max(-30, min(30, z))))


def verdict(title, pub_ts):
    ev, why = event_day(title, pub_ts)
    if why == "old" or (why == "old-year" and not (has_evidence(title) or RALLY.search(title or ""))):
        return "reject", "ancien"
    if not has_evidence(title) and _MODEL["w"] and hs_score(title) >= TH:
        return "reject", "hs"
    if ev:
        if ev < MIN_DAY:  # avant le début de l'historique : pas de journée fantôme
            return "reject", "ancien"
        return "retro", ev
    return "ok", None
