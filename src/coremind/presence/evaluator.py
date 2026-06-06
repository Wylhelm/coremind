"""Context-aware activity evaluator — Phase 2 of the presence detector.

Replaces the simple timer with LLM-powered contextual reasoning that considers
time of day, day of week, calendar, sleep, and activity patterns before deciding
whether to notify.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, Field

from coremind.errors import LLMError
from coremind.reasoning.llm import LLM, Layer

if TYPE_CHECKING:
    from coremind.world.store import WorldStore

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Response model — what the LLM must return
# ---------------------------------------------------------------------------


class ActivityEvaluation(BaseModel):
    """Structured evaluation of the current activity pattern."""

    salience: float = Field(
        ge=0.0,
        le=1.0,
        description="How urgent/important this notification is (0=ignore, 1=critical)",
    )
    should_notify: bool = Field(
        description="Whether a wellbeing notification should be sent right now"
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="How confident the LLM is in this evaluation",
    )
    reason: str = Field(
        description="Brief explanation of the decision (max 80 chars, for logging)"
    )
    suggested_message: str = Field(
        default="",
        description="Natural, warm message to send if should_notify is true. "
        "Use French if the user is Guillaume. Keep it casual and caring.",
    )


# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """Tu es un évaluateur de bien-être contextuel pour Guillaume, un développeur
de 47 ans qui vit seul avec ses 3 chats (Poukie, Timimi, Minuit) à Québec.
Tu analyses les patterns d'activité détectés par sa caméra de salon.

## Ta mission
Déterminer si une notification de "pause bien-être" est pertinente MAINTENANT,
en tenant compte du contexte complet — pas juste du temps écoulé.

## Règles de décision

### 🟢 Ne PAS notifier (should_notify: false)
- Matin de semaine (8h-12h): c'est normal d'être à son bureau, même plusieurs heures
- Après-midi de semaine (13h-17h): normal aussi, surtout si pas de pause déjeuner manquée
- Soirée (19h-22h): normal de regarder la TV ou se détendre sur le canapé
- Weekend: les longues périodes sur le canapé ou à diverses activités sont normales
- L'activité varie (alterne entre bureau/cuisine/canapé): signe de pauses naturelles
- La personne n'est pas dans la pièce: rien à signaler

### 🟡 Notifier avec salience basse (0.3-0.5)
- >4h continues au bureau un jour de semaine SANS variation d'activité
- >2h sur le canapé un jour de semaine pendant les heures de bureau (malade?)
- Tard le soir (23h+) au bureau un jour de semaine
- L'activité est "unknown" depuis longtemps (données potentiellement périmées)

### 🔴 Notifier avec salience élevée (0.6-1.0)
- >6h continues au bureau sans AUCUNE pause (pas de cuisine, pas de canapé)
- Nuit (1h-6h) avec activité "working at desk" — anormal, possible insomnie ou erreur caméra
- Pattern inhabituel vs les jours précédents (ex: normalement absent le mardi mais présent)
- Après une nuit de <4h de sommeil: >3h de travail continu = risque de fatigue

## Format de réponse
Tu DOIS retourner un objet JSON valide avec exactement ces champs:
{
  "salience": 0.0,
  "should_notify": false,
  "confidence": 0.0,
  "reason": "explication courte en français",
  "suggested_message": ""
}

Le message suggéré doit être chaleureux, en français, et adapté au contexte.
Pas de formule générique.
Exemples:
- "Hey Guillaume, 6h sans bouger de ton bureau... Et si tu allais prendre l'air 10 minutes? 🌿"
- "Minuit et demi et tu codes encore? Demain matin va être dur! 😅"
- "Je vois que tu alternes bien entre bureau et cuisine aujourd'hui — tout roule! Pas d'alerte."
"""

USER_PROMPT_TEMPLATE = """## Contexte actuel

**Jour/heure:** {day_of_week} {time_of_day}
**Activité détectée:** {current_activity}
**Durée continue:** {elapsed_hours}h{elapsed_minutes:02d}
**Personne détectée:** {person_name}

## Historique d'activité récent (dernières {history_hours}h)
{activity_history}

## Sommeil
{sleep_context}

## Calendrier
{calendar_context}

## Patterns hebdomadaires
{weekly_patterns}

Évalue la situation et décide si une notification est pertinente."""

# ---------------------------------------------------------------------------
# Evaluation helpers
# ---------------------------------------------------------------------------


def _format_timedelta(minutes: float) -> str:
    """Format minutes as a human-readable string."""
    if minutes < 60:
        return f"{int(minutes)}min"
    hours = int(minutes / 60)
    mins = int(minutes % 60)
    if mins == 0:
        return f"{hours}h"
    return f"{hours}h{mins:02d}"


def _day_name(dt: datetime) -> str:
    """French day name."""
    days = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
    return days[dt.weekday()]


def _is_weekend(dt: datetime) -> bool:
    """True if Saturday or Sunday."""
    return dt.weekday() >= 5


def _time_of_day(dt: datetime) -> str:
    """Human-readable time of day in French."""
    hour = dt.hour
    if 5 <= hour < 12:
        return "matin"
    if 12 <= hour < 14:
        return "midi"
    if 14 <= hour < 18:
        return "après-midi"
    if 18 <= hour < 22:
        return "soirée"
    return "nuit"


# ---------------------------------------------------------------------------
# Activity evaluator
# ---------------------------------------------------------------------------


class ActivityEvaluator:
    """LLM-powered contextual activity evaluator.

    Gathers multi-dimensional context and asks an LLM to decide whether a
    wellbeing notification is warranted, rather than relying on a simple timer.
    """

    def __init__(
        self,
        llm: LLM,
        world_store: WorldStore,
        *,
        layer: Layer = "reasoning_fast",
        history_window_hours: int = 6,
        min_elapsed_for_eval_minutes: int = 60,
    ) -> None:
        self._llm = llm
        self._world = world_store
        self._layer = layer
        self._history_window = history_window_hours
        self._min_elapsed = min_elapsed_for_eval_minutes

    async def evaluate(
        self,
        *,
        person_name: str = "unknown",
        current_activity: str = "unknown",
        elapsed_minutes: float = 0.0,
        activity_history: list[dict[str, object]] | None = None,
        sleep_hours: float | None = None,
        calendar_events: list[str] | None = None,
    ) -> ActivityEvaluation:
        """Evaluate the current activity pattern and return a structured decision.

        Args:
            person_name: Who is detected.
            current_activity: Current activity label.
            elapsed_minutes: How long the person has been continuously present.
            activity_history: List of {timestamp, activity} dicts from recent checks.
            sleep_hours: Last night's sleep in hours, if available.
            calendar_events: Upcoming calendar event summaries, if available.

        Returns:
            An :class:`ActivityEvaluation` with the LLM's decision.
        """
        now = datetime.now(UTC)
        now_local = now.astimezone()  # Uses system timezone (America/Toronto)

        # Build activity history text
        if activity_history:
            history_lines = []
            for entry in activity_history[-24:]:  # Last 24 entries (~2h at 5min intervals)
                ts = str(entry.get("timestamp", ""))[-8:] if entry.get("timestamp") else "?"
                act = entry.get("activity", "?")
                history_lines.append(f"  {ts} — {act}")
            history_text = "\n".join(history_lines) if history_lines else "aucune donnée"
        else:
            history_text = "aucune donnée historique disponible"

        # Build sleep context
        if sleep_hours is not None:
            if sleep_hours < 4:
                sleep_text = (
                f"A dormi seulement {sleep_hours:.1f}h la nuit dernière "
                f"— fatigue probable ⚠️"
            )
            elif sleep_hours < 6:
                sleep_text = f"A dormi {sleep_hours:.1f}h la nuit dernière — un peu court"
            else:
                sleep_text = f"A dormi {sleep_hours:.1f}h la nuit dernière — repos correct ✅"
        else:
            sleep_text = "Données de sommeil non disponibles"

        # Build calendar context
        if calendar_events:
            cal_text = "Événements aujourd'hui:\n" + "\n".join(f"  • {e}" for e in calendar_events)
        else:
            cal_text = "Aucun événement aujourd'hui ou données non disponibles"

        # Build weekly patterns
        if _is_weekend(now_local):
            patterns = (
                "Weekend — les longues périodes de détente sont normales. "
                "Guillaume travaille rarement le weekend."
            )
        else:
            patterns = (
                "Jour de semaine — Guillaume travaille normalement ~8h/jour, "
                "avec une pause déjeuner vers 12h-13h. Les soirées sont généralement "
                "détente (TV, canapé). Il se couche typiquement entre 23h et minuit."
            )

        # Build user prompt
        user_prompt = USER_PROMPT_TEMPLATE.format(
            day_of_week=_day_name(now_local),
            time_of_day=_time_of_day(now_local),
            current_activity=current_activity,
            elapsed_hours=int(elapsed_minutes / 60),
            elapsed_minutes=int(elapsed_minutes % 60),
            person_name=person_name,
            history_hours=self._history_window,
            activity_history=history_text,
            sleep_context=sleep_text,
            calendar_context=cal_text,
            weekly_patterns=patterns,
        )

        try:
            result = await self._llm.complete_structured(
                layer=self._layer,
                system=SYSTEM_PROMPT,
                user=user_prompt,
                response_model=ActivityEvaluation,
                max_tokens=300,
            )
            log.info(
                "activity_evaluator.result",
                salience=result.salience,
                should_notify=result.should_notify,
                confidence=result.confidence,
                reason=result.reason,
                elapsed_minutes=elapsed_minutes,
                activity=current_activity,
            )
            return result
        except LLMError as exc:
            log.warning("activity_evaluator.llm_error", error=str(exc))
            # Fallback: use simple heuristic when LLM is unavailable
            return self._fallback_evaluation(elapsed_minutes, current_activity)

    def _fallback_evaluation(
        self, elapsed_minutes: float, activity: str
    ) -> ActivityEvaluation:
        """Simple heuristic fallback when the LLM is unavailable.

        Conservative: only notify for very long periods at desk, never at night.
        """
        now = datetime.now(UTC).astimezone()

        # Never notify at night (23h-7h) — likely a camera error
        if now.hour < 7 or now.hour >= 23:
            return ActivityEvaluation(
                salience=0.0,
                should_notify=False,
                confidence=0.3,
                reason="Nuit — probablement une erreur de caméra, on ignore",
            )

        # Only notify for desk work, not relaxation
        desk_keywords = ["desk", "bureau", "computer", "ordinateur", "working", "travail"]
        is_desk = any(kw in str(activity).lower() for kw in desk_keywords)

        if is_desk and elapsed_minutes >= 180:  # 3h minimum
            h = int(elapsed_minutes / 60)
            m = int(elapsed_minutes % 60)
            return ActivityEvaluation(
                salience=0.55,
                should_notify=True,
                confidence=0.4,
                reason=f"Fallback heuristique: {h}h{m:02d} au bureau, LLM indisponible",
                suggested_message=(
                    f"Hey! Ça fait {h}h{m:02d} que tu es à ton bureau. "
                    f"Une petite pause? ☕ (Mode dégradé — LLM indisponible)"
                ),
            )

        return ActivityEvaluation(
            salience=0.0,
            should_notify=False,
            confidence=0.3,
            reason="Fallback: pas de critère d'alerte atteint",
        )
