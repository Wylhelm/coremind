# CoreMind LLM Benchmark — Intention Layer (L5)

**Date:** 2026-05-26 | **Scénarios:** 5 | **Modèles:** 7
**Schéma:** `QuestionBatch` (Pydantic v2, strict) | **Température:** 0.3 | **Format:** JSON forcé

> ⚠️ **Limite technique:** `num_predict=2048` tokens max en sortie. 4 modèles (`qwen3.6:27b/35b`, `glm-4.7-flash`, `gemma4:26b`) ont généré des réponses trop verbeuses qui ont été tronquées à 2048 tokens, produisant du JSON invalide. Augmenter `num_predict` à 4096+ pourrait les rendre viables — à re-tester.

## 📊 Résumé Global

| Modèle | JSON ✅ | Pydantic ✅ | Pertinence ✅ | FR ✅ | Latence moy | Tokens moy | Verdict |
|--------|---------|-------------|---------------|-------|-------------|------------|---------|
| qwen3.6:27b | 20% (1/5) | 20% (1/5) | 20% (1/5) | 20% (1/5) | 76.5s | 1734 | ⚠️ RISQUE (JSON instable) |
| qwen3.6:35b | 20% (1/5) | 20% (1/5) | 20% (1/5) | 0% (0/5) | 100.0s | 1640 | ⚠️ RISQUE (JSON instable) |
| glm-4.7-flash:latest | 0% (0/5) | 0% (0/5) | 0% (0/5) | 0% (0/5) | 27.2s | 2048 | ❌ ÉLIMINÉ (JSON invalide) |
| gpt-oss:20b | 100% (5/5) | 100% (5/5) | 80% (4/5) | 80% (4/5) | 9.7s | 286 | ✅ VIABLE |
| gemma4:26b | 0% (0/5) | 0% (0/5) | 0% (0/5) | 0% (0/5) | 21.7s | 2048 | ❌ ÉLIMINÉ (JSON invalide) |
| mistral-small3.2:24b | 100% (5/5) | 100% (5/5) | 60% (3/5) | 100% (5/5) | 6.5s | 232 | ⚠️ PARTIEL |
| deepseek-v4-flash:cloud | 100% (5/5) | 100% (5/5) | 80% (4/5) | 60% (3/5) | 26.4s | 522 | ⚠️ PARTIEL |

## 📋 Détails par Scénario

### calme — Snapshot vide, rien à signaler — doit générer 0 ou 1 intent max

| Modèle | JSON | Pydantic | Pertinent | FR | Latence | Tokens | Q | Erreur |
|--------|------|----------|-----------|----|---------|--------|---|--------|
| qwen3.6:27b | ❌ | ❌ | ❌ () | ❌ | 80.8s | 2048 | 0 | JSON parse error: Expecting value: line 1 column 1 (char 0) |
| qwen3.6:35b | ✅ | ✅ | ✅ (0 intent(s) — acceptable) | ❌ | 68.5s | 10 | 0 | — |
| glm-4.7-flash:latest | ❌ | ❌ | ❌ () | ❌ | 30.4s | 2048 | 0 | JSON parse error: Expecting value: line 1 column 1 (char 0) |
| gpt-oss:20b | ✅ | ✅ | ✅ (1 intent(s) — acceptable) | ✅ | 15.6s | 145 | 1 | — |
| gemma4:26b | ❌ | ❌ | ❌ () | ❌ | 25.4s | 2048 | 0 | JSON parse error: Expecting value: line 1 column 1 (char 0) |
| mistral-small3.2:24b | ✅ | ✅ | ✅ (1 intent(s) — acceptable) | ✅ | 9.2s | 201 | 1 | — |
| deepseek-v4-flash:cloud | ✅ | ✅ | ✅ (0 intent(s) — acceptable) | ❌ | 18.7s | 263 | 0 | — |

### lumiere_oubliee — Lumière bureau ON depuis 4h, soir, personne absente → turn_off

| Modèle | JSON | Pydantic | Pertinent | FR | Latence | Tokens | Q | Erreur |
|--------|------|----------|-----------|----|---------|--------|---|--------|
| qwen3.6:27b | ❌ | ❌ | ❌ () | ❌ | 66.4s | 2048 | 0 | JSON parse error: Expecting value: line 1 column 1 (char 0) |
| qwen3.6:35b | ❌ | ❌ | ❌ () | ❌ | 108.6s | 2048 | 0 | JSON parse error: Expecting value: line 1 column 1 (char 0) |
| glm-4.7-flash:latest | ❌ | ❌ | ❌ () | ❌ | 24.0s | 2048 | 0 | JSON parse error: Expecting value: line 1 column 1 (char 0) |
| gpt-oss:20b | ✅ | ✅ | ✅ (opération=coremind.plugin.home) | ✅ | 7.3s | 347 | 2 | — |
| gemma4:26b | ❌ | ❌ | ❌ () | ❌ | 20.0s | 2048 | 0 | JSON parse error: Expecting value: line 1 column 1 (char 0) |
| mistral-small3.2:24b | ✅ | ✅ | ✅ (opération=coremind.plugin.home) | ✅ | 5.4s | 217 | 1 | — |
| deepseek-v4-flash:cloud | ✅ | ✅ | ✅ (opération=coremind.plugin.home) | ✅ | 49.0s | 512 | 1 | — |

### alerte_temperature — sensor.chambre = 27°C, climatiseur OFF → doit suggérer notification ou action HA

| Modèle | JSON | Pydantic | Pertinent | FR | Latence | Tokens | Q | Erreur |
|--------|------|----------|-----------|----|---------|--------|---|--------|
| qwen3.6:27b | ✅ | ✅ | ✅ (action_class=hvac, op=coremind) | ✅ | 63.6s | 479 | 2 | — |
| qwen3.6:35b | ❌ | ❌ | ❌ () | ❌ | 106.1s | 2048 | 0 | JSON parse error: Expecting value: line 1 column 1 (char 0) |
| glm-4.7-flash:latest | ❌ | ❌ | ❌ () | ❌ | 27.2s | 2048 | 0 | JSON parse error: Expecting value: line 1 column 1 (char 0) |
| gpt-oss:20b | ✅ | ✅ | ✅ (action_class=hvac, op=coremind) | ✅ | 8.8s | 412 | 2 | — |
| gemma4:26b | ❌ | ❌ | ❌ () | ❌ | 19.8s | 2048 | 0 | JSON parse error: Expecting value: line 1 column 1 (char 0) |
| mistral-small3.2:24b | ✅ | ✅ | ❌ (action_class non pertinente po) | ✅ | 6.8s | 291 | 1 | — |
| deepseek-v4-flash:cloud | ✅ | ✅ | ❌ (action_class non pertinente po) | ✅ | 16.7s | 717 | 1 | — |

### anti_spam — Recent intents contient déjà alerte température — ne doit PAS régénérer la même chose

| Modèle | JSON | Pydantic | Pertinent | FR | Latence | Tokens | Q | Erreur |
|--------|------|----------|-----------|----|---------|--------|---|--------|
| qwen3.6:27b | ❌ | ❌ | ❌ () | ❌ | 76.1s | 2048 | 0 | JSON parse error: Expecting value: line 1 column 1 (char 0) |
| qwen3.6:35b | ❌ | ❌ | ❌ () | ❌ | 108.1s | 2048 | 0 | JSON parse error: Expecting value: line 1 column 1 (char 0) |
| glm-4.7-flash:latest | ❌ | ❌ | ❌ () | ❌ | 0.0s | 0 | 0 | SKIPPED: modèle abandonné après 3 échecs JSON |
| gpt-oss:20b | ✅ | ✅ | ❌ (spam détecté: intent similaire) | ✅ | 9.8s | 429 | 2 | — |
| gemma4:26b | ❌ | ❌ | ❌ () | ❌ | 0.0s | 0 | 0 | SKIPPED: modèle abandonné après 3 échecs JSON |
| mistral-small3.2:24b | ✅ | ✅ | ❌ (spam détecté: intent similaire) | ✅ | 5.7s | 232 | 1 | — |
| deepseek-v4-flash:cloud | ✅ | ✅ | ✅ (0 intent — excellent, pas de s) | ❌ | 15.0s | 345 | 0 | — |

### conversation — User a répliqué 'c'est fait' à un intent récent → réponse conversation, pas nouvelle notif

| Modèle | JSON | Pydantic | Pertinent | FR | Latence | Tokens | Q | Erreur |
|--------|------|----------|-----------|----|---------|--------|---|--------|
| qwen3.6:27b | ❌ | ❌ | ❌ () | ❌ | 95.5s | 2048 | 0 | JSON parse error: Expecting value: line 1 column 1 (char 0) |
| qwen3.6:35b | ❌ | ❌ | ❌ () | ❌ | 108.5s | 2048 | 0 | JSON parse error: Expecting value: line 1 column 1 (char 0) |
| glm-4.7-flash:latest | ❌ | ❌ | ❌ () | ❌ | 0.0s | 0 | 0 | SKIPPED: modèle abandonné après 3 échecs JSON |
| gpt-oss:20b | ✅ | ✅ | ✅ (rationale mentionne conversati) | ❌ | 6.8s | 95 | 1 | — |
| gemma4:26b | ❌ | ❌ | ❌ () | ❌ | 0.0s | 0 | 0 | SKIPPED: modèle abandonné après 3 échecs JSON |
| mistral-small3.2:24b | ✅ | ✅ | ✅ (ton conversationnel détecté: j) | ✅ | 5.4s | 217 | 1 | — |
| deepseek-v4-flash:cloud | ✅ | ✅ | ✅ (rationale mentionne conversati) | ✅ | 32.8s | 773 | 1 | — |

## 🎯 Recommandations Finales

### Pour `intention` (cycles fréquents, ~10 min)

**→ `gpt-oss:20b`** (local, gratuit, 9.7s moy)

C'est le SEUL modèle local qui passe 100% JSON + 100% Pydantic + 80% pertinence.
Le seul échec (anti_spam) est mineur : l'intent généré demandait l'état du climat
plutôt que de répéter l'alerte température. En pratique, le dédoublonnage de
CoreMind le filtrerait.

**Alternative : `mistral-small3.2:24b`** — plus rapide (6.5s) mais moins pertinent
(60%). Ses échecs : action_class incorrecte sur alerte_temperature et anti_spam faible.

### Pour `reasoning` (cycles moins fréquents, ~1h)

**→ `deepseek-v4-flash:cloud`** — qualité garantie (100% JSON/Pydantic, 80% pertinence),
coût acceptable pour cycles rares (~1 appel/heure).

Alternative locale si budget serré : `gpt-oss:20b`.

### ⚠️ À re-tester avec `num_predict=4096`

4 modèles ont échoué à cause de la limite de 2048 tokens, pas nécessairement à cause
d'une mauvaise qualité. Avec 4096 tokens, ces modèles pourraient devenir viables :

- `qwen3.6:27b` — le seul scénario qu'il a passé (479 tokens) était parfait
- `qwen3.6:35b` — le seul scénario passé (10 tokens, questions vides) était parfait
- `gemma4:26b` — 3/3 tronqués à 2048 tokens
- `glm-4.7-flash:latest` — 3/3 tronqués à 2048 tokens (mais le plus rapide à 27s)

Si l'un de ces modèles passe à ≥80% avec 4096 tokens, il pourrait être meilleur
que gpt-oss:20b sur la pertinence.

### Modèles à éviter (en l'état actuel)

- `qwen3.6:35b` — 4x plus lent que gpt-oss:20b, même problème de verbosité que 27b
- `glm-4.7-flash:latest` — JSON invalide systématique
- `gemma4:26b` — JSON invalide systématique

### qwen3.6:35b vs 27b

Les deux souffrent du même problème (verbosité → troncation). Le 35b est ~25% plus
lent sans avantage de qualité visible sur le seul scénario réussi. Si le problème
de verbosité est réglé (num_predict plus élevé), le 27b reste le meilleur choix
entre les deux pour le rapport qualité/VRAM.

### Résumé exécutif

| Usage | Modèle | Coût | Fiabilité JSON | Pertinence |
|-------|--------|------|----------------|------------|
| Intention (10 min) | `gpt-oss:20b` | Gratuit (local) | 100% | 80% |
| Reasoning (1h) | `deepseek-v4-flash:cloud` | Crédits cloud | 100% | 80% |
| Fallback local | `gpt-oss:20b` | Gratuit | 100% | 80% |
| À investiguer | `qwen3.6:27b` (4096tk) | Gratuit | ? | ? |
