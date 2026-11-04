"""Frozen copy of ALT's legacy send_single / send_album (alt_worker_legacy.py, 4 Nov 2026).

The test oracle for tests/test_engine_alt.py: the delivery engine must make
exactly the Telegram calls these made. Never imported by production code;
the test injects the globals they expect (client, translated_payload, ...).
"""


async def send_single(route, message):
    if owner_policy.is_stopped(route):
        return None
    if alt_vip5_block(route):
        log.warning("[ALT VIP5 DESTINATION PERMANENT BLOCK] writer=send")
        return None
    if has_username_mention(message):
        log_username_filter_alt([message], "send_single_hard_guard", route=route)
        return None
    target_reply = await resolve_reply_target(_WORKER, route, message, logger=log)
    text, entities = await translated_payload(route, message)
    topic_root = int(route["dest_topic"])
    if not real_media(message):
        if not text:
            return None

        async def send_text(reply_to):
            return await client.send_message(
                route["dest_chat"],
                text,
                formatting_entities=entities,
                parse_mode=None,
                reply_to=reply_to,
                link_preview=True,
            )

        try:
            sent = await send_text(target_reply)
        except Exception as exc:
            if target_reply == topic_root or not _reply_target_invalid(exc):
                raise
            log.warning(
                "[ALT REPLY TARGET GONE] route=%s source_msg=%s parent=%s fallback=topic_root",
                route.get("name"),
                getattr(message, "id", None),
                target_reply,
            )
            target_reply = topic_root
            sent = await send_text(target_reply)
    else:
        try:
            sent = await client.send_file(
                route["dest_chat"],
                message.media,
                caption=text if text else None,
                formatting_entities=entities if text else None,
                parse_mode=None,
                reply_to=target_reply,
            )
        except Exception as exc:
            if _reupload_cannot_help(exc):
                raise
            log.warning(
                "[ALT DIRECT MEDIA FAILED - USING REUPLOAD] source_msg=%s %s: %s",
                getattr(message, "id", None),
                type(exc).__name__,
                exc,
            )
            if _reply_target_invalid(exc):
                target_reply = topic_root
            downloaded = await message.download_media(file=str(MEDIA_DIR / f"single_{message.id}"))
            if not downloaded:
                raise RuntimeError("Media download fallback returned nothing")
            try:
                sent = await client.send_file(
                    route["dest_chat"],
                    downloaded,
                    caption=text if text else None,
                    formatting_entities=entities if text else None,
                    parse_mode=None,
                    reply_to=target_reply,
                )
            finally:
                try:
                    Path(downloaded).unlink(missing_ok=True)
                except Exception:
                    pass
    if target_reply != int(route["dest_topic"]):
        log.warning(
            "[ALT REPLY PRESERVED] route=%s source_child=%s dest_child=%s dest_parent=%s kind=SINGLE",
            route.get("name"),
            getattr(message, "id", None),
            getattr(sent, "id", None),
            target_reply,
        )
    return sent


async def send_album(route, messages):
    if owner_policy.is_stopped(route):
        return None
    if alt_vip5_block(route):
        log.warning("[ALT VIP5 DESTINATION PERMANENT BLOCK] writer=send")
        return None
    messages = list(messages or [])
    if unit_has_username_mention(messages):
        log_username_filter_alt(messages, "send_album_hard_guard", route=route)
        return None
    probe = next((m for m in messages if real_reply_source_ids(route, m)), messages[0] if messages else None)
    target_reply = (
        await resolve_reply_target(_WORKER, route, probe, logger=log)
        if probe is not None
        else int(route["dest_topic"])
    )
    caption = ""
    caption_entities = None
    caption_message = next((m for m in messages if text_of(m)), None)
    if caption_message is not None:
        caption, caption_entities = await translated_payload(route, caption_message)
    media_objects = [m.media for m in messages if real_media(m)]
    if not media_objects:
        return None
    try:
        sent = await client.send_file(
            route["dest_chat"],
            media_objects,
            caption=caption if caption else None,
            formatting_entities=caption_entities if caption else None,
            parse_mode=None,
            reply_to=target_reply,
        )
        sent_items = sent if isinstance(sent, list) else [sent]
    except Exception as exc:
        if _reupload_cannot_help(exc):
            raise
        log.warning("[ALT DIRECT ALBUM FAILED - USING REUPLOAD] %s: %s", type(exc).__name__, exc)
        if _reply_target_invalid(exc):
            target_reply = int(route["dest_topic"])
        downloaded_files = []
        try:
            for message in messages:
                if not real_media(message):
                    continue
                downloaded = await message.download_media(file=str(MEDIA_DIR / f"album_{message.id}"))
                if not downloaded:
                    raise RuntimeError("Album download returned nothing")
                downloaded_files.append(downloaded)
            sent = await client.send_file(
                route["dest_chat"],
                downloaded_files,
                caption=caption if caption else None,
                formatting_entities=caption_entities if caption else None,
                parse_mode=None,
                reply_to=target_reply,
            )
            sent_items = sent if isinstance(sent, list) else [sent]
        finally:
            for filename in downloaded_files:
                try:
                    Path(filename).unlink(missing_ok=True)
                except Exception:
                    pass
    if target_reply != int(route["dest_topic"]):
        log.warning(
            "[ALT REPLY PRESERVED] route=%s source_child=%s dest_parent=%s kind=ALBUM items=%s",
            route.get("name"),
            getattr(probe, "id", None),
            target_reply,
            len(sent_items),
        )
    return sent_items
