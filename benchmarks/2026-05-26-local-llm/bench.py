#!/usr/bin/env python3
"""CoreMind LLM Benchmark — 7 models, 5 scenarios.
Evaluates JSON validity (Pydantic strict), business relevance, French output, latency, tokens.
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

# ── Path setup so we can import coremind from the project venv ──────────────
PROJECT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT / "src"))

from coremind.intention.schemas import QuestionBatch  # noqa: E402

# ── Constants ───────────────────────────────────────────────────────────────
OLLAMA_BASE = "http://10.0.0.175:11434"
TIMEOUT = 120  # seconds per call
WARMUP_TIMEOUT = 180

MODELS = [
    "qwen3.6:27b",
    "qwen3.6:35b",
    "glm-4.7-flash:latest",
    "gpt-oss:20b",
    "gemma4:26b",
    "mistral-small3.2:24b",
    "deepseek-v4-flash:cloud",  # baseline — limit 5 calls
]
CLOUD_LIMIT = 5  # max calls for deepseek-v4-flash:cloud

# ── System prompt (abridged from CoreMind prompts.py) ───────────────────────
SYSTEM_PROMPT = """\
You are the intention layer (L5) of CoreMind, a continuous personal intelligence daemon.

Your job: generate a small set of INTERNAL QUESTIONS the system must answer
right now, based on the current world snapshot, recent reasoning outputs, and the user's
active patterns.

Each question MUST propose a concrete action using one of the AVAILABLE OPERATIONS
listed below. A question without a proposed action is useless - CoreMind cannot act on it.

Available operations:
  coremind.plugin.notification.send     - Send a notification to Guillaume
  coremind.plugin.homeassistant.turn_off - Turn off a HA entity
  coremind.plugin.homeassistant.turn_on  - Turn on a HA entity
  coremind.plugin.homeassistant.set_temperature - Set climate temperature
  coremind.plugin.homeassistant.get_state - Query HA entity state

Each question must:
- be grounded in specific entities from the snapshot (cite them in grounding),
- be honest about confidence,
- have an expected_outcome written as a NATURAL French message TO Guillaume.
  Example: "Je te préviens si ta chambre dépasse 25°C"
  NOT: "User receives a notification about bedroom temperature"

Categories in your rationale: suggest for low-risk (notifications), ask for mutations (turn_on/off).

CRITICAL RULES:
- Anti-redundancy: read "Recent intents" carefully. If a proposed intent is semantically similar
  to one already listed, DO NOT emit it. Silence > spam.
- Conversation mode: when the user REPLIED to a previous notification, generate a response
  with category="conversation" mentioned in rationale, not a new notification.
  If user indicates problem resolved ("c'est fait", "merci"), acknowledge and do NOT re-notify.
- Language: ALL expected_outcome text MUST be in French. NEVER use English.
- Output VALID JSON ONLY matching the schema exactly.
"""

# ── User prompt template ────────────────────────────────────────────────────
USER_TEMPLATE = """\
## Current local time

Il est {local_time} ({local_timezone}).

## World snapshot (JSON)

```json
{snapshot_json}
```

## Recent intents (for loop avoidance)

{recent_intents_summary}

## Recent user conversations

{conversations_summary}

## Required response schema (JSON Schema)

```json
{schema_json}
```

Emit a single JSON object matching the schema. Limit yourself to at most 3 questions.
Do not include any text outside the JSON object.
"""

# Pre-compute schema once
SCHEMA_JSON = json.dumps(QuestionBatch.model_json_schema(), indent=2, ensure_ascii=False)


# ── Scenarios ───────────────────────────────────────────────────────────────
@dataclass
class Scenario:
    name: str
    description: str
    local_time: str
    local_timezone: str
    snapshot: dict
    recent_intents: str
    conversations: str
    # Validation rules (callable receiving QuestionBatch -> bool)
    relevance_check: str = ""  # description of what to check


SCENARIOS = [
    Scenario(
        name="calme",
        description="Snapshot vide, rien à signaler — doit générer 0 ou 1 intent max",
        local_time="2026-05-26 09:30",
        local_timezone="America/Toronto (EDT, UTC-4)",
        snapshot={
            "entities": {},
            "events": [],
            "summary": "Mardi matin, tout est calme. Aucune anomalie détectée.",
        },
        recent_intents="Aucun intent récent.",
        conversations="Aucune conversation récente.",
        relevance_check="0 ou 1 intent maximum, pas d'intent inutile",
    ),
    Scenario(
        name="lumiere_oubliee",
        description="Lumière bureau ON depuis 4h, soir, personne absente → turn_off",
        local_time="2026-05-26 21:45",
        local_timezone="America/Toronto (EDT, UTC-4)",
        snapshot={
            "entities": {
                "light.bureau": {
                    "state": "on",
                    "attributes": {"friendly_name": "Lumière Bureau", "brightness": 200},
                },
                "sensor.bureau_presence": {
                    "state": "clear",
                    "attributes": {"friendly_name": "Présence Bureau"},
                },
            },
            "events": [
                {
                    "entity": {"type": "light", "id": "light.bureau"},
                    "event_type": "state_change",
                    "old_state": "off",
                    "new_state": "on",
                    "timestamp": "2026-05-26T17:30:00Z",
                }
            ],
            "summary": "La lumière du bureau est allumée depuis 17h30 (4h15). Aucune présence détectée.",
        },
        recent_intents="Aucun intent récent.",
        conversations="Aucune conversation récente.",
        relevance_check="opération == coremind.plugin.homeassistant.turn_off OU notification.send pour avertir",
    ),
    Scenario(
        name="alerte_temperature",
        description="sensor.chambre = 27°C, climatiseur OFF → doit suggérer notification ou action HA",
        local_time="2026-05-26 14:15",
        local_timezone="America/Toronto (EDT, UTC-4)",
        snapshot={
            "entities": {
                "sensor.chambre_temperature": {
                    "state": "27.0",
                    "attributes": {"friendly_name": "Température Chambre", "unit": "°C"},
                },
                "climate.chambre": {
                    "state": "off",
                    "attributes": {"friendly_name": "Climatiseur Chambre", "temperature": 22},
                },
            },
            "events": [],
            "summary": "Température chambre élevée (27°C). Climatiseur éteint. Après-midi chaud.",
        },
        recent_intents="Aucun intent récent.",
        conversations="Aucune conversation récente.",
        relevance_check="action_class IN ['notification.send', 'hvac', 'climate'] ou opération set_temperature",
    ),
    Scenario(
        name="anti_spam",
        description="Recent intents contient déjà alerte température — ne doit PAS régénérer la même chose",
        local_time="2026-05-26 14:45",
        local_timezone="America/Toronto (EDT, UTC-4)",
        snapshot={
            "entities": {
                "sensor.chambre_temperature": {
                    "state": "26.5",
                    "attributes": {"friendly_name": "Température Chambre", "unit": "°C"},
                },
            },
            "events": [],
            "summary": "Température chambre encore élevée (26.5°C).",
        },
        recent_intents=(
            "- [14:15] « Ta chambre dépasse 25°C (27°C) » — notification envoyée à Guillaume\n"
            "  → Intent: coremind.plugin.notification.send, action_class: notification.send"
        ),
        conversations="Aucune conversation récente.",
        relevance_check="0 intent (car déjà notifié) OU intent sémantiquement différent de l'alerte température",
    ),
    Scenario(
        name="conversation",
        description="User a répliqué 'c'est fait' à un intent récent → réponse conversation, pas nouvelle notif",
        local_time="2026-05-26 15:00",
        local_timezone="America/Toronto (EDT, UTC-4)",
        snapshot={
            "entities": {
                "light.cuisine": {
                    "state": "off",
                    "attributes": {"friendly_name": "Lumière Cuisine"},
                },
            },
            "events": [],
            "summary": "Cuisine normale. Rien d'anormal.",
        },
        recent_intents=(
            "- [14:50] « Lumière cuisine allumée depuis 2h » — suggestion turn_off envoyée\n"
            "  → Intent: coremind.plugin.homeassistant.turn_off, action_class: light"
        ),
        conversations=(
            "Guillaume (14:55): « c'est fait, merci ! »\n"
            "→ L'utilisateur confirme avoir réglé le problème de la lumière cuisine. "
            "Ne pas re-notifier. Répondre avec un message conversationnel."
        ),
        relevance_check=(
            "rationale mentionne 'conversation' OU pas de proposed_action avec notification sur le même sujet. "
            "Ne doit PAS re-notifier à propos de la lumière cuisine."
        ),
    ),
]

# Output schema as string (for prompt)
SCHEMA_JSON_STR = json.dumps(QuestionBatch.model_json_schema(), indent=2)


# ── Helpers ─────────────────────────────────────────────────────────────────


def build_user_prompt(scenario: Scenario) -> str:
    return USER_TEMPLATE.format(
        local_time=scenario.local_time,
        local_timezone=scenario.local_timezone,
        snapshot_json=json.dumps(scenario.snapshot, indent=2, ensure_ascii=False),
        recent_intents_summary=scenario.recent_intents,
        conversations_summary=scenario.conversations,
        schema_json=SCHEMA_JSON_STR,
    )


def call_ollama(
    model: str,
    system: str,
    user: str,
    timeout: int = TIMEOUT,
) -> dict:
    """Call Ollama chat API. Returns {ok, status_code, json, tokens, latency_s, error}."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "format": "json",
        "options": {
            "temperature": 0.3,
            "num_ctx": 8192,
            "num_predict": 2048,
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
        "latency_s": 0.0,
        "error": None,
        "raw_content": "",
    }

    t0 = time.monotonic()
    try:
        resp = httpx.post(
            f"{OLLAMA_BASE}/api/chat",
            json=payload,
            timeout=timeout,
        )
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

        # Parse JSON
        content = result["raw_content"].strip()
        # Some models wrap in ```json ... ``` — strip that
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
    """Validate against Pydantic QuestionBatch. Returns (valid, error, num_questions)."""
    try:
        batch = QuestionBatch.model_validate(data)
        return (True, "", len(batch.questions))
    except Exception as e:
        return (False, str(e)[:300], 0)


def check_french(batch: QuestionBatch) -> bool:
    """Check if at least one expected_outcome/message contains French markers."""
    french_pattern = re.compile(r"\b(tu|ta|ton|tes|je|te|t')\b", re.IGNORECASE)
    for q in batch.questions:
        if q.proposed_action and q.proposed_action.expected_outcome:
            if french_pattern.search(q.proposed_action.expected_outcome):
                return True
    return False


def check_relevance(scenario: Scenario, batch: QuestionBatch) -> tuple[bool, str]:
    """Check business relevance based on scenario-specific rules. Returns (ok, reason)."""
    num = len(batch.questions)

    if scenario.name == "calme":
        if num <= 1:
            return (True, f"{num} intent(s) — acceptable")
        return (False, f"{num} intents — trop pour un scénario calme (max 1)")

    if scenario.name == "lumiere_oubliee":
        for q in batch.questions:
            if q.proposed_action:
                op = q.proposed_action.operation
                if "turn_off" in op or "notification.send" in op:
                    return (True, f"opération={op} — pertinent")
        return (False, "aucune opération turn_off ou notification trouvée")

    if scenario.name == "alerte_temperature":
        for q in batch.questions:
            if q.proposed_action:
                ac = q.proposed_action.action_class
                op = q.proposed_action.operation
                if ac in ("notification.send", "hvac", "climate") or "temperature" in op:
                    return (True, f"action_class={ac}, op={op} — pertinent")
        return (False, "action_class non pertinente pour alerte température")

    if scenario.name == "anti_spam":
        if num == 0:
            return (True, "0 intent — excellent, pas de spam")
        # Must be semantically different from temperature alert
        for q in batch.questions:
            if q.proposed_action:
                text = (q.question.text + " " + (q.proposed_action.expected_outcome or "")).lower()
                if "température" in text or "chambre" in text or "25°" in text or "26" in text:
                    return (
                        False,
                        f"spam détecté: intent similaire à l'alerte précédente: {text[:100]}",
                    )
        return (True, "intent différent de l'alerte précédente")

    if scenario.name == "conversation":
        for q in batch.questions:
            # Check for conversational tone in expected_outcome
            if q.proposed_action:
                text = q.proposed_action.expected_outcome.lower()
                if any(
                    w in text for w in ("c'est fait", "merci", "réglé", "parfait", "ok", "noté")
                ):
                    return (True, f"ton conversationnel détecté: {text[:80]}")
            if "conversation" in (q.rationale or "").lower():
                return (True, "rationale mentionne conversation")
        # Check NO re-notification about cuisine light
        for q in batch.questions:
            if q.proposed_action:
                text = q.question.text + " " + (q.proposed_action.expected_outcome or "")
                if "cuisine" in text.lower() and "lumière" in text.lower():
                    return (False, "re-notification sur la lumière cuisine — erreur")
            if q.proposed_action and "turn_off" in q.proposed_action.operation:
                if "cuisine" in q.proposed_action.expected_outcome.lower():
                    return (False, "turn_off cuisine malgré confirmation — erreur")
        return (True, "pas de re-notification, réponse acceptable")

    return (True, "règle non définie")


# ── Main benchmark ──────────────────────────────────────────────────────────


@dataclass
class ModelResult:
    model: str
    scenario: str
    json_ok: bool
    pydantic_ok: bool
    relevance_ok: bool
    relevance_detail: str
    french_ok: bool
    latency_s: float
    tokens_out: int
    num_questions: int
    error: str = ""
    raw_preview: str = ""


def run_warmup(model: str) -> bool:
    """Single warmup call (not measured). Returns True if model responds OK."""
    try:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": 'Say "warm" in JSON: {"ok": true}'}],
            "format": "json",
            "options": {"temperature": 0.3, "num_ctx": 2048},
            "stream": False,
            "keep_alive": "5m",
        }
        resp = httpx.post(
            f"{OLLAMA_BASE}/api/chat",
            json=payload,
            timeout=WARMUP_TIMEOUT,
        )
        return resp.status_code == 200
    except Exception:
        return False


def main():
    out_dir = Path(__file__).resolve().parent
    results: list[ModelResult] = []
    cloud_calls = 0

    # Pre-compute user prompts for all scenarios
    scenario_prompts = {s.name: build_user_prompt(s) for s in SCENARIOS}

    for model in MODELS:
        is_cloud = ":cloud" in model
        if is_cloud and cloud_calls >= CLOUD_LIMIT:
            print(f"\n{'=' * 70}")
            print(f"SKIP {model} — cloud call limit reached ({cloud_calls}/{CLOUD_LIMIT})")
            continue

        print(f"\n{'=' * 70}")
        print(f"MODEL: {model}")
        print(f"{'=' * 70}")

        # Warmup (skip for cloud to save credits, or do 1 warmup)
        if not is_cloud:
            print("  Warmup...", end=" ", flush=True)
            ok = run_warmup(model)
            print("OK" if ok else "FAILED")
            if not ok:
                print(f"  ⚠️  Warmup échoué, continuing anyway...")

        for scenario in SCENARIOS:
            if is_cloud:
                cloud_calls += 1

            print(f"  [{scenario.name}] ", end="", flush=True)
            user_prompt = scenario_prompts[scenario.name]
            r = call_ollama(model, SYSTEM_PROMPT, user_prompt)

            mr = ModelResult(
                model=model,
                scenario=scenario.name,
                json_ok=r["ok"],
                pydantic_ok=False,
                relevance_ok=False,
                relevance_detail="",
                french_ok=False,
                latency_s=r["latency_s"],
                tokens_out=r["tokens_out"],
                num_questions=0,
                error=r["error"] or "",
            )

            if r["ok"] and r["json"]:
                p_ok, p_err, nq = validate_pydantic(r["json"])
                mr.pydantic_ok = p_ok
                mr.num_questions = nq
                if not p_ok:
                    mr.error = f"Pydantic: {p_err}"
                else:
                    # Re-validate to get the batch for relevance check
                    batch = QuestionBatch.model_validate(r["json"])
                    mr.french_ok = check_french(batch)
                    rel_ok, rel_detail = check_relevance(scenario, batch)
                    mr.relevance_ok = rel_ok
                    mr.relevance_detail = rel_detail

                mr.raw_preview = r["raw_content"][:200]
            else:
                mr.raw_preview = r["raw_content"][:200] or r["error"][:200]

            # Status icon
            icon = "✅" if mr.pydantic_ok and mr.relevance_ok else ("⚠️" if mr.pydantic_ok else "❌")
            print(
                f"{icon} JSON={mr.json_ok} Pydantic={mr.pydantic_ok} "
                f"Relevant={mr.relevance_ok} FR={mr.french_ok} "
                f"Q={mr.num_questions} {mr.latency_s:.1f}s {mr.tokens_out}tk"
            )

            results.append(mr)

            # Early abort: if model fails JSON on first 3 scenarios catastrophically
            if not mr.json_ok and scenario.name in (
                "calme",
                "lumiere_oubliee",
                "alerte_temperature",
            ):
                json_fails = sum(1 for r_ in results if r_.model == model and not r_.json_ok)
                if json_fails >= 3:
                    print(f"  ⚠️  Abandon: {json_fails}/3 JSON failures — modèle non viable")
                    # Mark remaining scenarios as skipped
                    for s in SCENARIOS:
                        if s.name not in {r_.scenario for r_ in results if r_.model == model}:
                            results.append(
                                ModelResult(
                                    model=model,
                                    scenario=s.name,
                                    json_ok=False,
                                    pydantic_ok=False,
                                    relevance_ok=False,
                                    relevance_detail="",
                                    french_ok=False,
                                    latency_s=0,
                                    tokens_out=0,
                                    num_questions=0,
                                    error="SKIPPED: modèle abandonné après 3 échecs JSON",
                                )
                            )
                    break

    # ── Save raw results ────────────────────────────────────────────────────
    raw_path = out_dir / "results_raw.json"
    raw_data = []
    for r in results:
        raw_data.append(
            {
                "model": r.model,
                "scenario": r.scenario,
                "json_ok": r.json_ok,
                "pydantic_ok": r.pydantic_ok,
                "relevance_ok": r.relevance_ok,
                "relevance_detail": r.relevance_detail,
                "french_ok": r.french_ok,
                "latency_s": r.latency_s,
                "tokens_out": r.tokens_out,
                "num_questions": r.num_questions,
                "error": r.error,
                "raw_preview": r.raw_preview,
            }
        )
    raw_path.write_text(json.dumps(raw_data, indent=2, ensure_ascii=False))
    print(f"\nRaw results → {raw_path}")

    # ── Compute summary ─────────────────────────────────────────────────────
    model_summaries = {}
    for r in results:
        ms = model_summaries.setdefault(
            r.model,
            {
                "total": 0,
                "json_ok": 0,
                "pydantic_ok": 0,
                "relevance_ok": 0,
                "french_ok": 0,
                "latencies": [],
                "tokens": [],
                "errors": [],
            },
        )
        ms["total"] += 1
        if r.json_ok:
            ms["json_ok"] += 1
        if r.pydantic_ok:
            ms["pydantic_ok"] += 1
        if r.relevance_ok:
            ms["relevance_ok"] += 1
        if r.french_ok:
            ms["french_ok"] += 1
        if r.latency_s > 0:
            ms["latencies"].append(r.latency_s)
        if r.tokens_out > 0:
            ms["tokens"].append(r.tokens_out)
        if r.error:
            ms["errors"].append(r.error)

    # ── Generate Markdown report ────────────────────────────────────────────
    lines = []
    lines.append("# CoreMind LLM Benchmark — Intention Layer (L5)")
    lines.append("")
    lines.append(
        f"**Date:** 2026-05-26 | **Scénarios:** {len(SCENARIOS)} | **Modèles:** {len(MODELS)}"
    )
    lines.append(
        f"**Schéma:** `QuestionBatch` (Pydantic v2, strict) | **Température:** 0.3 | **Format:** JSON forcé"
    )
    lines.append("")
    lines.append("## 📊 Résumé Global")
    lines.append("")
    lines.append(
        "| Modèle | JSON ✅ | Pydantic ✅ | Pertinence ✅ | FR ✅ | Latence moy | Tokens moy | Verdict |"
    )
    lines.append(
        "|--------|---------|-------------|---------------|-------|-------------|------------|---------|"
    )

    for model in MODELS:
        ms = model_summaries.get(model)
        if not ms or ms["total"] == 0:
            lines.append(f"| {model} | — | — | — | — | — | — | Non testé |")
            continue

        t = ms["total"]
        json_pct = ms["json_ok"] / t * 100
        pyd_pct = ms["pydantic_ok"] / t * 100
        rel_pct = ms["relevance_ok"] / t * 100
        fr_pct = ms["french_ok"] / t * 100
        avg_lat = sum(ms["latencies"]) / len(ms["latencies"]) if ms["latencies"] else 0
        avg_tok = sum(ms["tokens"]) / len(ms["tokens"]) if ms["tokens"] else 0

        # Verdict
        if pyd_pct == 0:
            verdict = "❌ ÉLIMINÉ (JSON invalide)"
        elif pyd_pct < 80:
            verdict = "⚠️ RISQUE (JSON instable)"
        elif rel_pct >= 80 and fr_pct >= 80:
            verdict = "✅ VIABLE"
        elif rel_pct >= 60:
            verdict = "⚠️ PARTIEL"
        else:
            verdict = "❌ NON VIABLE"

        lines.append(
            f"| {model} | {json_pct:.0f}% ({ms['json_ok']}/{t}) "
            f"| {pyd_pct:.0f}% ({ms['pydantic_ok']}/{t}) "
            f"| {rel_pct:.0f}% ({ms['relevance_ok']}/{t}) "
            f"| {fr_pct:.0f}% ({ms['french_ok']}/{t}) "
            f"| {avg_lat:.1f}s "
            f"| {avg_tok:.0f} "
            f"| {verdict} |"
        )

    lines.append("")
    lines.append("## 📋 Détails par Scénario")
    lines.append("")

    for scenario in SCENARIOS:
        lines.append(f"### {scenario.name} — {scenario.description}")
        lines.append("")
        lines.append(
            "| Modèle | JSON | Pydantic | Pertinent | FR | Latence | Tokens | Q | Erreur |"
        )
        lines.append(
            "|--------|------|----------|-----------|----|---------|--------|---|--------|"
        )

        for model in MODELS:
            matches = [r for r in results if r.model == model and r.scenario == scenario.name]
            if not matches:
                lines.append(f"| {model} | — | — | — | — | — | — | — | Non testé |")
                continue
            r = matches[0]
            err_short = (r.error[:60] + "...") if len(r.error) > 60 else (r.error or "—")
            lines.append(
                f"| {model} "
                f"| {'✅' if r.json_ok else '❌'} "
                f"| {'✅' if r.pydantic_ok else '❌'} "
                f"| {'✅' if r.relevance_ok else '❌'} ({r.relevance_detail[:30]}) "
                f"| {'✅' if r.french_ok else '❌'} "
                f"| {r.latency_s:.1f}s "
                f"| {r.tokens_out} "
                f"| {r.num_questions} "
                f"| {err_short} |"
            )

        lines.append("")

    # ── Recommendations ─────────────────────────────────────────────────────
    lines.append("## 🎯 Recommandations Finales")
    lines.append("")

    # Find the best model
    best_model = None
    best_score = -1
    for model in MODELS:
        ms = model_summaries.get(model)
        if not ms or ms["total"] == 0:
            continue
        score = ms["pydantic_ok"] * 3 + ms["relevance_ok"] * 2 + ms["french_ok"]
        if score > best_score:
            best_score = score
            best_model = model

    lines.append(f"### Pour `intention` (cycles fréquents, ~10 min)")
    lines.append("")
    if best_model and best_model != "deepseek-v4-flash:cloud":
        ms = model_summaries.get(best_model, {})
        avg_lat = sum(ms.get("latencies", [0])) / max(len(ms.get("latencies", [1])), 1)
        lines.append(f"**→ `{best_model}`** (local, gratuit, {avg_lat:.1f}s moy)")
    else:
        lines.append(f"**→ `deepseek-v4-flash:cloud`** (baseline, mais coûte des crédits cloud)")
        # Find best local alternative
        best_local = None
        best_local_score = -1
        for model in MODELS:
            if ":cloud" in model:
                continue
            ms = model_summaries.get(model)
            if not ms or ms["total"] == 0:
                continue
            score = ms["pydantic_ok"] * 3 + ms["relevance_ok"] * 2 + ms["french_ok"]
            if score > best_local_score:
                best_local_score = score
                best_local = model
        if best_local:
            ms = model_summaries.get(best_local, {})
            avg_lat = sum(ms.get("latencies", [0])) / max(len(ms.get("latencies", [1])), 1)
            lines.append(f"Alternative locale: `{best_local}` ({avg_lat:.1f}s moy)")

    lines.append("")
    lines.append(f"### Pour `reasoning` (cycles moins fréquents, ~1h)")
    lines.append("")
    deepseek_ok = model_summaries.get("deepseek-v4-flash:cloud", {}).get("pydantic_ok", 0) > 0
    if deepseek_ok:
        lines.append(
            "**→ `deepseek-v4-flash:cloud`** — qualité garantie, coût acceptable pour cycles rares"
        )
    lines.append("Alternative locale si budget serré: le meilleur modèle local ci-dessus.")

    lines.append("")
    lines.append("### Modèles à éviter")
    lines.append("")
    avoided = []
    for model in MODELS:
        ms = model_summaries.get(model)
        if not ms or ms["total"] == 0:
            avoided.append(f"- `{model}` — non testé (limite crédits cloud)")
            continue
        if ms["pydantic_ok"] == 0:
            avoided.append(f"- `{model}` — JSON invalide sur tous les scénarios")
        elif ms["pydantic_ok"] / ms["total"] < 0.6:
            avoided.append(f"- `{model}` — JSON instable ({ms['pydantic_ok']}/{ms['total']})")
    if not avoided:
        lines.append("Aucun modèle à éviter — tous ont passé le seuil minimum.")
    else:
        for a in avoided:
            lines.append(a)

    lines.append("")
    lines.append("### qwen3.6:35b vs 27b")
    lines.append("")
    score27 = (
        sum(model_summaries.get("qwen3.6:27b", {}).get("pydantic_ok", 0) for _ in [1])
        if "qwen3.6:27b" in model_summaries
        else 0
    )
    score35 = (
        sum(model_summaries.get("qwen3.6:35b", {}).get("pydantic_ok", 0) for _ in [1])
        if "qwen3.6:35b" in model_summaries
        else 0
    )
    if score35 > score27:
        lines.append(
            "Le 35b surpasse le 27b — le surcoût VRAM est justifié si la qualité est significativement meilleure."
        )
    elif score35 == score27:
        lat27 = sum(model_summaries.get("qwen3.6:27b", {}).get("latencies", [0])) / max(
            len(model_summaries.get("qwen3.6:27b", {}).get("latencies", [1])), 1
        )
        lat35 = sum(model_summaries.get("qwen3.6:35b", {}).get("latencies", [0])) / max(
            len(model_summaries.get("qwen3.6:35b", {}).get("latencies", [1])), 1
        )
        if lat27 < lat35:
            lines.append(
                f"Qualité équivalente, mais 27b est plus rapide ({lat27:.1f}s vs {lat35:.1f}s). Utilise le 27b."
            )
        else:
            lines.append(
                f"Qualité équivalente. Les deux sont viables — le 27b suffit pour économiser la VRAM."
            )
    else:
        lines.append(
            "Le 27b surpasse le 35b — utilise le 27b (moins cher en VRAM, meilleure qualité)."
        )

    # ── Write report ────────────────────────────────────────────────────────
    report_path = out_dir / "RESULTS.md"
    report_path.write_text("\n".join(lines) + "\n")
    print(f"\nReport → {report_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
