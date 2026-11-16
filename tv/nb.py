"""Classifieur de sujets maison pour les passages de franceinfo : Bayes naïf multinomial (Python pur), unigrammes + bigrammes.

train(passages, labels) -> modèle (dict JSON) ; predict(model, text) -> (sujet, probabilité).
Le modèle ne contient que des poids de mots, aucune phrase."""
import json
import math
import re
import unicodedata
from collections import Counter, defaultdict

STOP = set("le la les de des du un une et en au aux a à pour par sur dans avec ce cette ces qui que est sont ont son sa ses leur leurs plus pas ne se il elle ils elles nous vous on ou mais donc or ni car y d l s c j n m qu t "
           "apres avant sous entre vers chez cest nest ete etre fait faire comme tout tous toute toutes aussi encore deja tres bien sans ainsi depuis lors ca quand dont si meme autres autre je tu lui eux moi toi mon ma mes ton ta tes "
           "notre nos votre vos ceux celles celui celle cet ici voici voila veut peut doit va vont avait avaient sera seront fut etaient alors donc oui non voila bon".split())


def norm(s):
    s = unicodedata.normalize("NFD", (s or "").lower().replace("’", "'"))
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def feats(text):
    toks = [w for w in re.findall(r"[a-z]+", norm(text)) if len(w) > 2 and w not in STOP]
    f = Counter(toks)
    for a, b in zip(toks, toks[1:]):
        f[a + "_" + b] += 1
    return f


def train(passages, labels, alpha=0.5, min_df=2):
    classes = sorted(set(labels))
    df = Counter()
    docs = [feats(p) for p in passages]
    for d in docs:
        df.update(d.keys())
    vocab = {w for w, n in df.items() if n >= min_df}
    cnt = {c: Counter() for c in classes}
    ndoc = Counter(labels)
    for d, y in zip(docs, labels):
        for w, n in d.items():
            if w in vocab:
                cnt[y][w] += n
    V = len(vocab)
    model = {"classes": classes, "prior": {c: math.log(ndoc[c] / len(labels)) for c in classes}, "w": {}, "unk": {}}
    for c in classes:
        tot = sum(cnt[c].values()) + alpha * V
        model["unk"][c] = math.log(alpha / tot)
    for w in vocab:
        model["w"][w] = {c: round(math.log((cnt[c][w] + alpha) / (sum(cnt[c].values()) + alpha * V)), 4) for c in classes if cnt[c][w] > 0}
    model["unk"] = {c: round(v, 4) for c, v in model["unk"].items()}
    return model


def predict(model, text):
    f = feats(text)
    sc = {}
    for c in model["classes"]:
        s = model["prior"][c]
        for w, n in f.items():
            if w in model["w"]:
                s += n * model["w"][w].get(c, model["unk"][c])
        sc[c] = s
    m = max(sc.values())
    z = {c: math.exp(v - m) for c, v in sc.items()}
    tot = sum(z.values())
    best = max(z, key=z.get)
    return best, z[best] / tot


def save(model, path):
    json.dump(model, open(path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))


def load(path):
    return json.load(open(path, encoding="utf-8"))
