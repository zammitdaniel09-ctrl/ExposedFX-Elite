import os
import time
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ADMIN_CHAT = int(os.environ.get("ADMIN_CHAT_ID", "7121821750"))
TZ_NAME = "Europe/Malta"


def _period_today(tz):
    now = datetime.now(tz)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.timestamp(), now.timestamp(), start.strftime("%d %b %Y"), now.strftime("%d %b %Y %H:%M")


def _period_week(tz):
    now = datetime.now(tz)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    sunday = today - timedelta(days=(today.weekday() - 6) % 7)
    return sunday.timestamp(), now.timestamp(), sunday.strftime("%d %b %Y"), now.strftime("%d %b %Y %H:%M")


def _rows(stats, start_ts, end_ts):
    con = stats.connect()
    rows = con.execute(
        "SELECT * FROM results WHERE created_at>=? AND created_at<? ORDER BY created_at", (start_ts, end_ts)
    ).fetchall()
    con.close()
    return rows


def _rank(rows):
    data = {}
    for r in rows:
        source = r["source"] or "Unknown"
        item = data.setdefault(source, {"trades": 0, "wins": 0, "losses": 0, "pips": 0.0})
        item["trades"] += 1
        item["pips"] += float(r["pips"] or 0)
        if r["status"] == "WIN":
            item["wins"] += 1
        if r["status"] == "LOSS":
            item["losses"] += 1
    out = []
    for source, item in data.items():
        wr = item["wins"] / item["trades"] * 100 if item["trades"] else 0
        avg = item["pips"] / item["trades"] if item["trades"] else 0
        out.append({"source": source, "wr": wr, "avg": avg, **item})
    return sorted(out, key=lambda x: (x["pips"], x["wr"], x["trades"]), reverse=True)


def _report(stats, label, start_ts, end_ts, start_label, end_label):
    rows = _rows(stats, start_ts, end_ts)
    wins = sum(1 for r in rows if r["status"] == "WIN")
    losses = sum(1 for r in rows if r["status"] == "LOSS")
    total = wins + losses
    pips = sum(float(r["pips"] or 0) for r in rows)
    wr = wins / total * 100 if total else 0
    avg = pips / total if total else 0
    ranked = _rank(rows)
    top = ranked[0]["source"] if ranked else "N/A"
    lines = []
    for i, r in enumerate(ranked[:5], 1):
        lines.append(f"{i}. {r['source']} | {r['pips']:.1f} pips | {r['wr']:.1f}% WR | {r['trades']} trades")
    top_text = "\n".join(lines) if lines else "No tracked provider results yet."
    return (
        f"ExposedFX {label}\n"
        f"Period: {start_label} - {end_label}\n\n"
        f"Wins: {wins}\nLosses: {losses}\nWin Rate: {wr:.2f}%\n"
        f"Total Pips: {pips:.1f}\nAverage Pips: {avg:.1f}\nBest Group: {top}\n\n"
        f"Top Providers:\n{top_text}\n\nTracked results only."
    )


def _meta_get(stats, key):
    con = stats.connect()
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    con.close()
    return row["value"] if row else None


def _meta_set(stats, key, value):
    con = stats.connect()
    con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, str(value)))
    con.commit()
    con.close()


async def admin_startup(client):
    await client.send_message(
        ADMIN_CHAT, "ExposedFX worker is online. Saved Messages controls are active. Use /help"
    )


async def handle_admin_command(event, client, stats):
    text = (event.raw_text or "").strip()
    if not text.startswith("/"):
        return
    cmd = text.split()[0].lower()
    tz = ZoneInfo(TZ_NAME)

    if cmd == "/help":
        await client.send_message(
            ADMIN_CHAT, "Commands:\n/stats_today\n/stats_week\n/top_providers\n/send_weekly\n/help"
        )
        return
    if cmd == "/stats_today":
        await client.send_message(ADMIN_CHAT, _report(stats, "Daily Recap", *_period_today(tz)))
        return
    if cmd == "/stats_week":
        await client.send_message(ADMIN_CHAT, _report(stats, "Week So Far", *_period_week(tz)))
        return
    if cmd == "/top_providers":
        start_ts, end_ts, start_label, end_label = _period_week(tz)
        rows = _rows(stats, start_ts, end_ts)
        ranked = _rank(rows)
        lines = ["ExposedFX Top Providers", f"Period: {start_label} - {end_label}", ""]
        for i, r in enumerate(ranked[:10], 1):
            lines.append(
                f"{i}. {r['source']} | {r['pips']:.1f} pips | {r['wr']:.1f}% WR | {r['trades']} trades"
            )
        await client.send_message(
            ADMIN_CHAT, "\n".join(lines) if ranked else "No tracked provider results yet."
        )
        return
    if cmd == "/send_weekly":
        start_ts, file_path, caption = stats.build_report()
        await client.send_file(ADMIN_CHAT, file_path, caption="Manual admin report:\n" + caption)
        return


async def admin_loop(client, stats):
    tz = ZoneInfo(TZ_NAME)
    while True:
        try:
            now = datetime.now(tz)
            if now.hour == 23 and now.minute >= 55:
                key = "admin_daily_sent:" + now.strftime("%Y-%m-%d")
                if not _meta_get(stats, key):
                    await client.send_message(ADMIN_CHAT, _report(stats, "Daily Recap", *_period_today(tz)))
                    _meta_set(stats, key, time.time())
            if now.hour == 9 and now.minute <= 10:
                key = "admin_heartbeat_sent:" + now.strftime("%Y-%m-%d")
                if not _meta_get(stats, key):
                    await client.send_message(ADMIN_CHAT, "ExposedFX worker heartbeat: online.")
                    _meta_set(stats, key, time.time())
        except Exception as exc:
            print(f"[admin loop failed] {exc}")
        await asyncio.sleep(60)
