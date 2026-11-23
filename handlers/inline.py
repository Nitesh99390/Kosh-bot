from typing import Any, Optional

from aiogram import Bot, Router
from aiogram.types import (
    ChosenInlineResult,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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
from utils.formatters import MEDIA_LABELS, esc, format_duration, format_size

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
    packs_only = False
    sort_by = default_sort
    free_terms: list[str] = []

    tokens = raw.strip().split()
    for token in tokens:
        low = token.lower()
        if low in ("pack", "packs", "bundle", "bundles"):
            packs_only = True
        elif low in ("fav", "favs", "star", "⭐", "★"):
            only_fav = True
        elif low in ("top", "popular") and len(tokens) == 1:
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
        "packs_only": packs_only,
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


def _pack_to_inline_result(
    p: dict[str, Any], bot_username: str
) -> InlineQueryResultArticle:
    share_url = f"https://t.me/{bot_username}?start=pack_{p['share_token']}"
    lock = "🔒 " if p.get("pin_hash") else ""
    desc = f"{p['item_count']} files · {format_size(p['total_size'])}"
    if p.get("pin_hash"):
        desc += " · PIN Locked"
    if p.get("max_views"):
        desc += f" · {p['views']}/{p['max_views']} opens"
    note_line = (
        f"\n<i>{esc(p['description'][:100])}</i>" if p.get("description") else ""
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"📦 Open Pack ({p['item_count']} files)",
                    url=share_url,
                    style="primary",
                )
            ]
        ]
    )
    return InlineQueryResultArticle(
        id=f"pack_{p['id']}",
        title=f"{lock}🎒 {p['title']}",
        description=desc,
        input_message_content=InputTextMessageContent(
            message_text=(
                f"🎒 <b>{esc(p['title'])}</b>{note_line}\n"
                f"<blockquote>📦 <b>{p['item_count']}</b> files  ·  💾 <b>{format_size(p['total_size'])}</b></blockquote>"
            )
        ),
        reply_markup=kb,
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
                message_text=f"⭕ <a href='https://t.me/{bot_username}?start=file_{f['share_token']}'>{esc(title)}</a>"
            ),
        )
    return None


@router.inline_query()
async def handle_inline_query(
    inline_query: InlineQuery,
    db: Database,
    config: Config,
    bot_username: str = "KoshBox_Bot",
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
    uname = bot_username or "KoshBox_Bot"

    results: list[InlineQueryResult] = []
    next_offset = ""

    if parsed["packs_only"]:
        packs = await db.list_vault_packs(user["vault_id"], limit=page_size + 1, offset=offset)
        q_low = parsed["text_query"].lower()
        if q_low:
            packs = [p for p in packs if q_low in p["title"].lower()]
        has_more = len(packs) > page_size
        for p in packs[:page_size]:
            if p["item_count"] > 0:
                results.append(_pack_to_inline_result(p, uname))
        next_offset = str(offset + page_size) if has_more else ""
    else:
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
        for row in page_rows:
            item = _to_inline_result(row, uname)
            if item is not None:
                results.append(item)

    if not results and offset == 0:
        raw_q = (inline_query.query or "").strip()
        hint_title = f"No results for '{raw_q[:28]}'" if raw_q else "Vault is empty"
        hint_desc = (
            "Try: t:audio · t:video · g:folder · fav · top · packs"
            if raw_q
            else "Tap 'Open Vault' above to archive your first file!"
        )
        results.append(
            InlineQueryResultArticle(
                id="empty_vault_hint",
                title=hint_title,
                description=hint_desc,
                input_message_content=InputTextMessageContent(
                    message_text=f"⚡ Instant cloud vault & sharing via @{uname}"
                ),
            )
        )

    await inline_query.answer(
        results=results,
        cache_time=2,
        is_personal=True,
        next_offset=next_offset,
        button=InlineQueryResultsButton(
            text="⚡ Open Vault",
            start_parameter="upload",
        ),
    )


@router.chosen_inline_result()
async def handle_chosen_inline_result(
    chosen: ChosenInlineResult, db: Database
) -> None:
    if chosen.result_id.isdigit():
        await db.increment_use_count(int(chosen.result_id))
