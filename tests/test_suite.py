import asyncio
import os
import sys
import tempfile
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from database import Database, hash_pin, verify_pin
from handlers import accounts, admin, file_actions, groups, inline, packs, start_help
from utils.banners import (
    BANNER_FILES,
    compute_file_sha256,
    edit_or_send_banner,
    get_or_upload_banner_file_id,
    send_banner_message,
    warmup_banners_in_dump_channel,
)
from utils.formatters import (
    breadcrumb,
    build_file_card_text,
    build_file_keyboard,
    build_file_link_keyboard,
    build_file_manage_keyboard,
    build_upload_quick_keyboard,
    copy_btn,
    format_duration,
    format_expiry_badge,
    format_size,
    format_ttl_short,
    next_sort_key,
    progress_bar,
    send_stored_media,
    share_url_btn,
    toggle_label,
)
from utils.middleware import GuardAndThrottleMiddleware


class _DummyBot:
    def __init__(self) -> None:
        self.dump_uploads: list[tuple[int, str]] = []
        self.sent_media: list[dict] = []
        self.deleted_messages: list[tuple[int, int]] = []
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
        self, chat_id: int, audio: str, caption: str = "", protect_content: bool = False
    ):
        self._seq += 1
        self.sent_media.append(
            {
                "type": "audio",
                "chat_id": chat_id,
                "file_id": audio,
                "caption": caption,
                "protect": protect_content,
            }
        )
        return SimpleNamespace(message_id=8000 + self._seq)

    async def send_document(
        self,
        chat_id: int,
        document: str,
        caption: str = "",
        protect_content: bool = False,
    ):
        self._seq += 1
        self.sent_media.append(
            {
                "type": "document",
                "chat_id": chat_id,
                "file_id": document,
                "caption": caption,
                "protect": protect_content,
            }
        )
        return SimpleNamespace(message_id=8000 + self._seq)

    async def send_message(
        self, chat_id: int, text: str, protect_content: bool = False, reply_markup=None
    ):
        self._seq += 1
        self.sent_media.append(
            {"type": "msg", "chat_id": chat_id, "text": text, "protect": protect_content}
        )
        return SimpleNamespace(message_id=9000 + self._seq)

    async def delete_message(self, chat_id: int, message_id: int):
        self.deleted_messages.append((chat_id, message_id))


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
        assert "Settings & Security" in set_txt
        assert len(set_kb.inline_keyboard) == 5

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

        await db.close()
        print("ALL PRO FEATURES, SECURITY, PACKS & CRISP UI/UX TESTS PASSED!")


if __name__ == "__main__":
    asyncio.run(run_tests())
