# Phase 3.5 — JEPA Prediction Layer

> **Statut :** Spec — pas d'implémentation avant la fin des Phases 4 et 5  
> **Date :** 2026-05-27

---

## Problème

Aujourd'hui, CoreMind détecte les changements **après coup** (SnapshotDiffer : "qu'est-ce qui a changé depuis le dernier cycle ?"). Il ne **prévoit** pas.

Le vrai JEPA (Joint Embedding Predictive Architecture) prédit l'état futur en embedding space. Quand la réalité diverge de la prédiction → anomalie → signal de haute salience.

## Ce que ça apporterait

| Aujourd'hui (Phase 3) | Avec JEPA (Phase 3.5) |
|------------------------|----------------------|
| "Le salon est à 24°C" | "Le salon **devrait** être à 21°C, il est à 24°C — anomalie" |
| "3 nouveaux emails" | "3 nouveaux emails, mais **0 de GitHub** — inhabituel pour un mercredi" |
| "Tu es dans le salon" | "Tu es dans le salon à 23h — d'habitude tu es au bureau à cette heure" |
| Pas de prédiction | "La température extérieure chute → le chauffage va s'activer dans ~20 min" |

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Phase 3.5 — JEPA                      │
├─────────────────────────────────────────────────────────┤
│                                                          │
│  WorldSnapshot ──► EmbeddingEncoder ──► Vector (768d)    │
│                                              │           │
│                                              ▼           │
│                              ┌──────────────────────┐    │
│                              │   JEPA Predictor      │    │
│                              │                        │    │
│  Vectors t-6, t-5, ... t-1 ─┤ → Prédit vecteur t    │    │
│                              │ → Compare avec réel    │    │
│                              │ → Si divergence > seuil│    │
│                              │   → WorldEvent(anomaly)│    │
│                              └──────────────────────┘    │
│                                                          │
│  Stockage : Qdrant (déjà en place — SnapshotMemory)      │
│  Modèle   : LightGBM ou MLP léger (pas de LLM)           │
│  Entrée   : 6 derniers vecteurs d'embedding (séquence)   │
│  Sortie   : 1 vecteur prédit + score de divergence        │
└─────────────────────────────────────────────────────────┘
```

## Pourquoi un petit modèle ML, pas un LLM

- **Latence** : inférence < 50ms vs 2-10s pour un LLM
- **Coût** : CPU uniquement, zéro token, zéro appel API
- **Précision** : prédire un vecteur 768d à partir de 6 vecteurs 768d = problème de régression simple
- **Apprentissage** : s'entraîne sur l'historique réel des snapshots (déjà dans Qdrant)

## Intégration dans le pipeline existant

```python
# Dans WorldEncodingPipeline.process()
embedding = await self._encoder.encode_snapshot(current)

# NOUVEAU — Phase 3.5
if self._predictor is not None:
    predicted = await self._predictor.predict(embedding)  # prédit le prochain état
    divergence = cosine_distance(embedding, predicted)
    if divergence > self._anomaly_threshold:
        # Émet un WorldEvent "anomaly" avec divergence score
        # → L4/L5 décident si c'est significatif
        ...
```

## Configuration

```toml
[prediction]
enabled = false  # désactivé par défaut, à activer après validation
anomaly_threshold = 0.15  # cosine distance au-dessus = anomalie
history_window = 6  # nombre de snapshots passés pour la prédiction
min_training_samples = 50  # snapshots minimum avant d'activer
retrain_interval_hours = 24  # réentraînement périodique
```

## Exemples de détection

| Scénario | Attendu | Réel | Divergence | Notif |
|----------|---------|------|------------|-------|
| Nuit normale | Salon 21°C | Salon 21°C | 0.02 | ❌ |
| Fenêtre ouverte en hiver | Salon 21°C | Salon 16°C | 0.31 | 🚨 "Le salon refroidit anormalement vite" |
| Routine modifiée | Bureau 20h-23h | Salon 20h-23h | 0.18 | 📊 "Changement de routine détecté" |
| Appareil en panne | Humidificateur ON | Humidificateur OFF | 0.25 | ⚠️ "Humidificateur inactif anormalement" |

## Dépendances

- Phase 3 (Embedding Pipeline) : ✅ déjà en place
- Qdrant / SnapshotMemory : ✅ déjà en place
- Rien de nouveau à installer (LightGBM ou scikit-learn déjà dans les deps)

## Non-objectifs

- On n'entraîne pas un LLM à prédire le futur
- On ne fait pas de séries temporelles complexes (ARIMA, LSTM)
- On ne modifie pas le pipeline existant — on ajoute un composant optionnel
- Pas de nouveau service/process — tout dans le daemon

## Plan d'implémentation (quand on sera prêts)

1. **`predictor.py`** — classe JEPAPredictor (LightGBM → vecteur 768d → divergence)
2. **Intégration pipeline** — appel dans `WorldEncodingPipeline.process()`
3. **Entraînement initial** — script qui lit l'historique Qdrant et entraîne le modèle
4. **Réentraînement** — tâche périodique dans le daemon (toutes les 24h)
5. **CLI** — `coremind predict status|train|test`
6. **Dashboard** — graphique divergence dans le temps
