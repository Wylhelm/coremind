"""In-process effector registry — maps operation names to :class:`EffectorPort`.

Design principles
-----------------
1. **Never fail on parameter aliases** — the LLM may use ``entity_id``,
   ``entity_ids``, ``entities``, ``device``, etc.  Accept them all.
2. **Default, don't reject** — missing parameters produce useful defaults,
   not errors.  An effector only reports ``permanent_failure`` when the
   downstream system is genuinely unreachable in a non-retryable way.
3. **Partial results are success** — if one entity fails and another
   succeeds, return the successes and note the failures.
4. **Actionable error messages** — when something does go wrong, the
   message tells the intention loop *exactly* what to fix next time.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from coremind.action.executor import EffectorPort
from coremind.action.schemas import Action, ActionResult
from coremind.notify.port import NotificationPort

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Effector registry
# ---------------------------------------------------------------------------


class EffectorRegistry:
    """Maps operation names → :class:`EffectorPort` instances.

    Implements the :class:`~coremind.action.executor.EffectorResolver`
    callable protocol — pass directly to :class:`Executor`.
    """

    def __init__(self) -> None:
        self._effectors: dict[str, EffectorPort] = {}

    def register(self, operation: str, effector: EffectorPort) -> None:
        if operation in self._effectors:
            raise ValueError(f"Effector already registered for {operation!r}")
        self._effectors[operation] = effector
        log.debug("effector_registry.registered", operation=operation)

    def register_many(self, operations: list[str], effector: EffectorPort) -> None:
        for op in operations:
            self.register(op, effector)

    def __call__(self, operation: str, /) -> EffectorPort | None:
        return self._effectors.get(operation)


# ===================================================================
# Notification effector
# ===================================================================


class NotificationEffector:
    """Delivers notifications through the daemon's :class:`NotificationRouter`.

    Operation: ``coremind.plugin.notification.send``

    Parameter aliases accepted:
        message / body / text / content
        title / subject / heading
        priority / urgency
    """

    def __init__(self, notify_port: NotificationPort) -> None:
        self._notify = notify_port

    async def invoke(self, action: Action) -> ActionResult:
        params = dict(action.parameters)

        # Accept any message-like param
        message = (
            params.get("message")
            or params.get("body")
            or params.get("text")
            or params.get("content")
            or ""
        )
        title = (
            params.get("title")
            or params.get("subject")
            or params.get("heading")
            or ""
        )

        if not message and not title:
            # Degenerate: intent has nothing to say.  Succeed silently so
            # the intention loop can observe the no-op outcome.
            return ActionResult(
                action_id=action.id,
                status="noop",
                message="no message or title provided — nothing to send",
                completed_at=datetime.now(UTC),
            )

        full_message = f"{title}\n\n{message}" if title and message else (title or message)

        try:
            cat = action.category
            notify_cat = "info" if cat == "safe" else cat
            receipt = await self._notify.notify(
                message=full_message,
                category=notify_cat,  # type: ignore[arg-type]
                actions=None,
                intent_id=action.intent_id,
                action_class=action.action_class,
            )
            return ActionResult(
                action_id=action.id,
                status="ok",
                message=f"delivered to {receipt.port_id} as {receipt.channel_message_id}",
                output={
                    "port_id": receipt.port_id,
                    "channel_message_id": receipt.channel_message_id,
                },
                completed_at=datetime.now(UTC),
            )
        except Exception as exc:
            return ActionResult(
                action_id=action.id,
                status="transient_failure",
                message=f"notification delivery failed: {exc}",
                completed_at=datetime.now(UTC),
            )


# ===================================================================
# Home Assistant effector
# ===================================================================


class HomeAssistantEffector:
    """Calls the Home Assistant REST API for state queries and service calls.

    Operates through ``ha-mcp`` (mcporter).  Never fails on missing or
    mis-named parameters — it normalises everything the LLM might produce.
    """

    HA_TOKEN_PATH = Path.home() / ".openclaw" / "secrets" / "ha-token"

    # ---- parameter normalisation -------------------------------------------

    @staticmethod
    def _text(
        params: dict[str, Any],
        *keys: str,
        default: str = "",
    ) -> str:
        """Return the first non-empty value for any of *keys*."""
        for k in keys:
            v = params.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return default

    @staticmethod
    def _int(params: dict[str, Any], *keys: str, default: int = 0) -> int:
        for k in keys:
            v = params.get(k)
            if v is not None:
                try:
                    return int(v)
                except (TypeError, ValueError):
                    pass
        return default

    @staticmethod
    def _entity_ids(params: dict[str, Any]) -> list[str]:
        """Extract entity IDs from every conceivable parameter shape.

        Accepts: entity_id (str), entity_ids (list[str]), entities (list),
        entity (str), device (str), device_id (str), target (str/list).
        Returns a (possibly empty) list of normalised HA entity_id strings.
        """
        for key in ("entity_ids", "entities", "devices", "targets"):
            raw = params.get(key)
            if isinstance(raw, list):
                return [str(e) for e in raw if str(e).strip()]
        for key in ("entity_id", "entity", "device", "device_id", "target"):
            raw = params.get(key)
            if isinstance(raw, str) and raw.strip():
                return [raw.strip()]
        return []

    @staticmethod
    def _need_entity(action: Action, params: dict[str, Any]) -> ActionResult | None:
        """Return an ActionResult if entity info is missing for a
        write operation, otherwise ``None`` (caller proceeds).

        Write operations (turn_on/off, set_temperature, …) genuinely need
        a target.  Instead of a permanent failure we return a *noop* with a
        clear description, so the intention loop can self-correct.
        """
        if not HomeAssistantEffector._entity_ids(params):
            return ActionResult(
                action_id=action.id,
                status="noop",
                message=(
                    "no entity_id/device specified — need a target entity "
                    "like 'light.kitchen' or 'sensor.bedroom_temp'. "
                    f"Received params: {sorted(params.keys())}"
                ),
                completed_at=datetime.now(UTC),
            )
        return None

    # ---- entry point -------------------------------------------------------

    async def invoke(self, action: Action) -> ActionResult:
        op = action.operation
        params = dict(action.parameters)
        try:
            if op == "coremind.plugin.homeassistant.get_state":
                return await self._get_state(action, params)
            if op == "coremind.plugin.homeassistant.get_history":
                return await self._get_history(action, params)
            if op == "coremind.plugin.homeassistant.get_printer_estimated_pages":
                return await self._get_state(action, params)
            if op in (
                "coremind.plugin.homeassistant.turn_on",
                "coremind.plugin.homeassistant.turn_off",
                "coremind.plugin.homeassistant.light.turn_off",
            ):
                svc = "turn_on" if "turn_on" in op else "turn_off"
                return await self._call_service(action, params, svc)
            if op == "coremind.plugin.homeassistant.set_temperature":
                return await self._set_temperature(action, params)
            if op == "coremind.plugin.homeassistant.create_automation":
                return await self._create_automation(action, params)
            if op == "coremind.plugin.homeassistant.send_notification":
                return await self._send_ha_notification(action, params)
            return _noop(action, f"unsupported HA operation: {op}")
        except Exception as exc:
            return ActionResult(
                action_id=action.id,
                status="transient_failure",
                message=f"HA effector error: {exc}",
                completed_at=datetime.now(UTC),
            )

    # ---- query operations --------------------------------------------------

    async def _get_state(self, action: Action, params: dict[str, Any]) -> ActionResult:
        entity_ids = self._entity_ids(params)
        if not entity_ids:
            return ActionResult(
                action_id=action.id,
                status="ok",
                message="no entity specified — returning empty state",
                output={"entities": {}},
                completed_at=datetime.now(UTC),
            )

        results: dict[str, Any] = {}
        errors: list[str] = []
        for eid in entity_ids:
            try:
                results[eid] = _ha_mcp("ha_search_entities", f"query={eid}")
            except Exception as exc:
                errors.append(f"{eid}: {exc}")

        return ActionResult(
            action_id=action.id,
            status="ok",
            message=f"state retrieved for {len(results)}/{len(entity_ids)} entities"
            + (f"; errors: {errors}" if errors else ""),
            output={"entities": results, "errors": errors} if errors else {"entities": results},
            completed_at=datetime.now(UTC),
        )

    async def _get_history(self, action: Action, params: dict[str, Any]) -> ActionResult:
        entity_ids = self._entity_ids(params)
        if not entity_ids:
            return ActionResult(
                action_id=action.id,
                status="ok",
                message="no entity specified — returning empty history",
                output={"history": {}},
                completed_at=datetime.now(UTC),
            )

        results: dict[str, Any] = {}
        errors: list[str] = []
        for eid in entity_ids:
            try:
                results[eid] = _ha_mcp("ha_get_history", f"entity_id={eid}")
            except Exception as exc:
                errors.append(f"{eid}: {exc}")

        return ActionResult(
            action_id=action.id,
            status="ok",
            message=f"history retrieved for {len(results)}/{len(entity_ids)} entities"
            + (f"; errors: {errors}" if errors else ""),
            output={"history": results, "errors": errors} if errors else {"history": results},
            completed_at=datetime.now(UTC),
        )

    # ---- write operations --------------------------------------------------

    async def _call_service(
        self, action: Action, params: dict[str, Any], service: str
    ) -> ActionResult:
        no_entity = self._need_entity(action, params)
        if no_entity:
            return no_entity

        entity_id = self._entity_ids(params)[0]
        domain = entity_id.split(".", 1)[0]

        try:
            result = _ha_mcp(
                "ha_call_service",
                f"domain={domain}",
                f"service={service}",
                f"entity_id={entity_id}",
            )
            return ActionResult(
                action_id=action.id,
                status="ok",
                message=f"{domain}.{service} executed on {entity_id}",
                output=result if isinstance(result, dict) else {"raw": result},
                completed_at=datetime.now(UTC),
                reversed_by_operation=(
                    "coremind.plugin.homeassistant.turn_on"
                    if service == "turn_off"
                    else "coremind.plugin.homeassistant.turn_off"
                ),
            )
        except Exception as exc:
            return ActionResult(
                action_id=action.id,
                status="transient_failure",
                message=f"HA {domain}.{service} on {entity_id} failed: {exc}",
                completed_at=datetime.now(UTC),
            )

    async def _set_temperature(self, action: Action, params: dict[str, Any]) -> ActionResult:
        entity_ids = self._entity_ids(params)
        temperature = self._text(params, "temperature", "target_temp", "value")
        if not entity_ids:
            return ActionResult(
                action_id=action.id,
                status="noop",
                message="no entity_id specified for set_temperature",
                completed_at=datetime.now(UTC),
            )
        if not temperature:
            return ActionResult(
                action_id=action.id,
                status="noop",
                message="no temperature value specified",
                completed_at=datetime.now(UTC),
            )

        eid = entity_ids[0]
        try:
            result = _ha_mcp(
                "ha_call_service",
                "domain=climate",
                "service=set_temperature",
                f"entity_id={eid}",
                f"temperature={temperature}",
            )
            return ActionResult(
                action_id=action.id,
                status="ok",
                message=f"climate.set_temperature on {eid} to {temperature}",
                output=result if isinstance(result, dict) else {"raw": result},
                completed_at=datetime.now(UTC),
            )
        except Exception as exc:
            return ActionResult(
                action_id=action.id,
                status="transient_failure",
                message=f"set_temperature failed: {exc}",
                completed_at=datetime.now(UTC),
            )

    async def _send_ha_notification(self, action: Action, params: dict[str, Any]) -> ActionResult:
        message = self._text(params, "message", "body", "text")
        title = self._text(params, "title", "subject", default="CoreMind")

        if not message:
            return ActionResult(
                action_id=action.id,
                status="noop",
                message="no message content — nothing to send",
                completed_at=datetime.now(UTC),
            )

        try:
            data = json.dumps({"message": message, "title": title})
            _ha_mcp(
                "ha_call_service",
                "domain=notify",
                "service=persistent_notification",
                f"data={data}",
            )
            return ActionResult(
                action_id=action.id,
                status="ok",
                message=f"HA notification sent: {title}",
                completed_at=datetime.now(UTC),
            )
        except Exception as exc:
            return ActionResult(
                action_id=action.id,
                status="transient_failure",
                message=f"HA notification failed: {exc}",
                completed_at=datetime.now(UTC),
            )

    async def _create_automation(self, action: Action, params: dict[str, Any]) -> ActionResult:
        name = self._text(params, "name", "alias", "title", default="CoreMind Automation")
        trigger = params.get("trigger")
        ha_action = params.get("action")

        if not trigger or not ha_action:
            return ActionResult(
                action_id=action.id,
                status="noop",
                message=(
                    "automation requires 'trigger' and 'action' fields. "
                    f"Received: {sorted(params.keys())}"
                ),
                completed_at=datetime.now(UTC),
            )

        try:
            payload = json.dumps({
                "alias": name,
                "trigger": [trigger],
                "action": [ha_action],
            })
            result = _ha_mcp("ha_config_set_automation", f"data={payload}")
            return ActionResult(
                action_id=action.id,
                status="ok",
                message=f"automation '{name}' created",
                output=result if isinstance(result, dict) else {"raw": result},
                completed_at=datetime.now(UTC),
                reversal="Delete the automation via Home Assistant UI or API.",
            )
        except Exception as exc:
            return ActionResult(
                action_id=action.id,
                status="transient_failure",
                message=f"automation creation failed: {exc}",
                completed_at=datetime.now(UTC),
            )


# ===================================================================
# Vikunja effector
# ===================================================================


class VikunjaEffector:
    """Queries Vikunja task manager (local API on port 3456).

    Operations: ``list_tasks``, ``get_tasks``

    Parameter aliases: project / project_id / list, filter / status / kind
    """

    VIKUNJA_TOKEN_PATH = Path.home() / ".openclaw" / "secrets" / "vikunja-token"
    VIKUNJA_URL = "http://localhost:3456/api/v1/tasks"

    async def invoke(self, action: Action) -> ActionResult:
        import aiohttp

        params = dict(action.parameters)

        # Normalise parameter names
        project = (
            params.get("project")
            or params.get("project_id")
            or params.get("list")
            or ""
        )
        filter_type = (
            params.get("filter")
            or params.get("status")
            or params.get("kind")
            or "all"
        )

        token = self._read_token()
        if not token:
            return ActionResult(
                action_id=action.id,
                status="transient_failure",
                message="Vikunja token not available",
                completed_at=datetime.now(UTC),
            )

        try:
            async with aiohttp.ClientSession() as session:
                headers = {"Authorization": f"Bearer {token}"}
                async with session.get(
                    self.VIKUNJA_URL,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status >= 400:
                        body = await resp.text()
                        return ActionResult(
                            action_id=action.id,
                            status="transient_failure",
                            message=f"Vikunja HTTP {resp.status}: {body[:200]}",
                            completed_at=datetime.now(UTC),
                        )
                    tasks = await resp.json()
        except Exception as exc:
            return ActionResult(
                action_id=action.id,
                status="transient_failure",
                message=f"Vikunja unreachable: {exc}",
                completed_at=datetime.now(UTC),
            )

        if not isinstance(tasks, list):
            return ActionResult(
                action_id=action.id,
                status="transient_failure",
                message=f"unexpected Vikunja response type: {type(tasks).__name__}",
                completed_at=datetime.now(UTC),
            )

        # Apply filters
        if filter_type == "overdue":
            now = datetime.now(UTC)
            tasks = [
                t for t in tasks
                if t.get("due_date")
                and t.get("due_date") != "0001-01-01T00:00:00Z"
                and t["due_date"] < now.isoformat()
                and not t.get("done")
            ]
        elif project:
            match = project.lower()
            tasks = [
                t for t in tasks
                if match in str(t.get("title", "")).lower()
                or match in str(t.get("project_id", "")).lower()
            ]

        task_list = [
            {
                "id": t.get("id"),
                "title": t.get("title"),
                "done": t.get("done"),
                "due_date": t.get("due_date"),
                "priority": t.get("priority"),
            }
            for t in tasks[:20]
        ]

        return ActionResult(
            action_id=action.id,
            status="ok",
            message=(
                f"retrieved {len(task_list)} tasks "
                f"(filter={filter_type}, project={project or 'any'})"
            ),
            output={"tasks": task_list, "total": len(tasks)},
            completed_at=datetime.now(UTC),
        )

    def _read_token(self) -> str:
        try:
            return self.VIKUNJA_TOKEN_PATH.read_text().strip()
        except OSError:
            return ""


# ===================================================================
# Gmail effector (via gog CLI)
# ===================================================================


class GmailEffector:
    """Queries Gmail via the ``gog`` CLI.

    Operations: ``fetch_unread``, ``search_emails``

    Parameter aliases: max_results / limit / count, query / q / search
    """

    async def invoke(self, action: Action) -> ActionResult:
        params = dict(action.parameters)

        query = params.get("query") or params.get("q") or params.get("search") or "is:unread"
        max_results = max(
            1,
            min(20, (
                params.get("max_results")
                or params.get("limit")
                or params.get("count")
                or 5
            )),
        )

        cmd = ["gog", "gmail", "search", "--json", f"--max={max_results}", query]

        try:
            cp = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except FileNotFoundError:
            return ActionResult(
                action_id=action.id,
                status="transient_failure",
                message="gog CLI not installed — email queries unavailable",
                completed_at=datetime.now(UTC),
            )
        except subprocess.TimeoutExpired:
            return ActionResult(
                action_id=action.id,
                status="transient_failure",
                message="email query timed out",
                completed_at=datetime.now(UTC),
            )
        except Exception as exc:
            return ActionResult(
                action_id=action.id,
                status="transient_failure",
                message=f"email query failed: {exc}",
                completed_at=datetime.now(UTC),
            )

        if cp.returncode != 0:
            return ActionResult(
                action_id=action.id,
                status="transient_failure",
                message=f"gog exited {cp.returncode}: {cp.stderr[:200]}",
                completed_at=datetime.now(UTC),
            )

        try:
            data = json.loads(cp.stdout)
            threads = _normalize_gog_threads(data.get("threads", []))
        except json.JSONDecodeError as exc:
            return ActionResult(
                action_id=action.id,
                status="transient_failure",
                message=f"failed to parse gog JSON: {exc}",
                completed_at=datetime.now(UTC),
            )

        return ActionResult(
            action_id=action.id,
            status="ok",
            message=f"retrieved {len(threads)} email threads",
            output={"threads": threads, "total": len(threads)},
            completed_at=datetime.now(UTC),
        )


# ===================================================================
# Calendar effector
# ===================================================================


class CalendarEffector:
    """Queries Google Calendar via the ``gog`` CLI.

    Operations: ``fetch_upcoming_events``, ``get_next_payday``

    Parameter aliases: max_results / limit / count, date_range / range / window
    """

    async def invoke(self, action: Action) -> ActionResult:
        params = dict(action.parameters)

        max_results = max(
            1,
            min(50, (
                params.get("max_results")
                or params.get("limit")
                or params.get("count")
                or 5
            )),
        )

        days = params.get("days")
        cmd = ["gog", "calendar", "events", "--json", f"--max={max_results}"]
        if days:
            cmd.append(f"--days={days}")
        else:
            cmd.append("--days=7")

        try:
            cp = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except FileNotFoundError:
            return ActionResult(
                action_id=action.id,
                status="transient_failure",
                message="gog CLI not installed — calendar queries unavailable",
                completed_at=datetime.now(UTC),
            )
        except subprocess.TimeoutExpired:
            return ActionResult(
                action_id=action.id,
                status="transient_failure",
                message="calendar query timed out",
                completed_at=datetime.now(UTC),
            )
        except Exception as exc:
            return ActionResult(
                action_id=action.id,
                status="transient_failure",
                message=f"calendar query failed: {exc}",
                completed_at=datetime.now(UTC),
            )

        if cp.returncode != 0:
            return ActionResult(
                action_id=action.id,
                status="transient_failure",
                message=f"gog exited {cp.returncode}: {cp.stderr[:200]}",
                completed_at=datetime.now(UTC),
            )

        try:
            data = json.loads(cp.stdout)
            events = _normalize_gog_events(data.get("events", []))
        except json.JSONDecodeError as exc:
            return ActionResult(
                action_id=action.id,
                status="transient_failure",
                message=f"failed to parse calendar JSON: {exc}",
                completed_at=datetime.now(UTC),
            )

        return ActionResult(
            action_id=action.id,
            status="ok",
            message=f"retrieved {len(events)} upcoming events",
            output={"events": events},
            completed_at=datetime.now(UTC),
        )


def _normalize_gog_threads(raw_threads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize gog JSON email threads into the standard effector output format."""
    threads: list[dict[str, Any]] = []
    for t in raw_threads:
        threads.append({
            "id": t.get("id", ""),
            "subject": t.get("subject", "(no subject)"),
            "from": t.get("from", ""),
            "date": t.get("date", ""),
            "labels": t.get("labels", []),
            "messageCount": t.get("messageCount", 1),
        })
    return threads


def _normalize_gog_events(raw_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize gog JSON events into the standard effector output format."""
    events: list[dict[str, Any]] = []
    for ev in raw_events:
        events.append({
            "title": ev.get("summary", ev.get("subject", "(untitled)")),
            "start": ev.get("start", {}).get("dateTime", ev.get("start", {}).get("date", "")),
            "end": ev.get("end", {}).get("dateTime", ev.get("end", {}).get("date", "")),
            "status": ev.get("status", ""),
            "id": ev.get("id", ""),
        })
    return events


def _parse_gog_table(stdout: str) -> list[dict[str, Any]]:
    """Legacy parser for pre-JSON gog output. Kept for reference."""
    events: list[dict[str, Any]] = []
    for line in stdout.strip().split("\n"):
        line = line.strip()
        if not line or any(line.startswith(c) for c in ("─", "┌", "└", "├", "┐", "┘")):
            continue
        if "│" in line:
            parts = [p.strip() for p in line.split("│")][1:-1]
            if len(parts) >= 3:
                events.append({
                    "title": parts[0],
                    "start": parts[1],
                    "end": parts[2],
                })
    return events


# ===================================================================
# Helpers
# ===================================================================


def _ha_mcp(tool: str, *args: str) -> Any:
    """Call ha-mcp via mcporter and return parsed JSON, or raise."""
    cmd = ["mcporter", "call", f"ha-mcp.{tool}", *args]
    cp = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if cp.returncode != 0:
        raise RuntimeError(cp.stderr[:200] if cp.stderr else f"exit {cp.returncode}")
    return json.loads(cp.stdout)


def _noop(action: Action, message: str) -> ActionResult:
    """Return a no-op result — not a failure, just nothing to do."""
    return ActionResult(
        action_id=action.id,
        status="noop",
        message=message,
        completed_at=datetime.now(UTC),
    )
