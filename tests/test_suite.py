import asyncio
import os
import sys
import tempfile
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from database import (
    AUTO_DELETE_CYCLE,
    Database,
    hash_pin,
    natural_sort_key,
    resolve_effective_auto_delete_ttl,
    verify_pin,
)
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
        self.batch_forwarded_calls: list[dict] = []
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
        reply_markup=None,
        message_effect_id: str | None = None,
    ):
        self._seq += 1
        if message_effect_id:
            self.effects_used.append(message_effect_id)
        if not isinstance(photo, str):
            self.dump_uploads.append((chat_id, caption))
        else:
            self.sent_media.append(
                {
                    "type": "photo",
                    "chat_id": chat_id,
                    "protect": protect_content,
                    "caption": caption,
                    "reply_markup": reply_markup,
                    "effect": message_effect_id,
                }
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
        reply_markup=None,
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
                "reply_markup": reply_markup,
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
        reply_markup=None,
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
                "reply_markup": reply_markup,
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

    async def forward_messages(
        self,
        chat_id: int,
        from_chat_id: int,
        message_ids: list[int],
        disable_notification: bool = False,
        protect_content: bool = False,
    ):
        assert len(message_ids) <= 100, "forward_messages must chunk <= 100 IDs"
        assert all(
            message_ids[i] < message_ids[i + 1] for i in range(len(message_ids) - 1)
        ), "forward_messages requires strictly increasing message_ids!"
        self.batch_forwarded_calls.append(
            {
                "chat_id": chat_id,
                "from_chat_id": from_chat_id,
                "message_ids": list(message_ids),
                "disable_notification": disable_notification,
            }
        )
        out = []
        for mid in message_ids:
            self._seq += 1
            self.forwarded_messages.append((chat_id, from_chat_id, mid))
            out.append(SimpleNamespace(message_id=10000 + self._seq))
        return out

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
        assert all(
            message_ids[i] < message_ids[i + 1] for i in range(len(message_ids) - 1)
        ), "copy_messages requires strictly increasing message_ids!"
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

        # Wait for sliding-window debounce + sequential/batch archive to flush
        await asyncio.sleep(0.45)
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
        await ipv4_sess.close()

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

        # (a) Owner Preview: Creator opening their own 1×/User link is NEVER blocked, NEVER protected, NEVER schedules auto-delete!
        dummy_bot.batch_copied_calls.clear()
        dummy_bot.sent_media.clear()
        await start_help.cmd_start(
            _make_start_msg(4001, "creator_alpha", "Creator Alpha"),  # type: ignore[arg-type]
            SimpleNamespace(args=f"pack_{trial_pack['share_token']}"),  # type: ignore[arg-type]
            _DummyFSM(None),  # type: ignore[arg-type]
            db,
            dummy_bot,  # type: ignore[arg-type]
            dummy_cfg,
        )
        assert len(dummy_bot.batch_copied_calls) == 1
        assert dummy_bot.batch_copied_calls[0]["protect"] is False, "Owner self-retrieval must NEVER enforce protect_content=True!"
        assert any(
            m["type"] == "msg" and "Owner Access" in m["text"] and m["protect"] is False
            for m in dummy_bot.sent_media
        )
        assert await db.count_link_claims("pack", trial_pack["id"]) == 0
        assert (await db.get_pack(trial_pack["id"]))["views"] == 0
        owner_sched_cnt = (
            await (await db.conn.execute("SELECT COUNT(*) FROM scheduled_deletes WHERE chat_id = 4001")).fetchone()
        )[0]
        assert owner_sched_cnt == 0, "Owner self-retrieval must NEVER schedule auto-delete!"

        # (b) First Open by Visitor A: Claims slot atomically, delivers protected files + 2-column Request/Contact CTA, schedules 2h delete
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
        assert len(dummy_bot.batch_copied_calls) == 1
        assert dummy_bot.batch_copied_calls[0]["protect"] is True
        assert await db.count_link_claims("pack", trial_pack["id"]) == 1
        assert len(start_answers) == 0  # Delivered pack, not locked card
        vis_header_kb = dummy_bot.sent_media[0]["reply_markup"]
        assert vis_header_kb is not None
        assert vis_header_kb.inline_keyboard[0][0].text == "🔓 Request Full Access"
        assert vis_header_kb.inline_keyboard[0][0].callback_data == f"req:p:{trial_pack['id']}"
        assert vis_header_kb.inline_keyboard[0][0].style == "primary"
        assert vis_header_kb.inline_keyboard[0][1].text == "💬 Contact Owner"
        assert vis_header_kb.inline_keyboard[0][1].url == "https://t.me/AlphaSalesSupport"

        # (c) Repeat Open by Visitor A: Sends ZERO files, shows `🔒 Trial Access Ended` + 2-column `[🔓 Request Full Access] [💬 Contact Owner]`!
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
        req_btn = locked_msg_kb.inline_keyboard[0][0]
        contact_btn = locked_msg_kb.inline_keyboard[0][1]
        assert req_btn.text == "🔓 Request Full Access"
        assert req_btn.callback_data == f"req:p:{trial_pack['id']}"
        assert req_btn.style == "primary"
        assert contact_btn.text == "💬 Contact Owner"
        assert contact_btn.url == "https://t.me/AlphaSalesSupport"

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
            link_type="pack",
            link_id=trial_pack["id"],
        )
        # Force earlier scheduled jobs to be due as well
        await db.conn.execute("UPDATE scheduled_deletes SET delete_at = ?", (int(time.time()) - 2,))
        await db.conn.commit()

        dummy_bot.batch_deleted_calls.clear()
        dummy_bot.sent_media.clear()
        swept = await execute_due_scheduled_deletes(dummy_bot, db, limit=50)
        assert swept >= 2
        # Confirm 250-message job was split into chunks of <= 100 (100, 100, 50)
        chunk_sizes_5001 = [
            len(mids) for cid, mids in dummy_bot.batch_deleted_calls if cid == 5001
        ]
        assert 100 in chunk_sizes_5001 and 50 in chunk_sizes_5001
        assert all(sz <= 100 for sz in chunk_sizes_5001)

        # Confirm post-expiry notice was delivered with 2-column `[🔓 Request Full Access] [💬 Contact Owner]`!
        expiry_notices = [
            m
            for m in dummy_bot.sent_media
            if m["type"] == "msg" and "Trial Session Ended" in m["text"]
        ]
        assert len(expiry_notices) >= 1
        exp_kb = expiry_notices[-1]["reply_markup"]
        assert exp_kb is not None
        assert exp_kb.inline_keyboard[0][0].text == "🔓 Request Full Access"
        assert exp_kb.inline_keyboard[0][0].callback_data == f"req:p:{trial_pack['id']}"
        assert exp_kb.inline_keyboard[0][0].style == "primary"
        assert exp_kb.inline_keyboard[0][1].text == "💬 Contact Owner"
        assert exp_kb.inline_keyboard[0][1].url == "https://t.me/AlphaSalesSupport"

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

        # 32. Per-Link `💣 Auto-Del` Priority Over Global `/settings` + Full Cycle + Visitor Execution
        # Verify exact 7-step cycle: Off(0) -> 5m(300) -> 15m(900) -> 1h(3600) -> 2h(7200) -> 6h(21600) -> 24h(86400) -> Off(0)
        assert AUTO_DELETE_CYCLE == [0, 300, 900, 3600, 7200, 21600, 86400]
        assert [format_ttl_short(v) for v in AUTO_DELETE_CYCLE] == [
            "Off",
            "5m",
            "15m",
            "1h",
            "2h",
            "6h",
            "24h",
        ]

        owner_p = await db.ensure_user(9101, "prio_owner", "Priority Owner")
        await db.set_support_contact(9101, "@PriorityOwnerSupport")
        visitor_p = await db.ensure_user(9201, "prio_visitor", "Priority Visitor")
        # Set visitor's own `/settings` auto-delete to 15m (900s) to prove visitor's timer is NEVER used!
        await db.set_auto_delete_ttl(9201, 900)

        prio_file = await db.add_file(
            vault_id=owner_p["vault_id"],
            uploader_user_id=9101,
            group_id=None,
            media_type="document",
            file_id="PRIO_DOC_1",
            file_unique_id="PRIO_UNIQ_1",
            dump_message_id=88001,
            title="Priority_Test_Guide.pdf",
            file_size=2_500_000,
        )
        prio_file_2 = await db.add_file(
            vault_id=owner_p["vault_id"],
            uploader_user_id=9101,
            group_id=None,
            media_type="document",
            file_id="PRIO_DOC_2",
            file_unique_id="PRIO_UNIQ_2",
            dump_message_id=88002,
            title="Priority_Test_Bonus.pdf",
            file_size=1_500_000,
        )
        prio_ftok = await db.toggle_share_token(prio_file["id"])
        assert prio_ftok is not None

        prio_pack = await db.create_pack(
            vault_id=owner_p["vault_id"],
            creator_user_id=9101,
            title="Priority Pack Bundle",
            file_ids=[prio_file["id"], prio_file_2["id"]],
            protect_content=True,
        )
        assert prio_pack["auto_delete_ttl"] == 0
        assert (await db.get_file(prio_file["id"]))["auto_delete_ttl"] == 0

        # Verify cycling through all steps on both File Share card and Pack card
        expected_steps = [300, 900, 3600, 7200, 21600, 86400, 0]
        for step_ttl in expected_steps:
            assert await db.cycle_file_auto_delete_ttl(prio_file["id"]) == step_ttl
            assert await db.cycle_pack_auto_delete_ttl(prio_pack["id"]) == step_ttl

        # Verify UI button labels clearly distinguish `💣 Auto-Del: ...` vs `⏳ Link: Never`
        await db.set_file_auto_delete_ttl(prio_file["id"], 300)
        f_card_row = await db.get_file(prio_file["id"])
        f_link_kb = build_file_link_keyboard(f_card_row, bot_username="KoshBox_Bot")
        f_btn_texts = [b.text for r in f_link_kb.inline_keyboard for b in r]
        assert "💣 Auto-Del: 5m" in f_btn_texts
        assert "⏳ Link: Never" in f_btn_texts

        await db.set_pack_auto_delete_ttl(prio_pack["id"], 21600)
        p_det_rendered = await packs.render_pack_detail(
            9101, prio_pack["id"], db, dummy_bot  # type: ignore[arg-type]
        )
        assert p_det_rendered is not None
        p_btn_texts = [b.text for r in p_det_rendered[1].inline_keyboard for b in r]
        assert "💣 Auto-Del: 6h" in p_btn_texts
        assert "⏳ Link: Never" in p_btn_texts

        # Clean scheduled_deletes table before testing the 4 exact priority cases end-to-end
        await db.conn.execute("DELETE FROM scheduled_deletes")
        await db.conn.commit()

        async def _fetch_scheduled_rows() -> list[dict]:
            cur = await db.conn.execute(
                "SELECT id, chat_id, message_ids_json, delete_at, notify_text, contact_url FROM scheduled_deletes ORDER BY id ASC"
            )
            return [dict(r) for r in await cur.fetchall()]

        # Case (a): File `5m` (300s) + Global `1h` (3600s) -> MUST resolve to `5m` (300s)
        await db.set_file_auto_delete_ttl(prio_file["id"], 300)
        await db.set_auto_delete_ttl(9101, 3600)
        assert resolve_effective_auto_delete_ttl(300, 3600) == 300
        assert await db.resolve_file_auto_delete_ttl(await db.get_file(prio_file["id"])) == 300

        t_before = int(time.time())
        dummy_bot.sent_media.clear()
        await start_help.cmd_start(
            _make_start_msg(9201, "prio_visitor", "Priority Visitor"),  # type: ignore[arg-type]
            SimpleNamespace(args=f"file_{prio_ftok}"),  # type: ignore[arg-type]
            _DummyFSM(None),  # type: ignore[arg-type]
            db,
            dummy_bot,  # type: ignore[arg-type]
            dummy_cfg,
        )
        t_after = int(time.time())
        sched_a = await _fetch_scheduled_rows()
        assert len(sched_a) == 1
        assert t_before + 300 <= sched_a[0]["delete_at"] <= t_after + 300
        assert "(5m)" in sched_a[0]["notify_text"]
        assert sched_a[0]["contact_url"] == "https://t.me/PriorityOwnerSupport"
        assert "Auto-deletes in 5m" in dummy_bot.sent_media[-1]["caption"]
        await db.conn.execute("DELETE FROM scheduled_deletes")
        await db.conn.commit()

        # Also verify Case (a) on Pack (`5m` Pack + `1h` Global -> `5m`, saves header + all `copy_messages` IDs)
        await db.set_pack_auto_delete_ttl(prio_pack["id"], 300)
        assert await db.resolve_pack_auto_delete_ttl(await db.get_pack(prio_pack["id"])) == 300
        dummy_bot.sent_media.clear()
        dummy_bot.batch_copied_calls.clear()
        t_before = int(time.time())
        await start_help.cmd_start(
            _make_start_msg(9201, "prio_visitor", "Priority Visitor"),  # type: ignore[arg-type]
            SimpleNamespace(args=f"pack_{prio_pack['share_token']}"),  # type: ignore[arg-type]
            _DummyFSM(None),  # type: ignore[arg-type]
            db,
            dummy_bot,  # type: ignore[arg-type]
            dummy_cfg,
        )
        t_after = int(time.time())
        sched_ap = await _fetch_scheduled_rows()
        assert len(sched_ap) == 1
        assert t_before + 300 <= sched_ap[0]["delete_at"] <= t_after + 300
        assert "(5m)" in sched_ap[0]["notify_text"]
        assert sched_ap[0]["contact_url"] == "https://t.me/PriorityOwnerSupport"
        import json as _json

        saved_pack_mids = _json.loads(sched_ap[0]["message_ids_json"])
        assert len(saved_pack_mids) == 3  # 1 pack header + 2 `copy_messages` batch items!
        # Execute due sweeper and verify all 3 messages are deleted via `bot.delete_messages` + Contact Owner sent
        await db.conn.execute("UPDATE scheduled_deletes SET delete_at = ?", (int(time.time()) - 1,))
        await db.conn.commit()
        dummy_bot.batch_deleted_calls.clear()
        dummy_bot.sent_media.clear()
        assert await execute_due_scheduled_deletes(dummy_bot, db) == 1
        assert dummy_bot.batch_deleted_calls == [(9201, sorted(saved_pack_mids))]
        exp_row_ap = dummy_bot.sent_media[-1]["reply_markup"].inline_keyboard[0]
        assert exp_row_ap[0].text == "🔓 Request Full Access"
        assert exp_row_ap[0].callback_data == f"req:p:{prio_pack['id']}"
        assert exp_row_ap[1].text == "💬 Contact Owner"
        assert exp_row_ap[1].url == "https://t.me/PriorityOwnerSupport"

        # Case (b): File `Off` (0) + Global `1h` (3600s) -> MUST fall back to owner's Global `1h` (3600s)
        await db.set_file_auto_delete_ttl(prio_file["id"], 0)
        await db.set_auto_delete_ttl(9101, 3600)
        assert resolve_effective_auto_delete_ttl(0, 3600) == 3600
        assert await db.resolve_file_auto_delete_ttl(await db.get_file(prio_file["id"])) == 3600

        t_before = int(time.time())
        dummy_bot.sent_media.clear()
        await start_help.cmd_start(
            _make_start_msg(9201, "prio_visitor", "Priority Visitor"),  # type: ignore[arg-type]
            SimpleNamespace(args=f"file_{prio_ftok}"),  # type: ignore[arg-type]
            _DummyFSM(None),  # type: ignore[arg-type]
            db,
            dummy_bot,  # type: ignore[arg-type]
            dummy_cfg,
        )
        t_after = int(time.time())
        sched_b = await _fetch_scheduled_rows()
        assert len(sched_b) == 1
        assert t_before + 3600 <= sched_b[0]["delete_at"] <= t_after + 3600
        assert "(1h)" in sched_b[0]["notify_text"]
        assert "Auto-deletes in 1h" in dummy_bot.sent_media[-1]["caption"]
        await db.conn.execute("DELETE FROM scheduled_deletes")
        await db.conn.commit()

        # Case (c): File `2h` (7200s) + Global `Off` (0) -> MUST use File's `2h` (7200s)
        await db.set_file_auto_delete_ttl(prio_file["id"], 7200)
        await db.set_auto_delete_ttl(9101, 0)
        assert resolve_effective_auto_delete_ttl(7200, 0) == 7200
        assert await db.resolve_file_auto_delete_ttl(await db.get_file(prio_file["id"])) == 7200

        t_before = int(time.time())
        dummy_bot.sent_media.clear()
        await start_help.cmd_start(
            _make_start_msg(9201, "prio_visitor", "Priority Visitor"),  # type: ignore[arg-type]
            SimpleNamespace(args=f"file_{prio_ftok}"),  # type: ignore[arg-type]
            _DummyFSM(None),  # type: ignore[arg-type]
            db,
            dummy_bot,  # type: ignore[arg-type]
            dummy_cfg,
        )
        t_after = int(time.time())
        sched_c = await _fetch_scheduled_rows()
        assert len(sched_c) == 1
        assert t_before + 7200 <= sched_c[0]["delete_at"] <= t_after + 7200
        assert "(2h)" in sched_c[0]["notify_text"]
        assert "Auto-deletes in 2h" in dummy_bot.sent_media[-1]["caption"]
        await db.conn.execute("DELETE FROM scheduled_deletes")
        await db.conn.commit()

        # Case (d): File `Off` (0) + Global `Off` (0) -> NO auto-delete (even though visitor has 15m in their own `/settings`!)
        await db.set_file_auto_delete_ttl(prio_file["id"], 0)
        await db.set_pack_auto_delete_ttl(prio_pack["id"], 0)
        await db.set_auto_delete_ttl(9101, 0)
        assert (await db.get_user(9201))["auto_delete_ttl"] == 900  # Visitor's own setting is 15m
        assert resolve_effective_auto_delete_ttl(0, 0) == 0
        assert await db.resolve_file_auto_delete_ttl(await db.get_file(prio_file["id"])) == 0
        assert await db.resolve_pack_auto_delete_ttl(await db.get_pack(prio_pack["id"])) == 0

        dummy_bot.sent_media.clear()
        await start_help.cmd_start(
            _make_start_msg(9201, "prio_visitor", "Priority Visitor"),  # type: ignore[arg-type]
            SimpleNamespace(args=f"file_{prio_ftok}"),  # type: ignore[arg-type]
            _DummyFSM(None),  # type: ignore[arg-type]
            db,
            dummy_bot,  # type: ignore[arg-type]
            dummy_cfg,
        )
        await start_help.cmd_start(
            _make_start_msg(9201, "prio_visitor", "Priority Visitor"),  # type: ignore[arg-type]
            SimpleNamespace(args=f"pack_{prio_pack['share_token']}"),  # type: ignore[arg-type]
            _DummyFSM(None),  # type: ignore[arg-type]
            db,
            dummy_bot,  # type: ignore[arg-type]
            dummy_cfg,
        )
        sched_d = await _fetch_scheduled_rows()
        assert len(sched_d) == 0, "Neither file nor pack should schedule auto-delete when both per-link and owner global are Off!"

        # Verify SQLite auto-migration on legacy DB lacking `auto_delete_ttl` on `files` & `packs`
        legacy_db_path = os.path.join(tmpdir, "legacy_upgrade.db")
        import aiosqlite as _aiosqlite

        async with _aiosqlite.connect(legacy_db_path) as raw_conn:
            await raw_conn.executescript(
                """
                CREATE TABLE vaults (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER NOT NULL);
                CREATE TABLE users (user_id INTEGER PRIMARY KEY, vault_id INTEGER NOT NULL, created_at INTEGER NOT NULL, last_seen INTEGER NOT NULL);
                CREATE TABLE files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vault_id INTEGER NOT NULL,
                    uploader_user_id INTEGER NOT NULL,
                    media_type TEXT NOT NULL,
                    file_id TEXT NOT NULL,
                    file_unique_id TEXT NOT NULL,
                    dump_message_id INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE packs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vault_id INTEGER NOT NULL,
                    creator_user_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    share_token TEXT UNIQUE NOT NULL,
                    created_at INTEGER NOT NULL
                );
                """
            )
            await raw_conn.commit()
        migrated_db = Database(legacy_db_path)
        await migrated_db.connect()
        f_cols = {
            r["name"]
            for r in await (await migrated_db.conn.execute("PRAGMA table_info(files);")).fetchall()
        }
        p_cols = {
            r["name"]
            for r in await (await migrated_db.conn.execute("PRAGMA table_info(packs);")).fetchall()
        }
        g_cols = {
            r["name"]
            for r in await (await migrated_db.conn.execute("PRAGMA table_info(link_access_grants);")).fetchall()
        }
        assert "auto_delete_ttl" in f_cols and "auto_delete_ttl" in p_cols
        assert {"link_type", "link_id", "user_id", "granted_by", "expires_at", "created_at"} <= g_cols
        await migrated_db.close()

        # 33. Owner Self-Retrieval Bug Fix (Primary + Linked Secondary Account across Get File, Get All, Share Links)
        owner_main = await db.ensure_user(9501, "owner_main", "Owner Main")
        owner_alt = await db.ensure_user(9502, "owner_alt", "Owner Linked Alt")
        # Link `owner_alt` (9502) into `owner_main` (9501) vault
        lreq = await db.create_link_request(9501, owner_main["vault_id"], 600)
        await db.attach_requester_to_link(lreq["code"], 9502, "Owner Linked Alt", "owner_alt")
        await db.approve_link_request(lreq["code"], 9501)
        assert (await db.get_user(9502))["vault_id"] == owner_main["vault_id"]

        # Enable strict settings on owner (5m auto-delete, protect_content=True, 1x/User=True)
        await db.set_auto_delete_ttl(9501, 300)
        await db.set_auto_delete_ttl(9502, 300)
        await db.toggle_protect_content(9501)
        await db.toggle_default_one_time(9501)
        await db.set_support_contact(9501, "@OwnerSalesDesk")

        own_file = await db.add_file(
            vault_id=owner_main["vault_id"],
            uploader_user_id=9501,
            group_id=None,
            media_type="document",
            file_id="OWN_DOC_1",
            file_unique_id="OWN_UNIQ_1",
            dump_message_id=95001,
            title="2001_471_Chapter_1.pdf",
            file_size=4_000_000,
        )
        own_file_2 = await db.add_file(
            vault_id=owner_main["vault_id"],
            uploader_user_id=9501,
            group_id=None,
            media_type="audio",
            file_id="OWN_AUD_2",
            file_unique_id="OWN_UNIQ_2",
            dump_message_id=95002,
            title="Little_Monkey_Ep_2.mp3",
            file_size=5_000_000,
        )
        own_ftok = await db.toggle_share_token(own_file["id"])
        assert own_ftok is not None
        await db.set_file_auto_delete_ttl(own_file["id"], 300)
        await db.set_file_share_pin(own_file["id"], "9999")

        own_pack = await db.create_pack(
            vault_id=owner_main["vault_id"],
            creator_user_id=9501,
            title="Little monkey (5)",
            file_ids=[own_file["id"], own_file_2["id"]],
            pin="8888",
            protect_content=True,
            one_time_per_user=True,
            auto_delete_ttl=300,
            max_views=1,
        )

        await db.conn.execute("DELETE FROM scheduled_deletes")
        await db.conn.commit()
        dummy_bot.sent_media.clear()
        dummy_bot.batch_copied_calls.clear()

        def _make_cb(uid: int, uname: str, fname: str, data_str: str):
            cb_acks: list[tuple] = []
            msg_edits: list[tuple] = []
            msg_answers: list[tuple] = []

            async def _cb_ans(txt=None, show_alert=False):
                cb_acks.append((txt, show_alert))

            async def _m_edit(txt, reply_markup=None, disable_web_page_preview=False):
                msg_edits.append((txt, reply_markup))

            async def _m_ans(txt, reply_markup=None, disable_web_page_preview=False):
                msg_answers.append((txt, reply_markup))
                return SimpleNamespace(message_id=96000 + len(msg_answers))

            cb_obj = SimpleNamespace(
                data=data_str,
                from_user=SimpleNamespace(id=uid, username=uname, full_name=fname, first_name=fname),
                message=SimpleNamespace(
                    message_id=95500,
                    chat=SimpleNamespace(id=uid, type="private"),
                    photo=None,
                    text="card",
                    edit_text=_m_edit,
                    answer=_m_ans,
                ),
                answer=_cb_ans,
            )
            return cb_obj, cb_acks, msg_edits, msg_answers

        # (33a) `📥 Get File` (`f:get:<fid>`) by primary AND linked owner -> ZERO scheduled_deletes, protect_content=False
        cb_fget, fget_acks, _, _ = _make_cb(9502, "owner_alt", "Owner Linked Alt", f"f:get:{own_file['id']}")
        await file_actions.cb_get_file_here(cb_fget, db, dummy_bot)  # type: ignore[arg-type]
        assert len(dummy_bot.sent_media) == 1
        assert dummy_bot.sent_media[-1]["protect"] is False
        assert "Owner Access" in dummy_bot.sent_media[-1]["caption"]
        assert "Auto-deletes" not in dummy_bot.sent_media[-1]["caption"]
        assert "Owner Access" in (fget_acks[-1][0] or "")

        # (33b) `📥 Get All` (`pk:get:<pid>`) by primary AND linked owner -> ZERO scheduled_deletes, protect_content=False
        dummy_bot.sent_media.clear()
        dummy_bot.batch_copied_calls.clear()
        cb_pget, pget_acks, _, _ = _make_cb(9501, "owner_main", "Owner Main", f"pk:get:{own_pack['id']}")
        await packs.cb_pack_get_all(cb_pget, db, dummy_bot, dummy_cfg)  # type: ignore[arg-type]
        assert len(dummy_bot.batch_copied_calls) == 1
        assert dummy_bot.batch_copied_calls[0]["protect"] is False
        assert "Owner Access" in dummy_bot.sent_media[0]["text"]
        assert "Trial auto-deletes" not in dummy_bot.sent_media[0]["text"]

        # (33c) Linked secondary account opening `?start=file_...` & `?start=pack_...` (even with PIN, 1x/User, paused/limit)
        await start_help.cmd_start(
            _make_start_msg(9502, "owner_alt", "Owner Linked Alt"),  # type: ignore[arg-type]
            SimpleNamespace(args=f"file_{own_ftok}"),  # type: ignore[arg-type]
            _DummyFSM(None),  # type: ignore[arg-type]
            db,
            dummy_bot,  # type: ignore[arg-type]
            dummy_cfg,
        )
        await start_help.cmd_start(
            _make_start_msg(9502, "owner_alt", "Owner Linked Alt"),  # type: ignore[arg-type]
            SimpleNamespace(args=f"pack_{own_pack['share_token']}"),  # type: ignore[arg-type]
            _DummyFSM(None),  # type: ignore[arg-type]
            db,
            dummy_bot,  # type: ignore[arg-type]
            dummy_cfg,
        )
        assert await db.count_link_claims("file", own_file["id"]) == 0
        assert await db.count_link_claims("pack", own_pack["id"]) == 0
        assert len(await _fetch_scheduled_rows()) == 0, "Owner & linked account must NEVER have scheduled_deletes!"

        # 34. Full Access / Paid Unlock Approval System (Visitor Request -> 10m Rate Limit -> Owner Approval Card -> 1d/7d/30d/Lifetime/Reject)
        start_help.reset_access_request_cooldowns()
        buyer_uid = 9601
        await db.ensure_user(buyer_uid, "paid_buyer", "Paid Buyer")

        # Remove PIN on `own_pack` & raise `max_views` so buyer can take 1-time trial first
        await db.set_pack_pin(own_pack["id"], None)
        await db.conn.execute("UPDATE packs SET max_views = 0 WHERE id = ?", (own_pack["id"],))
        await db.conn.commit()

        # Step 1: Buyer opens pack -> gets 5m trial + 2-column CTA `[🔓 Request Full Access] [💬 Contact Owner]`
        dummy_bot.sent_media.clear()
        dummy_bot.batch_copied_calls.clear()
        await start_help.cmd_start(
            _make_start_msg(buyer_uid, "paid_buyer", "Paid Buyer"),  # type: ignore[arg-type]
            SimpleNamespace(args=f"pack_{own_pack['share_token']}"),  # type: ignore[arg-type]
            _DummyFSM(None),  # type: ignore[arg-type]
            db,
            dummy_bot,  # type: ignore[arg-type]
            dummy_cfg,
        )
        assert len(dummy_bot.batch_copied_calls) == 1
        assert await db.count_link_claims("pack", own_pack["id"]) == 1
        assert len(await _fetch_scheduled_rows()) == 1  # Trial auto-delete scheduled for buyer

        # Step 2: Buyer taps `🔓 Request Full Access` (`req:p:<pack_id>`)
        dummy_bot.sent_media.clear()
        req_cb_obj, req_acks, _, req_visitor_msgs = _make_cb(
            buyer_uid, "paid_buyer", "Paid Buyer", f"req:p:{own_pack['id']}"
        )
        await start_help.cb_request_full_access(req_cb_obj, db, dummy_bot)  # type: ignore[arg-type]
        assert len(req_visitor_msgs) == 1
        assert "Full access request sent to the owner" in req_visitor_msgs[0][0]
        assert req_visitor_msgs[0][1].inline_keyboard[0][0].text == "💬 Contact Owner"
        assert req_visitor_msgs[0][1].inline_keyboard[0][0].url == "https://t.me/OwnerSalesDesk"

        # Both linked owner accounts (9501 & 9502) receive the sleek Approval Card in DM!
        owner_dms = [
            m for m in dummy_bot.sent_media
            if m["type"] == "msg" and m["chat_id"] in (9501, 9502) and "Full Access Request" in m["text"]
        ]
        assert {m["chat_id"] for m in owner_dms} == {9501, 9502}
        app_card = owner_dms[0]
        assert "Paid Buyer" in app_card["text"]
        assert "@paid_buyer" in app_card["text"]
        assert str(buyer_uid) in app_card["text"]
        assert "Little monkey (5)" in app_card["text"]
        app_kb = app_card["reply_markup"]
        assert len(app_kb.inline_keyboard) == 3
        assert [b.text for b in app_kb.inline_keyboard[0]] == ["✅ 1 Day", "✅ 7 Days"]
        assert [b.style for b in app_kb.inline_keyboard[0]] == ["success", "success"]
        assert [b.text for b in app_kb.inline_keyboard[1]] == ["✅ 30 Days", "♾️ Lifetime"]
        assert [b.style for b in app_kb.inline_keyboard[1]] == ["success", "primary"]
        assert [b.text for b in app_kb.inline_keyboard[2]] == ["❌ Reject"]
        assert [b.style for b in app_kb.inline_keyboard[2]] == ["danger"]
        for r in app_kb.inline_keyboard:
            for b in r:
                assert len(b.callback_data.encode("utf-8")) <= 64

        # Step 3: Duplicate request within 10m is rate-limited via bounded TTLCache (zero owner spam!)
        dummy_bot.sent_media.clear()
        dup_cb_obj, dup_acks, _, dup_visitor_msgs = _make_cb(
            buyer_uid, "paid_buyer", "Paid Buyer", f"req:p:{own_pack['id']}"
        )
        await start_help.cb_request_full_access(dup_cb_obj, db, dummy_bot)  # type: ignore[arg-type]
        assert len(dup_visitor_msgs) == 0
        assert len(dummy_bot.sent_media) == 0
        assert dup_acks and dup_acks[0][1] is True and "already sent" in (dup_acks[0][0] or "").lower()

        # Step 4: Owner taps `[ ✅ 30 Days ]` (`gr:p:<pack_id>:<buyer_uid>:30`)
        dummy_bot.sent_media.clear()
        grant_cb_str = app_kb.inline_keyboard[1][0].callback_data
        assert grant_cb_str == f"gr:p:{own_pack['id']}:{buyer_uid}:30"
        gr_cb_obj, gr_acks, gr_edits, _ = _make_cb(
            9501, "owner_main", "Owner Main", grant_cb_str
        )
        await start_help.cb_owner_grant_or_reject_access(
            gr_cb_obj, db, dummy_bot, bot_username="KoshBox_Bot"  # type: ignore[arg-type]
        )
        assert len(gr_edits) == 1 and "Full Access Granted (30 Days)" in gr_edits[0][0]
        assert await db.has_active_access_grant("pack", own_pack["id"], buyer_uid) is True
        # Pending trial scheduled_delete for buyer + pack was automatically cancelled!
        assert len(await _fetch_scheduled_rows()) == 0
        # Visitor immediately received DM unlock notice with `📥 Open Unlocked Content` deep-link button
        buyer_unlock_dms = [
            m for m in dummy_bot.sent_media
            if m["type"] == "msg" and m["chat_id"] == buyer_uid and "Full Access Unlocked" in m["text"]
        ]
        assert len(buyer_unlock_dms) == 1
        unlock_btn = buyer_unlock_dms[0]["reply_markup"].inline_keyboard[0][0]
        assert unlock_btn.text == "📥 Open Unlocked Content"
        assert unlock_btn.url == f"https://t.me/KoshBox_Bot?start=pack_{own_pack['share_token']}"

        # Step 5: Buyer re-opens the 1x/User pack multiple times -> ALWAYS delivered in full, ZERO auto-delete, ZERO Trial Locked card!
        for _ in range(2):
            dummy_bot.batch_copied_calls.clear()
            start_answers.clear()
            await start_help.cmd_start(
                _make_start_msg(buyer_uid, "paid_buyer", "Paid Buyer"),  # type: ignore[arg-type]
                SimpleNamespace(args=f"pack_{own_pack['share_token']}"),  # type: ignore[arg-type]
                _DummyFSM(None),  # type: ignore[arg-type]
                db,
                dummy_bot,  # type: ignore[arg-type]
                dummy_cfg,
            )
            assert len(dummy_bot.batch_copied_calls) == 1
            assert len(start_answers) == 0
            assert len(await _fetch_scheduled_rows()) == 0

        # Step 6: Grant expiry -> once `expires_at <= now`, grant becomes inactive and 1x/User lock resumes
        await db.conn.execute(
            "UPDATE link_access_grants SET expires_at = ? WHERE link_type = 'pack' AND link_id = ? AND user_id = ?",
            (int(time.time()) - 10, own_pack["id"], buyer_uid),
        )
        await db.conn.commit()
        assert await db.has_active_access_grant("pack", own_pack["id"], buyer_uid) is False
        dummy_bot.batch_copied_calls.clear()
        start_answers.clear()
        await start_help.cmd_start(
            _make_start_msg(buyer_uid, "paid_buyer", "Paid Buyer"),  # type: ignore[arg-type]
            SimpleNamespace(args=f"pack_{own_pack['share_token']}"),  # type: ignore[arg-type]
            _DummyFSM(None),  # type: ignore[arg-type]
            db,
            dummy_bot,  # type: ignore[arg-type]
            dummy_cfg,
        )
        assert len(dummy_bot.batch_copied_calls) == 0
        assert len(start_answers) == 1 and "Trial Access Ended" in start_answers[0][0]

        # Step 7: Lifetime grant (`0`) & Reject (`-1`) flow on File link (`gr:f:<fid>:<uid>:0` & `-1`)
        lf_cb_obj, _, lf_edits, _ = _make_cb(
            9502, "owner_alt", "Owner Linked Alt", f"gr:f:{own_file['id']}:{buyer_uid}:0"
        )
        await start_help.cb_owner_grant_or_reject_access(
            lf_cb_obj, db, dummy_bot, bot_username="KoshBox_Bot"  # type: ignore[arg-type]
        )
        grant_f = await db.get_active_access_grant("file", own_file["id"], buyer_uid)
        assert grant_f is not None and grant_f["expires_at"] == 0
        # Buyer bypasses PIN & 1x/User & 5m auto-delete on unlocked file!
        dummy_bot.sent_media.clear()
        await start_help.cmd_start(
            _make_start_msg(buyer_uid, "paid_buyer", "Paid Buyer"),  # type: ignore[arg-type]
            SimpleNamespace(args=f"file_{own_ftok}"),  # type: ignore[arg-type]
            _DummyFSM(None),  # type: ignore[arg-type]
            db,
            dummy_bot,  # type: ignore[arg-type]
            dummy_cfg,
        )
        assert any(m["type"] == "document" and "Lifetime Full Access" in m["caption"] for m in dummy_bot.sent_media)
        assert len(await _fetch_scheduled_rows()) == 0

        # Reject / Revoke (`-1`) removes active grant and notifies visitor
        dummy_bot.sent_media.clear()
        rej_cb_obj, _, rej_edits, _ = _make_cb(
            9501, "owner_main", "Owner Main", f"gr:f:{own_file['id']}:{buyer_uid}:-1"
        )
        await start_help.cb_owner_grant_or_reject_access(
            rej_cb_obj, db, dummy_bot, bot_username="KoshBox_Bot"  # type: ignore[arg-type]
        )
        assert await db.has_active_access_grant("file", own_file["id"], buyer_uid) is False
        assert len(rej_edits) == 1 and "Declined" in rej_edits[0][0]
        assert any(m["type"] == "msg" and m["chat_id"] == buyer_uid and "Declined" in m["text"] for m in dummy_bot.sent_media)

        # 34. Pack File Ordering Bug Regression Suite:
        #     (a) Out-of-order incoming messages `[msg_id=103, msg_id=101, msg_id=102]` -> archived & packed in strict `[101, 102, 103]`!
        #     (b) Delivery of non-ascending `dump_message_id`s `[502, 501, 505]` -> preserves exact `position` order (`502` then `501` then `505`)!
        #     (c) Natural numeric sort (`🔢 Sort 1→N`) -> orders `Part 1`, `Part 2`, `Part 10`, `Part 28`, `Part 62` accurately!
        u_ord = await db.ensure_user(9801, "audiobook_owner", "Audiobook Owner")
        ord_replies: list[tuple[str, object]] = []

        def _make_ordered_audio_msg(msg_id_val: int, part_num: int, uid: int = 9801):
            async def _ord_reply(
                txt: str,
                reply_markup=None,
                disable_web_page_preview: bool = False,
                message_effect_id: str | None = None,
            ):
                ord_replies.append((txt, reply_markup))
                return SimpleNamespace(message_id=75000 + len(ord_replies))

            caption_str = f"🎧 I Opened a Medical Center in the Abyss World [hi] — Part {part_num} of 62"
            return SimpleNamespace(
                message_id=msg_id_val,
                chat=SimpleNamespace(id=uid, type="private"),
                from_user=SimpleNamespace(
                    id=uid, username="audiobook_owner", full_name="Audiobook Owner"
                ),
                media_group_id=None,
                caption=caption_str,
                audio=SimpleNamespace(
                    file_id=f"AB_SRC_{msg_id_val}",
                    file_unique_id=f"AB_UNIQ_{msg_id_val}",
                    title=f"I Opened a Medical Center — Part {part_num}",
                    performer="ModuBot Text-to-Voice",
                    file_name=f"Abyss_Part_{part_num}.mp3",
                    mime_type="audio/mpeg",
                    file_size=40_000_000,
                    duration=1800,
                ),
                document=None,
                video=None,
                video_note=None,
                voice=None,
                photo=None,
                animation=None,
                sticker=None,
                reply=_ord_reply,
            )

        # (a1) Multi-Drop Auto-Pack: arrive out of order [103 (Part 3), 101 (Part 1), 102 (Part 2)]
        dummy_bot.forwarded_messages.clear()
        dummy_bot.batch_forwarded_calls.clear()
        ord_fsm = _DummyFSM(None)
        for mid_in, pt_in in [(103, 3), (101, 1), (102, 2)]:
            await upload.handle_media_upload(
                _make_ordered_audio_msg(mid_in, pt_in),  # type: ignore[arg-type]
                ord_fsm,  # type: ignore[arg-type]
                dummy_bot,  # type: ignore[arg-type]
                db,
                dummy_cfg,
            )

        # Before sliding window flushes, ZERO messages should be forwarded to dump channel!
        assert len(dummy_bot.forwarded_messages) == 0, (
            "Multi-drop must buffer BEFORE forwarding so concurrent handlers never race forward_message!"
        )

        await asyncio.sleep(0.35)
        assert len(ord_replies) == 1 and "Auto-Packed 3 files" in ord_replies[0][0]
        # Verify batch forward_messages received strictly sorted [101, 102, 103]
        assert len(dummy_bot.batch_forwarded_calls) == 1
        assert dummy_bot.batch_forwarded_calls[0]["message_ids"] == [101, 102, 103]
        assert [m[2] for m in dummy_bot.forwarded_messages] == [101, 102, 103]

        ord_packs = await db.list_vault_packs(u_ord["vault_id"])
        assert len(ord_packs) == 1
        ord_pack = ord_packs[0]
        ord_pack_files = await db.get_pack_files(ord_pack["id"])
        assert [f["title"] for f in ord_pack_files] == [
            "I Opened a Medical Center — Part 1",
            "I Opened a Medical Center — Part 2",
            "I Opened a Medical Center — Part 3",
        ]
        assert [f["pack_position"] for f in ord_pack_files] == [1, 2, 3]
        assert ord_pack_files[0]["dump_message_id"] < ord_pack_files[1]["dump_message_id"] < ord_pack_files[2]["dump_message_id"]

        # (a2) Interactive `/batch` Pack Builder + >60 files (62-part audiobook arriving scrambled!)
        dummy_bot.forwarded_messages.clear()
        dummy_bot.batch_forwarded_calls.clear()
        builder_62_fsm = _DummyFSM(
            PackStates.collecting_items.state,
            {
                "pack_file_ids": [],
                "pack_total_size": 0,
                "pack_recent_titles": [],
                "pack_status_msg_id": 89999,
                "append_pack_id": None,
            },
        )
        # Simulate scrambled arrival order of 62 forwarded parts (msg_id 2001..2062)
        scrambled_62 = (
            [2, 1, 28, 60, 61, 62]
            + [i for i in range(3, 60) if i != 28]
        )
        assert len(scrambled_62) == 62 and sorted(scrambled_62) == list(range(1, 63))
        for p_no in scrambled_62:
            await upload.handle_media_upload(
                _make_ordered_audio_msg(2000 + p_no, p_no),  # type: ignore[arg-type]
                builder_62_fsm,  # type: ignore[arg-type]
                dummy_bot,  # type: ignore[arg-type]
                db,
                dummy_cfg,
            )
        await asyncio.sleep(0.30)
        assert [m[2] for m in dummy_bot.forwarded_messages] == list(range(2001, 2063))
        b62_data = await builder_62_fsm.get_data()
        b62_pack = await db.create_pack(
            vault_id=u_ord["vault_id"],
            creator_user_id=9801,
            title="Abyss Medical Center (62)",
            file_ids=b62_data["pack_file_ids"],
        )
        b62_files = await db.get_pack_files(b62_pack["id"])
        assert len(b62_files) == 62
        assert [f["pack_position"] for f in b62_files] == list(range(1, 63))
        assert [f["title"] for f in b62_files] == [
            f"I Opened a Medical Center — Part {i}" for i in range(1, 63)
        ]

        # (b) Pack Delivery Order Preservation when `dump_message_id`s are NOT strictly ascending `[502, 501, 505]`:
        #     MUST deliver `502` FIRST, then `501`, then `505` (NEVER scramble via `sorted(dump_ids)` -> `[501, 502, 505]`)!
        delivery_trace: list[tuple[str, object]] = []

        class _OrderTracingBot(_DummyBot):
            async def copy_messages(
                self,
                chat_id: int,
                from_chat_id: int,
                message_ids: list[int],
                protect_content: bool = False,
            ):
                res = await super().copy_messages(
                    chat_id=chat_id,
                    from_chat_id=from_chat_id,
                    message_ids=message_ids,
                    protect_content=protect_content,
                )
                for mid in message_ids:
                    delivery_trace.append(("copy", mid))
                return res

            async def send_audio(
                self,
                chat_id: int,
                audio: str,
                caption: str = "",
                protect_content: bool = False,
                reply_markup=None,
                message_effect_id: str | None = None,
            ):
                res = await super().send_audio(
                    chat_id=chat_id,
                    audio=audio,
                    caption=caption,
                    protect_content=protect_content,
                    reply_markup=reply_markup,
                    message_effect_id=message_effect_id,
                )
                delivery_trace.append(("audio", audio))
                return res

        trace_bot = _OrderTracingBot()
        f_d502 = await db.add_file(
            vault_id=u_ord["vault_id"],
            uploader_user_id=9801,
            group_id=None,
            media_type="audio",
            file_id="FID_FOR_502",
            file_unique_id="UNIQ_D502",
            dump_message_id=502,
            title="Track Pos 1 (dump 502)",
        )
        f_d501 = await db.add_file(
            vault_id=u_ord["vault_id"],
            uploader_user_id=9801,
            group_id=None,
            media_type="audio",
            file_id="FID_FOR_501",
            file_unique_id="UNIQ_D501",
            dump_message_id=501,
            title="Track Pos 2 (dump 501)",
        )
        f_d505 = await db.add_file(
            vault_id=u_ord["vault_id"],
            uploader_user_id=9801,
            group_id=None,
            media_type="audio",
            file_id="FID_FOR_505",
            file_unique_id="UNIQ_D505",
            dump_message_id=505,
            title="Track Pos 3 (dump 505)",
        )
        non_asc_pack = await db.create_pack(
            vault_id=u_ord["vault_id"],
            creator_user_id=9801,
            title="Non-Ascending Dump IDs Pack",
            file_ids=[f_d502["id"], f_d501["id"], f_d505["id"]],
        )
        deliv_non_asc = await packs.deliver_pack_to_chat(
            trace_bot,  # type: ignore[arg-type]
            9901,
            non_asc_pack,
            db,
            is_owner=True,
            count_view=False,
            dump_channel_id=dummy_cfg.dump_channel_id,
        )
        assert deliv_non_asc == 3
        # Map each trace item back to its dump_message_id to verify exact [502, 501, 505] sequence!
        fid_to_dump = {"FID_FOR_502": 502, "FID_FOR_501": 501, "FID_FOR_505": 505}
        delivered_dump_sequence = [
            val if kind == "copy" else fid_to_dump[str(val)]
            for kind, val in delivery_trace
        ]
        assert delivered_dump_sequence == [502, 501, 505], (
            f"Expected exact position order [502, 501, 505], got {delivered_dump_sequence}"
        )
        # Specifically: singleton 502 sent via send_stored_media, ascending run [501, 505] batched via copy_messages!
        assert delivery_trace == [
            ("audio", "FID_FOR_502"),
            ("copy", 501),
            ("copy", 505),
        ]

        # (c) 1-Tap `🔢 Sort 1→N` Natural Numeric Sort in `🛠️ Edit Pack`:
        #     Verify `Part 1`, `Part 2`, `Part 9`, `Part 10`, `Part 28`, `Part 62` sort naturally (NOT `Part 1, Part 10, Part 2`)!
        raw_labels = [
            "🎧 Abyss World — Part 28 of 62",
            "🎧 Abyss World — Part 2 of 62",
            "🎧 Abyss World — Part 10 of 62",
            "🎧 Abyss World — Part 1 of 62",
            "🎧 Abyss World — Part 62 of 62",
            "🎧 Abyss World — Part 9 of 62",
        ]
        assert sorted(raw_labels, key=natural_sort_key) == [
            "🎧 Abyss World — Part 1 of 62",
            "🎧 Abyss World — Part 2 of 62",
            "🎧 Abyss World — Part 9 of 62",
            "🎧 Abyss World — Part 10 of 62",
            "🎧 Abyss World — Part 28 of 62",
            "🎧 Abyss World — Part 62 of 62",
        ]

        messy_fids: list[int] = []
        for idx_m, lbl in enumerate(raw_labels, start=1):
            mf = await db.add_file(
                vault_id=u_ord["vault_id"],
                uploader_user_id=9801,
                group_id=None,
                media_type="audio",
                file_id=f"MESSY_FID_{idx_m}",
                file_unique_id=f"MESSY_UNIQ_{idx_m}",
                dump_message_id=9500 + idx_m,
                title=lbl,
                caption=lbl,
            )
            messy_fids.append(mf["id"])

        messy_pack = await db.create_pack(
            vault_id=u_ord["vault_id"],
            creator_user_id=9801,
            title="Scrambled Audiobook Pack",
            file_ids=messy_fids,
        )

        # Verify `🔢 Sort 1→N` button appears in `🛠️ Edit Pack` and obeys <=2 btns/row & <=64B callback_data
        messy_drawer = await packs.render_pack_edit_drawer(9801, messy_pack["id"], db, page=0)
        assert messy_drawer is not None
        sort_btns = [
            b
            for r in messy_drawer[1].inline_keyboard
            for b in r
            if b.callback_data == f"pk:sort:{messy_pack['id']}"
        ]
        assert len(sort_btns) == 1 and sort_btns[0].text == "🔢 Sort 1→N"
        for r in messy_drawer[1].inline_keyboard:
            is_pgr = len(r) == 3 and r[1].callback_data == "noop"
            assert is_pgr or len(r) <= 2
            for b in r:
                if b.callback_data:
                    assert len(b.callback_data.encode("utf-8")) <= 64

        # Invoke the actual `cb_pack_natural_sort` callback handler (simulating owner tapping `🔢 Sort 1→N`)
        sort_cb_obj, sort_acks, sort_edits, _ = _make_cb(
            9801, "audiobook_owner", "Audiobook Owner", f"pk:sort:{messy_pack['id']}"
        )
        await packs.cb_pack_natural_sort(sort_cb_obj, db)  # type: ignore[arg-type]
        assert sort_acks and "Sorted 6 files" in (sort_acks[0][0] or "")
        assert len(sort_edits) == 1

        fixed_files = await db.get_pack_files(messy_pack["id"])
        assert [f["caption"] for f in fixed_files] == [
            "🎧 Abyss World — Part 1 of 62",
            "🎧 Abyss World — Part 2 of 62",
            "🎧 Abyss World — Part 9 of 62",
            "🎧 Abyss World — Part 10 of 62",
            "🎧 Abyss World — Part 28 of 62",
            "🎧 Abyss World — Part 62 of 62",
        ]
        assert [f["pack_position"] for f in fixed_files] == [1, 2, 3, 4, 5, 6]

        # Also verify delivering the newly sorted pack sends Parts 1, 2, 9, 10, 28, 62 in exact sorted order!
        delivery_trace.clear()
        await packs.deliver_pack_to_chat(
            trace_bot,  # type: ignore[arg-type]
            9902,
            messy_pack,
            db,
            is_owner=True,
            count_view=False,
            dump_channel_id=dummy_cfg.dump_channel_id,
        )
        messy_fid_to_lbl = {
            f"MESSY_FID_{idx_m}": lbl for idx_m, lbl in enumerate(raw_labels, start=1)
        }
        messy_dump_to_lbl = {
            9500 + idx_m: lbl for idx_m, lbl in enumerate(raw_labels, start=1)
        }
        delivered_labels = [
            messy_dump_to_lbl[int(val)] if kind == "copy" else messy_fid_to_lbl[str(val)]
            for kind, val in delivery_trace
        ]
        assert delivered_labels == [
            "🎧 Abyss World — Part 1 of 62",
            "🎧 Abyss World — Part 2 of 62",
            "🎧 Abyss World — Part 9 of 62",
            "🎧 Abyss World — Part 10 of 62",
            "🎧 Abyss World — Part 28 of 62",
            "🎧 Abyss World — Part 62 of 62",
        ]

        await db.close()
        print("ALL PRO FEATURES, TRIAL LOCKS, BATCH APIS, BOT API 9.4 UX & ZERO-LEAK TESTS PASSED!")


if __name__ == "__main__":
    asyncio.run(run_tests())
