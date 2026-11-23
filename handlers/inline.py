from typing import Any, Optional

from aiogram import Bot, Router
from aiogram.types import (
    ChosenInlineResult,
    InlineQuery,
    InlineQueryResult,
    InlineQueryResultArticle,
    InlineQueryResultCachedAudio,
    InlineQueryResultCachedDocument,
    InlineQueryResultCachedGif,
    InlineQueryResultCachedPhoto,
    InlineQueryResultCachedSticker,
    InlineQueryResultCachedVideo,
    InlineQueryResultCachedVoice,
    InlineQueryResultsButton,
    InputTextMessageContent,
)

from config import Config
from database import Database
from utils.formatters import MEDIA_LABELS, format_duration, format_size

router = Router(name="inline")

TYPE_ALIASES = {
    "audio": "audio",
    "music": "audio",
    "song": "audio",
    "mp3": "audio",
    "voice": "voice",
    "vn": "voice",
    "video": "video",
    "vid": "video",
    "mp4": "video",
    "doc": "document",
    "document": "document",
    "file": "document",
    "pdf": "document",
    "apk": "document",
    "zip": "document",
    "photo": "photo",
    "img": "photo",
    "pic": "photo",
    "image": "photo",
    "gif": "animation",
    "animation": "animation",
    "sticker": "sticker",
    "stk": "sticker",
    "vnote": "video_note",
    "round": "video_note",
}

SORT_ALIASES = {
    "new": "new",
    "recent": "new",
    "old": "old",
    "top": "top",
    "popular": "top",
    "az": "az",
    "name": "az",
    "size": "size",
    "big": "size",
}


def _parse_inline_query(raw: str, default_sort: str = "new") -> dict[str, Any]:
    media_type: Optional[str] = None
    group_name: Optional[str] = None
    tag: Optional[str] = None
    only_fav = False
    sort_by = default_sort
    free_terms: list[str] = []

    for token in raw.strip().split():
        low = token.lower()
        if low in ("fav", "favs", "star", "⭐", "★"):
            only_fav = True
        elif low in ("top", "popular") and len(raw.strip().split()) == 1:
            sort_by = "top"
        elif low.startswith("sort:") or low.startswith("s:"):
            val = low.split(":", 1)[1]
            if val in SORT_ALIASES:
                sort_by = SORT_ALIASES[val]
        elif low.startswith("type:") or low.startswith("t:"):
            val = low.split(":", 1)[1]
            if val in TYPE_ALIASES:
                media_type = TYPE_ALIASES[val]
        elif low.startswith("g:") or low.startswith("group:") or low.startswith("f:"):
            val = token.split(":", 1)[1].strip()
            if val:
                group_name = val
        elif token.startswith("@") and len(token) > 1:
            group_name = token[1:]
        elif token.startswith("#") and len(token) > 1:
            tag = token
        else:
            free_terms.append(token)

    return {
        "text_query": " ".join(free_terms),
        "media_type": media_type,
        "group_name_filter": group_name,
        "tag_filter": tag,
        "only_favorites": only_fav,
        "sort_by": sort_by,
    }


def _build_description(f: dict[str, Any]) -> str:
    parts: list[str] = []
    if f.get("is_favorite"):
        parts.append("★")
    if f.get("performer"):
        parts.append(f["performer"])
    if f.get("group_name"):
        parts.append(f"📁 {f['group_name']}")
    if f.get("duration"):
        parts.append(format_duration(f["duration"]))
    if f.get("file_size"):
        parts.append(format_size(f["file_size"]))
    if f.get("tags"):
        parts.append(f["tags"])
    return (
        " · ".join(parts)
        if parts
        else MEDIA_LABELS.get(f["media_type"], f["media_type"].capitalize())
    )


def _to_inline_result(
    f: dict[str, Any], bot_username: str = ""
) -> Optional[InlineQueryResult]:
    res_id = str(f["id"])
    mtype = f["media_type"]
    fid = f["file_id"]
    title = (f.get("title") or "Untitled")[:128]
    desc = _build_description(f)[:200]
    caption = f.get("caption") or None

    if mtype == "audio":
        return InlineQueryResultCachedAudio(
            id=res_id,
            audio_file_id=fid,
            caption=caption,
        )
    if mtype == "document":
        return InlineQueryResultCachedDocument(
            id=res_id,
            title=title,
            document_file_id=fid,
            description=desc,
            caption=caption,
        )
    if mtype == "video":
        return InlineQueryResultCachedVideo(
            id=res_id,
            video_file_id=fid,
            title=title,
            description=desc,
            caption=caption,
        )
    if mtype == "photo":
        return InlineQueryResultCachedPhoto(
            id=res_id,
            photo_file_id=fid,
            title=title,
            description=desc,
            caption=caption,
        )
    if mtype == "voice":
        return InlineQueryResultCachedVoice(
            id=res_id,
            voice_file_id=fid,
            title=title,
            caption=caption,
        )
    if mtype == "animation":
        return InlineQueryResultCachedGif(
            id=res_id,
            gif_file_id=fid,
            title=title,
            caption=caption,
        )
    if mtype == "sticker":
        return InlineQueryResultCachedSticker(
            id=res_id,
            sticker_file_id=fid,
        )
    if mtype == "video_note" and f.get("share_token") and bot_username:
        return InlineQueryResultArticle(
            id=res_id,
            title=f"⭕ {title}",
            description=desc,
            input_message_content=InputTextMessageContent(
                message_text=f"⭕ <a href='https://t.me/{bot_username}?start=file_{f['share_token']}'>{title}</a>"
            ),
        )
    return None


@router.inline_query()
async def handle_inline_query(
    inline_query: InlineQuery, db: Database, config: Config, bot: Bot
) -> None:
    user = await db.ensure_user(
        inline_query.from_user.id,
        inline_query.from_user.username or "",
        inline_query.from_user.full_name or "",
    )

    try:
        offset = int(inline_query.offset) if inline_query.offset else 0
    except ValueError:
        offset = 0

    default_sort = user.get("default_sort") or "new"
    parsed = _parse_inline_query(inline_query.query or "", default_sort=default_sort)
    page_size = config.inline_page_size

    rows = await db.search_files(
        user["vault_id"],
        text_query=parsed["text_query"],
        media_type=parsed["media_type"],
        group_name_filter=parsed["group_name_filter"],
        tag_filter=parsed["tag_filter"],
        only_favorites=parsed["only_favorites"],
        sort_by=parsed["sort_by"],
        limit=page_size + 1,
        offset=offset,
    )

    has_more = len(rows) > page_size
    page_rows = rows[:page_size]
    next_offset = str(offset + page_size) if has_more else ""

    me = await bot.get_me()
    results: list[InlineQueryResult] = []
    for row in page_rows:
        item = _to_inline_result(row, me.username or "")
        if item is not None:
            results.append(item)

    if not results and offset == 0:
        raw_q = (inline_query.query or "").strip()
        hint_title = (
            f"No matches for '{raw_q[:32]}'"
            if raw_q
            else "Your KoshBox Vault is empty"
        )
        hint_desc = (
            "Try: type:audio · type:video · g:folder · fav · top · #tag"
            if raw_q
            else "Tap 'Open KoshBox Vault' above to drop & archive your first file!"
        )
        results.append(
            InlineQueryResultArticle(
                id="empty_vault_hint",
                title=hint_title,
                description=hint_desc,
                input_message_content=InputTextMessageContent(
                    message_text=f"✨ Archiving & sharing files instantly with @{me.username or 'KoshBox_Bot'}"
                ),
            )
        )

    await inline_query.answer(
        results=results,
        cache_time=2,
        is_personal=True,
        next_offset=next_offset,
        button=InlineQueryResultsButton(
            text="✨ Open KoshBox Vault",
            start_parameter="upload",
        ),
    )


@router.chosen_inline_result()
async def handle_chosen_inline_result(
    chosen: ChosenInlineResult, db: Database
) -> None:
    if chosen.result_id.isdigit():
        await db.increment_use_count(int(chosen.result_id))
