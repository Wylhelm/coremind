# Self-Model Plugin — World Model de Guillaume

> **Statut :** Spec — pas d'implémentation avant les Phases 4 et 5  
> **Date :** 2026-05-28  
> **Spéc liée :** [World Data Plugin](world-data-plugin.md) — données externes  
> **Spéc liée :** [JEPA Prediction](PHASE_3_5_JEPA_PREDICTION.md) — prédiction d'anomalies

---

## Problème

CoreMind a un World Model riche de la **maison** (108 entités : lumières, capteurs, caméras, emails). Mais il n'a aucun modèle de **Guillaume** — qui il est, ce qu'il veut, comment il pense, qui compte pour lui.

Un AGI personnel ne peut pas se limiter à "le salon est à 24°C et tu as 3 emails". Il doit comprendre la personne qu'il assiste.

## Vision

```
┌──────────────────────────────────────────────────────────────┐
│  Aujourd'hui                  │  Avec le Self-Model           │
├───────────────────────────────┼──────────────────────────────┤
│  "3 emails non lus"           │  "Un email de ton boss —     │
│                               │   priorité haute ?"           │
│                               │                               │
│  "Il est 23h, tu travailles"  │  "Tu codes depuis 4h. Les 3  │
│                               │   derniers soirs où t'as fait  │
│                               │   ça, ton sommeil a baissé."  │
│                               │                               │
│  Pas de contexte projets      │  "La Phase 4 est à 60%. Tu   │
│                               │   avais dit 'fini cette       │
│                               │   semaine' — sur la bonne     │
│                               │   voie."                      │
│                               │                               │
│  Pas de contexte social       │  "Pas parlé à Aurélie depuis   │
│                               │   5 jours. Son anniversaire    │
│                               │   est dans 2 semaines."       │
└──────────────────────────────────────────────────────────────┘
```

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                   Self-Model Plugin                          │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  SOURCES (collecte passive)                                  │
│  ┌──────────────────────────────────────────────────────┐   │
│  │ Explicite       │ Observé          │ Extrait          │   │
│  │ • Telegram      │ • GitHub commits  │ • Calendrier     │   │
│  │ • WhatsApp      │ • VS Code activity│ • Emails         │   │
│  │ • Commandes     │ • Apple Health    │ • Firefly        │   │
│  │   vocales       │ • Tapo présence   │ • Immich         │   │
│  └──────────────────────────────────────────────────────┘   │
│                         │                                     │
│                         ▼                                     │
│  EXTRACTION (LLM léger — Mistral Small 3.2)                  │
│  ┌──────────────────────────────────────────────────────┐   │
│  │ "Guillaume a dit X"         → entity:knowledge:*      │   │
│  │ "Pattern: code 20h-minuit"  → entity:routine:*        │   │
│  │ "Aurélie = fille"           → entity:relationship:*    │   │
│  │ "Retraite à 65 ans"         → entity:goal:*            │   │
│  │ "Phase 4 = priorité"        → entity:project:*         │   │
│  └──────────────────────────────────────────────────────┘   │
│                         │                                     │
│                         ▼                                     │
│  ÉMISSION (WorldEvents signés)                                │
│  ┌──────────────────────────────────────────────────────┐   │
│  │ entity=person:guillaume   attribute=working_on       │   │
│  │ entity=goal:retirement    attribute=progress_pct     │   │
│  │ entity=project:coremind   attribute=phase_completed  │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

## Entités du Self World Model

### `person:*` — Relations
| Entité | Attributs | Source |
|--------|----------|--------|
| `person:aurélie` | `relation=fille`, `last_contact`, `birthday`, `location` | Telegram, Calendrier, Immich |
| `person:jeff` | `relation=ami`, `frequency` | Telegram, WhatsApp |
| `person:julie` | `relation=ex`, `co_parenting` | Messages, Calendrier |

### `goal:*` — Buts
| Entité | Attributs | Source |
|--------|----------|--------|
| `goal:retirement` | `target_age=65`, `target_year=2043`, `location=chalet` | Déclaré |
| `goal:g-bot-immo` | `target=beta_launch`, `status=delayed` | Déclaré + GitHub |
| `goal:santé` | `sevrage_vape`, `qualité_sommeil` | Apple Health, Déclaré |

### `project:*` — Projets actifs
| Entité | Attributs | Source |
|--------|----------|--------|
| `project:coremind` | `current_phase=5`, `progress_pct`, `commits_week`, `intensity` | GitHub |
| `project:g-bot-immo` | `status=paused`, `last_commit`, `days_inactive` | GitHub |
| `project:work` | `employer=IA`, `start_date` | Calendrier, Emails |

### `routine:*` — Patterns de vie
| Entité | Attributs | Source |
|--------|----------|--------|
| `routine:coding` | `window=20:00-00:00`, `days=mon-fri` | VS Code, GitHub |
| `routine:sleep` | `avg_bedtime`, `avg_wake`, `quality` | Apple Health |
| `routine:social` | `peak_hours`, `active_contacts` | Telegram, WhatsApp |

### `identity:*` — Qui est Guillaume
| Entité | Attributs | Source |
|--------|----------|--------|
| `identity:tech` | `role=architecte_ia`, `languages=python,js`, `stack=ollama,docker` | GitHub, Conversations |
| `identity:valeurs` | `autonomie`, `apprentissage`, `famille` | Conversations (déclaré) |
| `identity:connaissances` | `immobilier`, `ia`, `crypto`, `finance` | Patterns d'intérêt |

### `preference:*` — Goûts et habitudes
| Entité | Attributs | Source |
|--------|----------|--------|
| `preference:code` | `prefers_evening=true`, `tolerance_22h+` | VS Code, GitHub |
| `preference:voice` | `nova`, `style=radio`, `max_45s` | Config, Feedback |
| `preference:food` | *(à apprendre)* | Achats Firefly |

## Comment CoreMind apprend

### Niveau 1 — Fait déclaré (confiance 1.0)
```
Guillaume: "Ma fille Aurélie habite à Montréal"
→ WorldEvent(entity=person:aurélie, attribute=location, value="Montréal",
              confidence=1.0, method="declared")
```

### Niveau 2 — Pattern observé (confiance 0.7-0.9)
```
Observé: 15 jours de suite, GitHub commits entre 20h et minuit
→ WorldEvent(entity=routine:coding, attribute=window, value="20:00-00:00",
              confidence=0.85, method="observed")
```

### Niveau 3 — Synthèse (confiance 0.5-0.7)
```
Fait A: 3 articles immo/semaine + Fait B: G-Bot Immo 0 commit/14j
→ WorldEvent(entity=project:g-bot-immo, attribute=intent_vs_action_gap,
              value="high", confidence=0.6, method="synthesized")
```

### Niveau 4 — Question générée (confiance 0.3-0.5)
```
Fait A: Guillaume lit sur JEPA + Fait B: Phase 3 pipeline activée aujourd'hui
→ CoreMind génère une question pour Guillaume:
   "Tu t'intéresses à JEPA — c'est lié à la Phase 3 ou tu explores pour plus tard ?"
→ La réponse de Guillaume devient un fait Niveau 1
```

**Principe clé :** les Niveaux 3 et 4 ne génèrent JAMAIS de notifications automatiques. Ils génèrent des **questions** ou des **observations** que CoreMind peut mentionner dans une conversation. Seuls les Niveaux 1 et 2 peuvent déclencher des notifs proactives.

## Faisabilité technique — Sources

| Source | Accès | Coût | Fraîcheur |
|--------|-------|------|-----------|
| Telegram | ✅ OpenClaw natif | Gratuit | Immédiat |
| WhatsApp | ⚠️ Config QR (5 min) | Gratuit | Immédiat |
| Gmail | ✅ Plugin GOG | Gratuit | 30 min |
| Calendar | ✅ Plugin GOG | Gratuit | 1h |
| GitHub | ✅ CLI + API | Gratuit | 30 min |
| Firefly | ✅ Plugin | Gratuit | 6h |
| Immich | ✅ API | Gratuit | On-demand |
| Apple Health | ✅ Plugin | Gratuit | Push |
| VS Code | ⚠️ Extension à créer | Gratuit | 5 min |
| Tapo Cam | ✅ Plugin | Gratuit | 5 min |
| Notion | ✅ API | Gratuit | On-demand |
| Facebook Messenger | ❌ Pas d'API personnelle | — | — |

## Configuration

```toml
# ~/.coremind/config.toml (nouvelle section)

[self_model]
enabled = true
extraction_interval_seconds = 3600  # Extraire des faits toutes les heures
max_facts_per_cycle = 10            # Limiter pour éviter le flood
min_confidence_declared = 0.95      # Faits déclarés explicitement
min_confidence_observed = 0.70      # Patterns détectés
min_confidence_synthesized = 0.50   # Inférences
allow_questions = true              # Autoriser les questions Niveau 4

# Quelles sources activer
[sources]
telegram_metadata = true
whatsapp_metadata = true    # Après config QR
github_activity = true
vscode_activity = true      # Après création extension
calendar_context = true
email_metadata = true       # Expéditeur + sujet uniquement (pas le corps)
health_patterns = true
firefly_spending = true
```

## Exemples de comportement

### Scénario 1 — Routine de coding
```
Contexte : 23h30, Guillaume code CoreMind depuis 3h
Self-Model : routine:coding (confidence 0.85), project:coremind (intensity=high)
CoreMind : "Tu codes depuis 3h. Les nuits où tu dépasses minuit, ton sommeil
           baisse de 15%. Je te le rappelle ou tu gères ?"
```

### Scénario 2 — Anniversaire
```
Contexte : 28 mai, calendrier silencieux
Self-Model : person:aurélie (birthday="2001-05-XX", en mai)
CoreMind : "L'anniversaire d'Aurélie est ce mois-ci. Tu as pensé à un cadeau ?"
```

### Scénario 3 — Objectif vs réalité
```
Contexte : Phase 4 à 60%, on est mercredi
Self-Model : project:coremind (target="fini cette semaine", progress=60%)
CoreMind : "Phase 4 à 60% mercredi. À ton rythme actuel, tu finis samedi.
           Tu veux prioriser ça demain ?"
```

### Scénario 4 — Lien transversal
```
Contexte : BTC +8% aujourd'hui, G-Bot Immo en pause
Self-Model : project:g-bot-immo (status=paused, reason=budget)
CoreMind : "Bitcoin a grimpé de 8% cette semaine. Si tu as des positions,
           c'est peut-être le bon moment pour débloquer le budget G-Bot Immo ?"
```

## Non-objectifs

- On ne lit PAS le contenu des emails personnels (seulement expéditeur + sujet)
- On ne fait PAS de profiling psychologique
- On ne partage JAMAIS ces données hors de CoreMind
- On ne remplace PAS le jugement humain — on informe, on suggère, on questionne
- Les inférences (Niveau 3-4) sont toujours présentées comme des hypothèses

## Plan d'implémentation

1. **`self_model/collectors/`** — collecteurs passifs (Telegram meta, GitHub, GOG)
2. **`self_model/extractor.py`** — extraction de faits via Mistral Small 3.2
3. **`self_model/entities.py`** — modèles Pydantic pour les entités self-model
4. **`self_model/plugin.py`** — plugin CoreMind standard
5. **Extension VS Code** — webhook → activité par projet
6. **WhatsApp config** — QR pairing dans OpenClaw
7. **`self_model.toml`** — configuration déclarative
