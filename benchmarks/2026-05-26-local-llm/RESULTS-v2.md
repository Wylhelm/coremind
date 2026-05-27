# CoreMind LLM Benchmark v2 — Intention Layer (L5)

**Date:** 2026-05-26 | **Scénarios:** 5 | **Modèles:** 7
**Schéma:** `QuestionBatch` (Pydantic v2, strict) | **Température:** 0.3 | **Format:** JSON forcé

## 🔄 Changements vs v1

| Paramètre | v1 | v2 | Raison |
|-----------|----|----|--------|
| `num_predict` | 2048 | 8192 | CoreMind max_tokens=4096 + marge |
| `num_ctx` | 8192 | 32768 | Snapshots réels = 10-30k tokens |
| Scénarios | ~200 tokens | ~5000-12000 tokens | 20-30 entités, events, reasoning, patterns |
| Timeout | 120s | 180s | Gros contextes = plus de temps |
| Truncation detection | Non | Oui (ratio tokens_out/num_predict) | Détecte si encore tronqué |
| Save progressive | Non | Oui (après chaque modèle) | Résilience aux crashs |

## 📊 Résumé Global

| Modèle | JSON ✅ | Pydantic ✅ | Pertinence ✅ | FR ✅ | Latence moy | Tokens sortie moy | Ratio trunc | Verdict |
|--------|---------|-------------|---------------|-------|-------------|-------------------|-------------|---------|
| qwen3.6:27b | 0% (0/3) | 0% (0/3) | 0% (0/3) | 0% (0/3) | 180.0s | 0 | 0.0% | ❌ ÉLIMINÉ (JSON invalide) |
| qwen3.6:35b | 25% (1/4) | 25% (1/4) | 25% (1/4) | 25% (1/4) | 169.2s | 298 | 3.6% | ⚠️ RISQUE (JSON instable) |
| glm-4.7-flash:latest | 100% (5/5) | 20% (1/5) | 0% (0/5) | 20% (1/5) | 51.7s | 703 | 9.7% | ⚠️ RISQUE (JSON instable) |
| gpt-oss:20b | 100% (5/5) | 100% (5/5) | 40% (2/5) | 100% (5/5) | 20.2s | 536 | 9.0% | ❌ NON VIABLE |
| gemma4:26b | 0% (0/3) | 0% (0/3) | 0% (0/3) | 0% (0/3) | 96.2s | 8192 | 100.0% ⚠️ | ❌ ÉLIMINÉ (JSON invalide) [TOUJOURS TRONQUÉ] |
| mistral-small3.2:24b | 100% (5/5) | 100% (5/5) | 60% (3/5) | 100% (5/5) | 17.6s | 646 | 9.4% | ⚠️ PARTIEL |
| deepseek-v4-flash:cloud | 100% (5/5) | 100% (5/5) | 80% (4/5) | 100% (5/5) | 85.6s | 2111 | 32.0% | ✅ VIABLE |

## 📋 Détails par Scénario

### calme — Mardi après-midi tranquille. Tout est normal, aucune anomalie détectée. Températures stables, lumières éteintes, chats dans leurs paniers.

| Modèle | JSON | Pydantic | Pertinent | FR | Latence | tk_in→tk_out | Q | Erreur |
|--------|------|----------|-----------|----|---------|--------------|---|--------|
| qwen3.6:27b | ❌ | ❌ | ❌ () | ❌ | 180.0s | 0→0 | 0 | Timeout after 180s |
| qwen3.6:35b | ❌ | ❌ | ❌ () | ❌ | 180.0s | 0→0 | 0 | Timeout after 180s |
| glm-4.7-flash:latest | ✅ | ✅ | ❌ (3 intents — trop pour un ) | ✅ | 52.7s | 7952→745 | 3 | — |
| gpt-oss:20b | ✅ | ✅ | ❌ (3 intents — trop pour un ) | ✅ | 21.8s | 6901→371 | 3 | — |
| gemma4:26b | ❌ | ❌ | ❌ () | ❌ | 86.1s | 6178→8192 | 0 | JSON parse error: Expecting value: line 1 column 1 (cha... |
| mistral-small3.2:24b | ✅ | ✅ | ❌ (3 intents — trop pour un ) | ✅ | 21.3s | 5506→599 | 3 | — |
| deepseek-v4-flash:cloud | ✅ | ✅ | ❌ (2 intents — trop pour un ) | ✅ | 129.2s | 5371→2479 | 2 | — |

### lumiere_oubliee — La lumière du bureau est allumée depuis 17h30 (4h15). Aucune présence détectée dans le bureau. Le reste de la maison est normal.

| Modèle | JSON | Pydantic | Pertinent | FR | Latence | tk_in→tk_out | Q | Erreur |
|--------|------|----------|-----------|----|---------|--------------|---|--------|
| qwen3.6:27b | ❌ | ❌ | ❌ () | ❌ | 180.0s | 0→0 | 0 | Timeout after 180s |
| qwen3.6:35b | ❌ | ❌ | ❌ () | ❌ | 180.0s | 0→0 | 0 | Timeout after 180s |
| glm-4.7-flash:latest | ✅ | ❌ | ❌ () | ❌ | 36.6s | 7345→774 | 0 | Pydantic: 3 validation errors for QuestionBatch
questio... |
| gpt-oss:20b | ✅ | ✅ | ✅ (op=coremind.plugin.notifi) | ✅ | 16.5s | 6703→634 | 3 | — |
| gemma4:26b | ❌ | ❌ | ❌ () | ❌ | 80.6s | 6415→8192 | 0 | JSON parse error: Expecting value: line 1 column 1 (cha... |
| mistral-small3.2:24b | ✅ | ✅ | ✅ (op=coremind.plugin.notifi) | ✅ | 15.2s | 5717→585 | 3 | — |
| deepseek-v4-flash:cloud | ✅ | ✅ | ✅ (op=coremind.plugin.homeas) | ✅ | 86.6s | 5555→1561 | 2 | — |

### alerte_temperature — Température chambre élevée (27.0°C). Climatiseur éteint. Après-midi chaud, fenêtre fermée. La chambre dépasse le seuil de confort de 25°C.

| Modèle | JSON | Pydantic | Pertinent | FR | Latence | tk_in→tk_out | Q | Erreur |
|--------|------|----------|-----------|----|---------|--------------|---|--------|
| qwen3.6:27b | ❌ | ❌ | ❌ () | ❌ | 180.0s | 0→0 | 0 | Timeout after 180s |
| qwen3.6:35b | ✅ | ✅ | ✅ (ac=hvac, op=coremind.plug) | ✅ | 136.9s | 8424→298 | 1 | — |
| glm-4.7-flash:latest | ✅ | ❌ | ❌ () | ❌ | 62.5s | 9589→515 | 0 | Pydantic: 2 validation errors for QuestionBatch
questio... |
| gpt-oss:20b | ✅ | ✅ | ✅ (ac=hvac, op=coremind.plug) | ✅ | 13.4s | 6256→735 | 3 | — |
| gemma4:26b | ❌ | ❌ | ❌ () | ❌ | 121.9s | 6507→8192 | 0 | JSON parse error: Unterminated string starting at: line... |
| mistral-small3.2:24b | ✅ | ✅ | ✅ (ac=hvac, op=coremind.plug) | ✅ | 19.1s | 5791→769 | 3 | — |
| deepseek-v4-flash:cloud | ✅ | ✅ | ✅ (ac=hvac, op=coremind.plug) | ✅ | 62.9s | 5637→2233 | 2 | — |

### anti_spam — Température chambre encore élevée (26.5°C) mais Guillaume a DÉJÀ été notifié il y a 30 minutes. Ne doit PAS renvoyer la même alerte.

| Modèle | JSON | Pydantic | Pertinent | FR | Latence | tk_in→tk_out | Q | Erreur |
|--------|------|----------|-----------|----|---------|--------------|---|--------|
| qwen3.6:27b | ❌ | ❌ | ❌ () | ❌ | 0.0s | 0→0 | 0 | SKIPPED: modèle abandonné après 3 échecs JSON |
| qwen3.6:35b | ❌ | ❌ | ❌ () | ❌ | 180.0s | 0→0 | 0 | Timeout after 180s |
| glm-4.7-flash:latest | ✅ | ❌ | ❌ () | ❌ | 38.9s | 7592→691 | 0 | Pydantic: 3 validation errors for QuestionBatch
questio... |
| gpt-oss:20b | ✅ | ✅ | ❌ (spam: répète alerte tempé) | ✅ | 23.9s | 7821→418 | 3 | — |
| gemma4:26b | ❌ | ❌ | ❌ () | ❌ | 0.0s | 0→0 | 0 | SKIPPED: modèle abandonné après 3 échecs JSON |
| mistral-small3.2:24b | ✅ | ✅ | ❌ (spam: répète alerte tempé) | ✅ | 17.8s | 5657→711 | 3 | — |
| deepseek-v4-flash:cloud | ✅ | ✅ | ✅ (intent différent de l'ale) | ✅ | 51.4s | 5512→1656 | 1 | — |

### conversation — Guillaume a répondu 'c'est fait merci !' à la suggestion d'éteindre la lumière cuisine. Ne pas re-notifier, répondre conversationnellement.

| Modèle | JSON | Pydantic | Pertinent | FR | Latence | tk_in→tk_out | Q | Erreur |
|--------|------|----------|-----------|----|---------|--------------|---|--------|
| qwen3.6:27b | ❌ | ❌ | ❌ () | ❌ | 0.0s | 0→0 | 0 | SKIPPED: modèle abandonné après 3 échecs JSON |
| qwen3.6:35b | ❌ | ❌ | ❌ () | ❌ | 0.0s | 0→0 | 0 | SKIPPED: modèle abandonné après 3 échecs JSON |
| glm-4.7-flash:latest | ✅ | ❌ | ❌ () | ❌ | 67.5s | 9662→792 | 0 | Pydantic: 3 validation errors for QuestionBatch
questio... |
| gpt-oss:20b | ✅ | ✅ | ❌ (re-notification cuisine —) | ✅ | 25.2s | 7783→523 | 3 | — |
| gemma4:26b | ❌ | ❌ | ❌ () | ❌ | 0.0s | 0→0 | 0 | SKIPPED: modèle abandonné après 3 échecs JSON |
| mistral-small3.2:24b | ✅ | ✅ | ✅ (ton conversationnel: je t) | ✅ | 14.8s | 5676→565 | 2 | — |
| deepseek-v4-flash:cloud | ✅ | ✅ | ✅ (rationale mentionne conve) | ✅ | 97.6s | 5538→2625 | 2 | — |

## 📈 Comparaison v1 → v2

| Modèle | v1 Pydantic | v2 Pydantic | v1 Pertinence | v2 Pertinence | v1 Latence | v2 Latence | Verdict |
|--------|------------|------------|---------------|---------------|------------|------------|---------|
| qwen3.6:27b | 20% | 0% (-20) | 20% | 0% (-20) | 76.5s | 180.0s | ↓ |
| qwen3.6:35b | 20% | 25% (+5) | 20% | 25% (+5) | 100.0s | 169.2s | ↑ |
| glm-4.7-flash:latest | 0% | 20% (+20) | 0% | 0% (+0) | 27.2s | 51.7s | ↑ |
| gpt-oss:20b | 100% | 100% (+0) | 80% | 40% (-40) | 9.7s | 20.2s | = |
| gemma4:26b | 0% | 0% (+0) | 0% | 0% (+0) | 21.7s | 96.2s | = |
| mistral-small3.2:24b | 100% | 100% (+0) | 60% | 60% (+0) | 6.5s | 17.6s | = |
| deepseek-v4-flash:cloud | 100% | 100% (+0) | 80% | 80% (+0) | 26.4s | 85.6s | = |

## 🎯 Recommandations Finales

### Meilleur modèle global

**→ `deepseek-v4-flash:cloud`** — Pydantic 100%, 85.6s moy

### Pour `intention` (cycles fréquents, ~10 min)

**→ `mistral-small3.2:24b`** (local, gratuit, Pydantic 100%, 17.6s moy)

### Pour `reasoning` et `reflection` (cycles rares)

**→ `deepseek-v4-flash:cloud`** — qualité garantie, coût acceptable (~1 appel/h)
Alternative locale: `mistral-small3.2:24b`

### qwen3.6:27b vs gpt-oss:20b (le match clé)

| Critère | qwen3.6:27b | gpt-oss:20b | Gagnant |
|---------|------------|-------------|---------|
| Pydantic | 0% | 100% | gpt-oss |
| Pertinence | 0% | 40% | gpt-oss |
| Latence | 180.0s | 20.2s | gpt-oss |

## ⚙️ Config TOML Recommandée

```toml
[llm]
# Intention: cycles fréquents (~10 min), modèle local gratuit
intention_model = "ollama/mistral-small3.2:24b"
intention_max_tokens = 4096
intention_temperature = 0.3

# Reasoning: cycles rares (~1h), qualité > vitesse
reasoning_model = "deepseek-v4-flash:cloud"
reasoning_max_tokens = 4096
reasoning_temperature = 0.5

# Reflection: cycles très rares, qualité max
reflection_model = "deepseek-v4-flash:cloud"
reflection_max_tokens = 4096
```
