import asyncio
import hashlib
import json
import logging
import os
import re
import time
from collections import defaultdict, deque
from pathlib import Path

from telethon import events
from telethon.tl import functions

from telegram_worker.worker_fixed import client, stats
from telegram_worker.admin_features import ADMIN_CHAT, admin_startup, admin_loop, handle_admin_command
from telegram_worker.runtime_guard import start_runtime_guard, alert_crash
from telegram_worker.provider_profiles import apply_provider_profile, is_promo_text
from telegram_worker.universal_signal_ai import extract_and_format, looks_like_signal

log = logging.getLogger("exposedfx-ai-signal-formatter")

PRICE_RE = r"\d{1,7}(?:\.\d+)?"

UPDATE_CUSTOM_EMOJIS = {
    "DIAMOND": os.environ.get("CUSTOM_EMOJI_DIAMOND", "5427168083074628963"),
    "RED_CROSS": os.environ.get("CUSTOM_EMOJI_RED_CROSS", "5210952531676504517"),
}

UPDATE_EMOJI_FALLBACKS = {
    "DIAMOND": "\U0001F48E",
    "RED_CROSS": "\u274C",
}


def update_ce(name: str) -> str:
    doc_id = str(UPDATE_CUSTOM_EMOJIS.get(name, "")).strip()
    emoji = UPDATE_EMOJI_FALLBACKS.get(name, "")
    if doc_id and doc_id.isdigit():
        return f'<tg-emoji emoji-id="{doc_id}">{emoji}</tg-emoji>'
    return emoji


EMOJI_DIAMOND = update_ce("DIAMOND")
EMOJI_CROSS = update_ce("RED_CROSS")


def chat_id_from_env(name, default):
    raw = os.environ.get(name, default).strip()
    if raw.startswith("http") and "#" in raw:
        raw = raw.split("#", 1)[1].split("_", 1)[0]
    raw = raw.replace("/", "").strip()
    return int(raw)


def topic_set_from_env(name, default):
    raw = os.environ.get(name, default).strip()
    out = set()
    for part in re.split(r"[,\s]+", raw):
        part = part.strip()
        if not part:
            continue
        if part.startswith("http") and "_" in part:
            part = part.rsplit("_", 1)[-1]
        part = part.replace("/", "")
        try:
            out.add(int(part))
        except ValueError:
            pass
    return out


SIGNAL_SOURCE_CHAT = chat_id_from_env("SIGNAL_SOURCE_CHAT", "-1003918958200")
SIGNAL_DEST_CHAT = chat_id_from_env("SIGNAL_DEST_CHAT", "-5252460120")

DATA_DIR = Path(os.environ.get("DATA_DIR") or "./data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
SIGNAL_PACKET_MAP_FILE = DATA_DIR / "signal_packet_map.json"
DELETE_OLD_SIGNAL_PACKET_ON_EDIT = os.environ.get("DELETE_OLD_SIGNAL_PACKET_ON_EDIT", "1").strip() == "1"
SEND_SOURCE_LINE = os.environ.get("SEND_SOURCE_LINE", "1").strip() == "1"
SEND_SIGNAL_UPDATES = os.environ.get("SEND_SIGNAL_UPDATES", "0").strip() == "1"
DROP_LINK_ONLY = os.environ.get("DROP_LINK_ONLY", "1").strip() == "1"
LINK_ONLY_RE = re.compile(r"^(?:https?://|t\.me/|www\.)\S+$", re.IGNORECASE)

FORWARD_SIGNAL_CANDIDATES = os.environ.get("FORWARD_SIGNAL_CANDIDATES", "1").strip() == "1"
PARTIAL_BUFFER_ENABLED = os.environ.get("PARTIAL_SIGNAL_BUFFER", "1").strip() == "1"
BUFFER_WINDOW_SECONDS = int(os.environ.get("SIGNAL_BUFFER_SECONDS", "600"))
BUFFER_MAX_MESSAGES = int(os.environ.get("SIGNAL_BUFFER_MAX_MESSAGES", "8"))
DEFAULT_ALLOWED_TOPICS = "23,430,28,2,31,35,11,363,25,29,33,20,362,17,34,22,9,36,10,14,4,16,13,12,8,7,6,5,3,15,567,568,569,570,1927,8587,26892,26898,28464,28840"
ALLOWED_SOURCE_TOPICS = topic_set_from_env("ALLOWED_SOURCE_TOPICS", DEFAULT_ALLOWED_TOPICS)
CONTENT_DEDUPE_ENABLED = os.environ.get("CONTENT_DEDUPE_ENABLED", "1").strip() == "1"
SIGNAL_SEND_RETRY_ATTEMPTS = int(os.environ.get("SIGNAL_SEND_RETRY_ATTEMPTS", "2"))
SIGNAL_SEND_RETRY_SLEEP_CAP_SECONDS = int(os.environ.get("SIGNAL_SEND_RETRY_SLEEP_CAP_SECONDS", "120"))
AI_FORMATTED_SIGNAL_WITH_MEDIA = os.environ.get("AI_FORMATTED_SIGNAL_WITH_MEDIA", "1").strip() == "1"
UPDATE_REPLY_DEDUPE_SECONDS = int(os.environ.get("UPDATE_REPLY_DEDUPE_SECONDS", "21600"))
SIGNAL_LIFECYCLE_FILE = DATA_DIR / "signal_lifecycle.json"
SIGNAL_CONTENT_DEDUPE_FILE = DATA_DIR / "signal_content_dedupe.json"
PURGE_DEST_ON_START = os.environ.get("PURGE_DEST_ON_START", "0").strip() == "1"
PURGE_DEST_LIMIT = int(os.environ.get("PURGE_DEST_LIMIT", "200000"))
PURGE_DEST_UNTIL_EMPTY = os.environ.get("PURGE_DEST_UNTIL_EMPTY", "1").strip() == "1"
PURGE_DEST_SLEEP_SECONDS = float(os.environ.get("PURGE_DEST_SLEEP_SECONDS", "0.35"))

def load_content_dedupe_signatures():
    try:
        if SIGNAL_CONTENT_DEDUPE_FILE.exists():
            data = json.loads(SIGNAL_CONTENT_DEDUPE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [str(x) for x in data if x]
    except Exception as exc:
        log.warning(f"[content dedupe load failed] {exc}")
    return []


def save_content_dedupe_signatures():
    try:
        if 'sent_content_signatures' not in globals():
            return
        tmp = SIGNAL_CONTENT_DEDUPE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(list(sent_content_signatures)[-1200:]), encoding="utf-8")
        tmp.replace(SIGNAL_CONTENT_DEDUPE_FILE)
    except Exception as exc:
        log.warning(f"[content dedupe save failed] {exc}")


buffers = defaultdict(lambda: deque(maxlen=BUFFER_MAX_MESSAGES))
sent_signatures = deque(maxlen=300)
sent_signature_set = set()
sent_content_signatures = deque(load_content_dedupe_signatures(), maxlen=1200)
sent_content_signature_set = set(sent_content_signatures)
update_reply_dedupe = {}
last_signal_context = {}


def load_signal_lifecycle():
    try:
        if SIGNAL_LIFECYCLE_FILE.exists():
            data = json.loads(SIGNAL_LIFECYCLE_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception as exc:
        log.warning(f"[lifecycle load failed] {exc}")
    return {}


def save_signal_lifecycle():
    try:
        tmp = SIGNAL_LIFECYCLE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(active_signal_lifecycle, default=str), encoding="utf-8")
        tmp.replace(SIGNAL_LIFECYCLE_FILE)
    except Exception as exc:
        log.warning(f"[lifecycle save failed] {exc}")


def rebuild_last_active_signal_by_topic(lifecycle):
    out = {}

    try:
        records = list(lifecycle.values())
    except Exception:
        return out

    records.sort(key=lambda r: float(r.get("last_update_ts") or r.get("created_ts") or 0))

    for rec in records:
        try:
            topic = int(rec.get("topic"))
        except Exception:
            continue

        status = str(rec.get("status") or "").upper()
        if status in ("CANCELLED", "SL_HIT", "CLOSED", "CLOSED_MANUAL"):
            continue

        out[topic] = rec

    return out



def load_content_dedupe_signatures():
    try:
        if SIGNAL_CONTENT_DEDUPE_FILE.exists():
            data = json.loads(SIGNAL_CONTENT_DEDUPE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [str(x) for x in data if x]
    except Exception as exc:
        log.warning(f"[content dedupe load failed] {exc}")
    return []


def save_content_dedupe_signatures():
    try:
        tmp = SIGNAL_CONTENT_DEDUPE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(list(sent_content_signatures)[-1200:]), encoding="utf-8")
        tmp.replace(SIGNAL_CONTENT_DEDUPE_FILE)
    except Exception as exc:
        log.warning(f"[content dedupe save failed] {exc}")


active_signal_lifecycle = load_signal_lifecycle()
last_active_signal_by_topic = rebuild_last_active_signal_by_topic(active_signal_lifecycle)
topic_title_cache = {}

TOPIC_NAMES = {
    2: "Triad FX",
    3: "Topic 3",
    4: "Gold Trader Sunny",
    5: "NS Trades",
    6: "Platinum Intro Channel",
    7: "TGF Montana",
    8: "BroadFX",
    9: "T Marz",
    10: "GTMO VIP",
    11: "Trading Central FX VIP",
    12: "SOL Gibbs",
    13: "Sniper Pro Academy",
    14: "McGarry and Gunter VIP",
    16: "ICT Trader",
    17: "1% VIP SIGNALS",
    20: "Master Premium",
    22: "Dropout VIP",
    23: "Market Slayers VIP",
    25: "Premium I Live Trade",
    28: "KEY / ALCHEMIST",
    29: "A4xXAUr PREMIUM",
    31: "MANJOX TRADES",
    33: "GotMeKayed",
    34: "MSC Premium",
    35: "BOLZEGHA VIP",
    36: "ELX Premium",
    362: "Route 362",
    363: "R363 Signals",
    430: "Route 430",
    1927: "Route 1927",
    567: "Route 567",
    568: "Route 568",
    569: "Route 569",
    570: "Route 570",
    8587: "Route 8587",
}


def source_channel_id():
    text = str(SIGNAL_SOURCE_CHAT)
    if text.startswith("-100"):
        return int(text[4:])
    return abs(SIGNAL_SOURCE_CHAT)


def event_is_from_source(event):
    cid = getattr(event, "chat_id", None)
    if cid == SIGNAL_SOURCE_CHAT:
        return True
    channel_id = source_channel_id()
    msg = getattr(event, "message", None)
    peer = getattr(msg, "peer_id", None)
    if getattr(peer, "channel_id", None) == channel_id:
        return True
    if getattr(peer, "chat_id", None) == abs(SIGNAL_SOURCE_CHAT):
        return True
    if cid == channel_id:
        return True
    return False


def message_text(message):
    return message.message or message.raw_text or message.text or ""


def topic_id_of(message):
    for attr in ("reply_to_top_id", "top_msg_id"):
        value = getattr(message, attr, None)
        if value:
            try:
                return int(value)
            except Exception:
                pass

    direct_msg = getattr(message, "reply_to_msg_id", None)
    if direct_msg and getattr(message, "is_topic_message", False):
        try:
            return int(direct_msg)
        except Exception:
            pass

    reply = getattr(message, "reply_to", None)
    if not reply:
        return None

    for attr in ("reply_to_top_id", "top_msg_id", "reply_to_msg_id"):
        value = getattr(reply, attr, None)
        if value:
            try:
                return int(value)
            except Exception:
                pass

    return None


def topic_label(topic_id):
    if topic_id is None:
        return "Main Chat"

    try:
        tid = int(topic_id)
    except Exception:
        return f"Topic {topic_id}"

    cached = topic_title_cache.get(tid)
    if cached:
        return cached

    return TOPIC_NAMES.get(tid, f"Topic {tid}")


async def resolve_topic_title_from_telegram(topic_id):
    try:
        tid = int(topic_id)
    except Exception:
        return None

    # Try direct forum topic API if this Telethon runtime supports it.
    try:
        req_cls = getattr(functions.channels, "GetForumTopicsByIDRequest", None)
        if req_cls:
            res = await client(req_cls(channel=int(SIGNAL_SOURCE_CHAT), topics=[tid]))
            topics = getattr(res, "topics", None) or []
            if topics:
                title = getattr(topics[0], "title", None)
                if title:
                    return str(title).strip()
    except Exception as exc:
        log.info(f"[topic title api fallback] topic={tid}: {type(exc).__name__}: {exc}")

    # Fallback: forum topic id normally points to the topic starter service message.
    try:
        msg = await client.get_messages(SIGNAL_SOURCE_CHAT, ids=tid)
        action = getattr(msg, "action", None)
        title = getattr(action, "title", None) or getattr(msg, "title", None)
        if title:
            return str(title).strip()
    except Exception as exc:
        log.info(f"[topic title message fallback failed] topic={tid}: {type(exc).__name__}: {exc}")

    return None


async def preload_topic_titles_once():
    loaded = 0

    for tid in sorted(ALLOWED_SOURCE_TOPICS):
        title = await resolve_topic_title_from_telegram(tid)

        if title:
            topic_title_cache[int(tid)] = title
            loaded += 1
            log.info(f"[topic title cached] topic={tid} title={title!r}")

    log.info(f"[topic title cache done] loaded={loaded}")


def should_skip(message):
    text = message_text(message).strip()
    if not text:
        return True
    topic_id = topic_id_of(message)
    if topic_id is not None and ALLOWED_SOURCE_TOPICS and int(topic_id) not in ALLOWED_SOURCE_TOPICS:
        log.info(f"[signal hub skipped] topic not in whitelist topic={topic_id}")
        return True
    if DROP_LINK_ONLY and LINK_ONLY_RE.match(text):
        log.info("[signal hub skipped] plain link")
        return True
    return False



def reply_source_ids_for_update(message):
    ids = []

    direct = getattr(message, "reply_to_msg_id", None)
    if direct:
        ids.append(direct)

    reply = getattr(message, "reply_to", None)
    if reply:
        for attr in ("reply_to_msg_id", "reply_to_top_id", "top_msg_id"):
            value = getattr(reply, attr, None)
            if value and value not in ids:
                ids.append(value)

    out = []
    for x in ids:
        try:
            out.append(int(x))
        except Exception:
            pass
    return out


def packet_for_reply_source(message, key):
    for source_id in reply_source_ids_for_update(message):
        pkey = f"{SIGNAL_SOURCE_CHAT}:{key}:{source_id}"
        ids = signal_packet_map.get(pkey) or []
        if len(ids) >= 2:
            return source_id, [int(x) for x in ids]
    return None, []


def has_next_trade_context(text: str) -> bool:
    t = update_match_text(text)
    return bool(re.search(
        r"\b(GO\s+IN\s+TO\s+THE\s+NEXT\s+TRADE|GO\s+INTO\s+THE\s+NEXT\s+TRADE|NEXT\s+TRADE|NEW\s+TRADE|NEXT\s+SETUP|NEW\s+SETUP)\b",
        t,
    ))


def looks_like_new_setup_inside_update(text: str) -> bool:
    t = update_match_text(text)
    has_direction = bool(re.search(r"\b(BUY|SELL|BUYS|SELLS|BUYING|SELLING|LONG|SHORT)\b", t))
    has_sl = bool(re.search(r"\b(SL|S/L|STOP|STOPLOSS|STOP\s*LOSS)\b\s*[:@\-]?\s*\d", t))
    has_price = bool(re.search(r"\b\d{3,7}(?:\.\d+)?\b", t))
    return has_direction and has_sl and has_price


def clean_update_body(text: str) -> str:
    raw = (text or "").strip()
    raw = raw.replace("\\n", "\n").replace("\\r", "\n")
    raw = re.sub(r"[ \t]+", " ", raw)
    return raw.strip()


def update_match_text(text: str) -> str:
    """
    Normalises messy provider updates:
    - breakEven -> break Even
    - break-even -> break even
    - S/L -> SL
    - extra spaces/newlines removed
    """
    raw = clean_update_body(text)
    raw = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", raw)
    raw = raw.replace("-", " ")
    raw = raw.replace("_", " ")
    raw = re.sub(r"\bS\s*/\s*L\b", "SL", raw, flags=re.IGNORECASE)
    raw = re.sub(r"[’']", "", raw)
    raw = re.sub(r"\s+", " ", raw)
    return raw.upper().strip()


def extract_update_pips(text: str):
    t = update_match_text(text) if "update_match_text" in globals() else (text or "").upper()
    out = []

    # Normal forms:
    # +50PIPS, 50 pips, +50 points, +50 pts, +50p
    patterns = [
        r"([+-]?\s*\d{1,5}(?:\.\d+)?)\s*(?:PIP|PIPS|POINT|POINTS|PTS|P)\b",
        r"\b(?:RUNNING|FLOATING|PROFIT|PROFITS|UP|MADE|SECURED|BANKED|BOOKED|CLOSED)\s*([+-]?\s*\d{1,5}(?:\.\d+)?)\s*(?:PIP|PIPS|POINT|POINTS|PTS|P)?\b",
        r"\b([+-]\s*\d{1,5}(?:\.\d+)?)\b",
    ]

    for pat in patterns:
        for m in re.findall(pat, t):
            value = str(m).replace(" ", "")
            if not value:
                continue

            # Avoid accidentally treating prices as pips.
            try:
                numeric = abs(float(value.replace("+", "").replace("-", "")))
            except Exception:
                continue

            if numeric > 2000:
                continue

            if not value.startswith(("+", "-")):
                value = "+" + value

            item = value + "PIPS"
            if item not in out:
                out.append(item)

    return out



def extract_update_tp_nums(text: str):
    t = update_match_text(text)
    nums = []

    patterns = [
        r"\bTP\s*#?\s*(\d{1,2})\b[^\n]{0,30}\b(HIT|DONE|SMASHED|CLEANED|REACHED|TOUCHED)\b",
        r"\b(HIT|DONE|SMASHED|CLEANED|REACHED|TOUCHED)\b[^\n]{0,30}\bTP\s*#?\s*(\d{1,2})\b",
        r"\bTARGET\s*#?\s*(\d{1,2})\b[^\n]{0,30}\b(HIT|DONE|REACHED|TOUCHED)\b",
        r"\b(?:FIRST|1ST)\s+(?:TP|TARGET)\b",
        r"\b(?:SECOND|2ND)\s+(?:TP|TARGET)\b",
        r"\b(?:THIRD|3RD)\s+(?:TP|TARGET)\b",
    ]

    for pat in patterns[:3]:
        for m in re.findall(pat, t):
            if isinstance(m, tuple):
                for item in m:
                    if str(item).isdigit():
                        nums.append(str(item))
            elif str(m).isdigit():
                nums.append(str(m))

    if re.search(patterns[3], t):
        nums.append("1")
    if re.search(patterns[4], t):
        nums.append("2")
    if re.search(patterns[5], t):
        nums.append("3")

    # word TP update aliases
    word_tp_map = {
        "ONE": "1", "TWO": "2", "THREE": "3", "FOUR": "4", "FIVE": "5",
        "FIRST": "1", "SECOND": "2", "THIRD": "3", "FOURTH": "4", "FIFTH": "5",
    }

    for word, number in word_tp_map.items():
        if re.search(rf"\b(TP|TARGET|TAKE\s*PROFIT)\s*(?:NUMBER\s*)?{word}\b[^\n]{{0,40}}\b(HIT|DONE|SMASHED|CLEANED|REACHED|TOUCHED|CLOSED|SECURED|BANKED)\b", t):
            nums.append(number)
        if re.search(rf"\b{word}\s+(TP|TARGET|TAKE\s*PROFIT)\b[^\n]{{0,40}}\b(HIT|DONE|SMASHED|CLEANED|REACHED|TOUCHED|CLOSED|SECURED|BANKED)\b", t):
            nums.append(number)

    # compact TP update aliases
    for m in re.findall(r"\b(?:TP|TARGET|TAKE\s*PROFIT)\s*#?\s*(\d{1,2})\b[^\n]{0,40}\b(HIT|DONE|SMASHED|CLEANED|REACHED|TOUCHED|CLOSED|SECURED|BANKED)\b", t):
        if isinstance(m, tuple):
            nums.append(str(m[0]))
        else:
            nums.append(str(m))

    # emoji-only compact updates like "TP1 ✅"
    for m in re.findall(r"\b(?:TP|TARGET)\s*#?\s*(\d{1,2})\s*(?:✅|✔|☑|💎|🔥)\b", t):
        nums.append(str(m))

    # ultra TP variation aliases
    tp_words = {
        "ONE": "1", "TWO": "2", "THREE": "3", "FOUR": "4", "FIVE": "5",
        "FIRST": "1", "SECOND": "2", "THIRD": "3", "FOURTH": "4", "FIFTH": "5",
    }

    # T1 hit, T2 done, tgt1 reached, target 1 secured, take profit 1 banked
    for m in re.findall(r"\b(?:TP|T|TGT|TARGET|TAKE\s*PROFIT)\s*#?\s*(\d{1,2})\b[^\n]{0,50}\b(HIT|DONE|SMASHED|CLEANED|REACHED|TOUCHED|CLOSED|SECURED|BANKED|TAKEN|FILLED)\b", t):
        if isinstance(m, tuple):
            nums.append(str(m[0]))
        else:
            nums.append(str(m))

    # hit TP1, secured T2, reached target 3
    for m in re.findall(r"\b(HIT|DONE|SMASHED|CLEANED|REACHED|TOUCHED|CLOSED|SECURED|BANKED|TAKEN|FILLED)\b[^\n]{0,50}\b(?:TP|T|TGT|TARGET|TAKE\s*PROFIT)\s*#?\s*(\d{1,2})\b", t):
        if isinstance(m, tuple):
            nums.append(str(m[1]))
        else:
            nums.append(str(m))

    # first target hit, second tp done, target one reached
    for word, number in tp_words.items():
        if re.search(rf"\b{word}\s+(?:TP|TGT|TARGET|TAKE\s*PROFIT)\b[^\n]{{0,50}}\b(HIT|DONE|SMASHED|CLEANED|REACHED|TOUCHED|CLOSED|SECURED|BANKED|TAKEN|FILLED)\b", t):
            nums.append(number)

        if re.search(rf"\b(?:TP|TGT|TARGET|TAKE\s*PROFIT)\s+{word}\b[^\n]{{0,50}}\b(HIT|DONE|SMASHED|CLEANED|REACHED|TOUCHED|CLOSED|SECURED|BANKED|TAKEN|FILLED)\b", t):
            nums.append(number)

    # emoji-only updates: TP1 ✅, T2 💎, target 3 🔥
    for m in re.findall(r"\b(?:TP|T|TGT|TARGET)\s*#?\s*(\d{1,2})\s*(?:✅|✔|☑|💎|🔥|🚀|🎯)\b", t):
        nums.append(str(m))

    unique = []
    for n in nums:
        if n not in unique:
            unique.append(n)

    return unique


def has_set_be_instruction(text: str) -> bool:
    t = update_match_text(text)

    patterns = [
        r"\bSET\s+(?:SL\s+)?(?:TO\s+)?BE\b",
        r"\bSET\s+(?:SL\s+)?(?:TO\s+)?BREAKEVEN\b",
        r"\bSET\s+(?:SL\s+)?(?:TO\s+)?BREAK\s*EVEN\b",
        r"\bMOVE\s+(?:SL|STOP|STOPLOSS|STOP\s*LOSS)\s+(?:TO\s+)?BE\b",
        r"\bMOVE\s+(?:SL|STOP|STOPLOSS|STOP\s*LOSS)\s+(?:TO\s+)?BREAKEVEN\b",
        r"\bSL\s+(?:TO|AT)?\s*BE\b",
        r"\bSL\s+(?:TO|AT)?\s*BREAKEVEN\b",
        r"\bSTOP\s*(?:LOSS)?\s+(?:TO|AT)?\s*BE\b",
        r"\bRISK\s*FREE\b",
        r"\bPROTECT(?:ED)?\s+(?:ENTRY|TRADE|POSITION)\b",
        r"\bSECURE\s+(?:BE|BREAKEVEN|BREAK\s*EVEN)\b",
    ]

    return any(re.search(p, t) for p in patterns)


def is_breakeven_hit_phrase(text: str) -> bool:
    t = update_match_text(text)
    return bool(
        re.search(r"\b(BE|BREAKEVEN|BREAK\s*EVEN)\b.{0,30}\b(HIT|TOUCHED|REACHED|DONE|TAKEN|CLOSED|GOT\s+HIT)\b", t)
        or re.search(r"\b(HIT|TOUCHED|REACHED|DONE|TAKEN|CLOSED)\b.{0,30}\b(BE|BREAKEVEN|BREAK\s*EVEN)\b", t)
        or re.search(r"\b(BE|BREAKEVEN|BREAK\s*EVEN)\b\s+(HAS\s+BEEN|HAS|WAS|GOT|IS)\s+(HIT|TOUCHED|REACHED|DONE|TAKEN|CLOSED)", t)
    )


def is_stop_loss_hit_phrase(text: str) -> bool:
    t = update_match_text(text)
    return bool(
        re.search(r"\b(SL|STOP|STOPLOSS|STOP\s*LOSS)\b.{0,35}\b(HIT|TOUCHED|REACHED|DONE|TAKEN|CLOSED|GOT\s+HIT)\b", t)
        or re.search(r"\b(HIT|TOUCHED|REACHED|DONE|TAKEN|CLOSED)\b.{0,35}\b(SL|STOP|STOPLOSS|STOP\s*LOSS)\b", t)
        or re.search(r"\b(STOPPED\s+OUT|STOP\s+OUT|STOP\s+PRESO|LOSS\s+TAKEN|LOSS\s+HIT)\b", t)
        or re.search(r"\b(SL|STOP|STOPLOSS|STOP\s*LOSS)\b\s+(HAS\s+BEEN|HAS|WAS|GOT|IS)\s+(HIT|TOUCHED|REACHED|DONE|TAKEN|CLOSED)", t)
    )


def is_entry_triggered_phrase(text: str) -> bool:
    t = update_match_text(text)
    return bool(
        re.search(r"\b(ENTRY|ORDER|TRADE|POSITION)\b.{0,30}\b(TRIGGERED|ACTIVATED|FILLED|EXECUTED|OPENED|ACTIVE)\b", t)
        or re.search(r"\b(TRIGGERED|ACTIVATED|FILLED|EXECUTED|OPENED)\b.{0,30}\b(ENTRY|ORDER|TRADE|POSITION)\b", t)
        or re.search(r"\b(WE\s+ARE\s+IN|IN\s+TRADE|IN\s+THE\s+TRADE|TRADE\s+IS\s+LIVE|ORDER\s+LIVE)\b", t)
    )


def is_cancel_update_phrase(text: str) -> bool:
    t = update_match_text(text)
    return bool(
        re.search(r"\b(CANCEL|CANCELLED|INVALID|REMOVE|REMOVED|DELETE|DELETED)\b.{0,25}\b(TRADE|SIGNAL|ORDER|SETUP|PENDING)?\b", t)
        or re.search(r"\b(NO\s+TRADE|DONT\s+ENTER|DO\s+NOT\s+ENTER|FORGET\s+IT|SKIP\s+THIS|SKIP\s+TRADE|SETUP\s+INVALID|TRADE\s+INVALID)\b", t)
    )


def is_close_update_phrase(text: str) -> bool:
    t = update_match_text(text)
    return bool(
        re.search(r"\b(CLOSE|EXIT|MANUALLY\s+CLOSE|BOOK)\b.{0,25}\b(NOW|TRADE|FULL|ALL|PROFIT|PROFITS|IT)?\b", t)
        or re.search(r"\b(FULL\s+CLOSE|CLOSE\s+IT|CLOSE\s+EVERYTHING|ALL\s+CLOSED|BOOKED\s+PROFIT|PROFIT\s+BOOKED)\b", t)
    )


def is_partial_update_phrase(text: str) -> bool:
    t = update_match_text(text)
    return bool(
        re.search(r"\b(PARTIAL|PARTIALS|HALF|50%)\b", t)
        or re.search(r"\b(CLOSE|SECURE|TAKE|BANK|BOOK)\b.{0,25}\b(SOME|PARTIAL|HALF|50)\b", t)
        or re.search(r"\b(SOME\s+PROFIT|PROFIT\s+SECURED|BANKED\s+SOME|BOOKED\s+SOME)\b", t)
    )


def phrase_bank_update(raw: str, pips_part: str | None):
    """
    Extra safety net for messy provider wording.
    Runs inside classify_signal_update_text after setup/promo blocking.
    """
    t = update_match_text(raw)

    def has_any(phrases):
        return any(p in t for p in phrases)

    extra = f" {pips_part}" if pips_part else ""

    be_hit_phrases = [
        "BE HAS BEEN HIT", "BE BEEN HIT", "BE GOT HIT", "BE WAS HIT", "BE IS HIT",
        "BREAKEVEN HAS BEEN HIT", "BREAKEVEN GOT HIT", "BREAKEVEN WAS HIT", "BREAKEVEN IS HIT",
        "BREAK EVEN HAS BEEN HIT", "BREAK EVEN GOT HIT", "BREAK EVEN WAS HIT",
        "BE TOUCHED", "BREAKEVEN TOUCHED", "BREAK EVEN TOUCHED",
        "BE REACHED", "BREAKEVEN REACHED", "BREAK EVEN REACHED",
        "BACK TO BE", "BACK TO BREAKEVEN", "BACK TO BREAK EVEN",
        "CLOSED AT BE", "CLOSE AT BE", "SCRATCHED", "NO LOSS", "NO LOSS TRADE",
    ]

    set_be_phrases = [
        "SET BE", "SET TO BE", "SET SL BE", "SET SL TO BE",
        "SET BREAKEVEN", "SET TO BREAKEVEN", "SET BREAK EVEN", "SET TO BREAK EVEN",
        "MOVE BE", "MOVE TO BE", "MOVE SL BE", "MOVE SL TO BE",
        "MOVE BREAKEVEN", "MOVE SL TO BREAKEVEN", "MOVE SL TO BREAK EVEN",
        "SL TO ENTRY", "SL AT ENTRY", "STOP TO ENTRY", "STOPLOSS TO ENTRY",
        "MAKE IT RISK FREE", "RISK FREE NOW", "SET RISK FREE",
        "PROTECT TRADE", "PROTECT POSITION", "SECURE ENTRY",
    ]

    sl_hit_phrases = [
        "SL HAS BEEN HIT", "SL BEEN HIT", "SL GOT HIT", "SL WAS HIT", "SL IS HIT",
        "STOP HAS BEEN HIT", "STOP GOT HIT", "STOP WAS HIT", "STOP IS HIT",
        "STOP LOSS HAS BEEN HIT", "STOP LOSS GOT HIT", "STOP LOSS WAS HIT",
        "STOPLOSS HAS BEEN HIT", "STOPLOSS GOT HIT",
        "SL TOUCHED", "STOP TOUCHED", "STOP LOSS TOUCHED", "STOPLOSS TOUCHED",
        "SL REACHED", "STOP REACHED", "STOP LOSS REACHED", "STOPLOSS REACHED",
        "SL TAKEN", "STOP TAKEN", "STOP LOSS TAKEN", "LOSS TAKEN",
        "STOPPED OUT", "STOP OUT", "LOSS BOOKED", "TRADE STOPPED",
    ]

    entry_phrases = [
        "ENTRY ACTIVE", "ENTRY ACTIVATED", "ENTRY FILLED", "ENTRY EXECUTED",
        "ORDER ACTIVE", "ORDER ACTIVATED", "ORDER FILLED", "ORDER EXECUTED",
        "TRADE ACTIVE", "TRADE LIVE", "TRADE IS LIVE", "POSITION OPEN",
        "WE ARE IN", "WE IN", "NOW IN", "IN TRADE", "IN THE TRADE",
        "TRIGGERED IN", "FILLED NOW", "ENTERED NOW",
    ]

    close_phrases = [
        "CLOSE IT", "CLOSE EVERYTHING", "CLOSE ALL", "CLOSE FULL", "FULL CLOSE",
        "EXIT NOW", "EXIT TRADE", "EXIT HERE", "ALL OUT", "GET OUT",
        "BOOK PROFITS", "BOOK PROFIT", "PROFIT BOOKED", "TAKE PROFIT NOW",
        "SECURE ALL", "BANK ALL", "CLOSE MANUALLY",
    ]

    partial_phrases = [
        "TAKE PARTIAL", "TAKE PARTIALS", "PARTIAL TAKEN", "PARTIAL SECURED",
        "SECURE PARTIAL", "SECURE PARTIALS", "BOOK PARTIAL", "BOOK PARTIALS",
        "BANK PARTIAL", "BANK PARTIALS", "BANK SOME", "BOOK SOME",
        "SECURE SOME", "TAKE SOME", "CLOSE HALF", "CLOSE 50",
        "CLOSE 50%", "HALF CLOSE", "50% CLOSE",
    ]

    cancel_phrases = [
        "CANCEL THIS", "CANCEL TRADE", "CANCEL SIGNAL", "CANCEL ORDER",
        "TRADE CANCELLED", "SIGNAL CANCELLED", "ORDER CANCELLED",
        "SETUP INVALID", "TRADE INVALID", "SIGNAL INVALID", "ORDER INVALID",
        "DO NOT ENTER", "DONT ENTER", "NO ENTRY", "NO TRADE",
        "FORGET THIS", "FORGET IT", "SKIP THIS", "SKIP TRADE",
        "REMOVE ORDER", "DELETE ORDER", "PENDING CANCELLED",
    ]

    if has_any(cancel_phrases):
        return {
            "type": "CANCEL",
            "status": "CANCELLED",
            "text": f"<b>SIGNAL CANCELLED / REMOVED {EMOJI_DIAMOND}</b>",
        }

    if has_any(sl_hit_phrases):
        return {
            "type": "SL_HIT",
            "status": "SL_HIT",
            "text": f"<b>STOP LOSS HIT{extra} {EMOJI_CROSS}</b>",
        }

    if has_any(be_hit_phrases):
        return {
            "type": "BE_HIT",
            "status": "BREAKEVEN_HIT",
            "text": f"<b>BREAKEVEN HIT{extra} {EMOJI_DIAMOND}</b>",
        }

    if has_any(set_be_phrases):
        return {
            "type": "MOVE_SL",
            "status": "SL_TO_BE",
            "text": f"<b>SL MOVED TO BREAKEVEN{extra} {EMOJI_DIAMOND}</b>",
        }

    if has_any(entry_phrases):
        return {
            "type": "ENTRY_TRIGGERED",
            "status": "ENTRY_TRIGGERED",
            "text": f"<b>ENTRY TRIGGERED{extra} {EMOJI_DIAMOND}</b>",
        }

    if has_any(close_phrases) and not extract_update_tp_nums(raw):
        return {
            "type": "CLOSE",
            "status": "CLOSED_MANUAL",
            "text": f"<b>CLOSE TRADE NOW{extra} {EMOJI_DIAMOND}</b>",
        }

    if has_any(partial_phrases) and not extract_update_tp_nums(raw):
        return {
            "type": "PARTIAL",
            "status": "PARTIAL_PROFIT",
            "text": f"<b>PARTIAL PROFITS SECURED{extra} {EMOJI_DIAMOND}</b>",
        }

    return None


def classify_signal_update_text(text):
    raw = clean_update_body(text)
    if not raw:
        return None

    t = update_match_text(raw)
    low = raw.lower()

    # Important: if provider says old trade hit SL then gives next trade,
    # do NOT treat it as an update. Let extractor parse the next setup.
    if has_next_trade_context(raw) and looks_like_new_setup_inside_update(raw):
        return None

    # Avoid fresh setups/recaps/promos.
    has_new_setup = bool(re.search(r"\b(BUY|SELL|LONG|SHORT)\b", t)) and bool(re.search(r"\b(SL|STOP|STOPLOSS|STOP\s*LOSS)\b\s*[:@\-]?\s*\d", t))
    if has_new_setup:
        return None

    blocked = [
        "DAILY RECAP",
        "WEEKLY RECAP",
        "RESULTS",
        "ALL TRADES",
        "CLIENT",
        "INSTAGRAM",
        "FREE LIFE",
        "EBOOK",
        "SCHOOL",
        "MT5 GUIDE",
        "CLICK WHAT YOU NEED",
    ]

    if any(x in t for x in blocked):
        return None

    pips = extract_update_pips(raw)
    pips_part = pips[0] if len(pips) == 1 else None

    phrase_bank = phrase_bank_update(raw, pips_part)
    if phrase_bank:
        return phrase_bank

    # Broad phrase catchers for messy provider updates.
    if is_cancel_update_phrase(raw):
        return {
            "type": "CANCEL",
            "status": "CANCELLED",
            "text": f"<b>SIGNAL CANCELLED / REMOVED {EMOJI_DIAMOND}</b>",
        }

    if is_stop_loss_hit_phrase(raw):
        extra = f" {pips_part}" if pips_part else ""
        return {
            "type": "SL_HIT",
            "status": "SL_HIT",
            "text": f"<b>STOP LOSS HIT{extra} {EMOJI_CROSS}</b>",
        }

    if is_breakeven_hit_phrase(raw):
        return {
            "type": "BE_HIT",
            "status": "BREAKEVEN_HIT",
            "text": f"<b>BREAKEVEN HIT {EMOJI_DIAMOND}</b>",
        }

    if is_entry_triggered_phrase(raw):
        return {
            "type": "ENTRY_TRIGGERED",
            "status": "ENTRY_TRIGGERED",
            "text": f"<b>ENTRY TRIGGERED {EMOJI_DIAMOND}</b>",
        }

    if is_close_update_phrase(raw) and not extract_update_tp_nums(raw):
        extra = f" {pips_part}" if pips_part else ""
        return {
            "type": "CLOSE",
            "status": "CLOSED_MANUAL",
            "text": f"<b>CLOSE TRADE NOW{extra} {EMOJI_DIAMOND}</b>",
        }

    if is_partial_update_phrase(raw) and not extract_update_tp_nums(raw):
        extra = f" {pips_part}" if pips_part else ""
        return {
            "type": "PARTIAL",
            "status": "PARTIAL_PROFIT",
            "text": f"<b>PARTIAL PROFITS SECURED{extra} {EMOJI_DIAMOND}</b>",
        }

    # Cancel / invalid / remove pending
    if re.search(r"\b(CANCEL|CANCELLED|INVALID|REMOVE|REMOVED|DELETE|DELETED|NO\s+TRADE|DON'?T\s+ENTER|DO\s+NOT\s+ENTER)\b", t):
        return {
            "type": "CANCEL",
            "status": "CANCELLED",
            "text": f"<b>SIGNAL CANCELLED / REMOVED {EMOJI_DIAMOND}</b>",
        }

    # Stop loss hit
    if re.search(r"\b(SL\s+HIT|HIT\s+SL|STOPLOSS\s+HIT|STOP\s*LOSS\s+HIT|HIT\s+STOPLOSS|HIT\s+STOP\s*LOSS|STOP\s+PRESO|STOPPED\s+OUT|STOP\s+HIT|HIT\s+STOP|SL\s+DONE|LOSS\s+HIT|STOPPED)\b", t):
        extra = f" {pips_part}" if pips_part else ""
        return {
            "type": "SL_HIT",
            "status": "SL_HIT",
            "text": f"<b>STOP LOSS HIT{extra} {EMOJI_CROSS}</b>",
        }

    # TP hit combined with BE instruction, e.g. "TP2 HIT SET BE"
    early_tp_nums = extract_update_tp_nums(raw)
    if early_tp_nums and has_set_be_instruction(raw):
        tp_part = " ".join(f"TP{n}" for n in early_tp_nums) + " HIT"
        extra = f" {pips_part}" if pips_part else ""
        return {
            "type": "TP_HIT_SET_BE",
            "status": "TP_HIT_SL_TO_BE",
            "text": f"<b>{tp_part}{extra} {EMOJI_DIAMOND}</b>\n<b>SL MOVED TO BREAKEVEN {EMOJI_DIAMOND}</b>",
        }

    # Bare breakeven instruction, e.g. "SET BE", "SET BREAKEVEN"
    if has_set_be_instruction(raw):
        return {
            "type": "MOVE_SL",
            "status": "SL_TO_BE",
            "text": f"<b>SL MOVED TO BREAKEVEN {EMOJI_DIAMOND}</b>",
        }

    # Breakeven hit
    if re.search(r"\b(BE\s+HIT|BREAKEVEN\s+HIT|BREAK\s*EVEN\s+HIT|HIT\s+BE)\b", t):
        return {
            "type": "BE_HIT",
            "status": "BREAKEVEN_HIT",
            "text": f"<b>BREAKEVEN HIT {EMOJI_DIAMOND}</b>",
        }

    # Move SL / protect
    m = re.search(r"\b(?:MOVE|MOVED|CHANGE|CHANGED|UPDATE|UPDATED|SET)\s+(?:YOUR\s+)?(?:SL|S/L|STOP|STOPLOSS|STOP\s*LOSS)\s*(?:TO|AT)?\s*[:@\-]?\s*(BE|BREAKEVEN|BREAK\s*EVEN|ENTRY|ENTRIES|\d{1,7}(?:\.\d+)?)\b", t)
    if not m:
        m = re.search(r"\b(?:SL|S/L|STOP|STOPLOSS|STOP\s*LOSS)\s*(?:TO|AT|MOVED\s+TO|UPDATED\s+TO)\s*[:@\-]?\s*(BE|BREAKEVEN|BREAK\s*EVEN|ENTRY|ENTRIES|\d{1,7}(?:\.\d+)?)\b", t)

    if m:
        target = m.group(1).replace("BREAK EVEN", "BREAKEVEN")
        if target in ("BE", "BREAKEVEN", "ENTRY", "ENTRIES"):
            return {
                "type": "MOVE_SL",
                "status": "SL_TO_BE",
                "text": f"<b>SL MOVED TO BREAKEVEN {EMOJI_DIAMOND}</b>",
            }
        return {
            "type": "MOVE_SL",
            "status": "SL_UPDATED",
            "price": target,
            "text": f"<b>SL UPDATED TO {target} {EMOJI_DIAMOND}</b>",
        }

    # Entry activated / order triggered
    if re.search(r"\b(ACTIVATED|ENTRY\s+HIT|ENTRY\s+TRIGGERED|TRIGGERED|ORDER\s+FILLED|ENTERED)\b", t):
        return {
            "type": "ENTRY_TRIGGERED",
            "status": "ENTRY_TRIGGERED",
            "text": f"<b>ENTRY TRIGGERED {EMOJI_DIAMOND}</b>",
        }

    # Close / secure trade
    if re.search(r"\b(CLOSE\s+NOW|CLOSE\s+TRADE|CLOSE\s+FULL|CLOSE\s+ALL|MANUALLY\s+CLOSE|TAKE\s+PROFIT\s+NOW|EXIT\s+NOW|EXIT\s+TRADE|CLOSE\s+IT|FULL\s+CLOSE|BOOK\s+PROFIT|BOOK\s+PROFITS)\b", t):
        extra = f" {pips_part}" if pips_part else ""
        return {
            "type": "CLOSE",
            "status": "CLOSED_MANUAL",
            "text": f"<b>CLOSE TRADE NOW{extra} {EMOJI_DIAMOND}</b>",
        }

    # Partial close / secure partial
    if re.search(r"\b(PARTIAL|PARTIALS|CLOSE\s+HALF|CLOSE\s+50|SECURE\s+SOME|SECURED\s+SOME|TAKE\s+SOME|TAKE\s+PARTIAL|TAKE\s+PROFIT\s+PARTIAL|BANK\s+SOME|BOOK\s+SOME)\b", t):
        extra = f" {pips_part}" if pips_part else ""
        return {
            "type": "PARTIAL",
            "status": "PARTIAL_PROFIT",
            "text": f"<b>PARTIAL PROFITS SECURED{extra} {EMOJI_DIAMOND}</b>",
        }

    # TP hit / target hit
    tp_nums = extract_update_tp_nums(raw)
    if tp_nums:
        tp_part = " ".join(f"TP{n}" for n in tp_nums) + " HIT"
        extra = f" {pips_part}" if pips_part else ""
        return {
            "type": "TP_HIT",
            "status": "TP_HIT",
            "text": f"<b>{tp_part}{extra} {EMOJI_DIAMOND}</b>",
        }

    # Pips-only running profit
    if pips_part:
        return {
            "type": "PIPS",
            "status": "RUNNING_PROFIT",
            "text": f"<b>{pips_part} {EMOJI_DIAMOND}</b>",
        }

    # Hold / running
    if re.search(r"\b(HOLD|HOLDING|RUNNING|STILL\s+RUNNING|LET\s+IT\s+RUN|KEEP\s+RUNNING|RUNNER)\b", t):
        return {
            "type": "RUNNING",
            "status": "RUNNING",
            "text": f"<b>TRADE STILL RUNNING {EMOJI_DIAMOND}</b>",
        }

    return None


def format_signal_update_text(text):
    update = classify_signal_update_text(text)
    if not update:
        return None
    return update.get("text")


async def maybe_send_signal_update_reply(message, key, text):
    update_text = format_signal_update_text(text)
    if not update_text:
        return False

    source_id, packet_ids = packet_for_reply_source(message, key)
    if not packet_ids:
        return False

    ai_msg_id = packet_ids[1]

    if should_skip_duplicate_update_reply(key, ai_msg_id, update_text):
        log.info(f"[signal update skipped] duplicate legacy update key={key} ai_msg={ai_msg_id} text={update_text}")
        return True

    sent = await send_message_with_retry(
        SIGNAL_DEST_CHAT,
        update_text,
        parse_mode="html",
        link_preview=False,
        reply_to=ai_msg_id,
    )

    log.info(f"[signal update sent] source_reply={message.id} replied_source={source_id} ai_msg={ai_msg_id} update_msg={getattr(sent, 'id', None)} text={update_text}")
    return True



def lifecycle_key_for(message, key):
    return f"{SIGNAL_SOURCE_CHAT}:{key}:{message.id}"


def norm_price_for_dedupe(symbol, value):
    try:
        v = float(value)
    except Exception:
        return "0"

    symbol = str(symbol or "").upper()

    if symbol in ("XAUUSD", "GOLD"):
        return f"{round(v / 1.0) * 1.0:.1f}"

    if symbol in ("BTCUSD", "BTC"):
        return f"{round(v / 50.0) * 50.0:.0f}"

    if symbol in ("NAS100", "US100", "NASDAQ", "US30", "US500"):
        return f"{round(v / 10.0) * 10.0:.0f}"

    if v < 10:
        return f"{v:.4f}"

    return f"{v:.2f}"


def content_signature_for(result, key):
    parsed = result.get("parsed") or {}

    symbol = str(parsed.get("symbol", ""))
    direction = str(parsed.get("direction", ""))
    order_type = str(parsed.get("order_type", ""))

    tps = parsed.get("tps") or []
    tp_key = ",".join(norm_price_for_dedupe(symbol, x) for x in tps[:3])

    parts = [
        str(key),
        symbol,
        direction,
        order_type,
        norm_price_for_dedupe(symbol, parsed.get("entry_low", 0)),
        norm_price_for_dedupe(symbol, parsed.get("entry_high", 0)),
        norm_price_for_dedupe(symbol, parsed.get("sl", 0)),
        tp_key,
    ]

    return hashlib.sha256("|".join(parts).encode("utf-8", errors="ignore")).hexdigest()


def remember_lifecycle(message, key, result, sent_messages):
    parsed = result.get("parsed") or {}
    ids = [getattr(m, "id", None) for m in sent_messages if getattr(m, "id", None)]

    rec = {
        "source_msg_id": int(message.id),
        "topic": int(key),
        "created_ts": time.time(),
        "last_update_ts": time.time(),
        "dest_ids": ids,
        "original_dest_id": ids[0] if len(ids) > 0 else None,
        "ai_dest_id": ids[1] if len(ids) > 1 else None,
        "source_dest_id": ids[2] if len(ids) > 2 else None,
        "status": "OPEN",
        "symbol": parsed.get("symbol"),
        "direction": parsed.get("direction"),
        "order_type": parsed.get("order_type"),
        "entry_low": parsed.get("entry_low"),
        "entry_high": parsed.get("entry_high"),
        "sl": parsed.get("sl"),
        "tps": parsed.get("tps") or [],
        "updates": [],
    }

    active_signal_lifecycle[lifecycle_key_for(message, key)] = rec
    last_active_signal_by_topic[int(key)] = rec

    save_signal_lifecycle()

    log.info(f"[lifecycle open] topic={key} source={message.id} ai_dest={rec['ai_dest_id']}")


def move_update_from_text(text):
    raw = text or ""
    t = update_match_text(raw)

    # Do not steal messages that contain a fresh next trade.
    if has_next_trade_context(raw) and looks_like_new_setup_inside_update(raw):
        return None

    if re.search(r"\b(CANCEL|CANCELLED|REMOVE|REMOVED|INVALID|DELETE|DELETED|NO\s+TRADE|DON'?T\s+ENTER|DO\s+NOT\s+ENTER)\b", t):
        return {"type": "CANCEL", "status": "CANCELLED", "text": f"<b>SIGNAL CANCELLED / REMOVED {EMOJI_DIAMOND}</b>"}

    if not re.search(r"\b(MOVE|MOVED|CHANGE|CHANGED|UPDATE|UPDATED|ENTERED|ENTRY|ENTRIES|LIMIT)\b", t):
        return None

    m = re.search(r"\b(?:MOVE|MOVED|CHANGE|CHANGED|UPDATE|UPDATED)\s+(?:LIMIT|ENTRY|ENTRIES|LEVEL|ZONE)?\s*(?:TO|AT)?\s*[:@\-]?\s*(\d{1,7}(?:\.\d+)?)\b", t)
    if not m:
        m = re.search(r"\b(?:ENTERED|ENTRY|ENTRIES)\s+(?:AT|TO)?\s*[:@\-]?\s*(\d{1,7}(?:\.\d+)?)\b", t)

    if not m:
        return None

    price = m.group(1)
    return {
        "type": "MOVE_ENTRY",
        "status": "ENTRY_UPDATED",
        "price": price,
        "text": f"<b>ENTRY UPDATED TO {price} {EMOJI_DIAMOND}</b>",
    }


def update_reply_signature(key, ai_msg_id, update_text):
    base = "|".join([
        str(key),
        str(ai_msg_id),
        re.sub(r"\s+", " ", str(update_text or "").strip().upper()),
    ])
    return hashlib.sha256(base.encode("utf-8", errors="ignore")).hexdigest()


def should_skip_duplicate_update_reply(key, ai_msg_id, update_text):
    now = time.time()
    cutoff = now - UPDATE_REPLY_DEDUPE_SECONDS

    for sig, ts in list(update_reply_dedupe.items()):
        try:
            if float(ts) < cutoff:
                update_reply_dedupe.pop(sig, None)
        except Exception:
            update_reply_dedupe.pop(sig, None)

    sig = update_reply_signature(key, ai_msg_id, update_text)
    if sig in update_reply_dedupe:
        return True

    update_reply_dedupe[sig] = now
    return False


def lifecycle_status_from_update(direct_update, move_update):
    if move_update:
        return move_update.get("status") or move_update.get("type") or "UPDATED"

    update = classify_signal_update_text(direct_update or "")
    if update:
        return update.get("status") or update.get("type") or "UPDATED"

    du = (direct_update or "").upper()
    if "STOP LOSS HIT" in du:
        return "SL_HIT"
    if "BREAKEVEN" in du:
        return "BREAKEVEN_HIT"
    if "TP" in du and "HIT" in du:
        return "TP_HIT"
    if "PIPS" in du:
        return "RUNNING_PROFIT"
    return "UPDATED"


def safe_float(value):
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def lifecycle_pip_multiplier(symbol: str) -> float:
    """
    Converts price difference into display pips.

    XAUUSD:
    $1 = 10 pips.

    Forex:
    normal pairs = 10000 pips per 1.0
    JPY pairs = 100 pips per 1.0

    Indices/crypto:
    fallback = 1 point = 1 pip-style unit.
    """
    s = str(symbol or "").upper().replace("/", "")

    if s in ("XAU", "XAUUSD", "GOLD"):
        return 10.0

    if len(s) == 6 and s.endswith("JPY"):
        return 100.0

    if len(s) == 6 and any(s.endswith(x) for x in ("USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD")):
        return 10000.0

    return 1.0


def lifecycle_entry_ref(rec) -> float | None:
    direction = str(rec.get("direction") or "").upper()
    lo = safe_float(rec.get("entry_low"))
    hi = safe_float(rec.get("entry_high"))

    if lo is None and hi is None:
        return None

    if lo is None:
        lo = hi
    if hi is None:
        hi = lo

    if direction == "SELL":
        return max(lo, hi)

    if direction == "BUY":
        return min(lo, hi)

    return (lo + hi) / 2


def format_lifecycle_pips(value) -> str:
    try:
        v = float(value)
    except Exception:
        return ""

    sign = "+" if v >= 0 else "-"
    av = abs(v)

    if abs(av - round(av)) < 0.05:
        number = str(int(round(av)))
    else:
        number = f"{av:.1f}".rstrip("0").rstrip(".")

    return f"{sign}{number}PIPS"


def lifecycle_update_pips_from_rec(update_obj, rec, raw_text: str):
    if not update_obj or not rec:
        return None

    utype = str(update_obj.get("type") or "").upper()
    direction = str(rec.get("direction") or "").upper()
    symbol = str(rec.get("symbol") or "").upper()

    entry = lifecycle_entry_ref(rec)
    if entry is None:
        return None

    mult = lifecycle_pip_multiplier(symbol)

    # TP updates: use TP number and original stored TP level.
    if utype in ("TP_HIT", "TP_HIT_SET_BE"):
        nums = extract_update_tp_nums(raw_text) or ["1"]
        tps = rec.get("tps") or []

        for n in nums:
            try:
                idx = int(n) - 1
            except Exception:
                continue

            if idx < 0 or idx >= len(tps):
                continue

            tp = safe_float(tps[idx])
            if tp is None:
                continue

            if direction == "SELL":
                return format_lifecycle_pips((entry - tp) * mult)

            if direction == "BUY":
                return format_lifecycle_pips((tp - entry) * mult)

    # SL hit: calculate negative pips from entry to SL.
    if utype == "SL_HIT":
        sl = safe_float(rec.get("sl"))
        if sl is None:
            return None

        if direction == "SELL":
            return format_lifecycle_pips((entry - sl) * mult)

        if direction == "BUY":
            return format_lifecycle_pips((sl - entry) * mult)

    # BE / set BE / entry triggered are always 0 pips by definition.
    if utype in ("BE_HIT", "MOVE_SL", "ENTRY_TRIGGERED"):
        status = str(update_obj.get("status") or "").upper()
        text = str(update_obj.get("text") or "").upper()

        if "BREAKEVEN" in status or "BREAKEVEN" in text or utype == "ENTRY_TRIGGERED":
            return "+0PIPS"

    return None


def insert_pips_into_update_text(update_text: str, pips: str) -> str:
    if not update_text or not pips:
        return update_text

    if "PIPS" in update_text.upper():
        return update_text

    lines = str(update_text).split("\n")
    if not lines:
        return update_text

    first = lines[0]

    inserted = False
    for emoji in (EMOJI_DIAMOND, EMOJI_CROSS):
        token = f" {emoji}</b>"
        if token in first:
            first = first.replace(token, f" {pips} {emoji}</b>", 1)
            inserted = True
            break

    if not inserted and "</b>" in first:
        first = first.replace("</b>", f" {pips}</b>", 1)

    lines[0] = first
    return "\n".join(lines)


def enrich_update_text_with_lifecycle_pips(update_text: str, update_obj, rec, raw_text: str) -> str:
    if not update_text or not update_obj or not rec:
        return update_text

    if "PIPS" in str(update_text).upper():
        return update_text

    pips = lifecycle_update_pips_from_rec(update_obj, rec, raw_text)
    if not pips:
        return update_text

    enriched = insert_pips_into_update_text(update_text, pips)

    if enriched != update_text:
        log.info(f"[update pips enriched] {update_text!r} -> {enriched!r}")

    return enriched


async def maybe_send_lifecycle_update(message, key, text):
    # First priority: direct reply to known signal packet.
    update_obj = classify_signal_update_text(text)
    direct_update = update_obj.get("text") if update_obj else None
    move_update = move_update_from_text(text)

    if not direct_update and not move_update:
        return False

    source_id, packet_ids = packet_for_reply_source(message, key)

    rec = None

    if source_id:
        rec = active_signal_lifecycle.get(f"{SIGNAL_SOURCE_CHAT}:{key}:{source_id}")

    if not rec:
        rec = last_active_signal_by_topic.get(int(key))

    if not rec:
        log.info(f"[signal update no lifecycle] key={key} msg={message.id} text={text[:100]!r}")
        return False

    ai_msg_id = rec.get("ai_dest_id")
    if not ai_msg_id:
        log.info(f"[signal update no ai target] key={key} msg={message.id} rec={rec}")
        return False

    update_text = direct_update or move_update["text"]
    update_text = enrich_update_text_with_lifecycle_pips(update_text, update_obj or move_update, rec, text)

    if should_skip_duplicate_update_reply(key, ai_msg_id, update_text):
        log.info(f"[signal update skipped] duplicate update key={key} ai_msg={ai_msg_id} text={update_text}")
        return True

    sent = await send_message_with_retry(
        SIGNAL_DEST_CHAT,
        update_text,
        parse_mode="html",
        link_preview=False,
        reply_to=int(ai_msg_id),
    )

    rec["last_update_ts"] = time.time()
    rec.setdefault("updates", []).append({
        "source_msg_id": int(message.id),
        "dest_msg_id": int(getattr(sent, "id", 0) or 0),
        "text": update_text,
        "ts": time.time(),
    })

    new_status = lifecycle_status_from_update(text, move_update)
    if new_status:
        rec["status"] = new_status

    if move_update and move_update.get("type") == "MOVE_ENTRY":
        try:
            rec["entry_low"] = float(move_update["price"])
            rec["entry_high"] = float(move_update["price"])
        except Exception:
            pass

    if move_update and move_update.get("type") == "MOVE_SL":
        try:
            rec["sl"] = float(move_update["price"])
        except Exception:
            pass

    if rec.get("status") not in ("CANCELLED", "SL_HIT", "CLOSED", "CLOSED_MANUAL"):
        try:
            last_active_signal_by_topic[int(key)] = rec
        except Exception:
            pass

    save_signal_lifecycle()

    log.info(f"[lifecycle update sent] topic={key} source_msg={message.id} ai_msg={ai_msg_id} status={rec.get('status')} text={update_text}")
    return True


def source_name_for(message):
    topic_id = topic_id_of(message)
    return topic_label(topic_id)


def buffer_key(message):
    topic_id = topic_id_of(message)
    return str(topic_id or "main")



def load_packet_map():
    if not SIGNAL_PACKET_MAP_FILE.exists():
        return {}
    try:
        return json.loads(SIGNAL_PACKET_MAP_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_packet_map():
    try:
        tmp = SIGNAL_PACKET_MAP_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(signal_packet_map), encoding="utf-8")
        tmp.replace(SIGNAL_PACKET_MAP_FILE)
    except Exception as exc:
        log.warning(f"[signal packet map save failed] {exc}")


def signal_packet_key(message, key):
    return f"{SIGNAL_SOURCE_CHAT}:{key}:{getattr(message, 'id', None)}"


async def delete_existing_signal_packet(message, key):
    global signal_packet_map

    if "signal_packet_map" not in globals():
        signal_packet_map = load_packet_map()
        log.warning("[signal packet map recovery] initialized missing signal_packet_map")

    if not DELETE_OLD_SIGNAL_PACKET_ON_EDIT:
        return False

    pkey = signal_packet_key(message, key)
    old_ids = signal_packet_map.get(pkey) or []

    if not old_ids:
        return False

    try:
        ids = [int(x) for x in old_ids if x]
        if ids:
            await client.delete_messages(SIGNAL_DEST_CHAT, ids)
            log.info(f"[signal packet cleanup] deleted old packet source_msg={message.id} dest_ids={ids}")
        signal_packet_map.pop(pkey, None)
        save_packet_map()
        return True
    except Exception as exc:
        log.warning(f"[signal packet cleanup failed] source_msg={message.id} ids={old_ids}: {exc}")
        return False


def remember_signal_packet(message, key, sent_messages):
    global signal_packet_map

    if "signal_packet_map" not in globals():
        signal_packet_map = load_packet_map()
        log.warning("[signal packet map recovery] initialized missing signal_packet_map in remember")

    pkey = signal_packet_key(message, key)
    ids = []

    for msg in sent_messages:
        mid = getattr(msg, "id", None)
        if mid:
            ids.append(int(mid))

    if ids:
        signal_packet_map[pkey] = ids
        save_packet_map()
        log.info(f"[signal packet mapped] source_msg={message.id} -> dest_ids={ids}")



signal_packet_map = load_packet_map()

def recover_lifecycle_from_packet_map_if_empty():
    """
    Minimal recovery:
    packet map has source_msg -> [original_dest_id, ai_dest_id, source_line_id].
    Even without parsed levels, this is enough to reply updates to latest AI message.
    """
    global active_signal_lifecycle, last_active_signal_by_topic

    if active_signal_lifecycle:
        return 0

    recovered = 0

    try:
        items = list(signal_packet_map.items())
    except Exception:
        return 0

    for pkey, ids in items:
        try:
            parts = str(pkey).split(":")
            if len(parts) < 3:
                continue

            topic = int(parts[-2])
            source_msg_id = int(parts[-1])

            if not isinstance(ids, list) or len(ids) < 2:
                continue

            dest_ids = [int(x) for x in ids if x]
            if len(dest_ids) < 2:
                continue

            rec = {
                "source_msg_id": source_msg_id,
                "topic": topic,
                "created_ts": time.time(),
                "last_update_ts": time.time(),
                "dest_ids": dest_ids,
                "original_dest_id": dest_ids[0],
                "ai_dest_id": dest_ids[1],
                "source_dest_id": dest_ids[2] if len(dest_ids) > 2 else None,
                "status": "OPEN",
                "symbol": None,
                "direction": None,
                "order_type": None,
                "entry_low": None,
                "entry_high": None,
                "sl": None,
                "tps": [],
                "updates": [],
                "recovered_from_packet_map": True,
            }

            active_signal_lifecycle[pkey] = rec
            last_active_signal_by_topic[topic] = rec
            recovered += 1

        except Exception as exc:
            log.warning(f"[lifecycle packet recovery item failed] pkey={pkey}: {exc}")

    if recovered:
        save_signal_lifecycle()
        log.info(f"[lifecycle packet recovery] recovered={recovered}")

    return recovered


recovered_lifecycle_records = recover_lifecycle_from_packet_map_if_empty()



def trim_buffer(key):
    now = time.time()
    dq = buffers[key]
    while dq and now - dq[0]["ts"] > BUFFER_WINDOW_SECONDS:
        dq.popleft()


def combined_text_for(key):
    trim_buffer(key)
    return "\n".join(item["text"] for item in buffers[key] if item["text"].strip())


def first_buffer_message(key):
    trim_buffer(key)
    if buffers[key]:
        return buffers[key][0].get("message")
    return None


def looks_like_fresh_signal_start(text: str) -> bool:
    """Detect a new setup so stale incomplete buffers do not swallow the next signal."""
    t = update_match_text(text)
    has_direction = bool(re.search(r"\b(BUY|BUYS|BUYING|SELL|SELLS|SELLING|LONG|LONGS|SHORT|SHORTS)\b", t))
    has_entry = bool(re.search(rf"\b(ENTRY|ENTRIES|ENTER|ENTERING)\b[^\n]{{0,40}}{PRICE_RE}", t))
    has_symbol = bool(re.search(r"\b(XAUUSD|XAGUSD|GOLD|SILVER|BTC|ETH|SOL|NAS100|NASDAQ|US100|US30|US500|GER40|DAX|[A-Z]{6})\b", t))
    return has_direction and (has_entry or has_symbol)



def extract_tp_context_from_result(result):
    parsed = result.get("parsed") or {}
    tps = parsed.get("tps") or []
    lines = []

    for i, tp in enumerate(tps[:8], start=1):
        try:
            lines.append(f"TP{i} {float(tp):.5f}".rstrip("0").rstrip("."))
        except Exception:
            pass

    if lines:
        return "\n".join(lines)
    return ""


def remember_last_signal_context(key, result):
    tp_text = extract_tp_context_from_result(result)
    if tp_text:
        last_signal_context[key] = {
            "ts": time.time(),
            "tp_text": tp_text,
            "result": result,
        }


def apply_same_as_above_context(key, text):
    t = update_match_text(text)
    if "SAME AS ABOVE" not in t:
        return text

    ctx = last_signal_context.get(key) or {}
    tp_text = ctx.get("tp_text") or ""

    if not tp_text:
        return text

    log.info(f"[signal hub context] applying previous TP context key={key}")
    return text + "\n" + tp_text


def looks_like_partial_signal_piece(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False

    t = update_match_text(raw)

    # Management/recap-only messages should not enter the signal buffer.
    recap_only = bool(re.search(r"\b(TP\s*\d*\s*HIT|PIPS?\s*[✅🎯💥🔥]?|BE\s*HIT|SL\s*TO\s*BE|CLOSE|CLOSED|CANCEL|INVALID|STILL\s+RUNNING)\b", t))
    has_setup_word = bool(re.search(r"\b(BUY|SELL|LONG|SHORT|ENTRY|ENTER|SL|STOP|TP|TARGET)\b", t))
    if recap_only and not has_setup_word:
        return False

    if re.search(r"\b(BUY|BUYS|BUYING|SELL|SELLS|SELLING|LONG|LONGS|SHORT|SHORTS|ENTER)\b", t):
        return True
    if re.search(r"\b(SL|S/L|STOP|STOPLOSS|STOP\s*LOSS|TP|TARGET|TAKE\s*PROFIT)\b", t):
        return True
    if re.search(rf"^\s*{PRICE_RE}\s*(?:-|:|/)\s*{PRICE_RE}\s*$", t, re.M):
        return True
    if "SAME AS ABOVE" in t:
        return True
    if re.search(r"\bENTERED\s+AT\s+\d", t):
        return True

    return False


def remember_content_signature(sig):
    if sig in sent_content_signature_set:
        return False

    sent_content_signature_set.add(sig)
    sent_content_signatures.append(sig)

    while len(sent_content_signature_set) > len(sent_content_signatures):
        sent_content_signature_set.clear()
        sent_content_signature_set.update(sent_content_signatures)

    save_content_dedupe_signatures()
    return True


def remember_signature(sig):
    if sig in sent_signature_set:
        return False
    sent_signature_set.add(sig)
    sent_signatures.append(sig)
    while len(sent_signature_set) > len(sent_signatures):
        sent_signature_set.clear()
        sent_signature_set.update(sent_signatures)
    return True


def signature_for(result, key, source_msg_id=None):
    """
    Duplicate protection must not block fresh Telegram messages.
    Include source_msg_id so repeated similar setups can still be sent.
    Edits keep the same source_msg_id, so old packets can still be replaced.
    """
    parsed = result.get("parsed") or {}
    base = "|".join([
        str(key),
        str(source_msg_id or ""),
        str(parsed.get("symbol", "")),
        str(parsed.get("direction", "")),
        str(parsed.get("entry_low", "")),
        str(parsed.get("entry_high", "")),
        str(parsed.get("sl", "")),
        ",".join(str(x) for x in parsed.get("tps", [])[:8]) if isinstance(parsed.get("tps"), list) else "",
    ])

    if base.count("|") < 6:
        base = f"{key}|{source_msg_id or ''}|{result.get('message', '')}"

    return hashlib.sha256(base.encode("utf-8", errors="ignore")).hexdigest()


def is_real_signal_media(message):
    media = getattr(message, "media", None)
    if not media:
        return False

    # Avoid treating webpage previews as real signal screenshots.
    if media.__class__.__name__ == "MessageMediaWebPage":
        return False

    return True


async def send_ai_formatted_signal(message, result, reply_to_id):
    """
    If the provider signal has a real image/video/document attached,
    send the AI formatted signal as a media message with the custom HTML caption.
    This keeps the screenshot + animated tg-emoji/custom formatted message together.
    """
    formatted = result["message"]

    if AI_FORMATTED_SIGNAL_WITH_MEDIA and is_real_signal_media(message):
        try:
            sent = await client.send_file(
                SIGNAL_DEST_CHAT,
                message.media,
                caption=formatted,
                parse_mode="html",
                link_preview=False,
                reply_to=reply_to_id,
            )
            log.info(f"[signal hub ai media sent] source_msg={getattr(message, 'id', None)} ai_msg={getattr(sent, 'id', None)}")
            return sent

        except Exception as exc:
            log.warning(
                f"[signal hub ai media send failed] source_msg={getattr(message, 'id', None)} "
                f"falling back to text AI message: {type(exc).__name__}: {exc}"
            )

    sent = await send_message_with_retry(
        SIGNAL_DEST_CHAT,
        formatted,
        parse_mode="html",
        link_preview=False,
        reply_to=reply_to_id,
    )

    return sent


async def forward_original(message, text):
    """
    Anonymous original copy only.

    Never use Telegram forward_messages here, because forwards can expose
    the forwarded-from/source header. This sends a fresh copied message instead.
    """
    if not FORWARD_SIGNAL_CANDIDATES:
        return None

    text = text or ""
    entities = getattr(message, "entities", None)

    if is_real_signal_media(message):
        try:
            sent = await client.send_file(
                SIGNAL_DEST_CHAT,
                message.media,
                caption=text if text else None,
                formatting_entities=entities if text else None,
                parse_mode=None,
            )
            log.info(f"[signal hub original anonymous media copied] msg={message.id} topic={topic_id_of(message)}")
            return sent
        except Exception as exc:
            log.warning(f"[signal hub original anonymous media direct copy failed] msg={message.id}: {type(exc).__name__}: {exc}")

        cache_dir = DATA_DIR / "ai_original_media_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        downloaded = None

        try:
            downloaded = await message.download_media(file=str(cache_dir / f"original_{message.id}"))
            if downloaded:
                sent = await client.send_file(
                    SIGNAL_DEST_CHAT,
                    downloaded,
                    caption=text if text else None,
                    formatting_entities=entities if text else None,
                    parse_mode=None,
                )
                log.info(f"[signal hub original anonymous media reuploaded] msg={message.id} topic={topic_id_of(message)}")
                return sent
        finally:
            if downloaded:
                try:
                    Path(downloaded).unlink(missing_ok=True)
                except Exception:
                    pass

    if not text:
        log.info(f"[signal hub original anonymous skipped empty] msg={message.id} topic={topic_id_of(message)}")
        return None

    sent = await send_message_with_retry(
        SIGNAL_DEST_CHAT,
        text,
        formatting_entities=entities if text else None,
        parse_mode=None,
        link_preview=False,
    )

    log.info(f"[signal hub original anonymous text copied] msg={message.id} topic={topic_id_of(message)}")
    return sent


def is_transient_send_error(exc):
    text = str(exc).lower()
    patterns = (
        "timeout",
        "timed out",
        "connection",
        "server disconnected",
        "temporarily",
        "transport",
        "network",
        "request failed",
        "flood",
    )
    return any(p in text for p in patterns)


async def send_message_with_retry(*args, **kwargs):
    attempts = max(1, SIGNAL_SEND_RETRY_ATTEMPTS)
    last_exc = None

    for attempt in range(1, attempts + 1):
        try:
            return await client.send_message(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            name = type(exc).__name__
            wait = 0

            seconds = getattr(exc, "seconds", None)
            if seconds is not None:
                wait = min(int(seconds) + 1, SIGNAL_SEND_RETRY_SLEEP_CAP_SECONDS)
            elif attempt < attempts and is_transient_send_error(exc):
                wait = min(2 * attempt, SIGNAL_SEND_RETRY_SLEEP_CAP_SECONDS)

            if wait and attempt < attempts:
                log.warning(f"[signal send retry] attempt={attempt}/{attempts} wait={wait}s error={name}: {exc}")
                await asyncio.sleep(wait)
                continue

            raise

    if last_exc:
        raise last_exc

    return None


async def forward_original_with_retry(message, text):
    attempts = max(1, SIGNAL_SEND_RETRY_ATTEMPTS)
    last_exc = None

    for attempt in range(1, attempts + 1):
        try:
            return await forward_original(message, text)
        except Exception as exc:
            last_exc = exc
            seconds = getattr(exc, "seconds", None)
            if seconds is not None and attempt < attempts:
                wait = min(int(seconds) + 1, SIGNAL_SEND_RETRY_SLEEP_CAP_SECONDS)
                log.warning(f"[signal original retry floodwait] attempt={attempt}/{attempts} wait={wait}s msg={getattr(message, 'id', None)}")
                await asyncio.sleep(wait)
                continue
            if attempt < attempts and is_transient_send_error(exc):
                wait = min(2 * attempt, SIGNAL_SEND_RETRY_SLEEP_CAP_SECONDS)
                log.warning(f"[signal original retry transient] attempt={attempt}/{attempts} wait={wait}s msg={getattr(message, 'id', None)} error={exc}")
                await asyncio.sleep(wait)
                continue
            raise

    if last_exc:
        raise last_exc

    return None


async def send_full_signal(message, result, key, original_text, forward_raw=True):
    sig = signature_for(result, key, getattr(message, "id", None))
    content_sig = content_signature_for(result, key)

    if sig in sent_signature_set:
        log.info("[signal hub skipped] duplicate signal packet")
        return False

    if CONTENT_DEDUPE_ENABLED and content_sig in sent_content_signature_set:
        log.info("[signal hub skipped] duplicate content signal")
        return False

    try:
        await delete_existing_signal_packet(message, key)

        original_sent = None
        sent_messages = []

        if forward_raw:
            original_sent = await forward_original_with_retry(message, original_text)
            if original_sent:
                sent_messages.append(original_sent)

        reply_to_id = getattr(original_sent, "id", None)

        ai_sent = await send_ai_formatted_signal(message, result, reply_to_id)
        sent_messages.append(ai_sent)

        if SEND_SOURCE_LINE:
            source_sent = await send_message_with_retry(
                SIGNAL_DEST_CHAT,
                result["source"],
                parse_mode="html",
                link_preview=False,
                reply_to=reply_to_id or getattr(ai_sent, "id", None),
            )
            sent_messages.append(source_sent)

        remember_signal_packet(message, key, sent_messages)
        remember_lifecycle(message, key, result, sent_messages)
        remember_signature(sig)
        if CONTENT_DEDUPE_ENABLED:
            remember_content_signature(content_sig)
        remember_last_signal_context(key, result)

    except Exception as exc:
        sent_signature_set.discard(sig)
        sent_content_signature_set.discard(content_sig)

        try:
            sent_signatures.remove(sig)
        except Exception:
            pass

        try:
            sent_content_signatures.remove(content_sig)
        except Exception:
            pass

        log.exception(f"[signal hub packet send failed] {exc}")
        return False

    buffers[key].clear()
    log.info(f"[signal hub sent] source_msg={message.id} topic={topic_id_of(message)} -> {SIGNAL_DEST_CHAT}")
    return True


def is_obvious_non_trade_partial(text: str) -> bool:
    t = (text or "").strip()
    low = t.lower()
    up = t.upper()

    if not t:
        return True

    blocked_phrases = [
        "click what you need",
        "spots are limited",
        "market tonight",
        "market open tonight",
        "are we ready",
        "happy sunday",
        "hello everyone",
        "ciao ragazzi",
        "come state",
        "caricate bene",
        "vote guys",
        "i'll post",
        "last few seconds",
        "school | vip",
        "ebook",
        "mt5 guide",
        "free life-time",
        "instagram",
        "daily overview",
        "weekly recap",
    ]

    if any(p in low for p in blocked_phrases):
        return True

    # Messages with only hype/announcement wording and no trade levels should not buffer.
    has_direction = bool(re.search(r"\b(BUY|SELL|BUYS|SELLS|BUYING|SELLING|LONG|SHORT)\b", up))
    has_symbol = bool(re.search(r"\b(XAU|XAUUSD|GOLD|BTC|BTCUSD|NAS100|NASDAQ|US100|GER40|US30|EURUSD|GBPUSD|USDJPY)\b", up))
    has_trade_level_word = bool(re.search(r"\b(SL|STOP|STOPLOSS|TP|TARGET|ENTRY|LIMIT|ABOVE|BELOW|UNDER|OVER)\b", up))
    has_price = bool(re.search(r"\b\d{3,7}(?:\.\d+)?\b", up))

    if not has_direction and not has_symbol and not has_trade_level_word:
        return True

    # Hype text with no usable price should not buffer.
    if not has_price and not has_trade_level_word:
        return True

    return False


def should_buffer_partial_piece(text: str) -> bool:
    if is_obvious_non_trade_partial(text):
        return False

    if looks_like_signal(text) or looks_like_partial_signal_piece(text):
        return True

    return False


def is_any_signal_update_text(text: str) -> bool:
    """
    Recognition-only check.
    If true, this message is an update, not a partial signal piece.
    """
    try:
        if classify_signal_update_text(text):
            return True
    except Exception as exc:
        log.warning(f"[update classify check failed] {exc}")

    try:
        if has_set_be_instruction(text):
            return True
    except Exception as exc:
        log.warning(f"[set be update check failed] {exc}")

    try:
        if move_update_from_text(text):
            return True
    except Exception as exc:
        log.warning(f"[move update check failed] {exc}")

    return False


@client.on(events.NewMessage())
@client.on(events.MessageEdited())
async def on_signal_hub_message(event):
    try:
        if not event_is_from_source(event):
            return

        message = event.message
        if should_skip(message):
            return

        text = message_text(message).strip()
        key = buffer_key(message)
        source_name = source_name_for(message)
        log.info(f"[signal hub seen] msg={message.id} topic={topic_id_of(message)} text={text[:80]}")

        if is_promo_text(text, topic_id_of(message)):
            log.info("[signal hub skipped] promo/spam filter")
            return

        # Management updates are now suppressed by default.
        # User wants only: original signal + AI formatted signal + source line.
        is_update_text = is_any_signal_update_text(text)

        if is_update_text and not SEND_SIGNAL_UPDATES:
            log.info(f"[signal update suppressed] key={key} msg={message.id} text={text[:100]!r}")
            return

        if is_update_text and SEND_SIGNAL_UPDATES:
            if await maybe_send_lifecycle_update(message, key, text):
                return
            if await maybe_send_signal_update_reply(message, key, text):
                return
            log.info(f"[signal update unmatched] recognized update but no AI target key={key} msg={message.id} text={text[:100]!r}")
            return

        # First try this message alone. Complete setups should not enter the partial buffer.
        standalone_text = apply_provider_profile(apply_same_as_above_context(key, text), topic_id_of(message))
        result = extract_and_format(standalone_text, source_name, message.id)
        if result:
            await send_full_signal(message, result, key, text, forward_raw=True)
            return

        if not PARTIAL_BUFFER_ENABLED:
            log.info("[signal hub skipped] not a clean signal")
            return

        if not should_buffer_partial_piece(text):
            log.info("[signal hub skipped] not signal-like / partial blocked")
            return

        trim_buffer(key)

        # If a previous incomplete signal is stuck and a new setup starts, clear it.
        if buffers[key] and looks_like_fresh_signal_start(text):
            log.info(f"[signal hub buffer reset] fresh setup detected key={key}")
            buffers[key].clear()

        first_piece = len(buffers[key]) == 0
        buffers[key].append({"ts": time.time(), "id": message.id, "text": text, "message": message})
        # Do NOT forward candidate messages yet.
        # Only forward the original after the setup is fully confirmed and formatted.

        combined = apply_provider_profile(apply_same_as_above_context(key, combined_text_for(key)), topic_id_of(message))
        result = extract_and_format(combined, source_name, message.id)
        if result:
            raw_msg = first_buffer_message(key) or message
            await send_full_signal(raw_msg, result, key, message_text(raw_msg).strip(), forward_raw=True)
            return

        log.info(f"[signal hub waiting] partial message stored key={key} topic={topic_id_of(message)} size={len(buffers[key])}")
    except Exception as exc:
        log.exception(f"[signal hub failed] {exc}")


@client.on(events.NewMessage(chats=ADMIN_CHAT))
async def on_admin_message(event):
    try:
        await handle_admin_command(event, client, stats)
    except Exception as exc:
        log.error(f"[admin command failed] {exc}")




async def purge_destination_on_start_once():
    if not PURGE_DEST_ON_START:
        log.info("[incoming purge] disabled")
        return

    deleted = 0
    limit = max(1, int(PURGE_DEST_LIMIT))

    log.warning(
        f"[incoming purge start] chat={SIGNAL_DEST_CHAT} "
        f"limit={limit} until_empty={PURGE_DEST_UNTIL_EMPTY}"
    )

    while True:
        if deleted >= limit:
            log.warning(f"[incoming purge limit reached] chat={SIGNAL_DEST_CHAT} deleted={deleted} limit={limit}")
            break

        batch_limit = min(100, limit - deleted)

        try:
            msgs = await client.get_messages(SIGNAL_DEST_CHAT, limit=batch_limit)
        except Exception as exc:
            log.warning(f"[incoming purge fetch failed] chat={SIGNAL_DEST_CHAT}: {type(exc).__name__}: {exc}")
            break

        ids = [int(getattr(m, "id", 0) or 0) for m in msgs or [] if getattr(m, "id", None)]

        if not ids:
            log.warning(f"[incoming purge empty] chat={SIGNAL_DEST_CHAT} deleted={deleted}")
            break

        try:
            await client.delete_messages(SIGNAL_DEST_CHAT, ids)
            deleted += len(ids)
            log.warning(f"[incoming purge deleted batch] count={len(ids)} total={deleted}")

            if PURGE_DEST_SLEEP_SECONDS > 0:
                await asyncio.sleep(PURGE_DEST_SLEEP_SECONDS)

        except Exception as exc:
            log.warning(f"[incoming purge delete failed] count={len(ids)}: {type(exc).__name__}: {exc}")
            break

        if not PURGE_DEST_UNTIL_EMPTY and deleted >= limit:
            break

    log.warning(f"[incoming purge done] chat={SIGNAL_DEST_CHAT} deleted={deleted}")


async def main():
    await start_runtime_guard("exposedfx-ai-signal-formatter", log)
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram session loaded but account is not authorised. Regenerate session chunks.")

    me = await client.get_me()
    log.info(f"Logged in as {me.first_name} | id={me.id}")
    log.info(f"Signal hub source: {SIGNAL_SOURCE_CHAT} | source channel id: {source_channel_id()}")
    log.info(f"Signal hub destination: {SIGNAL_DEST_CHAT}")
    log.info(f"Allowed topics: {sorted(ALLOWED_SOURCE_TOPICS)}")
    await purge_destination_on_start_once()
    await preload_topic_titles_once()
    log.info(f"Forward original only after confirmed signal: {FORWARD_SIGNAL_CANDIDATES}")
    log.info(f"SEND_SIGNAL_UPDATES={SEND_SIGNAL_UPDATES}")
    log.info(f"CONTENT_DEDUPE_ENABLED={CONTENT_DEDUPE_ENABLED}")
    log.info(f"PURGE_DEST_ON_START={PURGE_DEST_ON_START}")
    log.info(f"PURGE_DEST_UNTIL_EMPTY={PURGE_DEST_UNTIL_EMPTY}")
    log.info("Actual source topic names active: True")
    log.info(f"DELETE_OLD_SIGNAL_PACKET_ON_EDIT={DELETE_OLD_SIGNAL_PACKET_ON_EDIT}")
    log.info("AI/source replies to the anonymous original copy: True")
    log.info("Anonymous original copy mode active: True")
    log.info(f"Universal AI extractor active for any pair")
    log.info(f"Partial signal buffer: {PARTIAL_BUFFER_ENABLED} | window={BUFFER_WINDOW_SECONDS}s | max={BUFFER_MAX_MESSAGES}")
    log.info("Partial buffer strict guard active: True")
    log.info(f"Content signal dedupe active: {CONTENT_DEDUPE_ENABLED}")
    log.info(f"SIGNAL_SEND_RETRY_ATTEMPTS={SIGNAL_SEND_RETRY_ATTEMPTS}")
    log.info(f"SIGNAL_SEND_RETRY_SLEEP_CAP_SECONDS={SIGNAL_SEND_RETRY_SLEEP_CAP_SECONDS}")
    log.info(f"TP same-as-above context active: True")
    log.info(f"Signal update replies active: True")
    log.info("Signal update formatter v2 active: True")
    log.info(f"UPDATE_REPLY_DEDUPE_SECONDS={UPDATE_REPLY_DEDUPE_SECONDS}")
    log.info(f"Loaded lifecycle records: {len(active_signal_lifecycle)}")
    log.info(f"Recovered lifecycle records from packet map: {recovered_lifecycle_records}")
    log.info("Lifecycle open on signal send active: True")
    log.info("Recognized updates blocked from partial buffer: True")
    log.info("Any update classifier final active: True")
    log.info("Animated update tg-emoji active: True")
    log.info("Lifecycle pip enrichment active: True")
    log.info("Broad update phrase reinforcement active: True")
    log.info("Ultra update variation phrase bank active: True")
    log.info(f"AI_FORMATTED_SIGNAL_WITH_MEDIA={AI_FORMATTED_SIGNAL_WITH_MEDIA}")
    log.info("AI formatted media signal active: True")
    log.info("New mirror AI topics active: True")
    log.info("Topic 28464 AI route active: True")
    log.info("Topic 28840 AI route active: True")
    log.info("Signal lifecycle tracking active: True")
    log.info("Provider profiles active: True")
    log.info("Promo filter active: True")
    log.info("Tolerance dedupe active: True")
    await admin_startup(client)
    asyncio.create_task(admin_loop(client, stats))
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        log.exception(f"[signal hub fatal crash] {type(exc).__name__}: {exc}")
        alert_crash("exposedfx-ai-signal-formatter:fatal", exc)
        raise
