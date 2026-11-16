"""One-time seed of a live route's destination topic: clear it, then copy the
source's last N posts into it, oldest first, before the route goes live.

Owner request 16 Nov 2026: VIP topic 10877 is cleared, gets the last 50 Market
Slayers VIP posts, then copies Market Slayers live. Only posts the route would
send live are counted (a post its filters skip is never copied, so it is not one
of the N). Each post goes through the same delivery as a live post (filters,
formatting, replies, message map), so the intake later sees them as already
copied and never sends one twice. The result is saved on the volume, so a
restart never seeds again. `gate` is set when the seed ends, whatever happened:
until then the intake holds that route's live posts, so they land after the seed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from telethon.errors import FloodWaitError

from . import topic_reset
from .intake import units_of

log = logging.getLogger("engine-topic-seed")
FETCH_LIMIT = 500  # newest source posts read to find the last N sendable ones
SEND_ATTEMPTS = 3
PAUSE_SECONDS = 1.0  # between posts: 50 sends in a burst invite a flood wait
GATE_TIMEOUT_SECONDS = 300.0  # the intake never holds a live post longer than this
Sleep = Callable[[float], Awaitable[Any]]
Deliver = Callable[[Dict[str, Any], List[Any]], Awaitable[bool]]
Sendable = Callable[[Dict[str, Any], List[Any]], bool]


def state_path(data_dir: Any, route: Dict[str, Any]) -> Path:
    return Path(data_dir) / (
        f"topic_seed_{abs(int(route['source_chat']))}_to_"
        f"{abs(int(route['dest_chat']))}_{int(route['dest_topic'])}_v1.json"
    )


async def last_units(client: Any, route: Dict[str, Any], last: int, sendable: Sendable) -> List[List[Any]]:
    """The newest `last` posts (an album is one) the route would send, oldest first."""
    kwargs: Dict[str, Any] = {"limit": FETCH_LIMIT}
    if route.get("source_topic"):
        kwargs["reply_to"] = route["source_topic"]
    messages = [m async for m in client.iter_messages(route["source_chat"], **kwargs) if m is not None]
    units = units_of(messages)
    if len(messages) >= FETCH_LIMIT and units and getattr(units[0][0], "grouped_id", None):
        units = units[1:]  # the oldest album may be cut off by the limit
    chosen = [unit for unit in units if sendable(route, unit)]
    return chosen[-last:] if last > 0 else []


async def deliver_with_retry(deliver: Deliver, route: Dict[str, Any], unit: List[Any], sleep: Sleep) -> bool:
    for attempt in range(1, SEND_ATTEMPTS + 1):
        try:
            if await deliver(route, unit):
                return True
        except asyncio.CancelledError:
            raise
        except FloodWaitError as exc:
            await sleep(int(exc.seconds) + 1)
            continue
        except Exception as exc:
            log.warning(
                "[TOPIC SEED SEND FAILED] route=%s ids=%s attempt=%s/%s %s: %s",
                route.get("name"),
                [int(m.id) for m in unit],
                attempt,
                SEND_ATTEMPTS,
                type(exc).__name__,
                exc,
            )
        await sleep(2.0)
    return False


async def seed_once(
    client: Any,
    route: Dict[str, Any],
    last: int,
    data_dir: Any,
    deliver: Deliver,
    sendable: Sendable,
    sleep: Sleep = asyncio.sleep,
) -> Optional[Dict[str, Any]]:
    path = state_path(data_dir, route)
    if path.exists():
        log.warning("[TOPIC SEED SKIPPED] route=%s already_done=True", route.get("name"))
        return None
    units = await last_units(client, route, last, sendable)
    handled: List[int] = []
    failed: List[List[int]] = []
    for index, unit in enumerate(units):
        if index:
            await sleep(PAUSE_SECONDS)
        ids = [int(m.id) for m in unit]
        if await deliver_with_retry(deliver, route, unit, sleep):
            handled.append(ids[0])
        else:
            failed.append(ids)
            log.error("[TOPIC SEED POST FAILED] route=%s ids=%s", route.get("name"), ids)
    result = {
        "route": route.get("name"),
        "requested": last,
        "selected": len(units),
        "handled": len(handled),
        "failed": failed,
        "first_source_id": handled[0] if handled else None,
        "last_source_id": handled[-1] if handled else None,
        "at": time.time(),
    }
    path.write_text(json.dumps(result), encoding="utf-8")
    log.warning(
        "[TOPIC SEED DONE] route=%s requested=%s selected=%s handled=%s failed=%s",
        route.get("name"),
        last,
        len(units),
        len(handled),
        failed,
    )
    return result


async def clear_then_seed(
    client: Any,
    route: Dict[str, Any],
    last: int,
    data_dir: Any,
    deliver: Deliver,
    sendable: Sendable,
    gate: asyncio.Event,
    sleep: Sleep = asyncio.sleep,
) -> Optional[Dict[str, Any]]:
    """Reset the route's destination topic once, then seed it once. Never raises; opens `gate` at the end."""
    chat, topic = int(route["dest_chat"]), int(route["dest_topic"])
    try:
        await topic_reset.reset_when_ready(client, chat, topic, data_dir, sleep=sleep)
        if not topic_reset.state_path(data_dir, chat, topic).exists():
            # The reset failed (already reported); seeding now would mix old and new posts.
            # The next start retries the reset, then seeds.
            log.error("[TOPIC SEED SKIPPED] route=%s reset_not_done=True", route.get("name"))
            return None
        return await seed_once(client, route, last, data_dir, deliver, sendable, sleep)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.error("[TOPIC SEED FAILED] route=%s %s: %s", route.get("name"), type(exc).__name__, exc)
        return None
    finally:
        gate.set()


async def wait_for_gate(gate: Optional[asyncio.Event], timeout: float = GATE_TIMEOUT_SECONDS) -> None:
    """Hold a live post until the seed is done, but never longer than `timeout`."""
    if gate is None or gate.is_set():
        return
    try:
        await asyncio.wait_for(gate.wait(), timeout)
    except asyncio.TimeoutError:
        log.warning("[TOPIC SEED GATE TIMEOUT] waited=%ss; live post sent anyway", int(timeout))
