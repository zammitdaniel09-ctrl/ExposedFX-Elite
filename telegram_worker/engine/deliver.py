"""Engine executor: send a planned post, verify it and record the mapping.

Keeps every guarantee of the legacy copy pipeline it replaces:

- one attempt per new post when Telegram refuses a destination for good (the
  FASTVIP permanent-failure ledger), and the route stop policy;
- flood waits are slept (capped) and transient network errors retried;
- a media post that cannot be re-sent by reference is downloaded and
  re-uploaded once;
- a copy whose structure is wrong (missing media, wrong text) is deleted and
  re-sent once, and never mapped if still wrong; albums must arrive whole;
- source -> destination ids go into the worker's message map, so replies,
  edits and deletes keep working through the existing handlers.

Log lines keep the legacy markers ("[FASTVIP COPIED] ...") so the health
monitor, the daily digest and existing log searches see engine deliveries
exactly like legacy ones, with "engine=True lag=<s>" appended.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from telethon.errors import FloodWaitError

from telegram_worker import owner_forwarding_policy as policy
from telegram_worker.permanent_delivery_errors import permanent_error_name, route_key

from .model import Plan, Route, Send, Skip
from .plan import Planner
from .render import Rendered
from .replies import reply_parent_ids, reply_target
from .rules import is_real_media, text_of

log = logging.getLogger("imperium-worker")
TRANSIENT = (
    "timeout",
    "timed out",
    "connection",
    "server disconnected",
    "temporarily",
    "transport",
    "network",
    "request failed",
    "mirror structure",
    "mirror album structure",
)


def is_transient(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(p in text for p in TRANSIENT)


# Telethon's text for these errors does not contain the RPC code, so match the class too.
REPLY_GONE_ERRORS = frozenset({"MsgIdInvalidError", "MessageIdInvalidError", "ReplyMessageIdInvalidError"})


def reply_target_gone(exc: Exception) -> bool:
    """Telegram refused the reply parent (deleted or invalid), not the post."""
    if type(exc).__name__ in REPLY_GONE_ERRORS:
        return True
    text = str(exc).upper()
    return "REPLY" in text or "MSG_ID_INVALID" in text or "MESSAGE_ID_INVALID" in text


def _as_list(sent: Any) -> List:
    if sent is None:
        return []
    return [x for x in sent if x] if isinstance(sent, list) else [sent]


def structure_problem(source: Any, sent: Any, expected_text: str) -> Optional[str]:
    """None when the copy has the source's media presence and text."""
    items = _as_list(sent)
    if not items:
        return "no_sent_message"
    if is_real_media(source) != any(is_real_media(i) for i in items):
        return "missing_media" if is_real_media(source) else "unexpected_media"
    squash = lambda value: re.sub(r"\s+", " ", (value or "").strip())  # noqa: E731
    expected = squash(expected_text)
    if expected and expected != squash("\n".join(text_of(i) for i in items if text_of(i))):
        return "text_or_caption_mismatch"
    return None


class Deliverer:
    def __init__(
        self,
        worker: Any,
        planner: Planner,
        attempts: int = 3,
        sleep_cap: int = 120,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
        alert: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self.worker = worker
        self.client = worker.client
        self.planner = planner
        self.attempts = max(1, attempts)
        self.sleep_cap = sleep_cap
        self.sleep = sleep
        self.clock = clock
        self.alert = alert or _bot_alert

    # -- public ------------------------------------------------------------
    async def deliver(self, legacy_route: Dict, route: Route, unit: Sequence, live: bool = True) -> bool:
        """FASTVIP contract: True = handled (sent or rightly skipped), False = retry later."""
        unit = sorted(unit, key=lambda m: int(m.id))
        ids = [int(m.id) for m in unit]
        dest = f"{route.dest_chat}_{route.dest_topic}"
        if policy.is_stopped(legacy_route):
            return True
        plan = await self.plan(legacy_route, route, unit, ensure_reply=True, check_ledger=live)
        if isinstance(plan, Skip):
            if plan.reason == "partial_album_mapping":
                log.error(
                    "[FASTVIP DEST FAILED] route=%s ids=%s dest=%s RuntimeError: partial album mapping "
                    "engine=True",
                    route.name,
                    ids,
                    dest,
                )
                return False
            _log_skip(route, ids, plan.reason)
            return True
        try:
            sent = await self.send(legacy_route, route, unit, plan)
        except Exception as exc:
            error = permanent_error_name(exc, route.dest_chat) if live else None
            if error:
                ledger = getattr(self.worker, "FASTVIP_PERMANENT_FAILURES", None)
                saved = ledger.record(legacy_route, ids, error) if ledger else False
                log.error(
                    "[FASTVIP ROUTE IMPAIRED - NOT DELIVERED] route=%s ids=%s dest=%s error=%s "
                    "PERMANENT=True RETRY=False LEDGER_SAVED=%s engine=True",
                    route.name,
                    ids,
                    dest,
                    error,
                    saved,
                )
                self._alert_impaired(legacy_route, route, error, dest)
                return True
            log.exception(
                "[FASTVIP DEST FAILED] route=%s ids=%s dest=%s %s: %s engine=True",
                route.name,
                ids,
                dest,
                type(exc).__name__,
                exc,
            )
            return False
        lag = max(0.0, self.clock() - unit[0].date.timestamp()) if getattr(unit[0], "date", None) else 0.0
        kind = f"ALBUM items={len(unit)}" if plan.kind == "album" else "SINGLE"
        log.warning(
            "[FASTVIP COPIED] route=%s source=%s_%s ids=%s kind=%s dest=%s engine=True lag=%.1fs",
            route.name,
            route.source_chat,
            route.source_topic,
            ids,
            kind,
            dest,
            lag,
        )
        return bool(_as_list(sent))

    async def copy_unit(
        self, owner_routes: Any, legacy_route: Dict, route: Route, unit: Sequence
    ) -> Tuple[str, List[int]]:
        """Owner-route contract: ("existing" | "sent" | "filtered", destination ids); errors raise."""
        unit = sorted(unit, key=lambda m: int(m.id))
        before = owner_routes.mapped_ids(self.worker, legacy_route, unit)
        if self.planner.maps.all_mapped(route, unit):
            return "existing", before
        if policy.is_stopped(legacy_route):
            return "filtered", before
        plan = await self.plan(legacy_route, route, unit, ensure_reply=False, check_ledger=False)
        if isinstance(plan, Send):
            await self.send(legacy_route, route, unit, plan)
        else:
            _log_skip(route, [m.id for m in unit], plan.reason)
        after = owner_routes.mapped_ids(self.worker, legacy_route, unit)
        return ("sent" if any(i not in before for i in after) else "filtered"), after

    async def plan(
        self, legacy_route: Dict, route: Route, unit: Sequence, ensure_reply: bool, check_ledger: bool
    ) -> Plan:
        self.planner.context["emoji_registry"] = getattr(self.worker, "EMOJI_REGISTRY", None)
        plan = self.planner.plan(route, unit, legacy_route, check_ledger=check_ledger)
        if not isinstance(plan, Send) or not ensure_reply or plan.reply_to != route.dest_topic:
            return plan
        if not reply_parent_ids(unit[0], route):
            return plan
        # A real parent with no mapping: let reply hardening recover it from the
        # destination (exact text/media match), then resolve the target again.
        from telegram_worker.reply_hardening import ensure_reply_mapping

        resolved = await ensure_reply_mapping(
            self.worker,
            unit[0],
            legacy_route,
            registry=self.planner.context.get("emoji_registry"),
            logger=log,
        )
        if not resolved and route.formatter == "imperium_vip":
            await self._recover_signal_parent(legacy_route, route, unit[0])
        target = reply_target(unit[0], route, self.planner.maps)
        return plan if target == plan.reply_to else Send(**{**plan.__dict__, "reply_to": target})

    async def _recover_signal_parent(self, legacy_route: Dict, route: Route, message: Any) -> Optional[int]:
        """VIP topic 7: copy the one XAU signal a reply points at when it was missed.

        Only an explicitly replied-to parent that parses as a signal and is at
        most 24 h older than the reply; never other history.
        """
        from telegram_worker.imperium_vip_formatter import parse_xauusd_signal
        from telegram_worker.imperium_vip_inline_hook import (
            MAX_REPLY_PARENT_RECOVERY_AGE_SECONDS,
            format_imperium_vip_text,
        )

        parents = reply_parent_ids(message, route)
        if not parents:
            return None
        try:
            parent = await self.client.get_messages(route.source_chat, ids=parents[0])
        except Exception as exc:
            log.warning(
                "[IMPERIUM VIP REPLY PARENT FETCH FAILED] parent=%s %s: %s engine=True",
                parents[0],
                type(exc).__name__,
                exc,
            )
            return None
        text = (getattr(parent, "message", None) or getattr(parent, "raw_text", None) or "") if parent else ""
        if not text or not parse_xauusd_signal(text):
            return None
        try:
            age = max(0.0, message.date.timestamp() - parent.date.timestamp())
        except Exception:
            age = None
        if age is None or age > MAX_REPLY_PARENT_RECOVERY_AGE_SECONDS:
            log.warning(
                "[IMPERIUM VIP REPLY PARENT TOO OLD] parent=%s age=%s max=%s engine=True",
                parents[0],
                age,
                MAX_REPLY_PARENT_RECOVERY_AGE_SECONDS,
            )
            return None
        registry = self.planner.context.get("emoji_registry")
        if format_imperium_vip_text(text, getattr(parent, "entities", None), registry)[2] != "signal":
            return None
        parent_plan = self.planner.plan(route, [parent], legacy_route)
        if not isinstance(parent_plan, Send):
            return None
        sent = await self.send(legacy_route, route, [parent], parent_plan)
        dest_id = getattr(_as_list(sent)[0], "id", None) if _as_list(sent) else None
        log.warning(
            "[IMPERIUM VIP REPLY PARENT RECOVERED BY COPY] source_parent=%s dest_parent=%s age=%.1fs "
            "signal_only=True engine=True",
            parents[0],
            dest_id,
            age,
        )
        return dest_id

    async def send(self, legacy_route: Dict, route: Route, unit: Sequence, plan: Send) -> Any:
        last_exc = None
        attempt = 0
        while attempt < self.attempts:
            attempt += 1
            try:
                return await self._send_once(legacy_route, route, unit, plan)
            except FloodWaitError as exc:
                last_exc = exc
                if attempt >= self.attempts:
                    raise  # no point sleeping before giving up; the caller retries later
                wait = min(int(exc.seconds) + 1, self.sleep_cap)
                log.warning(
                    "[copy retry floodwait] attempt=%s/%s wait=%ss route=%s msg=%s engine=True",
                    attempt,
                    self.attempts,
                    wait,
                    route.name,
                    unit[0].id,
                )
                await self.sleep(wait)
            except Exception as exc:
                last_exc = exc
                reason = policy.permanent_access_error(exc)
                if reason:
                    policy.stop_route(legacy_route, reason, log)
                    raise
                if plan.reply_to != route.dest_topic and reply_target_gone(exc):
                    # The mapped parent was deleted in the destination: post in
                    # the topic instead of failing this post forever (as ALT does).
                    log.warning(
                        "[REPLY TARGET GONE] route=%s source_msg=%s parent=%s fallback=topic_root engine=True",
                        route.name,
                        unit[0].id,
                        plan.reply_to,
                    )
                    plan = Send(**{**plan.__dict__, "reply_to": route.dest_topic})
                    attempt -= 1  # the fallback is not a retry
                    continue
                if attempt < self.attempts and is_transient(exc):
                    wait = min(2 * attempt, self.sleep_cap)
                    log.warning(
                        "[copy retry transient] attempt=%s/%s wait=%ss route=%s msg=%s error=%s engine=True",
                        attempt,
                        self.attempts,
                        wait,
                        route.name,
                        unit[0].id,
                        exc,
                    )
                    await self.sleep(wait)
                    continue
                raise
        assert last_exc is not None  # the loop only ends early by returning
        raise last_exc

    # -- sending -----------------------------------------------------------
    async def _send_once(self, legacy_route: Dict, route: Route, unit: Sequence, plan: Send) -> Any:
        rendered = plan.payload
        if plan.kind == "album":
            sent = await self._send_file(route, unit, rendered.media, plan, rendered)
            if len(rendered.media) > 1 and len(_as_list(sent)) != len(rendered.media):
                await self._delete(route, sent, "album_item_count_mismatch")
                raise RuntimeError(
                    f"mirror album structure mismatch: expected={len(rendered.media)} "
                    f"sent={len(_as_list(sent))}"
                )
            items = _as_list(sent)
            # Each media item maps to its copy; an item without media (rare) to
            # the album's first copy, so a reply to it still lands in the album.
            media = [m for m in rendered.messages if is_real_media(m)]
            pairs = list(zip(media, items)) + [
                (m, items[0]) for m in rendered.messages if not is_real_media(m)
            ]
            for source, copy_ in pairs:
                self.worker.remember_message(_original(unit, source), copy_, legacy_route, save=False)
            self.worker.save_map()
            return sent
        sent = await self._send_single(route, unit, plan, rendered)
        problem = structure_problem(rendered.messages[0], sent, rendered.text)
        if problem:
            await self._delete(route, sent, problem)
            sent = await self._send_single(route, unit, plan, rendered)
            problem = structure_problem(rendered.messages[0], sent, rendered.text)
            if problem:
                await self._delete(route, sent, problem)
                log.error(
                    "[mirror structure repair rejected CLOSED] route=%s source_msg=%s reason=%s engine=True",
                    route.name,
                    unit[0].id,
                    problem,
                )
                raise RuntimeError("mirror structure mismatch after repair: " + problem)
            log.info(
                "[mirror structure repair ok] route=%s source_msg=%s engine=True", route.name, unit[0].id
            )
        self.worker.remember_message(unit[0], sent, legacy_route)
        return sent

    async def _send_single(self, route: Route, unit: Sequence, plan: Send, rendered: Rendered) -> Any:
        if plan.kind == "media":
            return await self._send_file(route, unit, rendered.media[0], plan, rendered)
        return await self.client.send_message(
            route.dest_chat,
            rendered.text,
            formatting_entities=rendered.entities,
            parse_mode=None,
            reply_to=plan.reply_to,
            link_preview=True,
        )

    async def _send_file(
        self, route: Route, unit: Sequence, file: Any, plan: Send, rendered: Rendered
    ) -> Any:
        caption = rendered.text or None
        kwargs = dict(
            caption=caption,
            formatting_entities=rendered.entities if caption else None,
            parse_mode=None,
            reply_to=plan.reply_to,
        )
        try:
            return await self.client.send_file(route.dest_chat, file, **kwargs)
        except Exception as exc:
            if isinstance(exc, FloodWaitError) or policy.permanent_access_error(exc):
                raise
            log.warning(
                "[media direct copy failed] msg=%s route=%s: %s engine=True", unit[0].id, route.name, exc
            )
        cache = Path(self.worker.DATA_DIR) / "media_cache"
        cache.mkdir(parents=True, exist_ok=True)
        sources = [m for m in rendered.messages if is_real_media(m)]
        paths = []
        try:
            for message in sources:
                path = await message.download_media(
                    file=str(cache / f"engine_{route.dest_topic}_{message.id}")
                )
                if path:
                    paths.append(path)
            if not paths or len(paths) != len(sources):
                raise RuntimeError("media fallback downloaded no files")
            upload = paths if isinstance(file, list) else paths[0]
            sent = await self.client.send_file(route.dest_chat, upload, **kwargs)
            log.info("[media fallback reupload ok] route=%s items=%s engine=True", route.name, len(paths))
            return sent
        finally:
            for path in paths:
                try:
                    Path(path).unlink(missing_ok=True)
                except Exception:
                    pass

    async def _delete(self, route: Route, sent: Any, reason: str) -> None:
        ids = [int(i.id) for i in _as_list(sent) if getattr(i, "id", None)]
        if not ids:
            return
        try:
            await self.client.delete_messages(route.dest_chat, ids)
            log.warning(
                "[mirror structure repair] deleted bad copy ids=%s route=%s reason=%s engine=True",
                ids,
                route.name,
                reason,
            )
        except Exception as exc:
            log.warning(
                "[mirror structure repair delete failed] ids=%s route=%s reason=%s: %s engine=True",
                ids,
                route.name,
                reason,
                exc,
            )

    def _alert_impaired(self, legacy_route: Dict, route: Route, error: str, dest: str) -> None:
        throttle = getattr(self.worker, "FASTVIP_IMPAIRED_ALERTS", None)
        if throttle and throttle.ready(route_key(legacy_route)):
            try:
                self.alert(
                    f"⚠️ <b>Route impaired</b>: {route.name}\n<code>{error}</code> at {dest}. "
                    "New messages get one attempt each; nothing is retried."
                )
            except Exception:
                pass


def _bot_alert(text: str) -> Any:
    from telegram_worker.runtime_guard import send_alert

    return send_alert(text)


RETIRED_MARKERS = {
    "vip5_retired": "[VIP5 DESTINATION BLOCKED]",
    "relay7250_retired": "[RELAY7250 PERMANENT OUTBOUND BLOCK]",
}


def _log_skip(route: Route, ids: Sequence[int], reason: str) -> None:
    marker = RETIRED_MARKERS.get(reason)
    if marker:
        log.warning(
            "%s route=%s ids=%s dest=%s_%s engine=True",
            marker,
            route.name,
            ids,
            route.dest_chat,
            route.dest_topic,
        )
    else:
        log.info("[ENGINE SKIP] route=%s ids=%s reason=%s", route.name, ids, reason)


def _original(unit: Sequence, rendered_message: Any) -> Any:
    """The source message a (possibly restyled) message was rendered from."""
    return next((m for m in unit if m.id == rendered_message.id), rendered_message)
