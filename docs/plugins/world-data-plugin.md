# Plugin World Data — Sources de données externes pour le World Model

> **Statut :** Planification — en attente de review Guillaume  
> **Date :** 2026-05-27  
> **Décision :** Ne pas coder avant GO

---

## Architecture

**1 plugin config-driven** (`plugin-world-data/`) — chaque collecteur = une section de config TOML.
Le plugin émet des `WorldEvents` avec une salience de base → CoreMind L4/L5 évalue et décide des notifications.

```
plugin-world-data/
├── main.py                    ← Point d'entrée du plugin
├── collectors/
│   ├── __init__.py
│   ├── crypto.py              ← CoinGecko
│   ├── traffic.py             ← Québec 511
│   ├── air_quality.py         ← Environnement Canada
│   ├── currency.py            ← Taux de change
│   ├── gas_price.py           ← Régie de l'énergie / CAA
│   ├── news.py                ← Hacker News / RSS
│   └── hydro.py               ← Hydro-Québec pannes
└── world_data.toml            ← Configuration déclarative
```

---

## Collecteurs — MVP (v1.0)

### 1. Crypto-monnaies 🪙

| Champ | Valeur |
|-------|--------|
| **API** | CoinGecko (gratuit, pas de clé) |
| **URL** | `https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=cad,usd&include_24hr_change=true` |
| **Rate limit** | 30 calls/min |
| **Fréquence** | 300s (5 min) |
| **Entités** | `crypto:bitcoin`, `crypto:ethereum` |
| **Attributs** | `price_cad`, `price_usd`, `change_24h_pct` |

**Salience de base :**
- Δ 24h > 5% → 0.5
- Δ 24h > 10% → 0.7
- Δ 24h < 1% → 0.1

**Exemple de notif CoreMind :**
> "Bitcoin a grimpé de 8% cette semaine — ton portefeuille crypto suit-il ? Veux-tu que je track tes positions ?"

---

### 2. Caméras de circulation — Québec 511 🚗

| Champ | Valeur |
|-------|--------|
| **API** | Québec 511 — Données ouvertes |
| **URL** | `https://www.quebec511.info/fr/Diffusion/DonneesOuvertes` |
| **Format** | XML/JSON |
| **Gratuit** | ✅ Open data gouvernemental |
| **Fréquence** | 900s (15 min) |
| **Entités** | `traffic_camera:<id>`, `road_condition:<route>` |
| **Attributs** | `status`, `snapshot_url`, `condition`, `visibility` |

**Caméras suggérées :**
- Pont Pierre-Laporte
- Autoroute 20 Ouest
- Autoroute 40 Nord (vers Lac-aux-Sables)
- Boulevard Charest
- Autoroute 73 / Henri-IV

**Salience de base :**
- Route bloquée/fermée → 0.7
- Accident signalé → 0.6
- Conditions hivernales → 0.5
- Circulation dense → 0.3
- Normale → 0.1

**Exemple :**
> "La 20 est bloquée à cause d'un accident — prévois 20 min de plus pour rentrer"
> "Il neige sur l'autoroute 40 — les routes vers le chalet sont mauvaises"

---

### 3. Qualité de l'air / Environnement Canada 🌫️

| Champ | Valeur |
|-------|--------|
| **API** | Environnement Canada — AQHI |
| **Format** | XML/JSON |
| **Gratuit** | ✅ |
| **Fréquence** | 3600s (1h) |
| **Entités** | `air_quality:quebec` |
| **Attributs** | `aqhi`, `uv_index`, `pollen`, `smog_alert` |

**Salience de base :**
- AQHI > 6 → 0.6
- Alerte smog → 0.7
- UV > 7 → 0.4
- Pollen élevé → 0.3
- Normal → 0.1

**Exemple :**
> "Indice UV à 8 aujourd'hui — crème solaire si tu sors"
> "Alerte smog à Québec cet après-midi, ferme les fenêtres"

---

### 4. Taux de change 💱

| Champ | Valeur |
|-------|--------|
| **API** | CoinGecko (simple/price) ou Yahoo Finance |
| **Fréquence** | 3600s (1h) |
| **Entités** | `currency:cad_usd`, `currency:cad_eur` |
| **Attributs** | `rate`, `change_24h_pct` |

**Salience de base :**
- Δ > 1% → 0.3
- Δ > 2% → 0.5

**Exemple :**
> "Le dollar canadien a perdu 1.5% face au USD cette semaine — impact possible sur tes achats en ligne"

---

### 5. Prix de l'essence ⛽

| Champ | Valeur |
|-------|--------|
| **API** | Régie de l'énergie du Québec (prix plancher officiel) |
| **Backup** | CAA Québec (moyenne régionale) |
| **Gratuit** | ✅ |
| **Fréquence** | Hebdomadaire (Régie), Quotidien (CAA) |
| **Entités** | `gas_price:regular`, `gas_price:premium`, `gas_price:quebec_avg`, `gas_price:mtl_avg` |
| **Attributs** | `price_per_liter`, `change_weekly`, `region` |

**Salience de base :**
- Hausse > 5¢/L → 0.4
- Hausse > 10¢/L → 0.6
- Québec vs Montréal > 5¢ d'écart → 0.3

**Exemple :**
> "Le prix plancher de l'essence monte à 1,67 $/L cette semaine — fais le plein avant mercredi"
> "L'essence est 8¢ moins chère qu'à Montréal — profites-en si tu passes par là"

---

## Collecteurs — V1.1 (semaine suivante)

### 6. Actualités IA / Tech 📰

| Champ | Valeur |
|-------|--------|
| **API** | Hacker News API |
| **Fréquence** | 1800s (30 min) |
| **Entités** | `news_topic:ia`, `news_topic:tech`, `news_topic:quebec` |
| **Attributs** | `title`, `url`, `score`, `comments` |

**Filtrage :** Top 5 articles + mots-clés : IA, LLM, OpenAI, Ollama, Québec, immobilier

**Exemple :**
> "3 articles sur l'IA ce matin, dont un sur un nouveau modèle open-source"

---

### 7. Hydro-Québec — Pannes ⚡

| Champ | Valeur |
|-------|--------|
| **API** | Hydro-Québec — Info Pannes |
| **Fréquence** | 900s (15 min) |
| **Entités** | `power_outage:quebec_city`, `power_outage:lac_aux_sables` |
| **Attributs** | `active`, `customers_affected`, `estimated_restoration` |

**Salience de base :**
- Panne active dans ton secteur → 0.8
- Panne majeure (>1000 clients) → 0.6

**Exemple :**
> "Panne Hydro dans ton secteur — 500 clients affectés, rétablissement estimé 18h30"

---

## Collecteurs — V1.2+ (planifiés)

| # | Collecteur | API | Entité | Intérêt |
|---|-----------|-----|--------|---------|
| 8 | **Sports — Canadiens** | NHL API | `sports:canadiens` | Scores, classement |
| 9 | **Collectes municipales** | Ville de Québec | `waste_collection:*` | Rappel bac bleu/brun |
| 10 | **Aurores boréales** | NOAA 30-min | `aurora:quebec` | Probabilité visible |
| 11 | **Taux directeur BoC** | Banque du Canada | `interest_rate:boc` | Impact hypothèque |
| 12 | **Indices boursiers** | Yahoo Finance | `index:tsx`, `index:sp500` | Impact placements |
| 13 | **Séismes** | USGS / RNCan | `earthquake:quebec` | Tremblements régionaux |
| 14 | **GitHub Trending** | GitHub API | `github_trending:ai` | Repos IA populaires |
| 15 | **Nouveaux modèles Ollama** | Reddit r/LocalLLaMA | `new_model:ollama` | Sorties pertinentes |
| 16 | **ArXiv AI papers** | ArXiv API | `arxiv_paper:ai` | Papiers qui buzzent |
| 17 | **IPC / Inflation** | StatsCan | `inflation:canada` | Impact épargne |
| 18 | **Festivals Québec** | Ville de Québec | `festival:quebec` | FEQ, événements |
| 19 | **Cinéma / Netflix** | TMDB / What's On Netflix | `entertainment:new` | Sorties semaine |
| 20 | **ISS / Starlink** | Open Notify | `satellite:iss`, `satellite:starlink` | Passages visibles |
| 21 | **Alerte Amber** | Flux gouvernemental | `alert:amber` | Notif immédiate |
| 22 | **Travaux routiers** | Ville de Québec | `road_work:*` | Rues fermées |
| 23 | **RTC perturbations** | RTC Open Data | `transit:rtc` | Retards bus |
| 24 | **Nouvelles lois** | Gazette Canada | `legislation:new` | Impact fiscal/immo |

---

## Fichier de configuration (exemple)

```toml
# ~/.coremind/plugins/world_data.toml

[collectors.crypto]
enabled = true
interval_seconds = 300
coins = ["bitcoin", "ethereum"]

[collectors.traffic]
enabled = true
interval_seconds = 900
cameras = [
    "pont_pierre_laporte",
    "autoroute_20_ouest",
    "autoroute_40_nord",
    "autoroute_73",
]

[collectors.air_quality]
enabled = true
interval_seconds = 3600
city = "quebec"

[collectors.currency]
enabled = true
interval_seconds = 3600
pairs = ["cad_usd", "cad_eur"]

[collectors.gas_price]
enabled = true
interval_seconds = 86400
region = "quebec_city"

[collectors.news]
enabled = true
interval_seconds = 1800
keywords = ["Québec", "IA", "immobilier", "économie", "crypto"]
max_articles = 5

[collectors.hydro]
enabled = true
interval_seconds = 900
regions = ["quebec_city", "lac_aux_sables"]
```

---

## Notes d'implémentation

- **1 plugin CoreMind standard** — socket Unix, enregistrement clé ed25519, émission WorldEvents signés
- **aiohttp** pour toutes les requêtes HTTP (dépendance existante)
- **Cache local** dans le plugin pour éviter d'appeler les API trop souvent
- **Salience = base × (pertinence locale)** déterminée par le collecteur, surchargeable par L4
- **Mécanisme anti-flood** — si les données n'ont pas changé depuis le dernier cycle, ne pas ré-émettre
- **Le plugin n'envoie JAMAIS de notifications directes** — il émet des WorldEvents, CoreMind décide

---

## Références API

- CoinGecko : https://www.coingecko.com/fr/api (gratuit, 30 calls/min)
- Québec 511 : https://www.quebec511.info/fr/Diffusion/DonneesOuvertes (open data)
- Environnement Canada AQHI : https://dd.weather.gc.ca/air_quality/
- Régie de l'énergie : https://www.regie-energie.qc.ca/
- CAA Québec : https://www.caaquebec.com/fr/sur-la-route/essence/prix-essence/
- Hacker News : https://github.com/HackerNews/API
- Hydro-Québec pannes : https://pannes.hydroquebec.com/
- NHL API : https://api-web.nhle.com/
