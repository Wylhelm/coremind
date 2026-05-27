#!/usr/bin/env python3
"""CoreMind LLM Benchmark v2 — 7 models, 5 realistic scenarios.

Changes vs v1:
- num_predict: 2048 → 8192 (CoreMind real max_tokens=4096, need margin)
- num_ctx: 8192 → 32768 (snapshots are 10-30k tokens in prod)
- Snapshots: 200-token mini → 5k-15k token realistic (20-30 entities, events, intents)
- Timeout: 120s → 180s
- 180s timeout per call, progressive saves after each model
- Cloud limit: MAX 5 calls for deepseek-v4-flash:cloud
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

# ── Path setup ──────────────────────────────────────────────────────────────
PROJECT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT / "src"))

from coremind.intention.schemas import QuestionBatch  # noqa: E402

# ── Constants ───────────────────────────────────────────────────────────────
OLLAMA_BASE = "http://10.0.0.175:11434"
TIMEOUT = 180  # seconds per call (v2: 120→180)
WARMUP_TIMEOUT = 300
NUM_CTX = 32768  # v2: 8192 → 32768 (real prod snapshot size)
NUM_PREDICT = 8192  # v2: 2048 → 8192 (margin over 4096 max_tokens)

MODELS = [
    "qwen3.6:27b",
    "qwen3.6:35b",
    "glm-4.7-flash:latest",
    "gpt-oss:20b",
    "gemma4:26b",
    "mistral-small3.2:24b",
    "deepseek-v4-flash:cloud",  # baseline — MAX 5 calls
]
CLOUD_LIMIT = 5

OUT_DIR = Path(__file__).resolve().parent

# ── Rich realistic system prompt (matches CoreMind intention.system.v1) ─────
SYSTEM_PROMPT = """\
You are the intention layer (L5) of CoreMind, a continuous personal intelligence daemon.

Your job: generate a small set of INTERNAL QUESTIONS the system must answer
right now, based on the current world snapshot, recent reasoning outputs, and the user's
active patterns.

Each question MUST propose a concrete action using one of the AVAILABLE OPERATIONS
listed below.  A question without a proposed action is useless - CoreMind cannot act on it.

Available operations (use EXACTLY these names):
  coremind.plugin.notification.send     - Send a Telegram notification to Guillaume
  coremind.plugin.homeassistant.get_state - Query Home Assistant entity state
  coremind.plugin.homeassistant.get_history - Query HA entity history
  coremind.plugin.homeassistant.turn_on  - Turn on a HA entity (light, switch)
  coremind.plugin.homeassistant.turn_off - Turn off a HA entity
  coremind.plugin.homeassistant.set_temperature - Set climate entity temperature
  coremind.plugin.homeassistant.create_automation - Create a HA automation
  coremind.plugin.homeassistant.send_notification - Send HA persistent notification
  coremind.plugin.vikunja.list_tasks    - List tasks from Vikunja
  coremind.plugin.vikunja.get_tasks     - Get task details from Vikunja
  coremind.plugin.calendar.fetch_upcoming_events - Get upcoming Google Calendar events
  coremind.plugin.calendar.get_next_payday - Find next payday

Parameters for each operation:
  - notification.send: {"title": "...", "message": "..."}
  - homeassistant.get_state/get_history: {"entity_id": "sensor.xxx"}
    or {"entity_ids": ["sensor.a", "sensor.b"]}
  - homeassistant.turn_on/turn_off: {"entity_id": "light.xxx"}
  - homeassistant.set_temperature: {"entity_id": "climate.xxx", "temperature": "21.5"}
  - homeassistant.create_automation: {"name": "...", "trigger": {...}, "action": {...}}
  - vikunja.list_tasks/get_tasks: {"project": "Inbox", "filter": "overdue"} (optional)
  - calendar.fetch_upcoming_events: {"max_results": 5}

Each question must also:
- be grounded in specific entities from the snapshot (cite them in ``grounding``),
- be honest about confidence - never claim certainty you do not have.
- have an ``expected_outcome`` written as a NATURAL French message TO Guillaume,
  describing what action you'll take. NOT a third-person description.
  ❌ "User receives a notification about bedroom temperature"
  ✅ "Je te préviens si ta chambre dépasse 25°C"
  ❌ "Guillaume gets a reminder about overdue tasks"
  ✅ "Tu as 3 tâches en retard dans Vikunja, je te les montre"

Categories: use ``suggest`` for low-risk informational actions (notifications, queries).
Use ``ask`` for mutations (turn_on/off, set_temperature, create_automation) and any
finance/email operations.
**Jamais "ask" pour une simple notification.** Si l'action est juste "envoyer une
notification", la catégorie est TOUJOURS ``suggest``.

CRITICAL RULES:
- **Money**: ALL amounts in the world snapshot are in **Canadian Dollars (CAD / $)**.
  Never convert to EUR or any other currency. Display amounts as "X,XX $ CAD".
- **action_class**: for notifications, use EXACTLY ``notification.send``.  Not
  "notification" or "notification.query".
- **Anti-spam**: do NOT generate intents for trivial cat movements ("le chat a bouge").
  Only notify about cats if the situation is genuinely unusual or noteworthy.
- **Anti-redundancy**: read the "Recent intents" section carefully.  If a proposed
  intent is semantically similar to one already listed there, DO NOT emit it.
  Duplicate intents flood the user and waste system resources.  Err on the side of
  silence — a missed notification is better than a repeated one.
- **Conversation mode**: when the user has REPLIED to a previous notification (indicated
  by conversations below), DO NOT generate a new intent for the same topic. Instead,
  generate a conversational intent with category="conversation" that responds directly
  to what the user said. Use the conversation history to understand context.
  When the user indicates a problem is RESOLVED ("c'est fait", "c'est réglé", "merci",
  etc), generate a conversation intent that acknowledges this and marks the topic
  as resolved. Do NOT re-notify about resolved issues.

Language: ALL expected_outcome text MUST be in French. NEVER use English.
Output VALID JSON ONLY, matching the schema exactly.
"""

# ── User prompt template (matches CoreMind intention.user.v1) ───────────────
USER_TEMPLATE = """\
## Current local time

Il est {local_time} ({local_timezone}).
**TOUS les horodatages ci-dessous sont en UTC.** Ne confonds pas l'heure UTC avec l'heure locale.

## World snapshot (JSON)

```json
{snapshot_json}
```

## Recent reasoning cycles (summary)

{reasoning_summary}

## Recent intents (for loop avoidance)

{recent_intents_summary}

## Active procedural patterns

{patterns_summary}

## Recent user conversations

{conversations_summary}

## Active predictions (from predictive memory)

{predictions_summary}

## Required response schema (JSON Schema)

```json
{schema_json}
```

Emit a single JSON object matching the schema.  Limit yourself to at most
3 high-salience questions.  Do not include any text outside the JSON object.
"""

# Pre-compute schema JSON once
_SCHEMA_JSON = json.dumps(QuestionBatch.model_json_schema(), indent=2, ensure_ascii=False)


# ══════════════════════════════════════════════════════════════════════════════
# Realistic entity factory
# ══════════════════════════════════════════════════════════════════════════════


def _e(t: str, name: str, **props: object) -> dict:
    """Build a CoreMind-format entity dict."""
    return {"type": t, "display_name": name, "properties": dict(props)}


def _ev(timestamp: str, entity: str, attr: str, value: object) -> dict:
    """Build a CoreMind-format recent event dict."""
    parts = entity.split(":", 1)
    return {
        "timestamp": timestamp,
        "entity": {"type": parts[0], "id": parts[1]},
        "attribute": attr,
        "value": str(value),
    }


# ── Shared baseline entities (20-30 HA entities) ────────────────────────────
BASE_ENTITIES = [
    _e("light", "Lumière Salon", state="off", brightness=0),
    _e("light", "Lumière Cuisine", state="off", brightness=0),
    _e("light", "Lumière Bureau", state="off", brightness=0),
    _e("light", "Lumière Chambre", state="off", brightness=0),
    _e("light", "Lumière Couloir", state="off", brightness=0),
    _e("light", "Lumière Salle à Manger", state="off", brightness=0),
    _e("sensor", "Température Salon", value="22.1", unit="°C"),
    _e("sensor", "Température Cuisine", value="23.5", unit="°C"),
    _e("sensor", "Température Bureau", value="21.8", unit="°C"),
    _e("sensor", "Température Chambre", value="23.0", unit="°C"),
    _e("sensor", "Température Couloir", value="22.0", unit="°C"),
    _e("sensor", "Température Extérieur", value="24.5", unit="°C"),
    _e("sensor", "Humidité Salon", value="45", unit="%"),
    _e("sensor", "Humidité Chambre", value="52", unit="%"),
    _e("sensor", "Humidité Extérieur", value="60", unit="%"),
    _e(
        "climate",
        "Climatiseur Chambre",
        state="off",
        temperature=22,
        current_temp=23.0,
        mode="cool",
    ),
    _e("climate", "Thermostat Salon", state="heat", temperature=21, current_temp=22.1, mode="heat"),
    _e("switch", "Chauffe-eau", state="on", power_w=1500),
    _e("switch", "Déshumidificateur Sous-sol", state="off"),
    _e("binary_sensor", "Présence Bureau", state="clear", device_class="occupancy"),
    _e("binary_sensor", "Présence Salon", state="detected", device_class="occupancy"),
    _e("binary_sensor", "Présence Cuisine", state="clear", device_class="occupancy"),
    _e("binary_sensor", "Fenêtre Chambre", state="closed", device_class="window"),
    _e("binary_sensor", "Fenêtre Salon", state="closed", device_class="window"),
    _e("binary_sensor", "Porte Entrée", state="closed", device_class="door"),
    _e("sensor", "Qualité Air Salon", pm25=8, pm10=12, co2=450),
    _e("sensor", "Qualité Air Chambre", pm25=5, pm10=8, co2=550),
    _e(
        "vacuum",
        "Aspirateur Roborock S7",
        state="docked",
        battery=100,
        last_clean="2026-05-26T09:30:00Z",
    ),
    _e("media_player", "Nest Hub Chambre", state="idle", volume=0.4),
    _e("media_player", "Nest Hub Cuisine", state="idle", volume=0.3),
    _e(
        "humidifier", "Humidificateur Chambre", state="off", target_humidity=50, current_humidity=52
    ),
    _e("camera", "Caméra Tapo Salon", state="idle", last_motion="2026-05-26T18:05:00Z"),
    # Health entities (Apple Health via HA)
    _e(
        "sensor",
        "Sommeil Guillaume",
        hours=7.2,
        deep_sleep_h=1.8,
        quality="good",
        last_night="2026-05-25",
    ),
    _e("sensor", "Pas Guillaume", steps=8432, distance_km=6.1, date="2026-05-26"),
    _e(
        "sensor", "Fréquence Cardiaque Guillaume", resting_bpm=62, current_bpm=68, date="2026-05-26"
    ),
    _e("sensor", "Poids Guillaume", weight_kg=81.3, date="2026-05-26"),
    # Finance entities (Firefly III)
    _e("finance", "Compte Chèque", balance=3245.67, currency="CAD", last_transaction="2026-05-25"),
    _e("finance", "Carte Crédit", balance=-1842.30, limit=10000, currency="CAD"),
    _e("finance", "Épargne", balance=12750.00, currency="CAD"),
    _e(
        "finance", "Dépenses Mai", total=2150.45, budget=2800.00, category="monthly", currency="CAD"
    ),
]

BASE_EVENTS = [
    _ev("2026-05-26T12:00:00Z", "sensor:Température Extérieur", "value", "24.5"),
    _ev("2026-05-26T11:30:00Z", "sensor:Pas Guillaume", "steps", "8432"),
    _ev("2026-05-26T11:00:00Z", "vacuum:Aspirateur Roborock S7", "state", "docked"),
    _ev("2026-05-26T10:45:00Z", "sensor:Sommeil Guillaume", "hours", "7.2"),
    _ev("2026-05-26T10:30:00Z", "binary_sensor:Présence Salon", "state", "detected"),
    _ev("2026-05-26T10:15:00Z", "finance:Compte Chèque", "balance", "3245.67"),
    _ev("2026-05-26T09:30:00Z", "vacuum:Aspirateur Roborock S7", "state", "cleaning"),
    _ev("2026-05-26T08:00:00Z", "light:Lumière Salon", "state", "off"),
]

BASE_REASONING = """\
- 2026-05-26T14:00:00Z patterns=3 anomalies=0 predictions=2
- 2026-05-26T13:00:00Z patterns=2 anomalies=0 predictions=1
- 2026-05-26T12:00:00Z patterns=1 anomalies=1 predictions=0
- 2026-05-26T11:00:00Z patterns=4 anomalies=0 predictions=3
"""

BASE_PATTERNS = (
    "- Guillaume se lève généralement entre 7h et 8h les jours de semaine\n"
    "- La température du bureau monte de 2°C entre 14h et 17h (soleil Ouest)\n"
    "- Le robot aspirateur nettoie le salon le mardi matin\n"
    "- Les lumières sont généralement éteintes dans les pièces inoccupées\n"
    "- Guillaume vérifie ses comptes Firefly le vendredi soir"
)

# ══════════════════════════════════════════════════════════════════════════════
# Scenarios
# ══════════════════════════════════════════════════════════════════════════════


@dataclass
class Scenario:
    name: str
    description: str
    local_time: str
    local_timezone: str
    snapshot: dict
    recent_intents: str
    conversations: str
    reasoning_summary: str
    patterns_summary: str
    predictions_summary: str
    relevance_check: str


def _make_scenario(
    name: str,
    desc: str,
    local_time: str,
    entities_mod: dict[str, dict] | None = None,
    extra_events: list[dict] | None = None,
    recent_intents: str = "Aucun intent récent.",
    conversations: str = "Aucune conversation récente.",
    predictions: str = "(no active predictions)",
    rel_check: str = "",
) -> Scenario:
    """Build a scenario with the full baseline + modifications."""
    ents = [dict(e) for e in BASE_ENTITIES]
    if entities_mod:
        for e in ents:
            if e["display_name"] in entities_mod:
                e["properties"].update(entities_mod[e["display_name"]])

    evts = list(BASE_EVENTS)
    if extra_events:
        evts = extra_events + evts

    return Scenario(
        name=name,
        description=desc,
        local_time=local_time,
        local_timezone="America/Toronto (EDT, UTC-4)",
        snapshot={
            "taken_at": f"2026-05-26T{local_time.replace(':', '')}00-0400",
            "entities": ents,
            "recent_events": evts[:25],
            "summary": desc,
        },
        recent_intents=recent_intents,
        conversations=conversations,
        reasoning_summary=BASE_REASONING,
        patterns_summary=BASE_PATTERNS,
        predictions_summary=predictions,
        relevance_check=rel_check,
    )


SCENARIOS = [
    _make_scenario(
        name="calme",
        desc="Mardi après-midi tranquille. Tout est normal, aucune anomalie détectée. Températures stables, lumières éteintes, chats dans leurs paniers.",
        local_time="09:30",
        recent_intents="Aucun intent récent.",
        conversations="Aucune conversation récente.",
        rel_check="0 ou 1 intent maximum, pas d'intent inutile",
    ),
    _make_scenario(
        name="lumiere_oubliee",
        desc="La lumière du bureau est allumée depuis 17h30 (4h15). Aucune présence détectée dans le bureau. Le reste de la maison est normal.",
        local_time="21:45",
        entities_mod={
            "Lumière Bureau": {"state": "on", "brightness": 200},
            "Présence Bureau": {"state": "clear"},
        },
        extra_events=[
            _ev("2026-05-26T21:30:00Z", "light:Lumière Bureau", "state", "on"),
            _ev("2026-05-26T21:15:00Z", "binary_sensor:Présence Bureau", "state", "clear"),
            _ev("2026-05-26T17:30:00Z", "light:Lumière Bureau", "state", "on"),
        ],
        recent_intents="Aucun intent récent.",
        conversations="Aucune conversation récente.",
        rel_check="opération == turn_off OU notification.send pour avertir de la lumière",
    ),
    _make_scenario(
        name="alerte_temperature",
        desc="Température chambre élevée (27.0°C). Climatiseur éteint. Après-midi chaud, fenêtre fermée. La chambre dépasse le seuil de confort de 25°C.",
        local_time="14:15",
        entities_mod={
            "Température Chambre": {"value": "27.0"},
            "Climatiseur Chambre": {"state": "off", "current_temp": 27.0},
            "Fenêtre Chambre": {"state": "closed"},
        },
        extra_events=[
            _ev("2026-05-26T14:10:00Z", "sensor:Température Chambre", "value", "27.0"),
            _ev("2026-05-26T14:00:00Z", "climate:Climatiseur Chambre", "state", "off"),
            _ev("2026-05-26T13:30:00Z", "sensor:Température Chambre", "value", "26.5"),
            _ev("2026-05-26T13:00:00Z", "sensor:Température Chambre", "value", "25.8"),
        ],
        recent_intents="Aucun intent récent.",
        conversations="Aucune conversation récente.",
        rel_check="action_class IN ['notification.send', 'hvac', 'climate'] OU opération set_temperature",
    ),
    _make_scenario(
        name="anti_spam",
        desc="Température chambre encore élevée (26.5°C) mais Guillaume a DÉJÀ été notifié il y a 30 minutes. Ne doit PAS renvoyer la même alerte.",
        local_time="14:45",
        entities_mod={
            "Température Chambre": {"value": "26.5"},
            "Climatiseur Chambre": {"state": "off", "current_temp": 26.5},
        },
        extra_events=[
            _ev("2026-05-26T14:40:00Z", "sensor:Température Chambre", "value", "26.5"),
        ],
        recent_intents=(
            "- [done] « Ta chambre dépasse 25°C (27.0°C actuellement). Je te suggère d'allumer la climatisation. »\n"
            "  → Intent: coremind.plugin.notification.send, action_class: notification.send, category: suggest\n"
            "  → Envoyé à 14:15, Guillaume vu à 14:18 (pas de réponse)"
        ),
        conversations="Aucune conversation récente.",
        rel_check=(
            "0 intent (car déjà notifié) OU intent sémantiquement différent de l'alerte température. "
            "Ne doit PAS re-notifier pour la température de la chambre."
        ),
    ),
    _make_scenario(
        name="conversation",
        desc="Guillaume a répondu 'c'est fait merci !' à la suggestion d'éteindre la lumière cuisine. Ne pas re-notifier, répondre conversationnellement.",
        local_time="15:00",
        entities_mod={
            "Lumière Cuisine": {"state": "off", "brightness": 0},
        },
        extra_events=[
            _ev("2026-05-26T14:55:00Z", "light:Lumière Cuisine", "state", "off"),
        ],
        recent_intents=(
            "- [done] « Lumière cuisine allumée depuis 2h. Veux-tu que je l'éteigne ? »\n"
            "  → Intent: coremind.plugin.homeassistant.turn_off, action_class: light, category: ask\n"
            "  → Envoyé à 14:50"
        ),
        conversations=(
            "Guillaume (14:55): « c'est fait, merci ! »\n"
            "→ L'utilisateur confirme avoir réglé le problème de la lumière cuisine. "
            "Ne pas re-notifier. Répondre avec un message conversationnel qui accuse réception."
        ),
        rel_check=(
            "rationale mentionne 'conversation' OU expected_outcome conversationnel. "
            "Ne doit PAS re-notifier à propos de la lumière cuisine."
        ),
    ),
]


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════


def build_user_prompt(s: Scenario) -> str:
    return USER_TEMPLATE.format(
        local_time=s.local_time,
        local_timezone=s.local_timezone,
        snapshot_json=json.dumps(s.snapshot, indent=2, ensure_ascii=False),
        reasoning_summary=s.reasoning_summary,
        recent_intents_summary=s.recent_intents,
        patterns_summary=s.patterns_summary,
        conversations_summary=s.conversations,
        predictions_summary=s.predictions_summary,
        schema_json=_SCHEMA_JSON,
    )


def call_ollama(model: str, system: str, user: str, timeout: int = TIMEOUT) -> dict:
    """Call Ollama chat API. Returns {ok, status_code, json, tokens_in, tokens_out, latency_s, load_duration_s, error, ...}."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "format": "json",
        "options": {
            "temperature": 0.3,
            "num_ctx": NUM_CTX,
            "num_predict": NUM_PREDICT,
        },
        "stream": False,
        "keep_alive": "5m",
    }

    result = {
        "ok": False,
        "status_code": None,
        "json": None,
        "tokens_out": 0,
        "tokens_in": 0,
        "total_duration_ns": 0,
        "load_duration_ns": 0,
        "latency_s": 0.0,
        "load_s": 0.0,
        "error": None,
        "raw_content": "",
    }

    t0 = time.monotonic()
    try:
        resp = httpx.post(f"{OLLAMA_BASE}/api/chat", json=payload, timeout=timeout)
        result["latency_s"] = time.monotonic() - t0
        result["status_code"] = resp.status_code

        if resp.status_code != 200:
            result["error"] = f"HTTP {resp.status_code}: {resp.text[:300]}"
            return result

        data = resp.json()
        result["raw_content"] = data.get("message", {}).get("content", "")
        result["tokens_out"] = data.get("eval_count", 0)
        result["tokens_in"] = data.get("prompt_eval_count", 0)
        result["total_duration_ns"] = data.get("total_duration", 0)
        result["load_duration_ns"] = data.get("load_duration", 0)
        result["load_s"] = data.get("load_duration", 0) / 1e9

        # Parse JSON (strip markdown fences)
        content = result["raw_content"].strip()
        if content.startswith("```"):
            lines = content.split("\n")
            content = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        result["json"] = json.loads(content)
        result["ok"] = True
    except json.JSONDecodeError as e:
        result["error"] = f"JSON parse error: {e}"
        result["latency_s"] = time.monotonic() - t0
    except httpx.TimeoutException:
        result["error"] = f"Timeout after {timeout}s"
        result["latency_s"] = timeout
    except Exception as e:
        result["error"] = f"Exception: {type(e).__name__}: {e}"
        result["latency_s"] = time.monotonic() - t0

    return result


def validate_pydantic(data: dict) -> tuple[bool, str, int]:
    try:
        batch = QuestionBatch.model_validate(data)
        return (True, "", len(batch.questions))
    except Exception as e:
        return (False, str(e)[:300], 0)


def check_french(batch: QuestionBatch) -> bool:
    french_pattern = re.compile(r"\b(tu|ta|ton|tes|je|te|t')\b", re.IGNORECASE)
    for q in batch.questions:
        if q.proposed_action and q.proposed_action.expected_outcome:
            if french_pattern.search(q.proposed_action.expected_outcome):
                return True
    return False


def check_relevance(s: Scenario, batch: QuestionBatch) -> tuple[bool, str]:
    num = len(batch.questions)

    def _op_contains(q, substr: str) -> bool:
        return q.proposed_action is not None and substr in q.proposed_action.operation

    def _ac_is(q, val: str) -> bool:
        return q.proposed_action is not None and q.proposed_action.action_class == val

    def _text(q) -> str:
        t = q.question.text
        if q.proposed_action:
            t += " " + (q.proposed_action.expected_outcome or "")
        return t.lower()

    if s.name == "calme":
        if num <= 1:
            return (True, f"{num} intent(s) — acceptable")
        return (False, f"{num} intents — trop pour un scénario calme (max 1)")

    if s.name == "lumiere_oubliee":
        for q in batch.questions:
            if _op_contains(q, "turn_off") or _op_contains(q, "notification.send"):
                return (True, f"op={q.proposed_action.operation} — pertinent")
        return (False, "aucune opération turn_off ou notification trouvée")

    if s.name == "alerte_temperature":
        for q in batch.questions:
            ac = q.proposed_action.action_class if q.proposed_action else ""
            op = q.proposed_action.operation if q.proposed_action else ""
            if ac in ("notification.send", "hvac", "climate") or "temperature" in op:
                return (True, f"ac={ac}, op={op} — pertinent")
        return (False, "action_class non pertinente")

    if s.name == "anti_spam":
        if num == 0:
            return (True, "0 intent — excellent anti-spam")
        for q in batch.questions:
            txt = _text(q)
            if any(w in txt for w in ("température", "chambre", "25°", "26")):
                if "chambre" in txt and ("température" in txt or "°" in txt):
                    return (False, f"spam: répète alerte température: {txt[:100]}")
        return (True, "intent différent de l'alerte précédente")

    if s.name == "conversation":
        for q in batch.questions:
            outcome = (
                (q.proposed_action.expected_outcome or "").lower() if q.proposed_action else ""
            )
            rat = (q.rationale or "").lower()
            if any(w in rat for w in ("conversation", "conversationnel")):
                return (True, "rationale mentionne conversation")
            if any(
                w in outcome for w in ("c'est fait", "merci", "réglé", "parfait", "noté", "super")
            ):
                return (True, f"ton conversationnel: {outcome[:80]}")
        for q in batch.questions:
            txt = _text(q)
            if "cuisine" in txt and "lumière" in txt:
                return (False, "re-notification cuisine — erreur")
        return (True, "pas de re-notification")

    return (True, "règle non définie")


# ══════════════════════════════════════════════════════════════════════════════
# Progressive save
# ══════════════════════════════════════════════════════════════════════════════


def save_partial(model: str, model_results: list[dict]) -> None:
    """Save partial results after each model completes."""
    partial_path = OUT_DIR / "results_v2_partial.json"
    partial_path.write_text(json.dumps(model_results, indent=2, ensure_ascii=False))


# ══════════════════════════════════════════════════════════════════════════════
# Warmup
# ══════════════════════════════════════════════════════════════════════════════


def run_warmup(model: str) -> bool:
    try:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": 'Say "warm" in JSON: {"ok": true}'}],
            "format": "json",
            "options": {"temperature": 0.3, "num_ctx": 2048},
            "stream": False,
            "keep_alive": "5m",
        }
        resp = httpx.post(f"{OLLAMA_BASE}/api/chat", json=payload, timeout=WARMUP_TIMEOUT)
        return resp.status_code == 200
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════


def main() -> int:
    all_results: list[dict] = []
    cloud_calls = 0

    scenario_prompts = {s.name: build_user_prompt(s) for s in SCENARIOS}

    # Log prompt sizes
    print("=" * 70)
    print("PROMPT SIZE ESTIMATES (~3 chars/token)")
    for s in SCENARIOS:
        total_chars = len(SYSTEM_PROMPT) + len(scenario_prompts[s.name])
        est_tokens = total_chars // 3
        print(f"  {s.name}: ~{total_chars} chars → ~{est_tokens} tokens input")
    print("=" * 70)

    for model in MODELS:
        is_cloud = ":cloud" in model
        if is_cloud and cloud_calls >= CLOUD_LIMIT:
            print(f"\nSKIP {model} — cloud limit ({cloud_calls}/{CLOUD_LIMIT})")
            continue

        print(f"\n{'=' * 70}")
        print(f"MODEL: {model}")
        print(f"{'=' * 70}")

        if not is_cloud:
            print("  Warmup...", end=" ", flush=True)
            ok = run_warmup(model)
            print("OK" if ok else "FAILED")
        else:
            print("  (cloud model — skip warmup)")

        model_results: list[dict] = []

        for scenario in SCENARIOS:
            if is_cloud:
                cloud_calls += 1

            print(f"  [{scenario.name}] ", end="", flush=True)
            r = call_ollama(model, SYSTEM_PROMPT, scenario_prompts[scenario.name])

            mr = {
                "model": model,
                "scenario": scenario.name,
                "json_ok": r["ok"],
                "pydantic_ok": False,
                "relevance_ok": False,
                "relevance_detail": "",
                "french_ok": False,
                "latency_s": r["latency_s"],
                "load_s": r["load_s"],
                "tokens_in": r["tokens_in"],
                "tokens_out": r["tokens_out"],
                "num_questions": 0,
                "error": r["error"] or "",
                "raw_preview": "",
                "truncation_ratio": 0.0,
            }

            if r["ok"] and r["json"]:
                p_ok, p_err, nq = validate_pydantic(r["json"])
                mr["pydantic_ok"] = p_ok
                mr["num_questions"] = nq
                mr["raw_preview"] = r["raw_content"][:200]
                if not p_ok:
                    mr["error"] = f"Pydantic: {p_err}"
                else:
                    batch = QuestionBatch.model_validate(r["json"])
                    mr["french_ok"] = check_french(batch)
                    rel_ok, rel_detail = check_relevance(scenario, batch)
                    mr["relevance_ok"] = rel_ok
                    mr["relevance_detail"] = rel_detail
            else:
                mr["raw_preview"] = r["raw_content"][:200] or r["error"][:200]

            # Truncation detection: if tokens_out >= 95% of NUM_PREDICT
            if r["tokens_out"] > 0:
                mr["truncation_ratio"] = r["tokens_out"] / NUM_PREDICT

            icon = (
                "✅"
                if mr["pydantic_ok"] and mr["relevance_ok"]
                else ("⚠️" if mr["pydantic_ok"] else "❌")
            )
            trunc = "⚠️TRUNC" if mr["truncation_ratio"] >= 0.95 else ""
            print(
                f"{icon} Pydantic={mr['pydantic_ok']} Relevant={mr['relevance_ok']} "
                f"FR={mr['french_ok']} Q={mr['num_questions']} "
                f"{mr['latency_s']:.1f}s tk_in={mr['tokens_in']} tk_out={mr['tokens_out']} "
                f"{trunc}"
            )

            model_results.append(mr)
            all_results.append(mr)

            # Early abort for hopeless models (3+ JSON failures on first 3)
            if not mr["json_ok"]:
                json_fails = sum(1 for r_ in model_results if not r_["json_ok"])
                if json_fails >= 3 and len(model_results) < 5:
                    print(f"  ⚠️  Abandon: {json_fails}/3 JSON failures — modèle non viable")
                    for s in SCENARIOS:
                        if s.name not in {r_["scenario"] for r_ in model_results}:
                            skipped = {
                                "model": model,
                                "scenario": s.name,
                                "json_ok": False,
                                "pydantic_ok": False,
                                "relevance_ok": False,
                                "relevance_detail": "",
                                "french_ok": False,
                                "latency_s": 0,
                                "load_s": 0,
                                "tokens_in": 0,
                                "tokens_out": 0,
                                "num_questions": 0,
                                "error": "SKIPPED: modèle abandonné après 3 échecs JSON",
                                "raw_preview": "",
                                "truncation_ratio": 0.0,
                            }
                            model_results.append(skipped)
                            all_results.append(skipped)
                    break

        # Progressive save after each model
        save_partial(model, all_results)
        print(f"  💾 saved {len(model_results)} results to results_v2_partial.json")

    # ── Save full results ───────────────────────────────────────────────────
    raw_path = OUT_DIR / "results_v2_raw.json"
    raw_path.write_text(json.dumps(all_results, indent=2, ensure_ascii=False))
    print(f"\nFull results → {raw_path}")

    # ── Summaries by model ──────────────────────────────────────────────────
    model_summaries: dict[str, dict] = {}
    for r in all_results:
        ms = model_summaries.setdefault(
            r["model"],
            {
                "total": 0,
                "json_ok": 0,
                "pydantic_ok": 0,
                "relevance_ok": 0,
                "french_ok": 0,
                "latencies": [],
                "tokens_in": [],
                "tokens_out": [],
                "errors": [],
                "truncation_ratio": [],
                "skipped": 0,
            },
        )
        ms["total"] += 1
        if r["json_ok"]:
            ms["json_ok"] += 1
        if r["pydantic_ok"]:
            ms["pydantic_ok"] += 1
        if r["relevance_ok"]:
            ms["relevance_ok"] += 1
        if r["french_ok"]:
            ms["french_ok"] += 1
        if r["latency_s"] > 0:
            ms["latencies"].append(r["latency_s"])
        if r["tokens_in"] > 0:
            ms["tokens_in"].append(r["tokens_in"])
        if r["tokens_out"] > 0:
            ms["tokens_out"].append(r["tokens_out"])
        if r["error"] and "SKIPPED" in r["error"]:
            ms["skipped"] += 1
        elif r["error"]:
            ms["errors"].append(r["error"])
        ms["truncation_ratio"].append(r.get("truncation_ratio", 0))

    # ── Generate Markdown report ────────────────────────────────────────────
    lines = [
        "# CoreMind LLM Benchmark v2 — Intention Layer (L5)",
        "",
        f"**Date:** 2026-05-26 | **Scénarios:** {len(SCENARIOS)} | **Modèles:** {len(MODELS)}",
        f"**Schéma:** `QuestionBatch` (Pydantic v2, strict) | **Température:** 0.3 | **Format:** JSON forcé",
        "",
        "## 🔄 Changements vs v1",
        "",
        "| Paramètre | v1 | v2 | Raison |",
        "|-----------|----|----|--------|",
        f"| `num_predict` | 2048 | {NUM_PREDICT} | CoreMind max_tokens=4096 + marge |",
        f"| `num_ctx` | 8192 | {NUM_CTX} | Snapshots réels = 10-30k tokens |",
        "| Scénarios | ~200 tokens | ~5000-12000 tokens | 20-30 entités, events, reasoning, patterns |",
        f"| Timeout | 120s | {TIMEOUT}s | Gros contextes = plus de temps |",
        "| Truncation detection | Non | Oui (ratio tokens_out/num_predict) | Détecte si encore tronqué |",
        "| Save progressive | Non | Oui (après chaque modèle) | Résilience aux crashs |",
        "",
        "## 📊 Résumé Global",
        "",
        "| Modèle | JSON ✅ | Pydantic ✅ | Pertinence ✅ | FR ✅ | Latence moy | Tokens sortie moy | Ratio trunc | Verdict |",
        "|--------|---------|-------------|---------------|-------|-------------|-------------------|-------------|---------|",
    ]

    for model in MODELS:
        ms = model_summaries.get(model)
        if not ms or ms["total"] == 0:
            lines.append(f"| {model} | — | — | — | — | — | — | — | Non testé |")
            continue

        t = ms["total"]
        eff_t = t - ms["skipped"]
        if eff_t == 0:
            lines.append(f"| {model} | — | — | — | — | — | — | — | Non testé (skipped) |")
            continue

        json_pct = ms["json_ok"] / eff_t * 100
        pyd_pct = ms["pydantic_ok"] / eff_t * 100
        rel_pct = ms["relevance_ok"] / eff_t * 100
        fr_pct = ms["french_ok"] / eff_t * 100
        avg_lat = sum(ms["latencies"]) / len(ms["latencies"]) if ms["latencies"] else 0
        avg_tok = sum(ms["tokens_out"]) / len(ms["tokens_out"]) if ms["tokens_out"] else 0
        max_trunc = max(ms["truncation_ratio"]) if ms["truncation_ratio"] else 0

        if pyd_pct == 0:
            verdict = "❌ ÉLIMINÉ (JSON invalide)"
            if max_trunc >= 0.95:
                verdict += " [TOUJOURS TRONQUÉ]"
        elif pyd_pct < 80:
            verdict = "⚠️ RISQUE (JSON instable)"
        elif rel_pct >= 80 and fr_pct >= 80:
            verdict = "✅ VIABLE"
        elif rel_pct >= 60:
            verdict = "⚠️ PARTIEL"
        else:
            verdict = "❌ NON VIABLE"

        trunc_str = f"{max_trunc:.1%}" if max_trunc < 0.95 else f"{max_trunc:.1%} ⚠️"
        lines.append(
            f"| {model} | {json_pct:.0f}% ({ms['json_ok']}/{eff_t}) "
            f"| {pyd_pct:.0f}% ({ms['pydantic_ok']}/{eff_t}) "
            f"| {rel_pct:.0f}% ({ms['relevance_ok']}/{eff_t}) "
            f"| {fr_pct:.0f}% ({ms['french_ok']}/{eff_t}) "
            f"| {avg_lat:.1f}s "
            f"| {avg_tok:.0f} "
            f"| {trunc_str} "
            f"| {verdict} |"
        )

    lines.append("")
    lines.append("## 📋 Détails par Scénario")
    lines.append("")

    for scenario in SCENARIOS:
        lines.append(f"### {scenario.name} — {scenario.description}")
        lines.append("")
        lines.append(
            "| Modèle | JSON | Pydantic | Pertinent | FR | Latence | tk_in→tk_out | Q | Erreur |"
        )
        lines.append(
            "|--------|------|----------|-----------|----|---------|--------------|---|--------|"
        )

        for model in MODELS:
            matches = [
                r for r in all_results if r["model"] == model and r["scenario"] == scenario.name
            ]
            if not matches:
                lines.append(f"| {model} | — | — | — | — | — | — | — | Non testé |")
                continue
            r = matches[0]
            err_short = (r["error"][:55] + "...") if len(r["error"]) > 55 else (r["error"] or "—")
            tok_str = f"{r['tokens_in']}→{r['tokens_out']}"
            lines.append(
                f"| {model} "
                f"| {'✅' if r['json_ok'] else '❌'} "
                f"| {'✅' if r['pydantic_ok'] else '❌'} "
                f"| {'✅' if r['relevance_ok'] else '❌'} ({r['relevance_detail'][:25]}) "
                f"| {'✅' if r['french_ok'] else '❌'} "
                f"| {r['latency_s']:.1f}s "
                f"| {tok_str} "
                f"| {r['num_questions']} "
                f"| {err_short} |"
            )
        lines.append("")

    # ── v1 → v2 Comparison ──────────────────────────────────────────────────
    lines.append("## 📈 Comparaison v1 → v2")
    lines.append("")
    lines.append(
        "| Modèle | v1 Pydantic | v2 Pydantic | v1 Pertinence | v2 Pertinence | v1 Latence | v2 Latence | Verdict |"
    )
    lines.append(
        "|--------|------------|------------|---------------|---------------|------------|------------|---------|"
    )

    v1_data = {
        "qwen3.6:27b": (20, 20, 76.5),
        "qwen3.6:35b": (20, 20, 100.0),
        "glm-4.7-flash:latest": (0, 0, 27.2),
        "gpt-oss:20b": (100, 80, 9.7),
        "gemma4:26b": (0, 0, 21.7),
        "mistral-small3.2:24b": (100, 60, 6.5),
        "deepseek-v4-flash:cloud": (100, 80, 26.4),
    }

    for model in MODELS:
        ms = model_summaries.get(model)
        v1 = v1_data.get(model, (0, 0, 0))
        if ms and ms["total"] > ms["skipped"]:
            eff_t = ms["total"] - ms["skipped"]
            v2_pyd = ms["pydantic_ok"] / eff_t * 100
            v2_rel = ms["relevance_ok"] / eff_t * 100
            v2_lat = sum(ms["latencies"]) / len(ms["latencies"]) if ms["latencies"] else 0
            delta_pyd = v2_pyd - v1[0]
            delta_rel = v2_rel - v1[1]
            change = (
                "↑ MEILLEUR"
                if delta_pyd > 20
                else ("↑" if delta_pyd > 0 else ("=" if delta_pyd == 0 else "↓"))
            )
            lines.append(
                f"| {model} | {v1[0]}% | {v2_pyd:.0f}% ({delta_pyd:+.0f}) | "
                f"{v1[1]}% | {v2_rel:.0f}% ({delta_rel:+.0f}) | "
                f"{v1[2]:.1f}s | {v2_lat:.1f}s | {change} |"
            )
        elif ms and ms["skipped"] > 0:
            lines.append(
                f"| {model} | {v1[0]}% | SKIPPED | {v1[1]}% | SKIPPED | {v1[2]:.1f}s | — | Abandonné |"
            )
        else:
            lines.append(
                f"| {model} | {v1[0]}% | — | {v1[1]}% | — | {v1[2]:.1f}s | — | Non testé |"
            )

    # ── Recommendations ─────────────────────────────────────────────────────
    lines.append("")
    lines.append("## 🎯 Recommandations Finales")
    lines.append("")

    # Find best models
    def model_score(model: str) -> float:
        ms = model_summaries.get(model)
        if not ms or ms["total"] - ms["skipped"] == 0:
            return -1
        eff_t = ms["total"] - ms["skipped"]
        return ms["pydantic_ok"] * 5 + ms["relevance_ok"] * 3 + ms["french_ok"] * 2

    ranked = sorted(MODELS, key=model_score, reverse=True)
    best = ranked[0] if model_score(ranked[0]) > 0 else None
    best_local = next((m for m in ranked if ":cloud" not in m and model_score(m) > 0), None)

    # Best overall
    lines.append("### Meilleur modèle global")
    lines.append("")
    if best:
        ms = model_summaries.get(best, {})
        eff_t = (ms.get("total", 0) - ms.get("skipped", 0)) or 1
        pyd_pct = ms.get("pydantic_ok", 0) / eff_t * 100
        lat = sum(ms.get("latencies", [0])) / max(len(ms.get("latencies", [1])), 1)
        lines.append(f"**→ `{best}`** — Pydantic {pyd_pct:.0f}%, {lat:.1f}s moy")

    lines.append("")
    lines.append("### Pour `intention` (cycles fréquents, ~10 min)")
    lines.append("")
    if best_local:
        ms = model_summaries.get(best_local, {})
        eff_t = (ms.get("total", 0) - ms.get("skipped", 0)) or 1
        pyd_pct = ms.get("pydantic_ok", 0) / eff_t * 100
        lat = sum(ms.get("latencies", [0])) / max(len(ms.get("latencies", [1])), 1)
        lines.append(
            f"**→ `{best_local}`** (local, gratuit, Pydantic {pyd_pct:.0f}%, {lat:.1f}s moy)"
        )
    else:
        lines.append("Aucun modèle local viable — fallback cloud nécessaire.")

    lines.append("")
    lines.append("### Pour `reasoning` et `reflection` (cycles rares)")
    lines.append("")
    deep_ok = model_summaries.get("deepseek-v4-flash:cloud", {}).get("pydantic_ok", 0) > 0
    if deep_ok:
        lines.append(
            "**→ `deepseek-v4-flash:cloud`** — qualité garantie, coût acceptable (~1 appel/h)"
        )
    if best_local:
        lines.append(f"Alternative locale: `{best_local}`")

    lines.append("")
    lines.append("### qwen3.6:27b vs gpt-oss:20b (le match clé)")
    lines.append("")
    q27 = model_summaries.get("qwen3.6:27b")
    gpt20 = model_summaries.get("gpt-oss:20b")
    if q27 and gpt20:
        eff27 = (q27.get("total", 0) - q27.get("skipped", 0)) or 1
        eff20 = (gpt20.get("total", 0) - gpt20.get("skipped", 0)) or 1
        p27 = q27.get("pydantic_ok", 0) / eff27 * 100
        p20 = gpt20.get("pydantic_ok", 0) / eff20 * 100
        r27 = q27.get("relevance_ok", 0) / eff27 * 100
        r20 = gpt20.get("relevance_ok", 0) / eff20 * 100
        l27 = sum(q27.get("latencies", [0])) / max(len(q27.get("latencies", [1])), 1)
        l20 = sum(gpt20.get("latencies", [0])) / max(len(gpt20.get("latencies", [1])), 1)
        lines.append(f"| Critère | qwen3.6:27b | gpt-oss:20b | Gagnant |")
        lines.append(f"|---------|------------|-------------|---------|")
        lines.append(
            f"| Pydantic | {p27:.0f}% | {p20:.0f}% | {'qwen' if p27 > p20 else 'gpt-oss' if p20 > p27 else 'égalité'} |"
        )
        lines.append(
            f"| Pertinence | {r27:.0f}% | {r20:.0f}% | {'qwen' if r27 > r20 else 'gpt-oss' if r20 > r27 else 'égalité'} |"
        )
        lines.append(
            f"| Latence | {l27:.1f}s | {l20:.1f}s | {'qwen' if l27 < l20 else 'gpt-oss'} |"
        )

    lines.append("")
    lines.append("## ⚙️ Config TOML Recommandée")
    lines.append("")
    lines.append("```toml")
    lines.append("[llm]")
    if best_local:
        lines.append(f"# Intention: cycles fréquents (~10 min), modèle local gratuit")
        lines.append(f'intention_model = "ollama/{best_local}"')
        lines.append("intention_max_tokens = 4096")
        lines.append("intention_temperature = 0.3")
        lines.append("")
    lines.append("# Reasoning: cycles rares (~1h), qualité > vitesse")
    lines.append('reasoning_model = "deepseek-v4-flash:cloud"')
    lines.append("reasoning_max_tokens = 4096")
    lines.append("reasoning_temperature = 0.5")
    lines.append("")
    lines.append("# Reflection: cycles très rares, qualité max")
    lines.append('reflection_model = "deepseek-v4-flash:cloud"')
    lines.append("reflection_max_tokens = 4096")
    lines.append("```")

    # ── Write report ────────────────────────────────────────────────────────
    report_path = OUT_DIR / "RESULTS-v2.md"
    report_path.write_text("\n".join(lines) + "\n")
    print(f"\nReport → {report_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
