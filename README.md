# ça brûle — carte des manifestations en France

Carte en direct des manifestations, grèves, blocages et rassemblements en France, construite à partir des titres de la presse en ligne.
Démo : **https://oraclemarin.fr/cabrule/**

![Carte](static/og.png)

## Ce que ça fait

- Lit en continu des flux de presse (RSS) et des agrégateurs d'actualités, garde les titres qui parlent de mobilisation, repère la commune citée et place un point sur la carte. Chaque point renvoie à l'article source.
- Mise à jour toutes les 10 minutes. Recherche d'une ville, zoom qui interroge la presse du secteur, filtres par thème (lycéens, agriculteurs, travail, transports, santé, écologie…).
- Deux cartes complémentaires, issues de recensements associatifs publics : féminicides conjugaux ([collectif Féminicides par compagnons ou ex](https://www.feminicides.fr/)) et décès liés à la police ([Désarmons-les](https://desarmons.net/)). Noms retirés, positions ramenées au centre de la commune.
- Pages rendues côté serveur pour le référencement : une page par ville active, pages des deux cartes, page « sources et méthode », `sitemap.xml`, `llms.txt`.

## Ce que contient ce dépôt

- `app.py` : le serveur (collecte, géocodage, pages rendues côté serveur, API). Bibliothèque standard uniquement, aucune dépendance.
- `cadrage.py`, `cadrage2.py`, `cadrage_extra.py`, `cadrage_page.py` : **la mesure du cadrage** ([page publique](https://oraclemarin.fr/cabrule/cadrage)). Les listes de mots par famille (désordre, autorité, suites, voix, victimes, parole, pratique…) sont dans `cadrage.py`, en clair, et appliquées de la même façon aux titres de chaque type de média, sans modèle de langage. Un autre choix de mots donnerait d'autres chiffres : c'est un choix éditorial, publié pour être critiqué.
- `tv/` + `tv_page.py` : **les sous-titres du direct de franceinfo** ([page publique](https://oraclemarin.fr/cabrule/franceinfo)). `collect.py` lit les sous-titres, `topics.py` classe les passages avec les mêmes listes de mots, `noms.py` et `noms.json` repèrent les personnalités citées. Seuls des comptages par jour et par heure sont publiés ; les passages citant un candidat sont gardés en privé 7 jours puis purgés (`tv_purge.py`). Aucun texte de sous-titre n'est dans ce dépôt ni sur le site.
- `hs.py` + `hs_model.json` : tri des titres hors-sujet et des comptes rendus tardifs (régression logistique sur mots, modèle lisible).
- `luttes.py`, `rue.py`, `travail.py`, `visits.py` : cartes et mesures annexes.
- Les fichiers de données (bases, stocks d'articles, jeux de données publics) ne sont pas dans le dépôt : ils sont régénérés depuis les sources publiques. Certains modules supposent donc des fichiers à produire d'abord ; l'adresse de contact est remplacée par `ADRESSE_DE_CONTACT`, à renseigner.

## Comment c'est fait

- **Bibliothèque standard uniquement** : `http.server`, `urllib`, `xml.etree`… Le front est une page HTML avec [Leaflet](https://leafletjs.com/) (inclus dans `static/lib`).
- **Géocodage** : la commune est détectée dans le titre à l'aide de la base officielle des communes ([geo.api.gouv.fr](https://geo.api.gouv.fr/)), téléchargée au premier lancement.
- **Cascade de sources** : flux RSS directs, Google Actualités, Bing Actualités, GDELT, Mastodon. Quand une source limite les requêtes (HTTP 429/503), un disjoncteur la met en pause et les autres prennent le relais ; pour la recherche d'une commune, dernier recours sur les articles déjà collectés.
- **Charge allégée** : l'API n'envoie pas les liens d'articles (56 % du poids) ; un clic passe par une redirection courte `/go/<id>`. Passage de 323 Ko à 39 Ko compressés.
- **Persistance** : les articles des dernières 24 h sont sauvegardés dans `store.json` pour survivre à un redémarrage.

## Lancer en local

```bash
python app.py            # http://localhost:8765
PORT=8780 python app.py  # autre port
```

Variables d'environnement : `PORT` (défaut 8765), `BASE_URL` et `BASE_PATH` (adresse publique et préfixe d'URL, pour les liens canoniques, le sitemap et les pages ville ; par défaut `https://oraclemarin.fr/cabrule` et `/cabrule`).

La première collecte prend 1 à 2 minutes. Évitez de redémarrer en boucle : chaque démarrage complet envoie une centaine de requêtes aux sources et peut vous faire limiter.

## Limites

- Carte **indicative** : elle ne montre que ce que la presse relaie, avec des erreurs de localisation possibles. Une mention dans un titre n'est pas une confirmation.
- Ce n'est pas un décompte officiel du nombre de manifestants ni d'incidents.
- Seuls le titre, la source, la date et le lien sont conservés ; les contenus des articles restent la propriété de leurs éditeurs.

## Licence et crédits

Code : licence [MIT](LICENSE). Les données et les textes publiés sur oraclemarin.fr sont sous [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/deed.fr) (citation obligatoire, usage commercial sur accord) ; les sources d'origine gardent leur propre licence.
Fond de carte © contributeurs [OpenStreetMap](https://www.openstreetmap.org/copyright). Leaflet : licence BSD-2-Clause.

Édité par Quentin Closset — [Oracle Marin](https://oraclemarin.fr/) · [À propos](https://oraclemarin.fr/apropos/)
