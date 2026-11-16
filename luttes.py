"""Luttes locales contre des projets (bétonnage, transport, industrie…), d'après la carte collaborative
Terres de Luttes (lutteslocales.gogocarto.fr), sous licence ODbL. Pas un recensement d'événements datés :
chaque fiche est une lutte en cours ou gagnée ; la date retenue est celle de la dernière mise à jour."""
import json
import time
import urllib.request
from datetime import datetime

API = "https://lutteslocales.gogocarto.fr/api/elements.json?limit=5000"
UA = {"User-Agent": "ca-brule/1.0 (+https://oraclemarin.fr/cabrule/)"}


def fetch(nearest):
    d = json.load(urllib.request.urlopen(urllib.request.Request(API, headers=UA), timeout=60))
    items = []
    for e in d.get("data", []):
        g = e.get("geo") or {}
        lat, lon = g.get("latitude"), g.get("longitude")
        if lat is None or lon is None:
            continue
        cats = e.get("categories") or []
        won = "Lutte gagnée" in cats
        cat = next((c for c in cats if c != "Lutte gagnée"), "")
        try:
            ts = datetime.fromisoformat(e.get("updatedAt") or e.get("createdAt")).timestamp()
        except Exception:
            ts = time.time()
        c = nearest(float(lat), float(lon))
        url = (e.get("Article") or e.get("URL_1") or "").strip() or "https://lutteslocales.gogocarto.fr/annuaire#/fiche/x/%s" % e["id"]
        items.append({"ts": ts, "lat": c["lat"] if c else round(float(lat), 3), "lon": c["lon"] if c else round(float(lon), 3),
                      "place": c["nom"] if c else (e.get("Commune") or "").split(" (")[0], "dep": c["dep"] if c else "",
                      "title": (e.get("name") or "").strip(), "projet": (e.get("Projet") or "").strip(), "cat": cat, "won": won,
                      "collectif": (e.get("Collectif_1") or "").strip(),
                      "text": " ".join((e.get("Description") or "").split())[:420], "url": url,
                      "fiche": "https://lutteslocales.gogocarto.fr/annuaire#/fiche/x/%s" % e["id"]})
    return items
