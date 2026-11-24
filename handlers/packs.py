import asyncio
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
from utils.formatters import (
    BATCH_CHUNK_SIZE,
    EFFECT_FIRE,
    EFFECT_PARTY,
    MEDIA_ICONS,
    back_btn,
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

PACKS_PAGE_SIZE = 5
PACK_DELIVERY_BATCH_SIZE = 100
PACK_ITEMS_EDIT_PAGE_SIZE = 6

# Per-(chat_id, pack_id) lock to prevent duplicate overlapping deliveries on rapid clicks
_ACTIVE_DELIVERIES: set[tuple[int, int]] = set()


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
    - Full Access Grant (`has_full_access=True`): ZERO auto-delete, ZERO trial notice (respects `protect_content` if set).
    - Copies up to 100 files from `dump_channel_id` in a SINGLE Bot API call (`copy_messages`),
      preserving `protect_content` (No-Forward / No-Save) and cutting 100 HTTP calls down to 1!
    - Gracefully falls back to `send_stored_media` if `dump_channel_id` is unavailable or custom captions require it.
    - Persists restart-safe `scheduled_deletes` timer + post-expiry `🔓 Request Full Access` & `💬 Contact Owner` notice!
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

        def _collect_copied_ids(copied_res: Any) -> int:
            added = 0
            if not copied_res:
                return 0
            for m_obj in copied_res:
                mid_val = getattr(m_obj, "message_id", None)
                if mid_val is None and isinstance(m_obj, dict):
                    mid_val = m_obj.get("message_id")
                if mid_val is None and isinstance(m_obj, int):
                    mid_val = m_obj
                if mid_val:
                    sent_ids.append(int(mid_val))
                added += 1
            return added

        if can_batch_copy:
            # Always preserve exact `pack_items.position ASC, pack_items.id ASC` order (`slice_items`)!
            # NEVER call `sorted(dump_ids)` in a way that scrambles `pack_items.position`!
            dump_ids = [int(f["dump_message_id"]) for f in slice_items]
            strictly_ascending = all(
                dump_ids[i] < dump_ids[i + 1] for i in range(len(dump_ids) - 1)
            )

            if strictly_ascending:
                # 100% fast path: strictly ascending dump_message_ids -> 1 native `copy_messages` per chunk!
                try:
                    for c_start in range(0, len(dump_ids), BATCH_CHUNK_SIZE):
                        chunk_ids = dump_ids[c_start : c_start + BATCH_CHUNK_SIZE]
                        copied_res = await tg_retry_call(
                            copy_batch_fn,
                            chat_id=chat_id,
                            from_chat_id=dump_channel_id,
                            message_ids=chunk_ids,
                            protect_content=protect,
                        )
                        delivered += _collect_copied_ids(copied_res)
                except Exception:
                    if delivered == 0:
                        delivered = 0
            else:
                # Older / manually reordered / naturally sorted pack where `dump_message_id`s are NOT
                # globally strictly ascending: split into maximal consecutive strictly ascending runs!
                # - Runs of length >= 2 use `bot.copy_messages()` (up to 100 per call).
                # - Singletons (or failed run copies) fall back to `send_stored_media()` in exact position order!
                runs: list[list[dict[str, Any]]] = []
                for item_f in slice_items:
                    cur_mid = int(item_f["dump_message_id"])
                    if runs and int(runs[-1][-1]["dump_message_id"]) < cur_mid:
                        runs[-1].append(item_f)
                    else:
                        runs.append([item_f])

                op_count = 0
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
                                    copied_cnt = _collect_copied_ids(copied_res)
                                    if copied_cnt > 0:
                                        delivered += copied_cnt
                                        op_count += 1
                                        await asyncio.sleep(0.05 if op_count % 15 else 0.25)
                                        continue
                                except Exception:
                                    pass
                            # Per-item sequential fallback for singleton tail or failed batch run
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

        if delivered == 0:
            # Clean fallback to cached file_id delivery in exact `pack_items.position ASC` order
            for idx, f in enumerate(slice_items):
                sent = await send_stored_media(
                    bot,
                    chat_id,
                    f,
                    protect_content=protect,
                    auto_delete_ttl=0,  # grouped cleanup below
                )
                if sent:
                    delivered += 1
                    if getattr(sent, "message_id", None):
                        sent_ids.append(sent.message_id)
                await asyncio.sleep(0.05 if (idx + 1) % 15 else 0.25)

        remaining = total_items - end_idx
        if remaining > 0:
            nxt_count = min(PACK_DELIVERY_BATCH_SIZE, remaining)
            cont_kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=f"📥 Next {nxt_count} ({end_idx + 1}–{end_idx + nxt_count}/{total_items})",
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
    """Delivers the next paginated batch of files for a large Pack (e.g. 100+ files)."""
    if not callback.from_user or not callback.message:
        return
    parts = callback.data.split(":")
    if len(parts) < 4 or not parts[3].isdigit():
        await callback.answer("Invalid batch cursor.", show_alert=True)
        return
    token = parts[2]
    offset = int(parts[3])

    # Allow continuation even if max_views or 1x/user was recorded on initial unlock
    pack = await db.get_pack_by_token(token, include_unavailable=True)
    if not pack or pack["item_count"] == 0:
        await callback.answer("⚠️ Pack no longer available.", show_alert=True)
        return

    visitor = await db.ensure_user(
        callback.from_user.id,
        callback.from_user.username or "",
        callback.from_user.full_name or "",
    )
    is_owner = (
        visitor["user_id"] == pack["creator_user_id"]
        or visitor["vault_id"] == pack["vault_id"]
    )
    grant = (
        None
        if is_owner
        else await db.get_active_access_grant("pack", pack["id"], visitor["user_id"])
    )
    has_full_access = grant is not None

    if not is_owner and not has_full_access:
        if pack.get("is_paused"):
            await callback.answer("⏸ This pack link was paused by the owner.", show_alert=True)
            return
        import time as _time

        if pack.get("expires_at") and int(pack["expires_at"]) < int(_time.time()):
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
    """Crisp Multi-File Packs Hub."""
    user = await db.get_user(user_id)
    total = await db.count_vault_packs(user["vault_id"])
    offset = max(0, page) * PACKS_PAGE_SIZE
    rows = await db.list_vault_packs(
        user["vault_id"], limit=PACKS_PAGE_SIZE + 1, offset=offset
    )
    has_next = len(rows) > PACKS_PAGE_SIZE
    packs = rows[:PACKS_PAGE_SIZE]
    total_pages = max(1, (total + PACKS_PAGE_SIZE - 1) // PACKS_PAGE_SIZE)

    lines = [
        breadcrumb("Vault", "Packs"),
        f"🎒 <b>File Packs</b> · <i>{total} total</i>",
    ]
    buttons: list[list[InlineKeyboardButton]] = []

    if not packs:
        lines.append(
            "<blockquote>Drop 2+ files together to auto-pack, or tap <b>➕ New Pack</b> to bundle with PIN &amp; limits.</blockquote>"
        )
    else:
        for p in packs:
            badges = ""
            if p.get("is_paused"):
                badges += "⏸ "
            if p.get("one_time_per_user"):
                badges += "👤 "
            if int(p.get("auto_delete_ttl") or 0) > 0:
                badges += "💣 "
            if p.get("pin_hash"):
                badges += "🔒 "
            lbl = f"{badges}🎒 {p['title'][:20]} ({p['item_count']}) · {format_size(p['total_size'])}"
            buttons.append(
                [InlineKeyboardButton(text=lbl, callback_data=f"pk:open:{p['id']}")]
            )

    if page > 0 or has_next:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀", callback_data=f"pk:list:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
        if has_next:
            nav.append(InlineKeyboardButton(text="▶", callback_data=f"pk:list:{page + 1}"))
        buttons.append(nav)

    buttons.append(
        [
            InlineKeyboardButton(
                text="➕ New Pack",
                callback_data="pk:new",
                style="primary",
            ),
            back_btn("nav:home", "◀ Vault"),
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


async def render_pack_detail(
    user_id: int,
    pack_id: int,
    db: Database,
    bot: Optional[Bot] = None,
    bot_username: str = "KoshBox_Bot",
) -> Optional[tuple[str, InlineKeyboardMarkup]]:
    """
    Advanced Bug-Proof Pack Detail & Share Link Console:
    - 1-tap Copy Link + Native Share Sheet
    - Get All, Edit Title, Add/Remove Files
    - PIN Lock, No-Forward, Expiry Timer, Max Views Limit, Pause/Resume, Reset Link, Delete
    """
    user = await db.get_user(user_id)
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

    sec_parts = [format_max_views_badge(cur_v, max_v)]
    if pack.get("one_time_per_user"):
        sec_parts.append(f"👤 1×/User ({claims_cnt})")
    if pack.get("is_paused"):
        sec_parts.append("⏸ Paused")
    if pack.get("pin_hash"):
        sec_parts.append("🔑 PIN")
    if pack.get("protect_content"):
        sec_parts.append("🛡️ No-Fwd")
    sec_parts.append(adel_badge)
    exp_lbl_badge = format_expiry_badge(pack.get("expires_at"))
    sec_parts.append(f"⏳ Link: {exp_lbl_badge}")

    preview_lines: list[str] = []
    if pack.get("description"):
        preview_lines.append(f"💬 <i>{esc(pack['description'][:80])}</i>")
    for f in items[:25]:
        ic = MEDIA_ICONS.get(f["media_type"], "📦")
        preview_lines.append(f"• {ic} {esc(f['title'][:34])}")
    if len(items) > 25:
        preview_lines.append(f"<i>…+{len(items) - 25} more files</i>")
    if not preview_lines:
        preview_lines.append("<i>No active files</i>")

    quote_tag = "<blockquote expandable>" if len(items) > 4 else "<blockquote>"
    text = (
        f"{breadcrumb('Vault', 'Packs', esc(pack['title'][:18]))}\n"
        f"🎒 <b>{esc(pack['title'])}</b>\n"
        f"<i>{pack['item_count']} files · {format_size(pack['total_size'])} · {' · '.join(sec_parts)}</i>\n"
        f"{quote_tag}" + "\n".join(preview_lines) + "</blockquote>\n"
        f"🔗 <code>{share_url}</code>"
    )

    one_time_on = bool(pack.get("one_time_per_user"))
    one_time_lbl = f"👤 1×/User: {'ON' if one_time_on else 'OFF'}"
    pin_lbl = "🔑 PIN: ON" if pack.get("pin_hash") else "🔑 Set PIN"
    prot_lbl = toggle_label(bool(pack.get("protect_content")), "No-Fwd")
    adel_lbl = f"💣 Auto-Del: {format_ttl_short(adel_val)}"
    exp_lbl = f"⏳ Link: {exp_lbl_badge}"
    lim_lbl = f"👁 Limit: {cur_v}/{max_v}" if max_v > 0 else "👁 Limit: ∞"
    pause_lbl = "▶️ Resume" if pack.get("is_paused") else "⏸ Pause"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                copy_btn("📋 Copy Link", share_url, style="primary"),
                share_url_btn("↗️ Share", share_url, pack["title"]),
            ],
            [
                InlineKeyboardButton(text="📥 Get All", callback_data=f"pk:get:{pack_id}"),
                InlineKeyboardButton(text="🛠️ Edit Pack", callback_data=f"pk:edit:{pack_id}"),
            ],
            [
                InlineKeyboardButton(
                    text=one_time_lbl,
                    callback_data=f"pk:1x:{pack_id}",
                    style="success" if one_time_on else None,
                ),
                InlineKeyboardButton(text=prot_lbl, callback_data=f"pk:prot:{pack_id}"),
            ],
            [
                InlineKeyboardButton(
                    text=adel_lbl,
                    callback_data=f"pk:adel:{pack_id}",
                    style="success" if adel_val > 0 else None,
                ),
                InlineKeyboardButton(text=exp_lbl, callback_data=f"pk:exp:{pack_id}"),
            ],
            [
                InlineKeyboardButton(text=pin_lbl, callback_data=f"pk:pin:{pack_id}"),
                InlineKeyboardButton(text=lim_lbl, callback_data=f"pk:maxv:{pack_id}"),
            ],
            [
                InlineKeyboardButton(text=pause_lbl, callback_data=f"pk:pause:{pack_id}"),
                InlineKeyboardButton(text="🔄 Reset", callback_data=f"pk:regen:{pack_id}"),
            ],
            [
                InlineKeyboardButton(
                    text="🗑️ Delete",
                    callback_data=f"pk:del_ask:{pack_id}",
                    style="danger",
                ),
                back_btn("pk:list:0", "◀ Packs"),
            ],
        ]
    )
    return text, kb


async def render_pack_edit_drawer(
    user_id: int, pack_id: int, db: Database, page: int = 0
) -> Optional[tuple[str, InlineKeyboardMarkup]]:
    """Manage Pack Title, Note/Description, Add More Files, or Remove Individual Files."""
    user = await db.get_user(user_id)
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
        f"{breadcrumb('Vault', 'Packs', esc(pack['title'][:16]), 'Edit')}\n"
        f"🛠️ <b>Edit Pack: {esc(pack['title'])}</b>\n"
        f"<blockquote>📦 <b>{total}</b> files · 💬 Note: <i>{desc_txt}</i>\n"
        f"Tap ✖️ on any file below to remove it from this pack.</blockquote>"
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
            InlineKeyboardButton(text="✏️ Rename", callback_data=f"pk:ren:{pack_id}"),
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
                    text="◀", callback_data=f"pk:editp:{pack_id}:{page - 1}"
                )
            )
        nav.append(
            InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop")
        )
        if page + 1 < total_pages:
            nav.append(
                InlineKeyboardButton(
                    text="▶", callback_data=f"pk:editp:{pack_id}:{page + 1}"
                )
            )
        rows.append(nav)

    rows.append([back_btn(f"pk:open:{pack_id}", "◀ Back to Pack")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def _render_batch_collector(
    count: int, append_pack_id: Optional[int] = None
) -> tuple[str, InlineKeyboardMarkup]:
    mode_lbl = "Add to Pack" if append_pack_id else "Batch Pack Builder"
    cancel_target = f"pk:open:{append_pack_id}" if append_pack_id else "pk:list:0"
    text = (
        f"{breadcrumb('Vault', 'Packs', 'Builder')}\n"
        f"🎒 <b>{mode_lbl}</b> · <b>{count}</b> queued\n"
        f"<blockquote>Drop or forward 1 to 100+ files right now — this card updates live without spamming your chat!</blockquote>"
    )
    rows: list[list[InlineKeyboardButton]] = []
    if count > 0:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"✓ Finish ({count})",
                    callback_data="pk:finish",
                    style="success",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="✕ Cancel", callback_data=f"fsm:cancel:{cancel_target}")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("packs"))
async def cmd_packs(message: Message, db: Database) -> None:
    if not message.from_user:
        return
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
async def cb_packs_list(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    page = int(callback.data.split(":")[2])
    await db.ensure_user(
        callback.from_user.id,
        callback.from_user.username or "",
        callback.from_user.full_name or "",
    )
    text, kb = await render_packs_list(callback.from_user.id, db, page)
    await safe_edit_or_send(callback, text, kb, also_ack=True)


@router.callback_query(F.data == "nav:packs")
async def cb_nav_packs(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    await db.ensure_user(
        callback.from_user.id,
        callback.from_user.username or "",
        callback.from_user.full_name or "",
    )
    text, kb = await render_packs_list(callback.from_user.id, db, 0)
    await safe_edit_or_send(callback, text, kb, also_ack=True)


@router.callback_query(F.data == "pk:new")
async def cb_pack_new(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PackStates.collecting_items)
    text, kb = _render_batch_collector(0)
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
        append_pack_id=None,
    )
    await safe_edit_or_send(
        callback, text, kb, answer_text="Drop files to bundle!", also_ack=True
    )


@router.callback_query(F.data.startswith("pk:add:"))
async def cb_pack_add_more_files(
    callback: CallbackQuery, state: FSMContext, db: Database
) -> None:
    """Allows appending more files into an existing Pack using the live zero-spam collector."""
    if not callback.from_user:
        return
    pid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
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
    """1-Tap: Bundle an entire Folder/Group into a shareable Pack link!"""
    if not callback.from_user:
        return
    gid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
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
    """Legacy 1-Tap Pack creation callback handler kept for backwards compatibility."""
    if not callback.from_user:
        return
    raw_ids = callback.data.split(":", 2)[2]
    fids = [int(x) for x in raw_ids.split(",") if x.isdigit()]
    if not fids:
        await callback.answer("No files found.", show_alert=True)
        return
    user = await db.get_user(callback.from_user.id)
    first = await db.get_file(fids[0])
    title = (
        (first["title"][:28] + f" (+{len(fids)-1})")
        if (first and len(fids) > 1)
        else "Album Pack"
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
        f"✏️ <b>Name this Pack</b> <i>({len(fids)} files)</i>\n"
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
                [InlineKeyboardButton(text="◀ Cancel", callback_data="fsm:cancel:pk:list:0")],
            ]
        ),
    )


@router.callback_query(F.data == "pk:finish")
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
        await db.append_files_to_pack(append_pid, fids)
        rendered = await render_pack_detail(
            callback.from_user.id, append_pid, db, bot, bot_username=bot_username
        )
        if rendered:
            await safe_edit_or_send(
                callback,
                rendered[0],
                rendered[1],
                answer_text=f"✓ Added {len(fids)} file(s)!",
                also_ack=True,
            )
        else:
            await callback.answer(f"✓ Added {len(fids)} file(s)!")
        return

    await state.set_state(PackStates.waiting_for_title)
    await safe_edit_or_send(
        callback,
        f"✏️ <b>Name this Pack</b> <i>({len(fids)} files)</i>\n"
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
                [InlineKeyboardButton(text="◀ Cancel", callback_data="fsm:cancel:pk:list:0")],
            ]
        ),
        also_ack=True,
    )


@router.callback_query(F.data == "pk:autotitle")
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
    user = await db.ensure_user(
        callback.from_user.id,
        callback.from_user.username or "",
        callback.from_user.full_name or "",
    )
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


@router.message(PackStates.waiting_for_title)
async def fsm_pack_save_title(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not message.from_user or not message.text:
        return
    data = await state.get_data()
    fids: list[int] = data.get("pack_file_ids") or []
    await state.clear()
    if not fids:
        return
    raw = message.text.strip()
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
) -> None:
    if not callback.from_user:
        return
    pid = int(callback.data.split(":")[2])
    rendered = await render_pack_detail(
        callback.from_user.id, pid, db, bot, bot_username=bot_username
    )
    if not rendered:
        await callback.answer("Pack not found.", show_alert=True)
        return
    await safe_edit_or_send(callback, rendered[0], rendered[1], also_ack=True)


@router.callback_query(F.data.startswith("pk:edit:"))
async def cb_pack_edit_drawer(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
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
    """1-Tap Natural Numeric Sort (1->N) for existing Packs (e.g. Part 1, Part 2, ..., Part 62)."""
    if not callback.from_user:
        return
    pid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
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
    user = await db.get_user(callback.from_user.id)
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
    user = await db.get_user(callback.from_user.id)
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


@router.message(PackStates.waiting_for_rename)
async def fsm_pack_rename_save(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not message.from_user or not message.text:
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
    await db.rename_pack(pid, message.text.strip()[:60])
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
    user = await db.get_user(callback.from_user.id)
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
        f"<blockquote>Send a note shown when someone opens your link (or <code>-</code> to clear):</blockquote>",
        cancel_kb(f"pk:edit:{pid}"),
        also_ack=True,
    )


@router.message(PackStates.waiting_for_description)
async def fsm_pack_desc_save(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not message.from_user or not message.text:
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
    raw = message.text.strip()
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
    user = await db.get_user(callback.from_user.id)
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
        count_view=False,  # Owner retrieving their own pack never burns max_views or triggers trial!
        dump_channel_id=config.dump_channel_id if config else None,
    )


@router.callback_query(F.data.startswith("pk:pin:"))
async def cb_pack_pin_prompt(
    callback: CallbackQuery, state: FSMContext, db: Database
) -> None:
    if not callback.from_user:
        return
    pid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    pack = await db.get_pack(pid)
    if not pack or pack["vault_id"] != user["vault_id"]:
        await callback.answer("Pack not found.", show_alert=True)
        return
    await state.set_state(PackStates.waiting_for_pin)
    await state.update_data(pin_pack_id=pid)
    status = "Active" if pack.get("pin_hash") else "Off"
    await safe_edit_or_send(
        callback,
        f"🔑 <b>Pack PIN Lock</b> · <i>{status}</i>\n"
        f"<blockquote>Send a PIN/password (up to 24 chars), or <code>-</code> to remove PIN:</blockquote>",
        cancel_kb(f"pk:open:{pid}"),
        also_ack=True,
    )


@router.message(PackStates.waiting_for_pin)
async def fsm_pack_set_pin(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not message.from_user or not message.text:
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
    raw = message.text.strip()
    pin = None if raw == "-" else raw[:24]
    await db.set_pack_pin(pid, pin)
    try:
        await message.delete()
    except Exception:
        pass
    rendered = await render_pack_detail(
        message.from_user.id, pid, db, bot, bot_username=bot_username
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
    pid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    pack = await db.get_pack(pid)
    if not pack or pack["vault_id"] != user["vault_id"]:
        await callback.answer("Pack not found.", show_alert=True)
        return
    on = await db.toggle_pack_one_time(pid)
    rendered = await render_pack_detail(
        callback.from_user.id, pid, db, bot, bot_username=bot_username
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
    pid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    pack = await db.get_pack(pid)
    if not pack or pack["vault_id"] != user["vault_id"]:
        await callback.answer("Pack not found.", show_alert=True)
        return
    on = await db.toggle_pack_protect(pid)
    rendered = await render_pack_detail(
        callback.from_user.id, pid, db, bot, bot_username=bot_username
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
    pid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    pack = await db.get_pack(pid)
    if not pack or pack["vault_id"] != user["vault_id"]:
        await callback.answer("Pack not found.", show_alert=True)
        return
    new_ttl = await db.cycle_pack_auto_delete_ttl(pid)
    owner_ttl = int((user or {}).get("auto_delete_ttl") or 0)
    rendered = await render_pack_detail(
        callback.from_user.id, pid, db, bot, bot_username=bot_username
    )
    if new_ttl > 0:
        ans = f"💣 Chat Auto-Delete: {format_ttl_short(new_ttl)} (overrides global)"
    elif owner_ttl > 0:
        ans = f"💣 Per-link Off (uses global {format_ttl_short(owner_ttl)})"
    else:
        ans = "💣 Chat Auto-Delete: Off"
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
    pid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    pack = await db.get_pack(pid)
    if not pack or pack["vault_id"] != user["vault_id"]:
        await callback.answer("Pack not found.", show_alert=True)
        return
    new_exp = await db.cycle_pack_expiry(pid)
    rendered = await render_pack_detail(
        callback.from_user.id, pid, db, bot, bot_username=bot_username
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
    pid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    pack = await db.get_pack(pid)
    if not pack or pack["vault_id"] != user["vault_id"]:
        await callback.answer("Pack not found.", show_alert=True)
        return
    lim = await db.cycle_pack_max_views(pid)
    rendered = await render_pack_detail(
        callback.from_user.id, pid, db, bot, bot_username=bot_username
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
    pid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    pack = await db.get_pack(pid)
    if not pack or pack["vault_id"] != user["vault_id"]:
        await callback.answer("Pack not found.", show_alert=True)
        return
    paused = await db.toggle_pack_paused(pid)
    rendered = await render_pack_detail(
        callback.from_user.id, pid, db, bot, bot_username=bot_username
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
    pid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    pack = await db.get_pack(pid)
    if not pack or pack["vault_id"] != user["vault_id"]:
        await callback.answer("Pack not found.", show_alert=True)
        return
    await db.regenerate_pack_token(pid)
    rendered = await render_pack_detail(
        callback.from_user.id, pid, db, bot, bot_username=bot_username
    )
    ans = "🔄 New pack link generated (views & user claims reset)"
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
    user = await db.get_user(callback.from_user.id)
    pack = await db.get_pack(pid)
    if not pack or pack["vault_id"] != user["vault_id"]:
        await callback.answer("Pack not found.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                back_btn(f"pk:open:{pid}", "◀ Cancel"),
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
        f"<blockquote>The share link stops working. Your underlying vault files stay untouched.</blockquote>",
        kb,
        also_ack=True,
    )


@router.callback_query(F.data.startswith("pk:del:"))
async def cb_pack_del(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    pid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    await db.delete_pack(pid, user["vault_id"])
    text, kb = await render_packs_list(callback.from_user.id, db, 0)
    await safe_edit_or_send(
        callback, text, kb, answer_text="Pack deleted", also_ack=True
    )
