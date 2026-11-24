import asyncio
import time
from typing import Any, Optional

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import Config
from database import Database
from utils.cache import TTLCache
from utils.formatters import (
    BATCH_CHUNK_SIZE,
    EFFECT_FIRE,
    EFFECT_PARTY,
    MEDIA_ICONS,
    back_btn,
    batch_delete_chat_messages,
    breadcrumb,
    build_contact_owner_kb,
    build_trial_expiry_notice_text,
    cancel_kb,
    copy_btn,
    esc,
    format_expiry_badge,
    format_grant_badge,
    format_max_views_badge,
    format_size,
    format_ttl_short,
    home_btn,
    safe_edit_or_send,
    schedule_persistent_auto_delete,
    send_stored_media,
    send_with_effect,
    share_url_btn,
    tg_retry_call,
    toggle_label,
)
from utils.states import PackStates

router = Router(name="packs")
# Tracks users who passed PIN/trial/max_views gates for multi-batch `pk:more:` continuation
_PACK_CONT_SESSIONS: TTLCache[tuple[int, int], bool] = TTLCache(maxsize=4096, ttl=3600.0)


def authorize_pack_continuation(user_id: int, pack_id: int) -> None:
    _PACK_CONT_SESSIONS.set((int(user_id), int(pack_id)), True)


def _cleanup_pack_builder(user_id: int) -> None:
    from handlers.upload import _PACK_BUILDER_BUFFER, _PACK_BUILDER_TASKS

    prev_task = _PACK_BUILDER_TASKS.pop(user_id, None)
    if prev_task and not prev_task.done():
        prev_task.cancel()
    _PACK_BUILDER_BUFFER.pop(user_id, None)

PACKS_PAGE_SIZE = 5
PACK_DELIVERY_BATCH_SIZE = 100
PACK_ITEMS_EDIT_PAGE_SIZE = 6

# Per-(chat_id, pack_id) lock to prevent duplicate overlapping deliveries on rapid clicks
_ACTIVE_DELIVERIES: set[tuple[int, int]] = set()


async def _ensure_cb_user(callback: CallbackQuery, db: Database) -> dict:
    assert callback.from_user is not None
    return await db.ensure_user(
        callback.from_user.id,
        callback.from_user.username or "",
        callback.from_user.full_name or "",
    )


async def deliver_pack_to_chat(
    bot: Bot,
    chat_id: int,
    pack: dict[str, Any],
    db: Database,
    *,
    auto_delete_ttl: int = 0,
    force_protect: bool = False,
    is_owner: bool = False,
    has_full_access: bool = False,
    access_badge: str = "",
    show_request_cta: bool = False,
    start_offset: int = 0,
    count_view: bool = True,
    dump_channel_id: Optional[int] = None,
    contact_url: str = "",
    celebrate: bool = False,
) -> int:
    """
    Ultra-fast, zero-FloodWait Pack Delivery using Telegram's native 100-in-1 `bot.copy_messages()`:
    - Prevents overlapping duplicate runs per `(chat_id, pack['id'])`.
    - Owner self-retrieval (`is_owner=True`): ZERO auto-delete, ZERO `protect_content`, ZERO trial notice.
    - Full Access Grant (`has_full_access=True`): ZERO auto-delete, ZERO trial notice.
    - Preserves exact `pack_items.position ASC, pack_items.id ASC` order.
    """
    delivery_key = (chat_id, int(pack["id"]))
    if delivery_key in _ACTIVE_DELIVERIES:
        return 0

    _ACTIVE_DELIVERIES.add(delivery_key)
    try:
        items = await db.get_pack_files(pack["id"])
        total_items = len(items)
        if total_items == 0:
            await tg_retry_call(bot.send_message, chat_id, "⚠️ <b>Empty Pack</b>")
            return 0

        start_offset = max(0, min(start_offset, total_items))
        slice_items = items[start_offset : start_offset + PACK_DELIVERY_BATCH_SIZE]
        if not slice_items:
            return 0

        if count_view and start_offset == 0 and not is_owner:
            await db.increment_pack_views(pack["id"])

        effective_ttl = 0 if (is_owner or has_full_access) else max(0, int(auto_delete_ttl or 0))
        protect = False if is_owner else (bool(pack.get("protect_content")) or force_protect)
        end_idx = start_offset + len(slice_items)
        range_badge = (
            f"{start_offset + 1}–{end_idx} of {total_items}"
            if total_items > PACK_DELIVERY_BATCH_SIZE
            else f"{total_items} files"
        )
        desc_line = (
            f"\n<blockquote>{esc(pack['description'])}</blockquote>"
            if (start_offset == 0 and pack.get("description"))
            else ""
        )
        status_note = ""
        if start_offset == 0:
            if is_owner:
                status_note = "\n<i>🔓 Owner Access — permanent &amp; unrestricted</i>"
            elif access_badge:
                status_note = f"\n<i>🔓 {esc(access_badge)}</i>"
        ttl_line = (
            f"\n<i>⏱ Trial auto-deletes in {format_ttl_short(effective_ttl)}</i>"
            if effective_ttl > 0
            else ""
        )

        resolved_contact = contact_url
        if (show_request_cta or effective_ttl > 0) and not resolved_contact:
            resolved_contact = await db.resolve_owner_contact_url(
                pack["vault_id"], preferred_user_id=pack.get("creator_user_id")
            )

        header_kb: Optional[InlineKeyboardMarkup] = None
        if show_request_cta and not is_owner and not has_full_access and start_offset == 0:
            header_kb = build_contact_owner_kb(
                resolved_contact,
                request_access_cb=f"req:p:{pack['id']}",
            )

        sent_ids: list[int] = []
        header_effect = EFFECT_FIRE if (celebrate and start_offset == 0 and not is_owner) else None
        header_kwargs: dict[str, Any] = {}
        if header_kb:
            header_kwargs["reply_markup"] = header_kb
        header = await send_with_effect(
            bot.send_message,
            chat_id=chat_id,
            text=(
                f"🎒 <b>{esc(pack['title'])}</b> · <i>{range_badge} ({format_size(pack['total_size'])})</i>"
                f"{desc_line}{status_note}{ttl_line}"
            ),
            protect_content=protect,
            message_effect_id=header_effect,
            disable_web_page_preview=True,
            **header_kwargs,
        )
        if getattr(header, "message_id", None):
            sent_ids.append(header.message_id)

        delivered = 0
        copy_batch_fn = getattr(bot, "copy_messages", None)
        can_batch_copy = (
            copy_batch_fn is not None
            and dump_channel_id is not None
            and len(slice_items) > 1
            and all(int(f.get("dump_message_id") or 0) > 0 for f in slice_items)
        )

        def _extract_copied_ids(copied_res: Any) -> list[int]:
            out: list[int] = []
            if not copied_res:
                return out
            for m_obj in copied_res:
                mid_val = getattr(m_obj, "message_id", None)
                if mid_val is None and isinstance(m_obj, dict):
                    mid_val = m_obj.get("message_id")
                if mid_val is None and isinstance(m_obj, int):
                    mid_val = m_obj
                if mid_val and int(mid_val) > 0:
                    out.append(int(mid_val))
            return out

        fallback_attempted = False
        if can_batch_copy:
            dump_ids = [int(f["dump_message_id"]) for f in slice_items]
            strictly_ascending = all(
                dump_ids[i] < dump_ids[i + 1] for i in range(len(dump_ids) - 1)
            )

            if strictly_ascending:
                for c_start in range(0, len(dump_ids), BATCH_CHUNK_SIZE):
                    chunk_items = slice_items[c_start : c_start + BATCH_CHUNK_SIZE]
                    chunk_ids = dump_ids[c_start : c_start + BATCH_CHUNK_SIZE]
                    chunk_ok = False
                    try:
                        copied_res = await tg_retry_call(
                            copy_batch_fn,
                            chat_id=chat_id,
                            from_chat_id=dump_channel_id,
                            message_ids=chunk_ids,
                            protect_content=protect,
                        )
                        c_ids = _extract_copied_ids(copied_res)
                        if len(c_ids) == len(chunk_ids):
                            sent_ids.extend(c_ids)
                            delivered += len(c_ids)
                            chunk_ok = True
                        elif c_ids:
                            try:
                                await batch_delete_chat_messages(bot, chat_id, c_ids)
                            except Exception:
                                pass
                    except Exception:
                        chunk_ok = False
                    if not chunk_ok:
                        fallback_attempted = True
                        for idx_f, single_f in enumerate(chunk_items):
                            sent = await send_stored_media(
                                bot,
                                chat_id,
                                single_f,
                                protect_content=protect,
                                auto_delete_ttl=0,
                            )
                            if sent:
                                delivered += 1
                                if getattr(sent, "message_id", None):
                                    sent_ids.append(sent.message_id)
                            await asyncio.sleep(0.05 if (idx_f + 1) % 15 else 0.25)
            else:
                runs: list[list[dict[str, Any]]] = []
                for item_f in slice_items:
                    cur_mid = int(item_f["dump_message_id"])
                    if runs and int(runs[-1][-1]["dump_message_id"]) < cur_mid:
                        runs[-1].append(item_f)
                    else:
                        runs.append([item_f])

                op_count = 0
                fallback_attempted = True
                for run in runs:
                    if len(run) >= 2:
                        for c_start in range(0, len(run), BATCH_CHUNK_SIZE):
                            sub_run = run[c_start : c_start + BATCH_CHUNK_SIZE]
                            if len(sub_run) >= 2:
                                run_ids = [int(x["dump_message_id"]) for x in sub_run]
                                try:
                                    copied_res = await tg_retry_call(
                                        copy_batch_fn,
                                        chat_id=chat_id,
                                        from_chat_id=dump_channel_id,
                                        message_ids=run_ids,
                                        protect_content=protect,
                                    )
                                    c_ids = _extract_copied_ids(copied_res)
                                    if len(c_ids) == len(sub_run):
                                        sent_ids.extend(c_ids)
                                        delivered += len(c_ids)
                                        op_count += 1
                                        await asyncio.sleep(0.05 if op_count % 15 else 0.25)
                                        continue
                                    elif c_ids:
                                        try:
                                            await batch_delete_chat_messages(bot, chat_id, c_ids)
                                        except Exception:
                                            pass
                                except Exception:
                                    pass
                            for single_f in sub_run:
                                sent = await send_stored_media(
                                    bot,
                                    chat_id,
                                    single_f,
                                    protect_content=protect,
                                    auto_delete_ttl=0,
                                )
                                if sent:
                                    delivered += 1
                                    if getattr(sent, "message_id", None):
                                        sent_ids.append(sent.message_id)
                                op_count += 1
                                await asyncio.sleep(0.05 if op_count % 15 else 0.25)
                    else:
                        single_f = run[0]
                        sent = await send_stored_media(
                            bot,
                            chat_id,
                            single_f,
                            protect_content=protect,
                            auto_delete_ttl=0,
                        )
                        if sent:
                            delivered += 1
                            if getattr(sent, "message_id", None):
                                sent_ids.append(sent.message_id)
                        op_count += 1
                        await asyncio.sleep(0.05 if op_count % 15 else 0.25)

        if delivered == 0 and not fallback_attempted:
            for idx, f in enumerate(slice_items):
                sent = await send_stored_media(
                    bot,
                    chat_id,
                    f,
                    protect_content=protect,
                    auto_delete_ttl=0,
                )
                if sent:
                    delivered += 1
                    if getattr(sent, "message_id", None):
                        sent_ids.append(sent.message_id)
                await asyncio.sleep(0.05 if (idx + 1) % 15 else 0.25)

        remaining = total_items - end_idx
        if remaining > 0:
            authorize_pack_continuation(chat_id, int(pack["id"]))
            nxt_count = min(PACK_DELIVERY_BATCH_SIZE, remaining)
            cont_kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=f"📥 Next {nxt_count} Files ({end_idx + 1}–{end_idx + nxt_count}/{total_items})",
                            callback_data=f"pk:more:{pack['share_token']}:{end_idx}",
                            style="primary",
                        )
                    ]
                ]
            )
            footer = await tg_retry_call(
                bot.send_message,
                chat_id=chat_id,
                text=(
                    f"✅ <b>Delivered {start_offset + 1}–{end_idx}</b> of <b>{total_items}</b> · "
                    f"<i>{remaining} remaining</i>"
                ),
                reply_markup=cont_kb,
                protect_content=protect,
            )
            if getattr(footer, "message_id", None):
                sent_ids.append(footer.message_id)

        if effective_ttl > 0 and sent_ids and not is_owner and not has_full_access:
            notice = build_trial_expiry_notice_text(
                pack.get("title") or "Pack", effective_ttl
            )
            await schedule_persistent_auto_delete(
                bot,
                db,
                chat_id,
                sent_ids,
                effective_ttl,
                notify_text=notice,
                contact_url=resolved_contact,
                link_type="pack",
                link_id=int(pack["id"]),
            )

        return delivered
    finally:
        _ACTIVE_DELIVERIES.discard(delivery_key)


@router.callback_query(F.data.startswith("pk:more:"))
async def cb_pack_deliver_more(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    config: Optional[Config] = None,
) -> None:
    if not callback.from_user or not callback.message:
        return
    parts = callback.data.split(":")
    if len(parts) < 4 or not parts[3].isdigit():
        await callback.answer("Invalid batch cursor.", show_alert=True)
        return
    token = parts[2]
    offset = int(parts[3])

    pack = await db.get_pack_by_token(token, include_unavailable=True)
    if not pack or pack["item_count"] == 0:
        await callback.answer("⚠️ Pack no longer available.", show_alert=True)
        return

    visitor = await _ensure_cb_user(callback, db)
    is_owner = visitor["vault_id"] == pack["vault_id"]
    grant = (
        None
        if is_owner
        else await db.get_active_access_grant("pack", pack["id"], visitor["user_id"])
    )
    has_full_access = grant is not None

    if offset < PACK_DELIVERY_BATCH_SIZE:
        await callback.answer("Invalid batch cursor.", show_alert=True)
        return

    if not is_owner and not has_full_access:
        if (visitor["user_id"], int(pack["id"])) not in _PACK_CONT_SESSIONS:
            await callback.answer("🔒 Session expired — please open the pack link again.", show_alert=True)
            return
        if pack.get("is_paused"):
            await callback.answer("⏸ This pack link was paused by the owner.", show_alert=True)
            return
        if pack.get("expires_at") and int(pack["expires_at"]) < int(time.time()):
            await callback.answer("⏳ This pack link has expired.", show_alert=True)
            return

    ttl = (
        0
        if (is_owner or has_full_access)
        else await db.resolve_pack_auto_delete_ttl(pack)
    )
    creator = await db.get_user(pack["creator_user_id"])
    force_protect = (
        False
        if is_owner
        else bool((creator or {}).get("protect_content"))
    )
    contact_url = (
        ""
        if is_owner
        else await db.resolve_owner_contact_url(
            pack["vault_id"], preferred_user_id=pack["creator_user_id"]
        )
    )

    await callback.answer("📥 Sending next batch…")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await deliver_pack_to_chat(
        bot,
        callback.message.chat.id,
        pack,
        db,
        auto_delete_ttl=ttl,
        force_protect=force_protect,
        is_owner=is_owner,
        has_full_access=has_full_access,
        access_badge=format_grant_badge(grant["expires_at"]) if grant else "",
        start_offset=offset,
        count_view=False,
        dump_channel_id=config.dump_channel_id if config else None,
        contact_url=contact_url,
    )


async def render_packs_list(
    user_id: int, db: Database, page: int = 0
) -> tuple[str, InlineKeyboardMarkup]:
    """Clean, Simple Multi-File Packs Hub."""
    user = await db.get_user(user_id)
    if not user:
        user = await db.ensure_user(user_id)
    total = await db.count_vault_packs(user["vault_id"])
    total_pages = max(1, (total + PACKS_PAGE_SIZE - 1) // PACKS_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    offset = page * PACKS_PAGE_SIZE
    rows = await db.list_vault_packs(
        user["vault_id"], limit=PACKS_PAGE_SIZE + 1, offset=offset
    )
    has_next = len(rows) > PACKS_PAGE_SIZE
    packs = rows[:PACKS_PAGE_SIZE]

    lines = [
        breadcrumb("🏠 Home", "🎒 File Packs"),
        f"🎒 <b>File Packs</b> · <i>{total} pack(s)</i>",
    ]
    buttons: list[list[InlineKeyboardButton]] = []

    if not packs:
        lines.append(
            "<blockquote>Bundle 2 to 100+ files into a single shareable link!\n"
            "• Drop 2+ files together to auto-pack, or\n"
            "• Tap <b>➕ Create Pack</b> below.</blockquote>"
        )
    else:
        lines.append("<i>Tap a pack to copy its link, reorder files, or set locks (👤 1× · ⏱ Timer · 🔑 PIN):</i>")
        for p in packs:
            badges = ""
            if p.get("is_paused"):
                badges += "⏸ "
            if p.get("one_time_per_user"):
                badges += "👤 "
            if int(p.get("auto_delete_ttl") or 0) > 0:
                badges += "💣 "
            if p.get("pin_hash"):
                badges += "🔑 "
            lbl = f"{badges}🎒 {p['title'][:20]} ({p['item_count']}) · {format_size(p['total_size'])}"
            buttons.append(
                [InlineKeyboardButton(text=lbl, callback_data=f"pk:open:{p['id']}")]
            )

    if page > 0 or has_next:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"pk:list:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"📄 {page + 1}/{total_pages}", callback_data="noop"))
        if has_next:
            nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"pk:list:{page + 1}"))
        buttons.append(nav)

    buttons.append(
        [
            InlineKeyboardButton(
                text="➕ Create Pack",
                callback_data="pk:new",
                style="primary",
            ),
            home_btn("🏠 Home"),
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


async def render_pack_detail(
    user_id: int,
    pack_id: int,
    db: Database,
    bot: Optional[Bot] = None,
    bot_username: str = "KoshBox_Bot",
    sec_mode: bool = False,
) -> Optional[tuple[str, InlineKeyboardMarkup]]:
    """
    Progressive Disclosure Pack Console (Clean 4-Row Primary Card vs Security Sub-Drawer):
    - Default (`sec_mode=False`): Clean 4-row primary card (Copy/Share, Get All/Edit, Trial/Auto-Del, Security/Back)
      so the screen is NEVER overcrowded with 14 buttons!
    - Security Drawer (`sec_mode=True`): Full security controls (1x/User, No-Fwd, Auto-Del, Expiry, PIN, Max Opens, Pause, Reset, Delete).
    """
    user = await db.get_user(user_id)
    if not user:
        user = await db.ensure_user(user_id)
    pack = await db.get_pack(pack_id)
    if not pack or pack["vault_id"] != user["vault_id"]:
        return None

    items = await db.get_pack_files(pack_id)
    uname = bot_username or "KoshBox_Bot"
    share_url = f"https://t.me/{uname}?start=pack_{pack['share_token']}"

    max_v = int(pack.get("max_views") or 0)
    cur_v = int(pack.get("views") or 0)
    claims_cnt = int(pack.get("unique_claims") or 0)
    adel_val = int(pack.get("auto_delete_ttl") or 0)
    owner_global_ttl = int((user or {}).get("auto_delete_ttl") or 0)
    if adel_val > 0:
        adel_badge = f"💣 Auto-Del: {format_ttl_short(adel_val)}"
    elif owner_global_ttl > 0:
        adel_badge = f"💣 Auto-Del: Off (Global {format_ttl_short(owner_global_ttl)})"
    else:
        adel_badge = "💣 Auto-Del: Off"

    exp_lbl_badge = format_expiry_badge(pack.get("expires_at"))
    state_str = "⏸ Paused" if pack.get("is_paused") else "🟢 Active"

    sec_parts = [state_str, format_max_views_badge(cur_v, max_v)]
    if pack.get("one_time_per_user"):
        sec_parts.append(f"👤 1×/User ({claims_cnt})")
    if pack.get("pin_hash"):
        sec_parts.append("🔑 PIN")
    if pack.get("protect_content"):
        sec_parts.append("🛡️ No-Fwd")
    sec_parts.append(adel_badge)
    sec_parts.append(f"⏳ Link: {exp_lbl_badge}")

    one_time_on = bool(pack.get("one_time_per_user"))
    one_time_lbl = f"👤 1×/User: {'ON' if one_time_on else 'OFF'}"
    pin_lbl = "🔑 PIN: ON" if pack.get("pin_hash") else "🔑 Set PIN"
    prot_lbl = toggle_label(bool(pack.get("protect_content")), "No-Fwd")
    adel_lbl = f"💣 Auto-Del: {format_ttl_short(adel_val)}"
    exp_lbl = f"⏳ Link: {exp_lbl_badge}"
    lim_lbl = f"👁 Limit: {cur_v}/{max_v}" if max_v > 0 else "👁 Limit: ∞"
    pause_lbl = "▶️ Resume" if pack.get("is_paused") else "⏸ Pause"

    if sec_mode:
        sec_text = (
            f"{breadcrumb('🏠 Home', '🎒 Packs', esc(pack['title'][:16]), '🔒 Security')}\n"
            f"🔒 <b>Pack Link &amp; Security</b>\n"
            f"🎒 <b>{esc(pack['title'])}</b> <i>({pack['item_count']} files)</i>\n"
            f"<blockquote>"
            f"📡 <b>Status:</b> {state_str}  •  {format_max_views_badge(cur_v, max_v)}  •  ⏳ <b>Link:</b> {exp_lbl_badge}\n"
            f"👤 <b>1×/User:</b> {'🟢 ON (' + str(claims_cnt) + ')' if one_time_on else '⚪ OFF'}  •  {adel_badge}\n"
            f"🛡️ <b>No-Fwd:</b> {'🟢 ON' if pack.get('protect_content') else '⚪ OFF'}  •  🔑 <b>PIN:</b> {'🟢 ON' if pack.get('pin_hash') else '⚪ OFF'}"
            f"</blockquote>\n"
            f"🔗 <code>{share_url}</code>\n"
            f"<i>Tap any protection below to update this pack link immediately:</i>"
        )
        sec_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    copy_btn("📋 Copy Link", share_url, style="primary"),
                    share_url_btn("↗️ Share", share_url, pack["title"]),
                ],
                [
                    InlineKeyboardButton(
                        text=one_time_lbl,
                        callback_data=f"pk:1x:{pack_id}:s",
                        style="success" if one_time_on else None,
                    ),
                    InlineKeyboardButton(
                        text=prot_lbl,
                        callback_data=f"pk:prot:{pack_id}:s",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text=adel_lbl,
                        callback_data=f"pk:adel:{pack_id}:s",
                        style="success" if adel_val > 0 else None,
                    ),
                    InlineKeyboardButton(
                        text=exp_lbl,
                        callback_data=f"pk:exp:{pack_id}:s",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text=pin_lbl,
                        callback_data=f"pk:pin:{pack_id}",
                    ),
                    InlineKeyboardButton(
                        text=lim_lbl,
                        callback_data=f"pk:maxv:{pack_id}:s",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text=pause_lbl,
                        callback_data=f"pk:pause:{pack_id}:s",
                    ),
                    InlineKeyboardButton(
                        text="🔄 New Link",
                        callback_data=f"pk:regen:{pack_id}:s",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="🗑️ Delete Pack",
                        callback_data=f"pk:del_ask:{pack_id}",
                        style="danger",
                    ),
                    back_btn(f"pk:open:{pack_id}", "⬅️ Pack"),
                ],
            ]
        )
        return sec_text, sec_kb

    preview_lines: list[str] = []
    preview_chars = 0
    if pack.get("description"):
        desc_line = f"💬 <i>{esc(pack['description'][:80])}</i>"
        preview_lines.append(desc_line)
        preview_chars += len(desc_line)
    shown_cnt = 0
    for idx_i, f in enumerate(items[:25], start=1):
        ic = MEDIA_ICONS.get(f["media_type"], "📦")
        line = f"<b>{idx_i}.</b> {ic} {esc(f['title'][:32])}"
        if preview_chars + len(line) + 1 > 3000:
            break
        preview_lines.append(line)
        preview_chars += len(line) + 1
        shown_cnt += 1
    if len(items) > shown_cnt:
        preview_lines.append(f"<i>…+{len(items) - shown_cnt} more files</i>")
    if not preview_lines:
        preview_lines.append("<i>No active files</i>")

    quote_tag = "<blockquote expandable>" if len(items) > 4 else "<blockquote>"
    text = (
        f"{breadcrumb('🏠 Home', '🎒 Packs', esc(pack['title'][:18]))}\n"
        f"🎒 <b>{esc(pack['title'])}</b>\n"
        f"<i>{pack['item_count']} files  •  {format_size(pack['total_size'])}  •  {' · '.join(sec_parts)}</i>\n"
        f"{quote_tag}" + "\n".join(preview_lines) + "</blockquote>\n"
        f"🔗 <code>{share_url}</code>"
    )

    active_locks = sum(
        [
            bool(pack.get("pin_hash")),
            bool(pack.get("protect_content")),
            bool(pack.get("is_paused")),
            bool(pack.get("expires_at")),
            max_v > 0,
        ]
    )
    sec_btn_lbl = f"🔒 Link Locks ({active_locks})" if active_locks else "🔒 Link Locks"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                copy_btn("📋 Copy Link", share_url, style="primary"),
                share_url_btn("↗️ Share Link", share_url, pack["title"]),
            ],
            [
                InlineKeyboardButton(text="📥 Get All Files", callback_data=f"pk:get:{pack_id}"),
                InlineKeyboardButton(text="✏️ Edit Files", callback_data=f"pk:edit:{pack_id}"),
            ],
            [
                InlineKeyboardButton(
                    text=one_time_lbl,
                    callback_data=f"pk:1x:{pack_id}",
                    style="success" if one_time_on else None,
                ),
                InlineKeyboardButton(
                    text=adel_lbl,
                    callback_data=f"pk:adel:{pack_id}",
                    style="success" if adel_val > 0 else None,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=sec_btn_lbl,
                    callback_data=f"pk:sec:{pack_id}",
                ),
                InlineKeyboardButton(
                    text=exp_lbl,
                    callback_data=f"pk:exp:{pack_id}",
                ),
            ],
            [
                back_btn("pk:list:0", "⬅️ Packs"),
                home_btn("🏠 Home"),
            ],
        ]
    )
    return text, kb


async def render_pack_edit_drawer(
    user_id: int, pack_id: int, db: Database, page: int = 0
) -> Optional[tuple[str, InlineKeyboardMarkup]]:
    """Manage Pack Title, Welcome Note, Natural Sort, Add Files, or Remove Files."""
    user = await db.get_user(user_id)
    if not user:
        user = await db.ensure_user(user_id)
    pack = await db.get_pack(pack_id)
    if not pack or pack["vault_id"] != user["vault_id"]:
        return None

    items = await db.get_pack_files(pack_id)
    total = len(items)
    total_pages = max(1, (total + PACK_ITEMS_EDIT_PAGE_SIZE - 1) // PACK_ITEMS_EDIT_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * PACK_ITEMS_EDIT_PAGE_SIZE
    slice_items = items[start : start + PACK_ITEMS_EDIT_PAGE_SIZE]

    desc_txt = esc(pack["description"][:70]) if pack.get("description") else "None"
    text = (
        f"{breadcrumb('🏠 Home', '🎒 Packs', esc(pack['title'][:16]), '✏️ Edit')}\n"
        f"✏️ <b>Edit Pack: {esc(pack['title'])}</b>\n"
        f"<blockquote>📦 <b>{total}</b> files  •  💬 <b>Note:</b> <i>{desc_txt}</i>\n"
        f"Tap ✖️ on any file below to remove it from this pack:</blockquote>"
    )

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="➕ Add Files",
                callback_data=f"pk:add:{pack_id}",
                style="primary",
            ),
            InlineKeyboardButton(
                text="🔢 Sort 1→N",
                callback_data=f"pk:sort:{pack_id}",
                style="success",
            ),
        ],
        [
            InlineKeyboardButton(text="✏️ Rename Pack", callback_data=f"pk:ren:{pack_id}"),
            InlineKeyboardButton(text="💬 Welcome Note", callback_data=f"pk:desc:{pack_id}"),
        ],
    ]

    for f in slice_items:
        ic = MEDIA_ICONS.get(f["media_type"], "📦")
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"✖️ {ic} {f['title'][:28]}",
                    callback_data=f"pk:rmf:{pack_id}:{f['id']}:{page}",
                )
            ]
        )

    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(
                InlineKeyboardButton(
                    text="⬅️ Prev", callback_data=f"pk:editp:{pack_id}:{page - 1}"
                )
            )
        nav.append(
            InlineKeyboardButton(text=f"📄 {page + 1}/{total_pages}", callback_data="noop")
        )
        if page + 1 < total_pages:
            nav.append(
                InlineKeyboardButton(
                    text="Next ➡️", callback_data=f"pk:editp:{pack_id}:{page + 1}"
                )
            )
        rows.append(nav)

    rows.append(
        [
            InlineKeyboardButton(
                text="🗑️ Delete Pack",
                callback_data=f"pk:del_ask:{pack_id}",
                style="danger",
            ),
            back_btn(f"pk:open:{pack_id}", "⬅️ Back to Pack"),
        ]
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def _render_batch_collector(
    count: int, append_pack_id: Optional[int] = None
) -> tuple[str, InlineKeyboardMarkup]:
    mode_lbl = "Add Files to Pack" if append_pack_id else "New Pack Builder"
    cancel_target = f"pk:open:{append_pack_id}" if append_pack_id else "pk:list:0"
    text = (
        f"{breadcrumb('🏠 Home', '🎒 Packs', mode_lbl)}\n"
        f"🎒 <b>{mode_lbl}</b> · <b>{count}</b> queued\n"
        f"<blockquote>Send or forward 1 to 100+ files right now.\n"
        f"This card updates live without spamming your chat!</blockquote>"
    )
    rows: list[list[InlineKeyboardButton]] = []
    if count > 0:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"✅ Finish Pack ({count})",
                    callback_data="pk:finish",
                    style="success",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="✖️ Cancel", callback_data=f"fsm:cancel:{cancel_target}")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def _parse_pid_and_sec(data: str) -> tuple[int, bool]:
    parts = data.split(":")
    pid = int(parts[2])
    in_sec = len(parts) >= 4 and parts[3] == "s"
    return pid, in_sec


@router.message(Command("packs"))
async def cmd_packs(message: Message, db: Database, state: Optional[FSMContext] = None) -> None:
    if not message.from_user:
        return
    if state is not None:
        _cleanup_pack_builder(message.from_user.id)
        await state.clear()
    await db.ensure_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.full_name or "",
    )
    text, kb = await render_packs_list(message.from_user.id, db, 0)
    await message.answer(text, reply_markup=kb)


@router.message(Command("batch"))
async def cmd_batch(message: Message, state: FSMContext, db: Database) -> None:
    if not message.from_user:
        return
    await db.ensure_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.full_name or "",
    )
    await state.set_state(PackStates.collecting_items)
    text, kb = _render_batch_collector(0)
    sent = await message.answer(text, reply_markup=kb)
    await state.update_data(
        pack_file_ids=[],
        pack_total_size=0,
        pack_recent_titles=[],
        pack_status_msg_id=getattr(sent, "message_id", None),
        append_pack_id=None,
    )


@router.callback_query(F.data.startswith("pk:list:"))
async def cb_packs_list(
    callback: CallbackQuery,
    db: Database,
    state: Optional[FSMContext] = None,
) -> None:
    if not callback.from_user:
        return
    if state is not None:
        _cleanup_pack_builder(callback.from_user.id)
        await state.clear()
    page = int(callback.data.split(":")[2])
    await _ensure_cb_user(callback, db)
    text, kb = await render_packs_list(callback.from_user.id, db, page)
    await safe_edit_or_send(callback, text, kb, also_ack=True)


@router.callback_query(F.data == "nav:packs")
async def cb_nav_packs(
    callback: CallbackQuery,
    db: Database,
    state: Optional[FSMContext] = None,
) -> None:
    if not callback.from_user:
        return
    if state is not None:
        _cleanup_pack_builder(callback.from_user.id)
        await state.clear()
    await _ensure_cb_user(callback, db)
    text, kb = await render_packs_list(callback.from_user.id, db, 0)
    await safe_edit_or_send(callback, text, kb, also_ack=True)


@router.callback_query(F.data == "pk:new")
async def cb_pack_new(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PackStates.collecting_items)
    text, kb = _render_batch_collector(0)
    if callback.message and getattr(callback.message, "photo", None):
        # Transition cleanly from photo banner to live-editable text card and save the NEW message_id!
        await callback.answer("Drop files to bundle!")
        sent = await callback.message.answer(text, reply_markup=kb)
        try:
            await callback.message.delete()
        except Exception:
            pass
        msg_id = getattr(sent, "message_id", None)
        await state.update_data(
            pack_file_ids=[],
            pack_total_size=0,
            pack_recent_titles=[],
            pack_status_msg_id=msg_id,
            append_pack_id=None,
        )
        return

    msg_id = callback.message.message_id if callback.message else None
    await state.update_data(
        pack_file_ids=[],
        pack_total_size=0,
        pack_recent_titles=[],
        pack_status_msg_id=msg_id,
        append_pack_id=None,
    )
    await safe_edit_or_send(
        callback, text, kb, answer_text="Drop files to bundle!", also_ack=True
    )


@router.callback_query(F.data.startswith("pk:add:"))
async def cb_pack_add_more_files(
    callback: CallbackQuery, state: FSMContext, db: Database
) -> None:
    if not callback.from_user:
        return
    pid = int(callback.data.split(":")[2])
    user = await _ensure_cb_user(callback, db)
    pack = await db.get_pack(pid)
    if not pack or pack["vault_id"] != user["vault_id"]:
        await callback.answer("Pack not found.", show_alert=True)
        return

    await state.set_state(PackStates.collecting_items)
    text, kb = _render_batch_collector(0, append_pack_id=pid)
    msg_id = (
        callback.message.message_id
        if (callback.message and not getattr(callback.message, "photo", None))
        else None
    )
    await state.update_data(
        pack_file_ids=[],
        pack_total_size=0,
        pack_recent_titles=[],
        pack_status_msg_id=msg_id,
        append_pack_id=pid,
    )
    await safe_edit_or_send(
        callback,
        text,
        kb,
        answer_text="Drop files to add to this pack!",
        also_ack=True,
    )


@router.callback_query(F.data.startswith("pk:from_grp:"))
async def cb_pack_from_group(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not callback.from_user:
        return
    gid = int(callback.data.split(":")[2])
    user = await _ensure_cb_user(callback, db)
    grp = await db.get_group(gid)
    if not grp or not await db.is_vault_in_group(user["vault_id"], gid):
        await callback.answer("Folder not found.", show_alert=True)
        return
    files = await db.search_files(
        user["vault_id"], group_id_filter=gid, sort_by="new", limit=500, offset=0
    )
    if not files:
        await callback.answer("Folder has no files to pack.", show_alert=True)
        return
    pack = await db.create_pack(
        vault_id=user["vault_id"],
        creator_user_id=user["user_id"],
        title=grp["name"],
        file_ids=[f["id"] for f in files],
        protect_content=bool(user.get("protect_content")),
    )
    rendered = await render_pack_detail(
        callback.from_user.id, pack["id"], db, bot, bot_username=bot_username
    )
    if rendered:
        await safe_edit_or_send(
            callback,
            rendered[0],
            rendered[1],
            answer_text=f"🎒 Packed {len(files)} files!",
            also_ack=True,
        )
    else:
        await callback.answer(f"🎒 Packed {len(files)} files!")


@router.callback_query(F.data.startswith("pk:quick_album:"))
async def cb_pack_quick_album(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not callback.from_user:
        return
    raw_ids = callback.data.split(":", 2)[2]
    fids = [int(x) for x in raw_ids.split(",") if x.isdigit()]
    if not fids:
        await callback.answer("No files found.", show_alert=True)
        return
    user = await _ensure_cb_user(callback, db)
    allowed_files: list[dict[str, Any]] = []
    for fid in fids:
        f_row = await db.get_file(fid)
        if f_row and not f_row.get("is_trashed") and await db.can_vault_access_file(user["vault_id"], f_row):
            allowed_files.append(f_row)
    if not allowed_files:
        await callback.answer("No accessible files found.", show_alert=True)
        return
    allowed_fids = [int(f["id"]) for f in allowed_files]
    first = allowed_files[0]
    title = (
        (first["title"][:28] + f" (+{len(allowed_fids)-1})")
        if len(allowed_fids) > 1
        else "Album Pack"
    )
    pack = await db.create_pack(
        vault_id=user["vault_id"],
        creator_user_id=user["user_id"],
        title=title,
        file_ids=allowed_fids,
        protect_content=bool(user.get("protect_content")),
    )
    rendered = await render_pack_detail(
        callback.from_user.id, pack["id"], db, bot, bot_username=bot_username
    )
    if rendered:
        await safe_edit_or_send(
            callback,
            rendered[0],
            rendered[1],
            answer_text="🎒 Pack Link Ready!",
            also_ack=True,
        )
    else:
        await callback.answer("🎒 Pack Link Ready!")


@router.message(PackStates.collecting_items, Command("done"))
async def cmd_batch_done(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
    config: Optional[Config] = None,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not message.from_user:
        return
    from handlers.upload import flush_pending_pack_builder_uploads

    await flush_pending_pack_builder_uploads(
        message.from_user.id, state, bot, db, config, bot_username=bot_username
    )
    data = await state.get_data()
    fids: list[int] = data.get("pack_file_ids") or []
    append_pid: Optional[int] = data.get("append_pack_id")
    if not fids:
        await message.answer("⚠️ Drop at least 1 file first, or /cancel.")
        return

    if append_pid:
        await state.clear()
        user = await db.ensure_user(
            message.from_user.id,
            message.from_user.username or "",
            message.from_user.full_name or "",
        )
        target_pack = await db.get_pack(append_pid)
        if not target_pack or target_pack["vault_id"] != user["vault_id"]:
            await message.answer("⚠️ Target pack no longer exists.")
            return
        await db.append_files_to_pack(append_pid, fids)
        rendered = await render_pack_detail(
            message.from_user.id, append_pid, db, bot, bot_username=bot_username
        )
        if rendered:
            await message.answer(
                f"✅ <b>Added {len(fids)} file(s)</b>\n{rendered[0]}",
                reply_markup=rendered[1],
                disable_web_page_preview=True,
            )
        return

    await state.set_state(PackStates.waiting_for_title)
    await message.answer(
        f"✏️ <b>Name Your Pack</b> <i>({len(fids)} files)</i>\n"
        f"<blockquote>Send a short title, or tap <b>⚡ Auto Title</b>:</blockquote>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⚡ Auto Title",
                        callback_data="pk:autotitle",
                        style="primary",
                    )
                ],
                [InlineKeyboardButton(text="✖️ Cancel", callback_data="fsm:cancel:pk:list:0")],
            ]
        ),
    )


@router.callback_query(PackStates.collecting_items, F.data == "pk:finish")
async def cb_batch_finish(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    bot: Bot,
    config: Optional[Config] = None,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not callback.from_user:
        return
    from handlers.upload import flush_pending_pack_builder_uploads

    await flush_pending_pack_builder_uploads(
        callback.from_user.id, state, bot, db, config, bot_username=bot_username
    )
    data = await state.get_data()
    fids: list[int] = data.get("pack_file_ids") or []
    append_pid: Optional[int] = data.get("append_pack_id")
    if not fids:
        await callback.answer("Drop at least 1 file first!", show_alert=True)
        return

    if append_pid:
        await state.clear()
        user = await _ensure_cb_user(callback, db)
        target_pack = await db.get_pack(append_pid)
        if not target_pack or target_pack["vault_id"] != user["vault_id"]:
            await callback.answer("⚠️ Target pack no longer exists.", show_alert=True)
            return
        await db.append_files_to_pack(append_pid, fids)
        rendered = await render_pack_detail(
            callback.from_user.id, append_pid, db, bot, bot_username=bot_username
        )
        if rendered:
            await safe_edit_or_send(
                callback,
                rendered[0],
                rendered[1],
                answer_text=f"✅ Added {len(fids)} file(s)!",
                also_ack=True,
            )
        else:
            await callback.answer(f"✅ Added {len(fids)} file(s)!")
        return

    await state.set_state(PackStates.waiting_for_title)
    await safe_edit_or_send(
        callback,
        f"✏️ <b>Name Your Pack</b> <i>({len(fids)} files)</i>\n"
        f"<blockquote>Send a short title, or tap <b>⚡ Auto Title</b>:</blockquote>",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⚡ Auto Title",
                        callback_data="pk:autotitle",
                        style="primary",
                    )
                ],
                [InlineKeyboardButton(text="✖️ Cancel", callback_data="fsm:cancel:pk:list:0")],
            ]
        ),
        also_ack=True,
    )


@router.callback_query(PackStates.waiting_for_title, F.data == "pk:autotitle")
async def cb_pack_auto_title(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not callback.from_user:
        return
    data = await state.get_data()
    fids: list[int] = data.get("pack_file_ids") or []
    await state.clear()
    if not fids:
        await callback.answer("No files queued.", show_alert=True)
        return
    user = await _ensure_cb_user(callback, db)
    first = await db.get_file(fids[0])
    title = (
        f"{first['title'][:32]} ({len(fids)})"
        if first
        else f"Pack ({len(fids)} files)"
    )
    pack = await db.create_pack(
        vault_id=user["vault_id"],
        creator_user_id=user["user_id"],
        title=title,
        file_ids=fids,
        protect_content=bool(user.get("protect_content")),
    )
    rendered = await render_pack_detail(
        callback.from_user.id, pack["id"], db, bot, bot_username=bot_username
    )
    if rendered:
        await safe_edit_or_send(
            callback,
            rendered[0],
            rendered[1],
            answer_text="🎒 Pack Ready!",
            also_ack=True,
        )
    else:
        await callback.answer("🎒 Pack Ready!")


@router.message(PackStates.waiting_for_title, F.text, ~F.text.startswith('/'))
async def fsm_pack_save_title(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not message.from_user or not message.text:
        return
    raw = message.text.strip()
    if raw.startswith("/") and raw.lower().split()[0] in ("/cancel", "/start"):
        await state.clear()
        return
    data = await state.get_data()
    fids: list[int] = data.get("pack_file_ids") or []
    await state.clear()
    if not fids:
        return
    title = f"Pack ({len(fids)} files)" if raw == "-" else raw[:60]
    user = await db.ensure_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.full_name or "",
    )
    pack = await db.create_pack(
        vault_id=user["vault_id"],
        creator_user_id=user["user_id"],
        title=title,
        file_ids=fids,
        protect_content=bool(user.get("protect_content")),
    )
    rendered = await render_pack_detail(
        message.from_user.id, pack["id"], db, bot, bot_username=bot_username
    )
    if rendered:
        await send_with_effect(
            message.answer,
            rendered[0],
            reply_markup=rendered[1],
            disable_web_page_preview=True,
            message_effect_id=EFFECT_PARTY,
        )


@router.callback_query(F.data.startswith("pk:open:"))
async def cb_pack_open(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
    state: Optional[FSMContext] = None,
) -> None:
    if not callback.from_user:
        return
    if state is not None:
        _cleanup_pack_builder(callback.from_user.id)
        await state.clear()
    await _ensure_cb_user(callback, db)
    pid = int(callback.data.split(":")[2])
    rendered = await render_pack_detail(
        callback.from_user.id, pid, db, bot, bot_username=bot_username, sec_mode=False
    )
    if not rendered:
        await callback.answer("Pack not found.", show_alert=True)
        return
    await safe_edit_or_send(callback, rendered[0], rendered[1], also_ack=True)


@router.callback_query(F.data.startswith("pk:sec:"))
async def cb_pack_security_drawer(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
    state: Optional[FSMContext] = None,
) -> None:
    if not callback.from_user:
        return
    if state is not None:
        _cleanup_pack_builder(callback.from_user.id)
        await state.clear()
    await _ensure_cb_user(callback, db)
    pid = int(callback.data.split(":")[2])
    rendered = await render_pack_detail(
        callback.from_user.id, pid, db, bot, bot_username=bot_username, sec_mode=True
    )
    if not rendered:
        await callback.answer("Pack not found.", show_alert=True)
        return
    await safe_edit_or_send(callback, rendered[0], rendered[1], also_ack=True)


@router.callback_query(F.data.startswith("pk:edit:"))
async def cb_pack_edit_drawer(
    callback: CallbackQuery,
    db: Database,
    state: Optional[FSMContext] = None,
) -> None:
    if not callback.from_user:
        return
    if state is not None:
        _cleanup_pack_builder(callback.from_user.id)
        await state.clear()
    await _ensure_cb_user(callback, db)
    pid = int(callback.data.split(":")[2])
    rendered = await render_pack_edit_drawer(callback.from_user.id, pid, db, page=0)
    if not rendered:
        await callback.answer("Pack not found.", show_alert=True)
        return
    await safe_edit_or_send(callback, rendered[0], rendered[1], also_ack=True)


@router.callback_query(F.data.startswith("pk:editp:"))
async def cb_pack_edit_page(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    await _ensure_cb_user(callback, db)
    _, _, pid_s, page_s = callback.data.split(":")
    rendered = await render_pack_edit_drawer(
        callback.from_user.id, int(pid_s), db, page=int(page_s)
    )
    if not rendered:
        await callback.answer("Pack not found.", show_alert=True)
        return
    await safe_edit_or_send(callback, rendered[0], rendered[1], also_ack=True)


@router.callback_query(F.data.startswith("pk:sort:"))
async def cb_pack_natural_sort(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    pid = int(callback.data.split(":")[2])
    user = await _ensure_cb_user(callback, db)
    pack = await db.get_pack(pid)
    if not pack or not user or pack["vault_id"] != user["vault_id"]:
        await callback.answer("Pack not found.", show_alert=True)
        return
    sorted_cnt = await db.natural_sort_pack_items(pid)
    rendered = await render_pack_edit_drawer(callback.from_user.id, pid, db, page=0)
    ans = f"🔢 Sorted {sorted_cnt} files in natural 1→N order!"
    if rendered:
        await safe_edit_or_send(
            callback,
            rendered[0],
            rendered[1],
            answer_text=ans,
            also_ack=True,
        )
    else:
        await callback.answer(ans)


@router.callback_query(F.data.startswith("pk:rmf:"))
async def cb_pack_remove_file(
    callback: CallbackQuery, db: Database, bot: Bot
) -> None:
    if not callback.from_user:
        return
    _, _, pid_s, fid_s, page_s = callback.data.split(":")
    pid, fid, page = int(pid_s), int(fid_s), int(page_s)
    user = await _ensure_cb_user(callback, db)
    pack = await db.get_pack(pid)
    if not pack or pack["vault_id"] != user["vault_id"]:
        await callback.answer("Pack not found.", show_alert=True)
        return

    items = await db.get_pack_files(pid)
    if len(items) <= 1:
        await callback.answer(
            "Pack must keep at least 1 file (or delete the pack).", show_alert=True
        )
        return

    await db.remove_file_from_pack(pid, fid)
    rendered = await render_pack_edit_drawer(callback.from_user.id, pid, db, page=page)
    if rendered:
        await safe_edit_or_send(
            callback,
            rendered[0],
            rendered[1],
            answer_text="Removed from pack",
            also_ack=True,
        )
    else:
        await callback.answer("Removed from pack")


@router.callback_query(F.data.startswith("pk:ren:"))
async def cb_pack_rename_prompt(
    callback: CallbackQuery, state: FSMContext, db: Database
) -> None:
    if not callback.from_user:
        return
    pid = int(callback.data.split(":")[2])
    user = await _ensure_cb_user(callback, db)
    pack = await db.get_pack(pid)
    if not pack or pack["vault_id"] != user["vault_id"]:
        await callback.answer("Pack not found.", show_alert=True)
        return
    await state.set_state(PackStates.waiting_for_rename)
    await state.update_data(rename_pack_id=pid)
    await safe_edit_or_send(
        callback,
        f"✏️ <b>Rename Pack</b> · <code>{esc(pack['title'])}</code>\n"
        f"<blockquote>Send new pack title (max 60 chars):</blockquote>",
        cancel_kb(f"pk:edit:{pid}"),
        also_ack=True,
    )


@router.message(PackStates.waiting_for_rename, F.text, ~F.text.startswith('/'))
async def fsm_pack_rename_save(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not message.from_user or not message.text:
        return
    raw = message.text.strip()
    if raw.startswith("/") and raw.lower().split()[0] in ("/cancel", "/start"):
        await state.clear()
        return
    data = await state.get_data()
    pid = data.get("rename_pack_id")
    await state.clear()
    if not pid:
        return
    user = await db.get_user(message.from_user.id)
    pack = await db.get_pack(pid)
    if not pack or not user or pack["vault_id"] != user["vault_id"]:
        return
    await db.rename_pack(pid, raw[:60])
    rendered = await render_pack_detail(
        message.from_user.id, pid, db, bot, bot_username=bot_username
    )
    if rendered:
        await message.answer(
            rendered[0], reply_markup=rendered[1], disable_web_page_preview=True
        )


@router.callback_query(F.data.startswith("pk:desc:"))
async def cb_pack_desc_prompt(
    callback: CallbackQuery, state: FSMContext, db: Database
) -> None:
    if not callback.from_user:
        return
    pid = int(callback.data.split(":")[2])
    user = await _ensure_cb_user(callback, db)
    pack = await db.get_pack(pid)
    if not pack or pack["vault_id"] != user["vault_id"]:
        await callback.answer("Pack not found.", show_alert=True)
        return
    await state.set_state(PackStates.waiting_for_description)
    await state.update_data(desc_pack_id=pid)
    cur_note = esc(pack["description"]) if pack.get("description") else "None"
    await safe_edit_or_send(
        callback,
        f"💬 <b>Pack Welcome Note</b> · <i>{cur_note}</i>\n"
        f"<blockquote>Send a short note shown when someone opens your pack link (or <code>-</code> to clear):</blockquote>",
        cancel_kb(f"pk:edit:{pid}"),
        also_ack=True,
    )


@router.message(PackStates.waiting_for_description, F.text, ~F.text.startswith('/'))
async def fsm_pack_desc_save(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not message.from_user or not message.text:
        return
    raw = message.text.strip()
    if raw.startswith("/") and raw.lower().split()[0] in ("/cancel", "/start"):
        await state.clear()
        return
    data = await state.get_data()
    pid = data.get("desc_pack_id")
    await state.clear()
    if not pid:
        return
    user = await db.get_user(message.from_user.id)
    pack = await db.get_pack(pid)
    if not pack or not user or pack["vault_id"] != user["vault_id"]:
        return
    note = "" if raw == "-" else raw[:300]
    await db.set_pack_description(pid, note)
    rendered = await render_pack_detail(
        message.from_user.id, pid, db, bot, bot_username=bot_username
    )
    if rendered:
        await message.answer(
            rendered[0], reply_markup=rendered[1], disable_web_page_preview=True
        )


@router.callback_query(F.data.startswith("pk:get:"))
async def cb_pack_get_all(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    config: Optional[Config] = None,
) -> None:
    if not callback.from_user or not callback.message:
        return
    pid = int(callback.data.split(":")[2])
    user = await _ensure_cb_user(callback, db)
    pack = await db.get_pack(pid)
    if not pack or not user or pack["vault_id"] != user["vault_id"]:
        await callback.answer("Pack not found.", show_alert=True)
        return
    await callback.answer("🔓 Owner Access — permanent & unrestricted")
    await deliver_pack_to_chat(
        bot,
        callback.message.chat.id,
        pack,
        db,
        auto_delete_ttl=0,
        force_protect=False,
        is_owner=True,
        count_view=False,
        dump_channel_id=config.dump_channel_id if config else None,
    )


@router.callback_query(F.data.startswith("pk:pin:"))
async def cb_pack_pin_prompt(
    callback: CallbackQuery, state: FSMContext, db: Database
) -> None:
    if not callback.from_user:
        return
    pid = int(callback.data.split(":")[2])
    user = await _ensure_cb_user(callback, db)
    pack = await db.get_pack(pid)
    if not pack or pack["vault_id"] != user["vault_id"]:
        await callback.answer("Pack not found.", show_alert=True)
        return
    await state.set_state(PackStates.waiting_for_pin)
    await state.update_data(pin_pack_id=pid)
    status = "🟢 Active" if pack.get("pin_hash") else "⚪ Off"
    await safe_edit_or_send(
        callback,
        f"🔑 <b>Pack PIN / Password</b> · <i>{status}</i>\n"
        f"<blockquote>Send a PIN/password (up to 24 chars), or send <code>-</code> to remove PIN:</blockquote>",
        cancel_kb(f"pk:open:{pid}"),
        also_ack=True,
    )


@router.message(PackStates.waiting_for_pin, F.text, ~F.text.startswith('/'))
async def fsm_pack_set_pin(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not message.from_user or not message.text:
        return
    raw = message.text.strip()
    if raw.startswith("/") and raw.lower().split()[0] in ("/cancel", "/start"):
        await state.clear()
        return
    data = await state.get_data()
    pid = data.get("pin_pack_id")
    await state.clear()
    if not pid:
        return
    user = await db.get_user(message.from_user.id)
    pack = await db.get_pack(pid)
    if not pack or not user or pack["vault_id"] != user["vault_id"]:
        return
    pin = None if raw == "-" else raw[:24]
    await db.set_pack_pin(pid, pin)
    try:
        await message.delete()
    except Exception:
        pass
    rendered = await render_pack_detail(
        message.from_user.id, pid, db, bot, bot_username=bot_username, sec_mode=True
    )
    if rendered:
        await message.answer(
            rendered[0], reply_markup=rendered[1], disable_web_page_preview=True
        )


@router.callback_query(F.data.startswith("pk:1x:"))
async def cb_pack_toggle_one_time(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not callback.from_user:
        return
    pid, in_sec = _parse_pid_and_sec(callback.data)
    user = await _ensure_cb_user(callback, db)
    pack = await db.get_pack(pid)
    if not pack or pack["vault_id"] != user["vault_id"]:
        await callback.answer("Pack not found.", show_alert=True)
        return
    on = await db.toggle_pack_one_time(pid)
    rendered = await render_pack_detail(
        callback.from_user.id, pid, db, bot, bot_username=bot_username, sec_mode=in_sec
    )
    ans = "👤 1×/User Trial Lock ON" if on else "👤 1×/User Trial Lock OFF"
    if rendered:
        await safe_edit_or_send(
            callback, rendered[0], rendered[1], answer_text=ans, also_ack=True
        )
    else:
        await callback.answer(ans)


@router.callback_query(F.data.startswith("pk:prot:"))
async def cb_pack_toggle_prot(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not callback.from_user:
        return
    pid, in_sec = _parse_pid_and_sec(callback.data)
    user = await _ensure_cb_user(callback, db)
    pack = await db.get_pack(pid)
    if not pack or pack["vault_id"] != user["vault_id"]:
        await callback.answer("Pack not found.", show_alert=True)
        return
    on = await db.toggle_pack_protect(pid)
    rendered = await render_pack_detail(
        callback.from_user.id, pid, db, bot, bot_username=bot_username, sec_mode=in_sec
    )
    ans = "🛡️ Content Protection ON" if on else "Content Protection OFF"
    if rendered:
        await safe_edit_or_send(
            callback, rendered[0], rendered[1], answer_text=ans, also_ack=True
        )
    else:
        await callback.answer(ans)


@router.callback_query(F.data.startswith("pk:adel:"))
async def cb_pack_cycle_auto_delete(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not callback.from_user:
        return
    pid, in_sec = _parse_pid_and_sec(callback.data)
    user = await _ensure_cb_user(callback, db)
    pack = await db.get_pack(pid)
    if not pack or pack["vault_id"] != user["vault_id"]:
        await callback.answer("Pack not found.", show_alert=True)
        return
    new_ttl = await db.cycle_pack_auto_delete_ttl(pid)
    owner_ttl = int((user or {}).get("auto_delete_ttl") or 0)
    rendered = await render_pack_detail(
        callback.from_user.id, pid, db, bot, bot_username=bot_username, sec_mode=in_sec
    )
    if new_ttl > 0:
        ans = f"💣 Auto-Delete: {format_ttl_short(new_ttl)} (overrides global)"
    elif owner_ttl > 0:
        ans = f"💣 Per-link Off (uses global {format_ttl_short(owner_ttl)})"
    else:
        ans = "💣 Auto-Delete: Off"
    if rendered:
        await safe_edit_or_send(
            callback, rendered[0], rendered[1], answer_text=ans, also_ack=True
        )
    else:
        await callback.answer(ans)


@router.callback_query(F.data.startswith("pk:exp:"))
async def cb_pack_cycle_exp(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not callback.from_user:
        return
    pid, in_sec = _parse_pid_and_sec(callback.data)
    user = await _ensure_cb_user(callback, db)
    pack = await db.get_pack(pid)
    if not pack or pack["vault_id"] != user["vault_id"]:
        await callback.answer("Pack not found.", show_alert=True)
        return
    new_exp = await db.cycle_pack_expiry(pid)
    rendered = await render_pack_detail(
        callback.from_user.id, pid, db, bot, bot_username=bot_username, sec_mode=in_sec
    )
    ans = f"⏳ Link Expiry: {format_expiry_badge(new_exp)}"
    if rendered:
        await safe_edit_or_send(
            callback, rendered[0], rendered[1], answer_text=ans, also_ack=True
        )
    else:
        await callback.answer(ans)


@router.callback_query(F.data.startswith("pk:maxv:"))
async def cb_pack_cycle_max_views(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not callback.from_user:
        return
    pid, in_sec = _parse_pid_and_sec(callback.data)
    user = await _ensure_cb_user(callback, db)
    pack = await db.get_pack(pid)
    if not pack or pack["vault_id"] != user["vault_id"]:
        await callback.answer("Pack not found.", show_alert=True)
        return
    lim = await db.cycle_pack_max_views(pid)
    rendered = await render_pack_detail(
        callback.from_user.id, pid, db, bot, bot_username=bot_username, sec_mode=in_sec
    )
    lbl = f"{lim} opens max" if lim > 0 else "Unlimited opens"
    ans = f"👁 Access Limit: {lbl}"
    if rendered:
        await safe_edit_or_send(
            callback, rendered[0], rendered[1], answer_text=ans, also_ack=True
        )
    else:
        await callback.answer(ans)


@router.callback_query(F.data.startswith("pk:pause:"))
async def cb_pack_toggle_pause(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not callback.from_user:
        return
    pid, in_sec = _parse_pid_and_sec(callback.data)
    user = await _ensure_cb_user(callback, db)
    pack = await db.get_pack(pid)
    if not pack or pack["vault_id"] != user["vault_id"]:
        await callback.answer("Pack not found.", show_alert=True)
        return
    paused = await db.toggle_pack_paused(pid)
    rendered = await render_pack_detail(
        callback.from_user.id, pid, db, bot, bot_username=bot_username, sec_mode=in_sec
    )
    ans = "⏸ Link Paused" if paused else "▶️ Link Active"
    if rendered:
        await safe_edit_or_send(
            callback, rendered[0], rendered[1], answer_text=ans, also_ack=True
        )
    else:
        await callback.answer(ans)


@router.callback_query(F.data.startswith("pk:regen:"))
async def cb_pack_regen(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not callback.from_user:
        return
    pid, in_sec = _parse_pid_and_sec(callback.data)
    user = await _ensure_cb_user(callback, db)
    pack = await db.get_pack(pid)
    if not pack or pack["vault_id"] != user["vault_id"]:
        await callback.answer("Pack not found.", show_alert=True)
        return
    await db.regenerate_pack_token(pid)
    rendered = await render_pack_detail(
        callback.from_user.id, pid, db, bot, bot_username=bot_username, sec_mode=in_sec
    )
    ans = "🔄 New pack link generated (views & claims reset)"
    if rendered:
        await safe_edit_or_send(
            callback, rendered[0], rendered[1], answer_text=ans, also_ack=True
        )
    else:
        await callback.answer(ans)


@router.callback_query(F.data.startswith("pk:del_ask:"))
async def cb_pack_del_ask(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    pid = int(callback.data.split(":")[2])
    user = await _ensure_cb_user(callback, db)
    pack = await db.get_pack(pid)
    if not pack or pack["vault_id"] != user["vault_id"]:
        await callback.answer("Pack not found.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                back_btn(f"pk:open:{pid}", "⬅️ Cancel"),
                InlineKeyboardButton(
                    text="💥 Delete Pack",
                    callback_data=f"pk:del:{pid}",
                    style="danger",
                ),
            ]
        ]
    )
    await safe_edit_or_send(
        callback,
        f"⚠️ <b>Delete Pack '{esc(pack['title'])}'?</b>\n"
        f"<blockquote>The share link will stop working, while your saved vault files remain completely safe.</blockquote>",
        kb,
        also_ack=True,
    )


@router.callback_query(F.data.startswith("pk:del:"))
async def cb_pack_del(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    pid = int(callback.data.split(":")[2])
    user = await _ensure_cb_user(callback, db)
    deleted = await db.delete_pack(pid, user["vault_id"])
    if not deleted:
        await callback.answer("Pack not found.", show_alert=True)
        return
    text, kb = await render_packs_list(callback.from_user.id, db, 0)
    await safe_edit_or_send(
        callback, text, kb, answer_text="Pack deleted", also_ack=True
    )
