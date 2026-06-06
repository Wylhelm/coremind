# False Positives & Narrative Hallucination Loop — 2026-05-31

## Résumé

CoreMind a généré **4 faux positifs** en une matinée et a manqué une vraie situation (visiteurs). Le système n'est pas fiable en l'état.

---

## Faux positif #1 : Apple Health "données figées"

**Alerte CoreMind:** « Les données Apple Health (sommeil, pas, fréquence cardiaque) restent figées à des valeurs statiques (9,26 h de sommeil, 39 pas) depuis au moins 5 jours. »

**Réalité:** Les données varient normalement.

| Date | Sommeil | Pas (Watch+iPhone) | RHR |
|------|---------|-------------------|-----|
| 26 mai | 6.3h | — | 65 |
| 27 mai | 3.0h | 402 | 75 |
| 28 mai | 1.6h | 2,179 | 74 |
| 29 mai | 2.9h | 3,431 | 80 |
| 30 mai | — | 1,172 | 72 |
| 31 mai | 4.3h | 1,328 | 84 |

Webhook fonctionnel: 6 syncs entre le 30 mai 13:18 et le 31 mai 14:02.

**Cause:** Le reasoning model (deepseek-v4-pro) a halluciné l'observation, puis l'a répétée 14 fois en boucle.

---

## Faux positif #2 : OAuth Gmail "en échec"

**Alerte CoreMind:** « Je te préviens que l'accès à tes emails échoue et je vérifie la configuration OAuth. »

**Réalité:** Gmail fonctionne parfaitement. Token OAuth renouvelé le 30 mai 17:34. Plugin gog poll avec succès toutes les 30 min (`unread=5` constant).

**Cause:** Le reasoning model a fabriqué une panne inexistante.

---

## Faux positif #3 : Narrative hallucination loop (bug structurel)

**Symptôme:** "Les données Apple Health restent figées" a été ajouté comme observation narrative **14 fois** entre 01:59 et 09:59.

Mécanisme:
1. Cycle reasoning N : le LLM hallucine « données figées » → ajouté au narrative
2. Cycle N+1 : le LLM voit l'observation dans le narrative → la répète (légèrement reformulée) → ré-ajoutée
3. Boucle infinie — l'hallucination s'auto-alimente

**Impact:** Le narrative est saturé par l'hallucination. Les vraies observations (Tapo, présence, etc.) sont noyées.

---

## Faux négatif : Visiteurs non détectés

**Situation réelle:** Guillaume avait de la visite en fin de semaine (29-30 mai).

**Ce que CoreMind a envoyé:** « Pause bien-être — Je te vois dans le salon depuis 2h00. Tout va bien ? »

**Ce que CoreMind aurait dû voir:** Le plugin Tapo détectait bien des personnes:
```
29 mai 09:01 — Guillaume working at desk ✅
31 mai 13:21 — Mathieu ✅
31 mai 13:26 — Guillaume, Aurélie, Julie, and Jeff ✅
31 mai 13:32 — Guillaume, Julie, and others (+ Minuit) ✅
31 mai 13:38 — Guillaume and Aurélie ✅
```

**Cause:** Les observations Tapo étaient dans les logs du plugin, mais le narrative du daemon était saturé par la boucle d'hallucination "santé figée". Les observations de présence n'ont jamais atteint la couche de raisonnement.

---

## Root Causes

### 1. Narrative hallucination loop (bug #1, le plus grave)
- **Fichier probable:** `daemon_narrative.py` ou équivalent
- Le narrative n'a pas de **déduplication** — la même observation peut être ajoutée indéfiniment
- Le narrative n'a pas de **grounding check** — une observation n'est pas validée contre les données réelles avant d'être ajoutée
- Le narrative n'a pas de **limite de répétition** — une observation fausse pollue tout le contexte

### 2. Seuils trop bas
- `min_salience = 0.25` (remonté à 0.40 le 31 mai)
- `min_confidence = 0.30` (remonté à 0.45 le 31 mai)
- `health autonomy = 0.5` (descendu à 0.2 le 31 mai)

Même avec ces seuils, les hallucinations avec salience >0.40 passeraient.

### 3. Modèle d'intention peu fiable
- `mistral-small3.2:24b` est un petit modèle, plus sujet aux hallucinations
- Les scores de salience/confiance qu'il génère ne sont pas fiables

### 4. Anomaly checker instable
- Traceback dans `daemon_anomalies.py:184` à 06:46 — le checker lui-même a crashé

---

## Correctifs appliqués (mitigation temporaire)

| Paramètre | Avant | Après |
|-----------|-------|-------|
| `min_salience` | 0.25 | 0.40 |
| `min_confidence` | 0.30 | 0.45 |
| `health` autonomy | 0.50 | 0.20 |

CoreMind désactivé en attendant un fix structurel.

---

## Correctifs nécessaires (structurels)

### Priority 1: Narrative deduplication
- Détecter les observations identiques/similaires
- Max 2-3 occurrences de la même observation
- Utiliser un embedding similarity check ou un hash du texte normalisé

### Priority 2: Grounding before observation
- Avant d'ajouter une observation au narrative, valider contre les données réelles
- Ex: "données figées" → requêter InfluxDB pour vérifier si les valeurs changent vraiment
- Ex: "OAuth échoue" → tester l'API Gmail

### Priority 3: Stronger intention model
- Remplacer `mistral-small3.2:24b` par un modèle plus fiable
- Ou utiliser le même modèle que reasoning pour l'intention

### Priority 4: Anomaly checker stability
- Fix du traceback dans `daemon_anomalies.py:184`
- Ajouter un try/except autour de notify_router.notify()

---

## Data collected

- Logs: `~/.coremind/logs/daemon.log`, `plugin-tapo.log`, `plugin-gog.log`, `plugin-health.log`
- Config au moment des bugs: `config.toml` (min_salience=0.25, min_confidence=0.30)
- Tapo vision analysis shows person detection working correctly
- Gmail polling working correctly every 30 min
- InfluxDB health data showing real variations (not frozen)
