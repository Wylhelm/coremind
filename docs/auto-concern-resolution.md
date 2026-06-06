# Option B: Auto-Resolution des Active Concerns

**Date:** 2026-06-03
**Priorité:** Medium (l'Option A — prompt amélioré — règle 80% du problème)

## Problème

Les `active_concerns` dans le Narrative State sont définies par le LLM de reflection
mais **jamais vérifiées automatiquement** contre les données réelles. Quand une concern
devient obsolète (ex: "Apple Health unavailable" alors que les données coulent), rien
ne la nettoie automatiquement. Le LLM d'intention voit cette concern stale dans le
prompt et peut générer de fausses anomalies.

## Solution proposée

Ajouter une méthode `auto_resolve()` à `NarrativeMemory` qui, avant chaque cycle
de reflection (ou périodiquement), vérifie chaque `active_concern` contre les
données récentes du World Model.

### Architecture

```python
# narrative.py — nouvelle méthode

async def auto_resolve(
    self,
    world_store: WorldStore,  # accès aux données récentes
) -> list[str]:
    """Vérifie chaque active_concern contre les données récentes.
    
    Retourne la liste des concerns retirées (pour logging).
    """
    now = self._clock()
    resolved = []
    
    for concern in self._state.active_concerns:
        if self._is_concern_resolved(concern, world_store, now):
            resolved.append(concern.text)
    
    if resolved:
        self._state.active_concerns = [
            c for c in self._state.active_concerns
            if c.text not in resolved
        ]
        await self._save()
    
    return resolved


def _is_concern_resolved(
    self,
    concern: TimestampedItem,
    world_store: WorldStore,
    now: datetime,
) -> bool:
    """Vérifie si une concern est résolue en inspectant les données."""
    text = concern.text.lower()
    
    # Règle 1: "Service X unavailable" — vérifier si X a des données récentes
    if "unavailable" in text or "indisponible" in text:
        # Extraire le nom du service
        service = _extract_service_name(text)
        if service:
            recent_data = world_store.get_recent(service, since=now - timedelta(hours=24))
            if recent_data:
                return True  # Des données récentes → service fonctionne
    
    # Règle 2: Concern trop vieille sans mise à jour
    age_days = (now - concern.recorded_at).days
    if age_days > 14:
        return True  # Stale > 2 semaines → retirer
    
    # Règle 3: "Calendar event stale" — vérifier si l'événement existe encore
    # ...
    
    return False
```

### Règles de résolution automatique

| Pattern dans la concern | Vérification | Action |
|--------------------------|-------------|--------|
| "X unavailable/indisponible" | Données récentes (<24h) de X? | Si oui → résoudre |
| Concern > 14 jours sans update | Age du `recorded_at` | Résoudre (stale) |
| "Calendar event X" | Événement existe encore? | Si non → résoudre |
| "Transaction Y" | Transaction rapprochée? | Si oui → résoudre |
| "Sensor/Device X" | État actuel du device? | Si online → résoudre |

### Intégration

```python
# daemon_reflection.py — appeler auto_resolve avant chaque cycle
async def build_reflection_system(...):
    # Avant le cycle de reflection:
    resolved = await narrative_memory.auto_resolve(world_store)
    if resolved:
        log.info("narrative.auto_resolved", concerns=resolved)
```

### Effort estimé

- **Code:** ~100 lignes (méthode + règles + intégration)
- **Tests:** ~50 lignes (cas: unavailable résolu, vieille concern, faux positif)
- **Total:** 2-3 heures
- **Risque:** Faible — complémentaire à l'Option A, pas de breaking change

### Avantages vs Option A seule

| | Option A (prompt) | Option B (auto-resolve) |
|---|---|---|
| Vérification | LLM (peut halluciner) | Code déterministe |
| Latence | Au prochain cycle de reflection (6h) | Immédiat |
| Fiabilité | ~90% (dépend du modèle) | 100% pour les règles codées |
| Maintenance | Ajuster le prompt | Ajouter des règles |

### Recommandation

Déployer l'Option A d'abord (déjà fait) et observer pendant 1-2 semaines.
Si des faux positifs persistent, implémenter l'Option B pour les cas
déterministes (service unavailable, sensor down).
