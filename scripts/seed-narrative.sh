#!/usr/bin/env bash
# Seed CoreMind's narrative with knowledge about Guillaume
# Run once after fresh install or narrative reset

cd ~/.openclaw/workspace/coremind

.venv/bin/python3 -c "
from coremind.memory.narrative import NarrativeMemory
import asyncio

async def seed():
    nm = NarrativeMemory()
    await nm.load()
    await nm.update(
        user_mood_trend='stable',
        recent_patterns=[
            'Guillaume (47 ans, Québec) vit avec ses 3 chats: Poukie (noire, anxieuse), Timimi (noire/caramel, gourmande), Minuit (noir, explorateur)',
            'Travaille de la maison (bureau), généralement 9h-17h, sport mardi et jeudi',
            'Dort habituellement 23h-23h30, lever ~7h. Suivi sommeil via Apple Health',
            'Fille Aurélie (née mai 2001). Contacts proches: Julie, Geneviève, Mélanie, Jeff',
            'Chalet à Lac-aux-Sables, visites occasionnelles le weekend',
        ],
        active_concerns=[
            'Qualité du sommeil — suivi actif, température chambre suspectée comme facteur',
            'Dépenses restaurant — tendance à la hausse ce mois-ci',
            'Sevrage vape et pot en cours (depuis avril 2026)',
        ],
        relationship_notes='Sevrage alcool réussi (janvier 2026). Motivation élevée pour santé globale. Intéressé par IA, automatisation, projets tech.',
    )
    print('Narrative seeded:')
    print(nm._render_for_prompt()[:500])

asyncio.run(seed())
"
