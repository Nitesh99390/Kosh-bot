import asyncio
import os
import sys
import tempfile
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from database import Database, hash_pin, verify_pin
from handlers import accounts, admin, file_actions, groups, inline, packs, start_help, upload
from utils.cache import TTLCache
from utils.states import PackStates
from utils.banners import (
    BANNER_FILES,
    compute_file_sha256,
    edit_or_send_banner,
    get_or_upload_banner_file_id,
    send_banner_message,
    warmup_banners_in_dump_channel,
)
from utils.formatters import (
    EFFECT_FIRE,
    EFFECT_PARTY,
    EFFECT_THUMBS_UP,
    batch_delete_chat_messages,
    breadcrumb,
    build_contact_owner_kb,
    build_file_card_text,
    build_file_keyboard,
    build_file_link_keyboard,
    build_file_manage_keyboard,
    build_trial_expiry_notice_text,
    build_trial_locked_card,
    build_upload_quick_keyboard,
    copy_btn,
    execute_due_scheduled_deletes,
    format_duration,
    format_expiry_badge,
    format_size,
    format_ttl_short,
    next_sort_key,
    progress_bar,
    safe_edit_or_send,
    safe_gather,
    send_stored_media,
    share_url_btn,
    toggle_label,
)
from utils.middleware import (
    GuardAndThrottleMiddleware,
    check_force_sub,
    invalidate_force_sub_cache,
)


class _DummyBot:
    def __init__(self) -> None:
        self.dump_uploads: list[tuple[int, str]] = []
        self.sent_media: list[dict] = []
        self.edited_messages: list[dict] = []
        self.forwarded_messages: list[tuple[int, int, int]] = []
        self.deleted_messages: list[tuple[int, int]] = []
        self.batch_deleted_calls: list[tuple[int, list[int]]] = []
        self.batch_copied_calls: list[dict] = []
        self.effects_used: list[str] = []
        self._seq = 0

    async def get_me(self):
        return SimpleNamespace(
            username="KoshBox_Bot", id=99999, supports_inline_queries=True
        )

    async def send_photo(
        self,
        chat_id: int,
        photo,
        caption: str = "",
        disable_notification: bool = False,
        protect_content: bool = False,
    ):
        self._seq += 1
        if not isinstance(photo, str):
            self.dump_uploads.append((chat_id, caption))
        else:
            self.sent_media.append(
                {"type": "photo", "protect": protect_content, "caption": caption}
            )
        p_obj = SimpleNamespace(
            file_id=f"CACHED_TG_PHOTO_ID_{self._seq}",
            file_unique_id=f"UNIQ_PHOTO_{self._seq}",
        )
        return SimpleNamespace(message_id=7000 + self._seq, photo=[p_obj])

    async def send_audio(
        self,
        chat_id: int,
        audio: str,
        caption: str = "",
        protect_content: bool = False,
        message_effect_id: str | None = None,
    ):
        self._seq += 1
        if message_effect_id:
            self.effects_used.append(message_effect_id)
        self.sent_media.append(
            {
                "type": "audio",
                "chat_id": chat_id,
                "file_id": audio,
                "caption": caption,
                "protect": protect_content,
                "effect": message_effect_id,
            }
        )
        return SimpleNamespace(message_id=8000 + self._seq)

    async def send_document(
        self,
        chat_id: int,
        document: str,
        caption: str = "",
        protect_content: bool = False,
        message_effect_id: str | None = None,
    ):
        self._seq += 1
        if message_effect_id:
            self.effects_used.append(message_effect_id)
        self.sent_media.append(
            {
                "type": "document",
                "chat_id": chat_id,
                "file_id": document,
                "caption": caption,
                "protect": protect_content,
                "effect": message_effect_id,
            }
        )
        return SimpleNamespace(message_id=8000 + self._seq)

    async def send_message(
        self,
        chat_id: int,
        text: str,
        protect_content: bool = False,
        reply_markup=None,
        disable_web_page_preview: bool = False,
        message_effect_id: str | None = None,
    ):
        self._seq += 1
        if message_effect_id:
            self.effects_used.append(message_effect_id)
        self.sent_media.append(
            {
                "type": "msg",
                "chat_id": chat_id,
                "text": text,
                "protect": protect_content,
                "reply_markup": reply_markup,
                "effect": message_effect_id,
            }
        )
        return SimpleNamespace(message_id=9000 + self._seq)

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup=None,
        disable_web_page_preview: bool = False,
    ):
        self.edited_messages.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "reply_markup": reply_markup,
            }
        )
        return SimpleNamespace(message_id=message_id)

    async def forward_message(
        self, chat_id: int, from_chat_id: int, message_id: int
    ):
        self._seq += 1
        self.forwarded_messages.append((chat_id, from_chat_id, message_id))
        return SimpleNamespace(
            message_id=10000 + self._seq,
            audio=SimpleNamespace(
                file_id=f"DUMP_AUD_{self._seq}",
                file_unique_id=f"DUMP_UNIQ_{self._seq}",
                title=f"Episode_{self._seq}",
                performer="Gtts",
                file_name=f"Little_Monkey_King_Ep_{self._seq}.mp3",
                mime_type="audio/mpeg",
                file_size=1_000_000,
                duration=600,
            ),
            document=None,
            video=None,
            video_note=None,
            voice=None,
            photo=None,
            animation=None,
            sticker=None,
            caption="",
        )

    async def delete_message(self, chat_id: int, message_id: int):
        self.deleted_messages.append((chat_id, message_id))

    async def delete_messages(self, chat_id: int, message_ids: list[int]):
        self.batch_deleted_calls.append((chat_id, list(message_ids)))
        for mid in message_ids:
            self.deleted_messages.append((chat_id, mid))
        return True

    async def copy_messages(
        self,
        chat_id: int,
        from_chat_id: int,
        message_ids: list[int],
        protect_content: bool = False,
    ):
        assert len(message_ids) <= 100, "copy_messages must chunk <= 100 IDs"
        self.batch_copied_calls.append(
            {
                "chat_id": chat_id,
                "from_chat_id": from_chat_id,
                "message_ids": list(message_ids),
                "protect": protect_content,
            }
        )
        out = []
        for _ in message_ids:
            self._seq += 1
            out.append(SimpleNamespace(message_id=12000 + self._seq))
        return out


async def run_tests() -> None:
    dummy_bot = _DummyBot()
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_koshbox.db")
        db = Database(db_path)
        await db.connect()

        # 1. Users & Vaults
        u1 = await db.ensure_user(1001, "alice", "Alice Verma")
        u2 = await db.ensure_user(1002, "bob", "Bob Sharma")
        assert u1["vault_id"] != u2["vault_id"]

        # 2. Sort preferences, Bulk Mode, Content Protection, Auto-Delete Timer, Ban toggle
        assert await db.toggle_bulk_mode(1001) is True
        assert await db.toggle_bulk_mode(1001) is False
        assert await db.toggle_protect_content(1001) is True
        assert await db.toggle_protect_content(1001) is False
        assert await db.cycle_auto_delete_ttl(1001) == 300
        assert await db.cycle_auto_delete_ttl(1001) == 900
        assert await db.set_auto_delete_ttl(1001, 0) == 0
        assert await db.set_default_sort(1001, "top") == "top"

        assert await db.set_user_banned(1002, True) is True
        assert await db.is_user_banned(1002) is True
        assert await db.set_user_banned(1002, False) is True
        assert await db.is_user_banned(1002) is False

        # 3. PIN Hashing & Constant-Time Verification
        p_hash = hash_pin("4829")
        assert verify_pin("4829", p_hash) is True
        assert verify_pin("9999", p_hash) is False

        # 4. Folders & Shared Groups + Rename + Invite regeneration
        personal_grp = await db.create_group(u1["vault_id"], "Lofi Beats", is_shared=False)
        shared_grp = await db.create_group(u1["vault_id"], "Study Club", is_shared=True)
        assert shared_grp["invite_code"] is not None
        old_code = shared_grp["invite_code"]

        await db.rename_group(personal_grp["id"], "Chill Mixes")
        updated_personal = await db.get_group(personal_grp["id"])
        assert updated_personal["name"] == "Chill Mixes"

        new_code = await db.regenerate_group_invite(shared_grp["id"])
        assert new_code != old_code
        assert await db.join_shared_group(u2["vault_id"], old_code) is None
        joined = await db.join_shared_group(u2["vault_id"], new_code)
        assert joined is not None and joined["id"] == shared_grp["id"]

        # 5. Files, FTS5, Sorting, Type filtering
        f1 = await db.add_file(
            vault_id=u1["vault_id"],
            uploader_user_id=1001,
            group_id=personal_grp["id"],
            media_type="audio",
            file_id="AUD_FILE_1",
            file_unique_id="UNIQ_1",
            dump_message_id=501,
            title="Midnight Rain Remix",
            performer="DJ Nova",
            file_size=5_242_880,
            duration=215,
            tags="#lofi #chill",
        )
        f2 = await db.add_file(
            vault_id=u1["vault_id"],
            uploader_user_id=1001,
            group_id=shared_grp["id"],
            media_type="document",
            file_id="DOC_FILE_2",
            file_unique_id="UNIQ_2",
            dump_message_id=502,
            title="Architecture Notes.pdf",
            file_size=1_048_576,
            tags="#notes",
        )

        # FTS5 & shared group visibility
        u2_results = await db.search_files(u2["vault_id"], text_query="Architecture")
        assert len(u2_results) == 1 and u2_results[0]["id"] == f2["id"]
        u2_private_check = await db.search_files(u2["vault_id"], text_query="Midnight")
        assert len(u2_private_check) == 0

        await db.update_file_caption(f1["id"], "Best late night track")
        await db.toggle_favorite(f1["id"])
        await db.increment_use_count(f2["id"])
        await db.increment_use_count(f2["id"])

        top_sorted = await db.search_files(u1["vault_id"], sort_by="top")
        assert top_sorted[0]["id"] == f2["id"]
        size_sorted = await db.search_files(u1["vault_id"], sort_by="size")
        assert size_sorted[0]["id"] == f1["id"]

        # 6. Advanced Share Link Security (PIN, Protect Content, Expiry)
        tok = await db.toggle_share_token(f1["id"])
        assert tok is not None
        assert await db.set_file_share_pin(f1["id"], "1234") is True
        assert await db.toggle_file_protect_share(f1["id"]) is True
        exp_ts = await db.cycle_file_share_expiry(f1["id"])
        assert exp_ts is not None and exp_ts > int(time.time())

        shared_row = await db.get_file_by_share_token(tok)
        assert shared_row is not None
        assert verify_pin("1234", shared_row["share_pin_hash"]) is True
        assert shared_row["protect_share"] == 1

        # Simulate expired link -> returns None
        await db.conn.execute(
            "UPDATE files SET share_expires_at = ? WHERE id = ?",
            (int(time.time()) - 10, f1["id"]),
        )
        await db.conn.commit()
        assert await db.get_file_by_share_token(tok) is None
        # Reset expiry
        await db.conn.execute(
            "UPDATE files SET share_expires_at = NULL WHERE id = ?", (f1["id"],)
        )
        await db.conn.commit()

        # 7. Multi-File Packs (Bundles) — Create, PIN, Protect, Expiry, Deliver
        pack = await db.create_pack(
            vault_id=u1["vault_id"],
            creator_user_id=1001,
            title="Starter Bundle",
            file_ids=[f1["id"], f2["id"]],
            pin="7777",
            protect_content=True,
        )
        assert pack["item_count"] == 2
        assert pack["total_size"] == 5_242_880 + 1_048_576
        assert verify_pin("7777", pack["pin_hash"]) is True

        p_by_tok = await db.get_pack_by_token(pack["share_token"])
        assert p_by_tok is not None and p_by_tok["id"] == pack["id"]

        delivered_cnt = await packs.deliver_pack_to_chat(
            dummy_bot, 5555, pack, db, auto_delete_ttl=0  # type: ignore[arg-type]
        )
        assert delivered_cnt == 2
        assert (await db.get_pack(pack["id"]))["views"] == 1

        pack_list_txt, pack_list_kb = await packs.render_packs_list(1001, db, 0)
        assert "File Packs" in pack_list_txt
        pack_det = await packs.render_pack_detail(
            1001, pack["id"], db, dummy_bot  # type: ignore[arg-type]
        )
        assert pack_det is not None
        assert "Starter Bundle" in pack_det[0]

        # 8. Auto-Delete Timer test (fast 0.05s)
        sent_m = await send_stored_media(
            dummy_bot,  # type: ignore[arg-type]
            5555,
            await db.get_file(f1["id"]),
            protect_content=True,
            auto_delete_ttl=1,
        )
        assert sent_m is not None
        await asyncio.sleep(1.15)
        assert (5555, sent_m.message_id) in dummy_bot.deleted_messages

        # 9. Safe Trash, Restore, and Purge
        await db.trash_file(f1["id"])
        assert await db.count_trashed_files(u1["vault_id"]) == 1
        assert len(await db.search_files(u1["vault_id"], text_query="Midnight")) == 0
        await db.restore_file(f1["id"])
        assert await db.count_trashed_files(u1["vault_id"]) == 0
        assert len(await db.search_files(u1["vault_id"], text_query="Midnight")) == 1

        # 10. Inline query parser (including new `packs` & `t:` shorthand)
        parsed = inline._parse_inline_query("g:chill t:audio fav top #lofi midnight")
        assert parsed["media_type"] == "audio"
        assert parsed["group_name_filter"] == "chill"
        assert parsed["only_favorites"] is True
        assert parsed["tag_filter"] == "#lofi"

        parsed_packs = inline._parse_inline_query("packs starter")
        assert parsed_packs["packs_only"] is True
        assert parsed_packs["text_query"] == "starter"

        # 11. Crisp UI/UX Verification (Short text, compact button grids, <=64B callbacks)
        home_txt, home_kb = await start_help.render_home_dashboard(
            1001, "Alice", db, dummy_bot  # type: ignore[arg-type]
        )
        assert "<blockquote>" in home_txt
        assert len(home_txt) < 320, "Home dashboard text must stay ultra-short & crisp!"
        assert len(home_kb.inline_keyboard) == 3
        assert sum(len(r) for r in home_kb.inline_keyboard) == 6

        f1_fresh = await db.get_file(f1["id"])
        card_txt = build_file_card_text(f1_fresh, "KoshBox_Bot")
        assert "Midnight Rain Remix" in card_txt
        assert "<blockquote>" in card_txt

        kb_primary = build_file_keyboard(
            f1_fresh, back_cb="br:all:new:0:0", bot_username="KoshBox_Bot"
        )
        kb_manage = build_file_manage_keyboard(
            f1_fresh, back_cb="br:all:new:0:0", bot_username="KoshBox_Bot"
        )
        kb_link = build_file_link_keyboard(
            f1_fresh, back_cb="br:all:new:0:0", bot_username="KoshBox_Bot"
        )
        assert any(
            btn.copy_text is not None for row in kb_link.inline_keyboard for btn in row
        )

        # Verify all callback_data across all keyboards strictly <= 64 bytes
        for kb_test in (home_kb, kb_primary, kb_manage, kb_link, pack_list_kb, pack_det[1]):
            for row in kb_test.inline_keyboard:
                for btn in row:
                    if btn.callback_data:
                        assert len(btn.callback_data.encode("utf-8")) <= 64

        up_kb = build_upload_quick_keyboard(f1_fresh)
        assert len(up_kb.inline_keyboard) == 2

        br_txt, br_kb = await file_actions.render_browse_page(1001, "all", "def", 0, 0, db)
        assert "My Files" in br_txt
        assert len(br_kb.inline_keyboard) == 4

        flt_txt, flt_kb = file_actions.render_filter_sort_drawer("audio", "top", 0, 0)
        assert "Filter & Sort" in flt_txt
        assert len(flt_kb.inline_keyboard) == 6

        grp_txt, grp_kb = await groups.render_groups_panel(1001, db)
        assert "Folders & Groups" in grp_txt
        assert len(grp_kb.inline_keyboard) == 2

        sgrp_m = await groups.render_single_group(
            1001, shared_grp["id"], db, dummy_bot, manage_mode=True  # type: ignore[arg-type]
        )
        assert sgrp_m is not None
        assert any(
            "Pack All" in b.text for r in sgrp_m[1].inline_keyboard for b in r
        )

        set_txt, set_kb = await start_help._render_settings_panel(1001, db)
        assert "Settings" in set_txt
        assert len(set_kb.inline_keyboard) == 6

        st_txt, st_kb = await start_help._render_stats_panel(1001, db)
        assert "Vault Analytics" in st_txt

        adm_txt, adm_kb = await admin._render_admin_panel(db)
        assert "Admin Console" in adm_txt
        assert len(adm_kb.inline_keyboard) == 3

        # 12. Middleware Anti-Flood & Ban Guard Test
        dummy_cfg = Config(
            bot_token="123456:TEST",
            dump_channel_id=-1009988776655,
            admin_ids=(1001,),
            db_path=db_path,
            inline_page_size=25,
            link_code_ttl_seconds=300,
            rate_limit_interval=0.5,
        )
        mw = GuardAndThrottleMiddleware(rate_limit=0.5)
        calls = 0

        async def _next_handler(ev, dt):
            nonlocal calls
            calls += 1
            return "OK"

        fake_event = SimpleNamespace(
            from_user=SimpleNamespace(id=1002),
            media_group_id=None,
            chat=SimpleNamespace(type="private"),
        )
        data_ctx = {"config": dummy_cfg, "db": db, "bot": dummy_bot}
        res1 = await mw(_next_handler, fake_event, data_ctx)
        res2 = await mw(_next_handler, fake_event, data_ctx)  # Throttled!
        assert res1 == "OK" and res2 is None and calls == 1

        # 13. One-Time Dump Channel Banner Caching
        for key, bpath in BANNER_FILES.items():
            assert os.path.isfile(bpath), f"Missing banner file: {bpath}"
            assert len(compute_file_sha256(bpath)) == 64

        warm1 = await warmup_banners_in_dump_channel(dummy_bot, db, dummy_cfg)  # type: ignore[arg-type]
        assert warm1 == {"welcome": True, "upload": True, "guide": True}
        assert len(dummy_bot.dump_uploads) == 3

        warm2 = await warmup_banners_in_dump_channel(dummy_bot, db, dummy_cfg)  # type: ignore[arg-type]
        assert warm2 == {"welcome": True, "upload": True, "guide": True}
        assert len(dummy_bot.dump_uploads) == 3  # Zero extra server uploads!

        for _ in range(5):
            assert (
                await get_or_upload_banner_file_id("welcome", dummy_bot, db, dummy_cfg)  # type: ignore[arg-type]
                is not None
            )
        assert len(dummy_bot.dump_uploads) == 3

        # 14. Account Sync & Merge (including packs migration!)
        u3 = await db.ensure_user(1003, "alice_alt", "Alice Alt")
        u3_pack = await db.create_pack(u3["vault_id"], 1003, "Alt Pack", [f2["id"]])
        link_req = await db.create_link_request(1001, u1["vault_id"], 300)
        assert await db.attach_requester_to_link(
            link_req["code"], 1003, "Alice Alt", "alice_alt"
        )
        approved = await db.approve_link_request(link_req["code"], 1001)
        assert approved is not None
        assert len(await db.get_linked_accounts(u1["vault_id"])) == 2
        assert (await db.get_pack(u3_pack["id"]))["vault_id"] == u1["vault_id"]

        # 15. Utility formatters
        assert format_size(5_242_880) == "5.0 MB"
        assert format_duration(215) == "3:35"
        assert format_ttl_short(300) == "5m"
        assert format_expiry_badge(None) == "Never"
        assert len(progress_bar(0.5, 8)) == 8
        assert next_sort_key("new") == "top"
        assert toggle_label(True, "Bulk") == "✅ Bulk"
        assert "Vault › Files" in breadcrumb("Vault", "Files")
        assert copy_btn("Copy", "hello").copy_text.text == "hello"
        assert "t.me/share/url" in (share_url_btn("Share", "https://t.me/test").url or "")

        # 16. Middleware NEVER drops media uploads even during 100-file simultaneous bursts
        burst_calls = 0

        async def _burst_handler(ev, dt):
            nonlocal burst_calls
            burst_calls += 1
            return "SAVED"

        for idx in range(100):
            media_ev = SimpleNamespace(
                from_user=SimpleNamespace(id=1002),
                media_group_id=None,  # Even non-album forwarded files!
                audio=SimpleNamespace(file_id=f"A_{idx}"),
                chat=SimpleNamespace(type="private"),
            )
            r = await mw(_burst_handler, media_ev, data_ctx)
            assert r == "SAVED"
        assert burst_calls == 100, "All 100 rapid non-album media uploads must pass middleware!"

        # 17. 100-File Simultaneous Drop -> Auto-Packs into ONE Pack & sends ONLY 1 reply!
        upload.DROP_WINDOW_SECONDS = 0.12
        upload.PACK_STATUS_DEBOUNCE_SECONDS = 0.08
        u_burst = await db.ensure_user(2026, "nitesh", "Nitesh")
        replies_received: list[tuple[str, object]] = []

        class _DummyFSM:
            def __init__(self, state_val=None, initial_data=None):
                self._state = state_val
                self._data = dict(initial_data or {})

            async def get_state(self):
                return self._state

            async def set_state(self, st):
                self._state = st.state if hasattr(st, "state") else st

            async def get_data(self):
                return dict(self._data)

            async def update_data(self, **kwargs):
                self._data.update(kwargs)

            async def clear(self):
                self._state = None
                self._data.clear()

        def _make_audio_msg(idx: int, uid: int = 2026):
            async def _reply(
                txt: str,
                reply_markup=None,
                disable_web_page_preview: bool = False,
                message_effect_id: str | None = None,
            ):
                if message_effect_id:
                    dummy_bot.effects_used.append(message_effect_id)
                replies_received.append((txt, reply_markup))
                return SimpleNamespace(message_id=50000 + len(replies_received))

            return SimpleNamespace(
                message_id=30000 + idx,
                chat=SimpleNamespace(id=uid, type="private"),
                from_user=SimpleNamespace(id=uid, username="nitesh", full_name="Nitesh"),
                media_group_id=None,
                caption="",
                audio=SimpleNamespace(
                    file_id=f"SRC_AUD_{idx}",
                    file_unique_id=f"UNIQ_MONKEY_{idx}",
                    title=f"Little_Monkey_King_Ep_{idx:03d}.mp3",
                    performer="Gtts",
                    file_name=f"Little_Monkey_King_Ep_{idx:03d}.mp3",
                    mime_type="audio/mpeg",
                    file_size=2_000_000,
                    duration=900,
                ),
                document=None,
                video=None,
                video_note=None,
                voice=None,
                photo=None,
                animation=None,
                sticker=None,
                reply=_reply,
            )

        idle_fsm = _DummyFSM(None)
        for i in range(1, 101):
            await upload.handle_media_upload(
                _make_audio_msg(i),  # type: ignore[arg-type]
                idle_fsm,  # type: ignore[arg-type]
                dummy_bot,  # type: ignore[arg-type]
                db,
                dummy_cfg,
            )

        # Wait for sliding-window debounce to flush
        await asyncio.sleep(0.30)
        assert len(replies_received) == 1, (
            f"Expected 1 consolidated Auto-Pack message for 100 files, got {len(replies_received)}"
        )
        auto_pack_reply_txt, auto_pack_kb = replies_received[0]
        assert "Auto-Packed 100 files" in auto_pack_reply_txt
        assert "Little Monkey King" in auto_pack_reply_txt

        user_packs = await db.list_vault_packs(u_burst["vault_id"])
        assert len(user_packs) == 1 and user_packs[0]["item_count"] == 100
        big_pack = user_packs[0]

        # Single file drop check -> still produces standard 1-file Saved card (not a Pack)
        replies_received.clear()
        await upload.handle_media_upload(
            _make_audio_msg(999),  # type: ignore[arg-type]
            idle_fsm,  # type: ignore[arg-type]
            dummy_bot,  # type: ignore[arg-type]
            db,
            dummy_cfg,
        )
        await asyncio.sleep(0.30)
        assert len(replies_received) == 1
        assert "Saved" in replies_received[0][0]
        assert (await db.count_vault_packs(u_burst["vault_id"])) == 1

        # 18. 100-File Interactive `/batch` Builder -> ZERO per-file spam (1 live status edit!)
        replies_received.clear()
        dummy_bot.edited_messages.clear()
        msgs_before = len(dummy_bot.sent_media)
        batch_fsm = _DummyFSM(
            PackStates.collecting_items.state,
            {
                "pack_file_ids": [],
                "pack_total_size": 0,
                "pack_recent_titles": [],
                "pack_status_msg_id": 88888,
                "append_pack_id": None,
            },
        )
        for i in range(1, 101):
            # Re-dropping the 100 files into /batch builder
            await upload.handle_media_upload(
                _make_audio_msg(i),  # type: ignore[arg-type]
                batch_fsm,  # type: ignore[arg-type]
                dummy_bot,  # type: ignore[arg-type]
                db,
                dummy_cfg,
            )
        await asyncio.sleep(0.25)
        assert len(replies_received) == 0, "Batch builder must NEVER send 100 per-file reply messages!"
        assert len(dummy_bot.sent_media) == msgs_before, "No new spam messages sent!"
        assert len(dummy_bot.edited_messages) == 1, "Single live collector card edited once after burst!"
        assert "100</b> queued" in dummy_bot.edited_messages[0]["text"]

        # 19. Large 100-File Pack Native 100-in-1 Batch Delivery (`copy_messages`) + Continuation >100
        # Add 1 extra file (101 total) to verify both 100-in-1 batch copy AND >100 continuation!
        extra_f = await db.add_file(
            vault_id=u_burst["vault_id"],
            uploader_user_id=2026,
            group_id=None,
            media_type="audio",
            file_id="EXTRA_101",
            file_unique_id="UNIQ_MONKEY_101",
            dump_message_id=99999,
            title="Little_Monkey_King_Ep_101.mp3",
            file_size=2_000_000,
        )
        await db.append_files_to_pack(big_pack["id"], [extra_f["id"]])
        big_pack = await db.get_pack(big_pack["id"])
        assert big_pack["item_count"] == 101

        dummy_bot.sent_media.clear()
        dummy_bot.batch_copied_calls.clear()
        delivered_batch_1 = await packs.deliver_pack_to_chat(
            dummy_bot,  # type: ignore[arg-type]
            7777,
            big_pack,
            db,
            start_offset=0,
            dump_channel_id=dummy_cfg.dump_channel_id,
            celebrate=True,
        )
        assert delivered_batch_1 == 100  # Native 100-in-1 batch!
        assert len(dummy_bot.batch_copied_calls) == 1
        assert len(dummy_bot.batch_copied_calls[0]["message_ids"]) == 100
        # Check continuation footer message has `pk:more:<TOKEN>:100` button
        footer_msgs = [
            m for m in dummy_bot.sent_media if m["type"] == "msg" and m.get("reply_markup")
        ]
        assert len(footer_msgs) == 1
        more_cb = footer_msgs[0]["reply_markup"].inline_keyboard[0][0].callback_data
        assert more_cb == f"pk:more:{big_pack['share_token']}:100"
        assert len(more_cb.encode("utf-8")) <= 64

        # 20. Advanced Bug-Proof Link Security: Max Views, Pause/Resume, Edit Pack, PIN Brute-Force Lockout
        # Pack Max Views & Pause/Resume
        assert await db.cycle_pack_max_views(big_pack["id"]) == 1  # Limit: 1 open
        # Note: big_pack already has 1 view from delivered_batch_1 above -> should now be blocked!
        assert await db.get_pack_by_token(big_pack["share_token"]) is None
        raw_p = await db.get_pack_by_token(big_pack["share_token"], include_unavailable=True)
        diag_hdr, _ = start_help._diagnose_pack_unavailable(raw_p)
        assert "Limit Reached" in diag_hdr

        # Reset link resets views to 0 and unpauses
        new_ptok = await db.regenerate_pack_token(big_pack["id"])
        assert new_ptok != big_pack["share_token"]
        assert await db.get_pack_by_token(new_ptok) is not None

        # Pause / Resume pack link
        assert await db.toggle_pack_paused(big_pack["id"]) is True
        assert await db.get_pack_by_token(new_ptok) is None
        diag_pause_hdr, _ = start_help._diagnose_pack_unavailable(
            await db.get_pack_by_token(new_ptok, include_unavailable=True)
        )
        assert "Paused" in diag_pause_hdr
        assert await db.toggle_pack_paused(big_pack["id"]) is False
        assert await db.get_pack_by_token(new_ptok) is not None

        # Pack Rename, Description, Remove Item & Append Item
        await db.rename_pack(big_pack["id"], "Monkey King Complete")
        await db.set_pack_description(big_pack["id"], "Full Audiobook Season 1")
        p_files = await db.get_pack_files(big_pack["id"])
        assert await db.remove_file_from_pack(big_pack["id"], p_files[-1]["id"]) is True
        assert (await db.get_pack(big_pack["id"]))["item_count"] == 100
        await db.append_files_to_pack(big_pack["id"], [p_files[-1]["id"]])
        assert (await db.get_pack(big_pack["id"]))["item_count"] == 101

        edit_drawer = await packs.render_pack_edit_drawer(2026, big_pack["id"], db, page=0)
        assert edit_drawer is not None
        for row in edit_drawer[1].inline_keyboard:
            for btn in row:
                if btn.callback_data:
                    assert len(btn.callback_data.encode("utf-8")) <= 64

        # Single-File Link Max Views & Pause/Resume
        assert await db.cycle_file_share_max_views(f1["id"]) == 1
        await db.increment_file_share_views(f1["id"])
        assert await db.get_file_by_share_token(tok) is None
        new_ftok = await db.regenerate_file_share_token(f1["id"])
        assert await db.get_file_by_share_token(new_ftok) is not None
        assert await db.toggle_file_share_paused(f1["id"]) is True
        assert await db.get_file_by_share_token(new_ftok) is None
        assert await db.toggle_file_share_paused(f1["id"]) is False
        assert await db.get_file_by_share_token(new_ftok) is not None

        # PIN Brute-Force Lockout (5 wrong tries -> 300s lockout)
        start_help._clear_pin_failures(9090, "p:TESTTOK")
        for attempt in range(1, 5):
            left, lck = start_help._record_pin_failure(9090, "p:TESTTOK")
            assert left == 5 - attempt and lck == 0
        left5, lck5 = start_help._record_pin_failure(9090, "p:TESTTOK")
        assert left5 == 0 and lck5 == start_help.PIN_LOCKOUT_SECONDS
        assert start_help._check_pin_lockout(9090, "p:TESTTOK") > 0
        start_help._clear_pin_failures(9090, "p:TESTTOK")
        assert start_help._check_pin_lockout(9090, "p:TESTTOK") == 0

        # 21. Bounded TTLCache LRU eviction & TTL expiry
        fake_now = [1000.0]
        tcache: TTLCache[str, int] = TTLCache(
            maxsize=3, ttl=10.0, clock=lambda: fake_now[0]
        )
        tcache.set("a", 1)
        tcache.set("b", 2)
        tcache.set("c", 3)
        # Touch 'a' so 'b' becomes LRU
        assert tcache.get("a") == 1
        tcache.set("d", 4)
        assert "b" not in tcache and len(tcache) == 3
        assert tcache.get("a") == 1 and tcache.get("c") == 3 and tcache.get("d") == 4
        # Advance clock past TTL -> all entries expire cleanly
        fake_now[0] += 11.0
        assert tcache.get("a") is None
        assert len(tcache) == 0

        # 22. ensure_user() cache invalidation + 90s last_seen write throttling (zero-write read path)
        u_perf = await db.ensure_user(77777, "perf_user", "Perf User")
        assert u_perf["user_id"] == 77777
        await db.conn.commit()
        changes_before = db.conn.total_changes
        for _ in range(25):
            cached_u = await db.ensure_user(77777, "perf_user", "Perf User")
            assert cached_u["vault_id"] == u_perf["vault_id"]
        assert (
            db.conn.total_changes == changes_before
        ), "Repeated ensure_user() within 90s must perform ZERO SQLite writes!"

        # Mutations must immediately invalidate user cache
        assert await db.toggle_bulk_mode(77777) is True
        assert (await db.get_user(77777))["bulk_mode"] == 1
        assert await db.set_default_sort(77777, "size") == "size"
        assert (await db.get_user(77777))["default_sort"] == "size"
        assert await db.set_user_banned(77777, True) is True
        assert await db.is_user_banned(77777) is True
        assert await db.set_user_banned(77777, False) is True
        assert await db.is_user_banned(77777) is False

        # Simulate >90s elapsed -> next ensure_user() persists updated last_seen once
        db._last_seen_written.set(77777, time.monotonic() - 95.0)
        changes_mid = db.conn.total_changes
        await db.ensure_user(77777, "perf_user", "Perf User")
        assert db.conn.total_changes == changes_mid + 1

        # 23. Force-Subscribe TTLCache + immediate invalidation on Recheck
        fsub_calls = 0

        class _FSubBot:
            async def get_chat_member(self, chat_id, user_id):
                nonlocal fsub_calls
                fsub_calls += 1
                from aiogram.enums import ChatMemberStatus

                return SimpleNamespace(
                    status=(
                        ChatMemberStatus.LEFT
                        if fsub_calls == 1
                        else ChatMemberStatus.MEMBER
                    )
                )

        fsub_bot = _FSubBot()
        invalidate_force_sub_cache("@kosh_updates", 88888)
        assert await check_force_sub(fsub_bot, "@kosh_updates", 88888) is False  # type: ignore[arg-type]
        assert await check_force_sub(fsub_bot, "@kosh_updates", 88888) is False  # cached!
        assert fsub_calls == 1
        invalidate_force_sub_cache("@kosh_updates", 88888)
        assert await check_force_sub(fsub_bot, "@kosh_updates", 88888) is True  # type: ignore[arg-type]
        assert fsub_calls == 2

        # 24. FTS5-First Search + Substring Fallback Correctness
        u_fts = await db.ensure_user(88889, "searcher", "Searcher")
        await db.add_file(
            vault_id=u_fts["vault_id"],
            uploader_user_id=88889,
            group_id=None,
            media_type="document",
            file_id="FTS_DOC_1",
            file_unique_id="FTS_U_1",
            dump_message_id=901,
            title="Quantum_Cryptography_2026_Handbook.pdf",
            file_name="Quantum_Cryptography_2026_Handbook.pdf",
            tags="#crypto #quantum",
        )
        await db.add_file(
            vault_id=u_fts["vault_id"],
            uploader_user_id=88889,
            group_id=None,
            media_type="document",
            file_id="FTS_DOC_2",
            file_unique_id="FTS_U_2",
            dump_message_id=902,
            title="SuperSecretInfixToken99X.zip",
            file_name="SuperSecretInfixToken99X.zip",
        )
        # Prefix / token match via FTS5 fast path
        fts_hits = await db.search_files(u_fts["vault_id"], text_query="Quantum Hand", limit=5)
        assert len(fts_hits) == 1 and "Quantum" in fts_hits[0]["title"]
        # Mid-word substring match via automatic LIKE fallback when FTS5 returns < limit
        infix_hits = await db.search_files(u_fts["vault_id"], text_query="InfixToken99", limit=5)
        assert len(infix_hits) == 1 and "SuperSecretInfixToken99X" in infix_hits[0]["title"]

        # 25. Smart Banner Editing (edit_caption when same banner vs edit_media when switching)
        #     & Parallel callback.answer() + UI Edit verification
        banner_events: list[str] = []
        welcome_asset = await db.get_ui_asset("welcome")
        assert welcome_asset is not None

        async def _cb_answer(text=None, show_alert=False):
            banner_events.append(f"ans:{text or ''}")

        async def _msg_edit_caption(caption="", reply_markup=None):
            banner_events.append("edit_caption")

        async def _msg_edit_media(media=None, reply_markup=None):
            banner_events.append("edit_media")

        same_banner_cb = SimpleNamespace(
            answer=_cb_answer,
            message=SimpleNamespace(
                photo=[
                    SimpleNamespace(
                        file_id=welcome_asset["file_id"],
                        file_unique_id=welcome_asset["file_unique_id"],
                    )
                ],
                text=None,
                edit_caption=_msg_edit_caption,
                edit_media=_msg_edit_media,
            ),
        )
        await edit_or_send_banner(
            same_banner_cb,  # type: ignore[arg-type]
            "welcome",
            "Updated dashboard caption",
            None,
            dummy_bot,  # type: ignore[arg-type]
            db,
            dummy_cfg,
            answer_text="Fast!",
            also_ack=True,
        )
        assert "edit_caption" in banner_events and "edit_media" not in banner_events
        assert "ans:Fast!" in banner_events

        banner_events.clear()
        await edit_or_send_banner(
            same_banner_cb,  # type: ignore[arg-type]
            "guide",
            "Switched to guide banner",
            None,
            dummy_bot,  # type: ignore[arg-type]
            db,
            dummy_cfg,
            also_ack=True,
        )
        assert "edit_media" in banner_events and "ans:" in banner_events

        # 26. SQLite PRAGMA verification & IPv4-only session check
        import socket
        from bot import IPv4AiohttpSession

        ipv4_sess = IPv4AiohttpSession(limit=64)
        assert ipv4_sess._connector_init["family"] == socket.AF_INET
        assert ipv4_sess._connector_init["ttl_dns_cache"] == 300
        assert ipv4_sess._connector_init["keepalive_timeout"] == 60.0
        assert ipv4_sess._connector_init["limit"] == 64

        pragma_journal = (await (await db.conn.execute("PRAGMA journal_mode;")).fetchone())[0]
        pragma_cache = (await (await db.conn.execute("PRAGMA cache_size;")).fetchone())[0]
        assert str(pragma_journal).lower() == "wal"
        assert int(pragma_cache) == -16000

        # 27. Static AST / source verification: ZERO `bot.get_me()` calls inside handlers/ or utils/
        base_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for folder in ("handlers", "utils"):
            folder_path = os.path.join(base_root, folder)
            for fname in os.listdir(folder_path):
                if not fname.endswith(".py"):
                    continue
                fpath = os.path.join(folder_path, fname)
                with open(fpath, "r", encoding="utf-8") as src_f:
                    src = src_f.read()
                assert "get_me(" not in src, (
                    f"Forbidden per-request get_me() call found in {folder}/{fname}!"
                )
                if not (folder == "utils" and fname == "formatters.py"):
                    assert "asyncio.gather(" not in src, (
                        f"Raw asyncio.gather() found in {folder}/{fname} — use safe_gather() so unhashable TelegramMethod objects never crash!"
                    )

        # 28. Regression Test: Unhashable aiogram 3 TelegramMethod / Pydantic awaitables in gather
        unhashable_events: list[str] = []

        class _UnhashableTelegramMethod:
            """Simulates aiogram 3.x Pydantic TelegramMethod (e.g. AnswerCallbackQuery) where __hash__ is None."""

            __hash__ = None  # type: ignore[assignment]

            def __init__(self, tag: str) -> None:
                self.tag = tag

            def __await__(self):
                async def _coro() -> str:
                    unhashable_events.append(self.tag)
                    return self.tag

                return _coro().__await__()

        # Confirm raw object is genuinely unhashable (reproduces Python 3.10+ asyncio.gather crash)
        unhashable_probe = _UnhashableTelegramMethod("probe")
        try:
            hash(unhashable_probe)
            raise AssertionError("Expected _UnhashableTelegramMethod to be unhashable")
        except TypeError:
            pass

        gather_res = await safe_gather(
            _UnhashableTelegramMethod("direct_method_1"),
            _UnhashableTelegramMethod("direct_method_2"),
        )
        assert gather_res == ["direct_method_1", "direct_method_2"]

        unhashable_events.clear()
        unhashable_text_cb = SimpleNamespace(
            answer=lambda text=None, show_alert=False: _UnhashableTelegramMethod(
                f"ack:{text or ''}"
            ),
            message=SimpleNamespace(
                photo=None,
                text="Old text panel",
                edit_text=lambda text, reply_markup=None, disable_web_page_preview=False: _UnhashableTelegramMethod(
                    f"edit_text:{text}"
                ),
            ),
        )
        await safe_edit_or_send(
            unhashable_text_cb,  # type: ignore[arg-type]
            "New text panel",
            None,
            answer_text="Loaded",
            also_ack=True,
        )
        assert "ack:Loaded" in unhashable_events
        assert "edit_text:New text panel" in unhashable_events

        unhashable_events.clear()
        unhashable_banner_cb = SimpleNamespace(
            answer=lambda text=None, show_alert=False: _UnhashableTelegramMethod(
                f"banner_ack:{text or ''}"
            ),
            message=SimpleNamespace(
                photo=[
                    SimpleNamespace(
                        file_id=welcome_asset["file_id"],
                        file_unique_id=welcome_asset["file_unique_id"],
                    )
                ],
                text=None,
                edit_caption=lambda caption="", reply_markup=None: _UnhashableTelegramMethod(
                    f"edit_caption:{caption}"
                ),
                edit_media=lambda media=None, reply_markup=None: _UnhashableTelegramMethod(
                    "edit_media"
                ),
            ),
        )
        await edit_or_send_banner(
            unhashable_banner_cb,  # type: ignore[arg-type]
            "welcome",
            "Hero caption",
            None,
            dummy_bot,  # type: ignore[arg-type]
            db,
            dummy_cfg,
            answer_text="Home",
            also_ack=True,
        )
        assert "banner_ack:Home" in unhashable_events
        assert "edit_caption:Hero caption" in unhashable_events

        # 29. Permanent Group-Safe Trial System + Atomic 1×/User Claim Lock + Dynamic Per-Owner Contact
        creator_a = await db.ensure_user(4001, "creator_alpha", "Creator Alpha")
        creator_no_user = await db.ensure_user(4002, "", "No Handle Owner")
        visitor_a = await db.ensure_user(5001, "visitor_one", "Visitor One")
        visitor_b = await db.ensure_user(5002, "visitor_two", "Visitor Two")

        # Dynamic Per-Owner Contact Support resolution:
        # Fallback to @username when support_contact is unset
        assert (
            await db.resolve_owner_contact_url(creator_a["vault_id"], preferred_user_id=4001)
            == "https://t.me/creator_alpha"
        )
        # Override with custom @support handle -> normalized https://t.me/...
        saved_sup = await db.set_support_contact(4001, "@AlphaSalesSupport")
        assert saved_sup == "https://t.me/AlphaSalesSupport"
        assert (
            await db.resolve_owner_contact_url(creator_a["vault_id"], preferred_user_id=4001)
            == "https://t.me/AlphaSalesSupport"
        )
        # Owner with neither support_contact nor @username -> cleanly returns '' (never invalid URL)
        assert (
            await db.resolve_owner_contact_url(
                creator_no_user["vault_id"], preferred_user_id=4002
            )
            == ""
        )
        no_contact_txt, no_contact_kb = build_trial_locked_card("", title="Secret Demo")
        assert "Trial Access Ended" in no_contact_txt
        assert no_contact_kb is None  # Cleanly omitted when owner has no contact!

        # Configure Creator Alpha: 2-hour trial auto-delete (7200s), No-Fwd ON, Default 1×/User ON
        await db.set_auto_delete_ttl(4001, 7200)
        assert format_ttl_short(7200) == "2h"
        await db.toggle_protect_content(4001)
        assert await db.toggle_default_one_time(4001) is True

        trial_f1 = await db.add_file(
            vault_id=creator_a["vault_id"],
            uploader_user_id=4001,
            group_id=None,
            media_type="document",
            file_id="TRIAL_DOC_1",
            file_unique_id="TRIAL_UNIQ_1",
            dump_message_id=70001,
            title="Course_Module_1.pdf",
            file_size=4_000_000,
        )
        trial_f2 = await db.add_file(
            vault_id=creator_a["vault_id"],
            uploader_user_id=4001,
            group_id=None,
            media_type="document",
            file_id="TRIAL_DOC_2",
            file_unique_id="TRIAL_UNIQ_2",
            dump_message_id=70002,
            title="Course_Module_2.pdf",
            file_size=4_000_000,
        )
        trial_pack = await db.create_pack(
            vault_id=creator_a["vault_id"],
            creator_user_id=4001,
            title="Flagship Masterclass Trial",
            file_ids=[trial_f1["id"], trial_f2["id"]],
            protect_content=True,
        )
        # Permanent by default + inherits default_one_time=1
        assert trial_pack["expires_at"] is None
        assert trial_pack["max_views"] == 0
        assert trial_pack["one_time_per_user"] == 1

        # Helper to simulate /start pack_<token> messages
        start_answers: list[tuple[str, object]] = []

        def _make_start_msg(uid: int, uname: str, fname: str):
            async def _ans(txt: str, reply_markup=None, disable_web_page_preview: bool = False, **kw):
                start_answers.append((txt, reply_markup))
                return SimpleNamespace(message_id=60000 + len(start_answers))

            return SimpleNamespace(
                message_id=61000 + uid,
                chat=SimpleNamespace(id=uid, type="private"),
                from_user=SimpleNamespace(id=uid, username=uname, full_name=fname, first_name=fname),
                answer=_ans,
            )

        # (a) Owner Preview: Creator opening their own 1×/User link is NEVER blocked and consumes 0 claims!
        dummy_bot.batch_copied_calls.clear()
        await start_help.cmd_start(
            _make_start_msg(4001, "creator_alpha", "Creator Alpha"),  # type: ignore[arg-type]
            SimpleNamespace(args=f"pack_{trial_pack['share_token']}"),  # type: ignore[arg-type]
            _DummyFSM(None),  # type: ignore[arg-type]
            db,
            dummy_bot,  # type: ignore[arg-type]
            dummy_cfg,
        )
        assert len(dummy_bot.batch_copied_calls) == 1
        assert await db.count_link_claims("pack", trial_pack["id"]) == 0
        assert (await db.get_pack(trial_pack["id"]))["views"] == 0

        # (b) First Open by Visitor A: Claims slot atomically, delivers protected files, schedules 2h delete
        dummy_bot.batch_copied_calls.clear()
        start_answers.clear()
        await start_help.cmd_start(
            _make_start_msg(5001, "visitor_one", "Visitor One"),  # type: ignore[arg-type]
            SimpleNamespace(args=f"pack_{trial_pack['share_token']}"),  # type: ignore[arg-type]
            _DummyFSM(None),  # type: ignore[arg-type]
            db,
            dummy_bot,  # type: ignore[arg-type]
            dummy_cfg,
        )
        assert len(dummy_bot.batch_copied_calls) == 1
        assert dummy_bot.batch_copied_calls[0]["protect"] is True
        assert await db.count_link_claims("pack", trial_pack["id"]) == 1
        assert len(start_answers) == 0  # Delivered pack, not locked card

        # (c) Repeat Open by Visitor A: Sends ZERO files, shows `🔒 Trial Access Ended` + Creator Alpha's Contact button!
        dummy_bot.batch_copied_calls.clear()
        dummy_bot.sent_media.clear()
        start_answers.clear()
        await start_help.cmd_start(
            _make_start_msg(5001, "visitor_one", "Visitor One"),  # type: ignore[arg-type]
            SimpleNamespace(args=f"pack_{trial_pack['share_token']}"),  # type: ignore[arg-type]
            _DummyFSM(None),  # type: ignore[arg-type]
            db,
            dummy_bot,  # type: ignore[arg-type]
            dummy_cfg,
        )
        assert len(dummy_bot.batch_copied_calls) == 0, "Repeat visitor must receive ZERO files!"
        assert len(dummy_bot.sent_media) == 0, "Repeat visitor must receive ZERO media!"
        assert len(start_answers) == 1
        locked_msg_txt, locked_msg_kb = start_answers[0]
        assert "Trial Access Ended" in locked_msg_txt
        assert locked_msg_kb is not None
        contact_btn = locked_msg_kb.inline_keyboard[0][0]
        assert contact_btn.text == "💬 Contact Owner"
        assert contact_btn.url == "https://t.me/AlphaSalesSupport"
        assert contact_btn.style == "primary"

        # (d) New Visitor B on the SAME permanent link: Gets fresh 1-time trial + independent timer!
        await start_help.cmd_start(
            _make_start_msg(5002, "visitor_two", "Visitor Two"),  # type: ignore[arg-type]
            SimpleNamespace(args=f"pack_{trial_pack['share_token']}"),  # type: ignore[arg-type]
            _DummyFSM(None),  # type: ignore[arg-type]
            db,
            dummy_bot,  # type: ignore[arg-type]
            dummy_cfg,
        )
        assert len(dummy_bot.batch_copied_calls) == 1
        assert await db.count_link_claims("pack", trial_pack["id"]) == 2

        # (e) Resetting link (`regenerate_pack_token`) clears `link_user_claims`!
        reset_tok = await db.regenerate_pack_token(trial_pack["id"])
        assert reset_tok != trial_pack["share_token"]
        assert await db.count_link_claims("pack", trial_pack["id"]) == 0

        # 30. Restart-Safe Batch Auto-Delete (`scheduled_deletes` + `bot.delete_messages()` <=100 chunking)
        # Verify Visitor A & B scheduled_deletes rows were persisted in SQLite, plus test >100 msg chunking
        big_msg_ids = list(range(90001, 90251))  # 250 messages -> must chunk into 3 calls (100 + 100 + 50)
        await db.add_scheduled_delete(
            chat_id=5001,
            message_ids=big_msg_ids,
            delete_at=int(time.time()) - 5,  # Already due!
            notify_text=build_trial_expiry_notice_text("Flagship Masterclass Trial", 7200),
            contact_url="https://t.me/AlphaSalesSupport",
        )
        # Force earlier scheduled jobs to be due as well
        await db.conn.execute("UPDATE scheduled_deletes SET delete_at = ?", (int(time.time()) - 2,))
        await db.conn.commit()

        dummy_bot.batch_deleted_calls.clear()
        dummy_bot.sent_media.clear()
        swept = await execute_due_scheduled_deletes(dummy_bot, db, limit=50)
        assert swept >= 3
        # Confirm 250-message job was split into chunks of <= 100 (100, 100, 50)
        chunk_sizes_5001 = [
            len(mids) for cid, mids in dummy_bot.batch_deleted_calls if cid == 5001
        ]
        assert 100 in chunk_sizes_5001 and 50 in chunk_sizes_5001
        assert all(sz <= 100 for sz in chunk_sizes_5001)

        # Confirm post-expiry notice was delivered with `💬 Contact Owner` primary button!
        expiry_notices = [
            m
            for m in dummy_bot.sent_media
            if m["type"] == "msg" and "Trial Session Ended" in m["text"]
        ]
        assert len(expiry_notices) >= 1
        exp_kb = expiry_notices[-1]["reply_markup"]
        assert exp_kb is not None
        assert exp_kb.inline_keyboard[0][0].text == "💬 Contact Owner"
        assert exp_kb.inline_keyboard[0][0].url == "https://t.me/AlphaSalesSupport"
        assert exp_kb.inline_keyboard[0][0].style == "primary"

        # 31. Bot API 9.4+ Polish: `<blockquote expandable>`, Message Effects, & Mobile 360px Ergonomics (<=2 btns/row, <=64B cb)
        big_pack_det = await packs.render_pack_detail(
            2026, big_pack["id"], db, dummy_bot  # type: ignore[arg-type]
        )
        assert big_pack_det is not None
        assert "<blockquote expandable>" in big_pack_det[0]
        assert EFFECT_PARTY in dummy_bot.effects_used
        assert EFFECT_FIRE in dummy_bot.effects_used

        # Check mobile-first max 2 buttons per row (3 allowed only for compact pagination ◀ 1/3 ▶)
        all_kbs = [
            home_kb,
            kb_primary,
            kb_manage,
            kb_link,
            up_kb,
            set_kb,
            pack_list_kb,
            big_pack_det[1],
            edit_drawer[1],
        ]
        for kb_item in all_kbs:
            for row in kb_item.inline_keyboard:
                is_pager = len(row) == 3 and row[1].callback_data == "noop"
                assert is_pager or len(row) <= 2, f"Row exceeds 2 buttons on 360px screen: {[b.text for b in row]}"
                for btn in row:
                    if btn.callback_data:
                        assert len(btn.callback_data.encode("utf-8")) <= 64

        await db.close()
        print("ALL PRO FEATURES, TRIAL LOCKS, BATCH APIS, BOT API 9.4 UX & ZERO-LEAK TESTS PASSED!")


if __name__ == "__main__":
    asyncio.run(run_tests())
