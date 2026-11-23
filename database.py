import hashlib
import hmac
import os
import secrets
import string
import time
from typing import Any, Optional

import aiosqlite


def _gen_token(length: int = 8) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def hash_pin(pin: str) -> str:
    salt = secrets.token_hex(8)
    digest = hashlib.sha256(f"{salt}:{pin.strip()}".encode("utf-8")).hexdigest()
    return f"{salt}${digest}"


def verify_pin(pin: str, stored_hash: str) -> bool:
    if not stored_hash or "$" not in stored_hash:
        return False
    salt, expected = stored_hash.split("$", 1)
    actual = hashlib.sha256(f"{salt}:{pin.strip()}".encode("utf-8")).hexdigest()
    return hmac.compare_digest(actual, expected)


AUTO_DELETE_CYCLE = [0, 300, 900, 3600, 86400]
SHARE_TTL_CYCLE = [0, 3600, 86400, 604800]
MAX_VIEWS_CYCLE = [0, 1, 5, 10, 50, 100]

SORT_CLAUSES = {
    "new": "f.is_favorite DESC, f.created_at DESC",
    "old": "f.created_at ASC",
    "top": "f.use_count DESC, f.is_favorite DESC, f.created_at DESC",
    "az": "f.title COLLATE NOCASE ASC",
    "size": "f.file_size DESC, f.created_at DESC",
}


class Database:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL;")
        await self.conn.execute("PRAGMA synchronous=NORMAL;")
        await self.conn.execute("PRAGMA foreign_keys=ON;")
        await self._init_schema()
        await self._run_migrations()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()
            self.conn = None

    async def _init_schema(self) -> None:
        assert self.conn is not None
        await self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS vaults (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT DEFAULT '',
                full_name TEXT DEFAULT '',
                vault_id INTEGER NOT NULL,
                active_group_id INTEGER DEFAULT NULL,
                bulk_mode INTEGER DEFAULT 0,
                default_sort TEXT DEFAULT 'new',
                auto_delete_ttl INTEGER DEFAULT 0,
                protect_content INTEGER DEFAULT 0,
                is_banned INTEGER DEFAULT 0,
                created_at INTEGER NOT NULL,
                last_seen INTEGER NOT NULL,
                FOREIGN KEY (vault_id) REFERENCES vaults(id)
            );

            CREATE TABLE IF NOT EXISTS groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                owner_vault_id INTEGER NOT NULL,
                is_shared INTEGER DEFAULT 0,
                invite_code TEXT UNIQUE,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (owner_vault_id) REFERENCES vaults(id)
            );

            CREATE TABLE IF NOT EXISTS group_members (
                group_id INTEGER NOT NULL,
                vault_id INTEGER NOT NULL,
                role TEXT NOT NULL DEFAULT 'member',
                joined_at INTEGER NOT NULL,
                PRIMARY KEY (group_id, vault_id),
                FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE,
                FOREIGN KEY (vault_id) REFERENCES vaults(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vault_id INTEGER NOT NULL,
                uploader_user_id INTEGER NOT NULL,
                group_id INTEGER DEFAULT NULL,
                media_type TEXT NOT NULL,
                file_id TEXT NOT NULL,
                file_unique_id TEXT NOT NULL,
                dump_message_id INTEGER NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                performer TEXT DEFAULT '',
                file_name TEXT DEFAULT '',
                mime_type TEXT DEFAULT '',
                file_size INTEGER DEFAULT 0,
                duration INTEGER DEFAULT 0,
                caption TEXT DEFAULT '',
                tags TEXT DEFAULT '',
                is_favorite INTEGER DEFAULT 0,
                is_trashed INTEGER DEFAULT 0,
                trashed_at INTEGER DEFAULT NULL,
                share_token TEXT UNIQUE,
                share_pin_hash TEXT DEFAULT '',
                share_expires_at INTEGER DEFAULT NULL,
                protect_share INTEGER DEFAULT 0,
                share_max_views INTEGER DEFAULT 0,
                share_views INTEGER DEFAULT 0,
                share_paused INTEGER DEFAULT 0,
                use_count INTEGER DEFAULT 0,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (vault_id) REFERENCES vaults(id) ON DELETE CASCADE,
                FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_files_vault_created
                ON files(vault_id, is_trashed, is_favorite DESC, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_files_group
                ON files(group_id, is_trashed, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_files_unique_vault
                ON files(vault_id, file_unique_id);
            CREATE INDEX IF NOT EXISTS idx_files_share_token
                ON files(share_token);

            CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
                file_id UNINDEXED,
                search_text
            );

            CREATE TABLE IF NOT EXISTS link_requests (
                code TEXT PRIMARY KEY,
                owner_user_id INTEGER NOT NULL,
                owner_vault_id INTEGER NOT NULL,
                requester_user_id INTEGER DEFAULT NULL,
                requester_name TEXT DEFAULT '',
                requester_username TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS packs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vault_id INTEGER NOT NULL,
                creator_user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                share_token TEXT UNIQUE NOT NULL,
                pin_hash TEXT DEFAULT '',
                protect_content INTEGER DEFAULT 0,
                expires_at INTEGER DEFAULT NULL,
                max_views INTEGER DEFAULT 0,
                is_paused INTEGER DEFAULT 0,
                views INTEGER DEFAULT 0,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (vault_id) REFERENCES vaults(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS pack_items (
                pack_id INTEGER NOT NULL,
                file_id INTEGER NOT NULL,
                pos INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (pack_id, file_id),
                FOREIGN KEY (pack_id) REFERENCES packs(id) ON DELETE CASCADE,
                FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_packs_vault
                ON packs(vault_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_packs_token
                ON packs(share_token);

            CREATE TABLE IF NOT EXISTS ui_assets (
                asset_key TEXT PRIMARY KEY,
                file_id TEXT NOT NULL,
                file_unique_id TEXT DEFAULT '',
                dump_message_id INTEGER NOT NULL,
                sha256 TEXT NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL
            );
            """
        )
        await self.conn.commit()

    async def _run_migrations(self) -> None:
        """Non-destructive column upgrades for existing SQLite databases."""
        assert self.conn is not None

        async def _table_cols(table: str) -> set[str]:
            cur = await self.conn.execute(f"PRAGMA table_info({table});")
            return {row["name"] for row in await cur.fetchall()}

        user_cols = await _table_cols("users")
        if "default_sort" not in user_cols:
            await self.conn.execute(
                "ALTER TABLE users ADD COLUMN default_sort TEXT DEFAULT 'new';"
            )
        if "auto_delete_ttl" not in user_cols:
            await self.conn.execute(
                "ALTER TABLE users ADD COLUMN auto_delete_ttl INTEGER DEFAULT 0;"
            )
        if "protect_content" not in user_cols:
            await self.conn.execute(
                "ALTER TABLE users ADD COLUMN protect_content INTEGER DEFAULT 0;"
            )
        if "is_banned" not in user_cols:
            await self.conn.execute(
                "ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0;"
            )

        file_cols = await _table_cols("files")
        if "is_trashed" not in file_cols:
            await self.conn.execute(
                "ALTER TABLE files ADD COLUMN is_trashed INTEGER DEFAULT 0;"
            )
        if "trashed_at" not in file_cols:
            await self.conn.execute(
                "ALTER TABLE files ADD COLUMN trashed_at INTEGER DEFAULT NULL;"
            )
        if "share_pin_hash" not in file_cols:
            await self.conn.execute(
                "ALTER TABLE files ADD COLUMN share_pin_hash TEXT DEFAULT '';"
            )
        if "share_expires_at" not in file_cols:
            await self.conn.execute(
                "ALTER TABLE files ADD COLUMN share_expires_at INTEGER DEFAULT NULL;"
            )
        if "protect_share" not in file_cols:
            await self.conn.execute(
                "ALTER TABLE files ADD COLUMN protect_share INTEGER DEFAULT 0;"
            )
        if "share_max_views" not in file_cols:
            await self.conn.execute(
                "ALTER TABLE files ADD COLUMN share_max_views INTEGER DEFAULT 0;"
            )
        if "share_views" not in file_cols:
            await self.conn.execute(
                "ALTER TABLE files ADD COLUMN share_views INTEGER DEFAULT 0;"
            )
        if "share_paused" not in file_cols:
            await self.conn.execute(
                "ALTER TABLE files ADD COLUMN share_paused INTEGER DEFAULT 0;"
            )

        pack_cols = await _table_cols("packs")
        if "description" not in pack_cols:
            await self.conn.execute(
                "ALTER TABLE packs ADD COLUMN description TEXT DEFAULT '';"
            )
        if "max_views" not in pack_cols:
            await self.conn.execute(
                "ALTER TABLE packs ADD COLUMN max_views INTEGER DEFAULT 0;"
            )
        if "is_paused" not in pack_cols:
            await self.conn.execute(
                "ALTER TABLE packs ADD COLUMN is_paused INTEGER DEFAULT 0;"
            )

        await self.conn.commit()

    # -------------------- USERS & VAULTS --------------------

    async def ensure_user(
        self, user_id: int, username: str = "", full_name: str = ""
    ) -> dict[str, Any]:
        assert self.conn is not None
        now = int(time.time())
        cur = await self.conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cur.fetchone()
        if row:
            await self.conn.execute(
                "UPDATE users SET username = ?, full_name = ?, last_seen = ? WHERE user_id = ?",
                (username or "", full_name or "", now, user_id),
            )
            await self.conn.commit()
            user = dict(row)
            user["username"] = username or ""
            user["full_name"] = full_name or ""
            return user

        v_cur = await self.conn.execute(
            "INSERT INTO vaults (created_at) VALUES (?)", (now,)
        )
        vault_id = v_cur.lastrowid
        await self.conn.execute(
            """
            INSERT INTO users (user_id, username, full_name, vault_id, created_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, username or "", full_name or "", vault_id, now, now),
        )
        await self.conn.commit()
        cur = await self.conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        )
        return dict(await cur.fetchone())

    async def get_user(self, user_id: int) -> Optional[dict[str, Any]]:
        assert self.conn is not None
        cur = await self.conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def set_active_group(self, user_id: int, group_id: Optional[int]) -> None:
        assert self.conn is not None
        await self.conn.execute(
            "UPDATE users SET active_group_id = ? WHERE user_id = ?",
            (group_id, user_id),
        )
        await self.conn.commit()

    async def toggle_bulk_mode(self, user_id: int) -> bool:
        assert self.conn is not None
        user = await self.get_user(user_id)
        new_val = 0 if (user and user["bulk_mode"]) else 1
        await self.conn.execute(
            "UPDATE users SET bulk_mode = ? WHERE user_id = ?", (new_val, user_id)
        )
        await self.conn.commit()
        return bool(new_val)

    async def toggle_protect_content(self, user_id: int) -> bool:
        assert self.conn is not None
        user = await self.get_user(user_id)
        new_val = 0 if (user and user.get("protect_content")) else 1
        await self.conn.execute(
            "UPDATE users SET protect_content = ? WHERE user_id = ?", (new_val, user_id)
        )
        await self.conn.commit()
        return bool(new_val)

    async def cycle_auto_delete_ttl(self, user_id: int) -> int:
        assert self.conn is not None
        user = await self.get_user(user_id)
        cur_val = int((user or {}).get("auto_delete_ttl") or 0)
        if cur_val in AUTO_DELETE_CYCLE:
            nxt = AUTO_DELETE_CYCLE[(AUTO_DELETE_CYCLE.index(cur_val) + 1) % len(AUTO_DELETE_CYCLE)]
        else:
            nxt = 0
        await self.conn.execute(
            "UPDATE users SET auto_delete_ttl = ? WHERE user_id = ?", (nxt, user_id)
        )
        await self.conn.commit()
        return nxt

    async def set_auto_delete_ttl(self, user_id: int, ttl_seconds: int) -> int:
        assert self.conn is not None
        val = max(0, int(ttl_seconds))
        await self.conn.execute(
            "UPDATE users SET auto_delete_ttl = ? WHERE user_id = ?", (val, user_id)
        )
        await self.conn.commit()
        return val

    async def set_user_banned(self, user_id: int, banned: bool) -> bool:
        assert self.conn is not None
        cur = await self.conn.execute(
            "UPDATE users SET is_banned = ? WHERE user_id = ?",
            (1 if banned else 0, user_id),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def is_user_banned(self, user_id: int) -> bool:
        assert self.conn is not None
        cur = await self.conn.execute(
            "SELECT is_banned FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cur.fetchone()
        return bool(row and row["is_banned"])

    async def set_default_sort(self, user_id: int, sort_key: str) -> str:
        assert self.conn is not None
        key = sort_key if sort_key in SORT_CLAUSES else "new"
        await self.conn.execute(
            "UPDATE users SET default_sort = ? WHERE user_id = ?", (key, user_id)
        )
        await self.conn.commit()
        return key

    async def get_linked_accounts(self, vault_id: int) -> list[dict[str, Any]]:
        assert self.conn is not None
        cur = await self.conn.execute(
            "SELECT * FROM users WHERE vault_id = ? ORDER BY created_at ASC",
            (vault_id,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # -------------------- ACCOUNT LINKING & VERIFICATION --------------------

    async def create_link_request(
        self, owner_user_id: int, owner_vault_id: int, ttl_seconds: int
    ) -> dict[str, Any]:
        assert self.conn is not None
        now = int(time.time())
        expires_at = now + ttl_seconds
        code = _gen_token(6)
        await self.conn.execute(
            "DELETE FROM link_requests WHERE expires_at < ? OR (owner_user_id = ? AND status = 'pending')",
            (now, owner_user_id),
        )
        await self.conn.execute(
            """
            INSERT INTO link_requests (code, owner_user_id, owner_vault_id, status, created_at, expires_at)
            VALUES (?, ?, ?, 'pending', ?, ?)
            """,
            (code, owner_user_id, owner_vault_id, now, expires_at),
        )
        await self.conn.commit()
        return {"code": code, "expires_at": expires_at}

    async def get_link_request(self, code: str) -> Optional[dict[str, Any]]:
        assert self.conn is not None
        cur = await self.conn.execute(
            "SELECT * FROM link_requests WHERE code = ?", (code.upper(),)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def attach_requester_to_link(
        self,
        code: str,
        requester_user_id: int,
        requester_name: str,
        requester_username: str,
    ) -> bool:
        assert self.conn is not None
        now = int(time.time())
        cur = await self.conn.execute(
            """
            UPDATE link_requests
            SET requester_user_id = ?, requester_name = ?, requester_username = ?, status = 'awaiting_approval'
            WHERE code = ? AND status = 'pending' AND expires_at >= ?
            """,
            (
                requester_user_id,
                requester_name,
                requester_username,
                code.upper(),
                now,
            ),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def approve_link_request(
        self, code: str, approver_user_id: int
    ) -> Optional[dict[str, Any]]:
        assert self.conn is not None
        now = int(time.time())
        req = await self.get_link_request(code)
        if not req:
            return None
        if req["owner_user_id"] != approver_user_id:
            return None
        if req["status"] != "awaiting_approval" or req["expires_at"] < now:
            return None

        requester_id = req["requester_user_id"]
        target_vault_id = req["owner_vault_id"]
        requester = await self.get_user(requester_id)
        if not requester:
            return None

        old_vault_id = requester["vault_id"]
        if old_vault_id != target_vault_id:
            await self.conn.execute(
                """
                UPDATE files SET vault_id = ?
                WHERE vault_id = ?
                  AND file_unique_id NOT IN (
                      SELECT file_unique_id FROM files WHERE vault_id = ?
                  )
                """,
                (target_vault_id, old_vault_id, target_vault_id),
            )
            await self.conn.execute(
                "UPDATE groups SET owner_vault_id = ? WHERE owner_vault_id = ?",
                (target_vault_id, old_vault_id),
            )
            await self.conn.execute(
                "UPDATE packs SET vault_id = ? WHERE vault_id = ?",
                (target_vault_id, old_vault_id),
            )
            await self.conn.execute(
                """
                INSERT OR IGNORE INTO group_members (group_id, vault_id, role, joined_at)
                SELECT group_id, ?, role, joined_at FROM group_members WHERE vault_id = ?
                """,
                (target_vault_id, old_vault_id),
            )
            await self.conn.execute(
                "DELETE FROM group_members WHERE vault_id = ?", (old_vault_id,)
            )
            await self.conn.execute(
                "UPDATE users SET vault_id = ? WHERE vault_id = ?",
                (target_vault_id, old_vault_id),
            )

        await self.conn.execute(
            "UPDATE link_requests SET status = 'approved' WHERE code = ?",
            (code.upper(),),
        )
        await self.conn.commit()
        return req

    async def reject_link_request(
        self, code: str, approver_user_id: int
    ) -> Optional[dict[str, Any]]:
        assert self.conn is not None
        req = await self.get_link_request(code)
        if not req or req["owner_user_id"] != approver_user_id:
            return None
        await self.conn.execute(
            "UPDATE link_requests SET status = 'rejected' WHERE code = ?",
            (code.upper(),),
        )
        await self.conn.commit()
        return req

    async def unlink_account(self, target_user_id: int, vault_id: int) -> bool:
        assert self.conn is not None
        linked = await self.get_linked_accounts(vault_id)
        if len(linked) <= 1:
            return False
        if not any(u["user_id"] == target_user_id for u in linked):
            return False
        now = int(time.time())
        v_cur = await self.conn.execute(
            "INSERT INTO vaults (created_at) VALUES (?)", (now,)
        )
        new_vault_id = v_cur.lastrowid
        await self.conn.execute(
            "UPDATE users SET vault_id = ?, active_group_id = NULL WHERE user_id = ?",
            (new_vault_id, target_user_id),
        )
        await self.conn.commit()
        return True

    # -------------------- GROUPS & FOLDERS --------------------

    async def create_group(
        self, vault_id: int, name: str, is_shared: bool = False
    ) -> dict[str, Any]:
        assert self.conn is not None
        now = int(time.time())
        invite_code = _gen_token(8) if is_shared else None
        cur = await self.conn.execute(
            """
            INSERT INTO groups (name, owner_vault_id, is_shared, invite_code, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (name.strip(), vault_id, 1 if is_shared else 0, invite_code, now),
        )
        group_id = cur.lastrowid
        await self.conn.execute(
            """
            INSERT INTO group_members (group_id, vault_id, role, joined_at)
            VALUES (?, ?, 'owner', ?)
            """,
            (group_id, vault_id, now),
        )
        await self.conn.commit()
        return await self.get_group(group_id)

    async def get_group(self, group_id: int) -> Optional[dict[str, Any]]:
        assert self.conn is not None
        cur = await self.conn.execute(
            """
            SELECT g.*,
                   (SELECT COUNT(*) FROM files f WHERE f.group_id = g.id AND COALESCE(f.is_trashed, 0) = 0) AS file_count,
                   (SELECT COUNT(*) FROM group_members m WHERE m.group_id = g.id) AS member_count
            FROM groups g
            WHERE g.id = ?
            """,
            (group_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def rename_group(self, group_id: int, new_name: str) -> None:
        assert self.conn is not None
        await self.conn.execute(
            "UPDATE groups SET name = ? WHERE id = ?", (new_name.strip(), group_id)
        )
        cur = await self.conn.execute(
            "SELECT id FROM files WHERE group_id = ?", (group_id,)
        )
        file_ids = [r["id"] for r in await cur.fetchall()]
        for fid in file_ids:
            await self._upsert_fts(fid)
        await self.conn.commit()

    async def regenerate_group_invite(self, group_id: int) -> str:
        assert self.conn is not None
        new_code = _gen_token(8)
        await self.conn.execute(
            "UPDATE groups SET invite_code = ? WHERE id = ? AND is_shared = 1",
            (new_code, group_id),
        )
        await self.conn.commit()
        return new_code

    async def get_group_by_invite(self, invite_code: str) -> Optional[dict[str, Any]]:
        assert self.conn is not None
        cur = await self.conn.execute(
            "SELECT * FROM groups WHERE invite_code = ? AND is_shared = 1",
            (invite_code.upper(),),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def list_vault_groups(self, vault_id: int) -> list[dict[str, Any]]:
        assert self.conn is not None
        cur = await self.conn.execute(
            """
            SELECT g.*, gm.role,
                   (SELECT COUNT(*) FROM files f WHERE f.group_id = g.id AND COALESCE(f.is_trashed, 0) = 0) AS file_count,
                   (SELECT COUNT(*) FROM group_members m WHERE m.group_id = g.id) AS member_count
            FROM groups g
            JOIN group_members gm ON gm.group_id = g.id
            WHERE gm.vault_id = ?
            ORDER BY g.is_shared ASC, g.name COLLATE NOCASE ASC
            """,
            (vault_id,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def is_vault_in_group(self, vault_id: int, group_id: int) -> bool:
        assert self.conn is not None
        cur = await self.conn.execute(
            "SELECT 1 FROM group_members WHERE group_id = ? AND vault_id = ?",
            (group_id, vault_id),
        )
        return (await cur.fetchone()) is not None

    async def join_shared_group(
        self, vault_id: int, invite_code: str
    ) -> Optional[dict[str, Any]]:
        assert self.conn is not None
        group = await self.get_group_by_invite(invite_code)
        if not group:
            return None
        now = int(time.time())
        await self.conn.execute(
            """
            INSERT OR IGNORE INTO group_members (group_id, vault_id, role, joined_at)
            VALUES (?, ?, 'member', ?)
            """,
            (group["id"], vault_id, now),
        )
        await self.conn.commit()
        return await self.get_group(group["id"])

    async def leave_or_delete_group(self, vault_id: int, group_id: int) -> str:
        """Deletes the group if owner, otherwise leaves it."""
        assert self.conn is not None
        group = await self.get_group(group_id)
        if not group:
            return "not_found"
        if group["owner_vault_id"] == vault_id:
            await self.conn.execute(
                "UPDATE users SET active_group_id = NULL WHERE active_group_id = ?",
                (group_id,),
            )
            await self.conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))
            await self.conn.commit()
            return "deleted"
        cur = await self.conn.execute(
            "DELETE FROM group_members WHERE group_id = ? AND vault_id = ?",
            (group_id, vault_id),
        )
        await self.conn.execute(
            "UPDATE users SET active_group_id = NULL WHERE vault_id = ? AND active_group_id = ?",
            (vault_id, group_id),
        )
        await self.conn.commit()
        return "left" if cur.rowcount > 0 else "not_member"

    # -------------------- FILES & FTS5 INDEXING --------------------

    async def _upsert_fts(self, file_id: int) -> None:
        assert self.conn is not None
        cur = await self.conn.execute(
            """
            SELECT f.id, f.title, f.performer, f.file_name, f.caption, f.tags, f.media_type,
                   COALESCE(f.is_trashed, 0) AS is_trashed,
                   COALESCE(g.name, '') AS group_name
            FROM files f
            LEFT JOIN groups g ON g.id = f.group_id
            WHERE f.id = ?
            """,
            (file_id,),
        )
        row = await cur.fetchone()
        await self.conn.execute("DELETE FROM files_fts WHERE file_id = ?", (file_id,))
        if row and not row["is_trashed"]:
            blob = " ".join(
                str(row[k] or "")
                for k in (
                    "title",
                    "performer",
                    "file_name",
                    "caption",
                    "tags",
                    "media_type",
                    "group_name",
                )
            )
            await self.conn.execute(
                "INSERT INTO files_fts (file_id, search_text) VALUES (?, ?)",
                (file_id, blob),
            )

    async def find_duplicate(
        self, vault_id: int, file_unique_id: str
    ) -> Optional[dict[str, Any]]:
        assert self.conn is not None
        cur = await self.conn.execute(
            "SELECT * FROM files WHERE vault_id = ? AND file_unique_id = ? LIMIT 1",
            (vault_id, file_unique_id),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def add_file(
        self,
        *,
        vault_id: int,
        uploader_user_id: int,
        group_id: Optional[int],
        media_type: str,
        file_id: str,
        file_unique_id: str,
        dump_message_id: int,
        title: str,
        performer: str = "",
        file_name: str = "",
        mime_type: str = "",
        file_size: int = 0,
        duration: int = 0,
        caption: str = "",
        tags: str = "",
    ) -> dict[str, Any]:
        assert self.conn is not None
        now = int(time.time())
        cur = await self.conn.execute(
            """
            INSERT INTO files (
                vault_id, uploader_user_id, group_id, media_type, file_id,
                file_unique_id, dump_message_id, title, performer, file_name,
                mime_type, file_size, duration, caption, tags, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                vault_id,
                uploader_user_id,
                group_id,
                media_type,
                file_id,
                file_unique_id,
                dump_message_id,
                title,
                performer,
                file_name,
                mime_type,
                file_size,
                duration,
                caption,
                tags,
                now,
            ),
        )
        new_id = cur.lastrowid
        await self._upsert_fts(new_id)
        await self.conn.commit()
        return await self.get_file(new_id)

    async def get_file(self, db_file_id: int) -> Optional[dict[str, Any]]:
        assert self.conn is not None
        cur = await self.conn.execute(
            """
            SELECT f.*, COALESCE(g.name, '') AS group_name
            FROM files f
            LEFT JOIN groups g ON g.id = f.group_id
            WHERE f.id = ?
            """,
            (db_file_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_file_by_share_token(
        self, token: str, *, include_unavailable: bool = False
    ) -> Optional[dict[str, Any]]:
        assert self.conn is not None
        if not token or not token.strip():
            return None
        now = int(time.time())
        cur = await self.conn.execute(
            """
            SELECT f.*, COALESCE(g.name, '') AS group_name
            FROM files f
            LEFT JOIN groups g ON g.id = f.group_id
            WHERE f.share_token = ?
              AND COALESCE(f.is_trashed, 0) = 0
            """,
            (token.strip(),),
        )
        row = await cur.fetchone()
        if not row:
            return None
        data = dict(row)
        if include_unavailable:
            return data
        if data.get("share_paused"):
            return None
        exp = data.get("share_expires_at")
        if exp and int(exp) > 0 and int(exp) < now:
            return None
        max_v = int(data.get("share_max_views") or 0)
        cur_v = int(data.get("share_views") or 0)
        if max_v > 0 and cur_v >= max_v:
            return None
        return data

    async def can_vault_access_file(
        self, vault_id: int, file_row: dict[str, Any]
    ) -> bool:
        if file_row["vault_id"] == vault_id:
            return True
        if file_row["group_id"]:
            return await self.is_vault_in_group(vault_id, file_row["group_id"])
        return False

    async def update_file_title(self, db_file_id: int, new_title: str) -> None:
        assert self.conn is not None
        await self.conn.execute(
            "UPDATE files SET title = ? WHERE id = ?", (new_title.strip(), db_file_id)
        )
        await self._upsert_fts(db_file_id)
        await self.conn.commit()

    async def update_file_caption(self, db_file_id: int, caption: str) -> None:
        assert self.conn is not None
        await self.conn.execute(
            "UPDATE files SET caption = ? WHERE id = ?", (caption.strip(), db_file_id)
        )
        await self._upsert_fts(db_file_id)
        await self.conn.commit()

    async def update_file_tags(self, db_file_id: int, tags: str) -> None:
        assert self.conn is not None
        await self.conn.execute(
            "UPDATE files SET tags = ? WHERE id = ?", (tags.strip(), db_file_id)
        )
        await self._upsert_fts(db_file_id)
        await self.conn.commit()

    async def move_file_to_group(
        self, db_file_id: int, group_id: Optional[int]
    ) -> None:
        assert self.conn is not None
        await self.conn.execute(
            "UPDATE files SET group_id = ? WHERE id = ?", (group_id, db_file_id)
        )
        await self._upsert_fts(db_file_id)
        await self.conn.commit()

    async def toggle_favorite(self, db_file_id: int) -> bool:
        assert self.conn is not None
        f = await self.get_file(db_file_id)
        new_val = 0 if (f and f["is_favorite"]) else 1
        await self.conn.execute(
            "UPDATE files SET is_favorite = ? WHERE id = ?", (new_val, db_file_id)
        )
        await self.conn.commit()
        return bool(new_val)

    async def toggle_share_token(self, db_file_id: int) -> Optional[str]:
        assert self.conn is not None
        f = await self.get_file(db_file_id)
        if not f:
            return None
        if f["share_token"]:
            await self.conn.execute(
                """
                UPDATE files
                SET share_token = NULL, share_pin_hash = '', share_expires_at = NULL,
                    protect_share = 0, share_max_views = 0, share_views = 0, share_paused = 0
                WHERE id = ?
                """,
                (db_file_id,),
            )
            await self.conn.commit()
            return None
        token = _gen_token(10)
        await self.conn.execute(
            "UPDATE files SET share_token = ?, share_views = 0, share_paused = 0 WHERE id = ?",
            (token, db_file_id),
        )
        await self.conn.commit()
        return token

    async def regenerate_file_share_token(self, db_file_id: int) -> str:
        assert self.conn is not None
        token = _gen_token(10)
        await self.conn.execute(
            "UPDATE files SET share_token = ?, share_views = 0 WHERE id = ?",
            (token, db_file_id),
        )
        await self.conn.commit()
        return token

    async def toggle_file_share_paused(self, db_file_id: int) -> bool:
        assert self.conn is not None
        f = await self.get_file(db_file_id)
        new_val = 0 if (f and f.get("share_paused")) else 1
        await self.conn.execute(
            "UPDATE files SET share_paused = ? WHERE id = ?", (new_val, db_file_id)
        )
        await self.conn.commit()
        return bool(new_val)

    async def cycle_file_share_max_views(self, db_file_id: int) -> int:
        assert self.conn is not None
        f = await self.get_file(db_file_id)
        cur_val = int((f or {}).get("share_max_views") or 0)
        if cur_val in MAX_VIEWS_CYCLE:
            nxt = MAX_VIEWS_CYCLE[(MAX_VIEWS_CYCLE.index(cur_val) + 1) % len(MAX_VIEWS_CYCLE)]
        else:
            nxt = 0
        await self.conn.execute(
            "UPDATE files SET share_max_views = ? WHERE id = ?", (nxt, db_file_id)
        )
        await self.conn.commit()
        return nxt

    async def increment_file_share_views(self, db_file_id: int) -> None:
        assert self.conn is not None
        await self.conn.execute(
            "UPDATE files SET share_views = COALESCE(share_views, 0) + 1, use_count = use_count + 1 WHERE id = ?",
            (db_file_id,),
        )
        await self.conn.commit()

    async def set_file_share_pin(self, db_file_id: int, pin: Optional[str]) -> bool:
        assert self.conn is not None
        hashed = hash_pin(pin) if (pin and pin.strip()) else ""
        await self.conn.execute(
            "UPDATE files SET share_pin_hash = ? WHERE id = ?", (hashed, db_file_id)
        )
        await self.conn.commit()
        return bool(hashed)

    async def toggle_file_protect_share(self, db_file_id: int) -> bool:
        assert self.conn is not None
        f = await self.get_file(db_file_id)
        new_val = 0 if (f and f.get("protect_share")) else 1
        await self.conn.execute(
            "UPDATE files SET protect_share = ? WHERE id = ?", (new_val, db_file_id)
        )
        await self.conn.commit()
        return bool(new_val)

    async def cycle_file_share_expiry(self, db_file_id: int) -> Optional[int]:
        assert self.conn is not None
        f = await self.get_file(db_file_id)
        if not f:
            return None
        now = int(time.time())
        cur_exp = f.get("share_expires_at")
        remaining = max(0, int(cur_exp) - now) if cur_exp else 0
        # Determine next step in SHARE_TTL_CYCLE [0, 3600, 86400, 604800]
        if remaining == 0:
            nxt_ttl = 3600
        elif remaining <= 3600 + 60:
            nxt_ttl = 86400
        elif remaining <= 86400 + 60:
            nxt_ttl = 604800
        else:
            nxt_ttl = 0
        new_exp = (now + nxt_ttl) if nxt_ttl > 0 else None
        await self.conn.execute(
            "UPDATE files SET share_expires_at = ? WHERE id = ?", (new_exp, db_file_id)
        )
        await self.conn.commit()
        return new_exp

    async def trash_file(self, db_file_id: int) -> None:
        assert self.conn is not None
        now = int(time.time())
        await self.conn.execute(
            "UPDATE files SET is_trashed = 1, trashed_at = ? WHERE id = ?",
            (now, db_file_id),
        )
        await self.conn.execute("DELETE FROM files_fts WHERE file_id = ?", (db_file_id,))
        await self.conn.commit()

    async def restore_file(self, db_file_id: int) -> None:
        assert self.conn is not None
        await self.conn.execute(
            "UPDATE files SET is_trashed = 0, trashed_at = NULL WHERE id = ?",
            (db_file_id,),
        )
        await self._upsert_fts(db_file_id)
        await self.conn.commit()

    async def delete_file(self, db_file_id: int) -> None:
        assert self.conn is not None
        await self.conn.execute("DELETE FROM files WHERE id = ?", (db_file_id,))
        await self.conn.execute("DELETE FROM files_fts WHERE file_id = ?", (db_file_id,))
        await self.conn.commit()

    async def list_trashed_files(
        self, vault_id: int, limit: int = 20, offset: int = 0
    ) -> list[dict[str, Any]]:
        assert self.conn is not None
        cur = await self.conn.execute(
            """
            SELECT f.*, COALESCE(g.name, '') AS group_name
            FROM files f
            LEFT JOIN groups g ON g.id = f.group_id
            WHERE f.vault_id = ? AND COALESCE(f.is_trashed, 0) = 1
            ORDER BY COALESCE(f.trashed_at, f.created_at) DESC
            LIMIT ? OFFSET ?
            """,
            (vault_id, limit, offset),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def count_trashed_files(self, vault_id: int) -> int:
        assert self.conn is not None
        cur = await self.conn.execute(
            "SELECT COUNT(*) FROM files WHERE vault_id = ? AND COALESCE(is_trashed, 0) = 1",
            (vault_id,),
        )
        return int((await cur.fetchone())[0])

    async def empty_trash(self, vault_id: int) -> int:
        assert self.conn is not None
        cur = await self.conn.execute(
            "SELECT id FROM files WHERE vault_id = ? AND COALESCE(is_trashed, 0) = 1",
            (vault_id,),
        )
        ids = [r["id"] for r in await cur.fetchall()]
        for fid in ids:
            await self.conn.execute("DELETE FROM files_fts WHERE file_id = ?", (fid,))
        del_cur = await self.conn.execute(
            "DELETE FROM files WHERE vault_id = ? AND COALESCE(is_trashed, 0) = 1",
            (vault_id,),
        )
        await self.conn.commit()
        return del_cur.rowcount

    async def increment_use_count(self, db_file_id: int) -> None:
        assert self.conn is not None
        await self.conn.execute(
            "UPDATE files SET use_count = use_count + 1 WHERE id = ?", (db_file_id,)
        )
        await self.conn.commit()

    # -------------------- PACKS / MULTI-FILE BUNDLES --------------------

    async def create_pack(
        self,
        vault_id: int,
        creator_user_id: int,
        title: str,
        file_ids: list[int],
        *,
        description: str = "",
        pin: Optional[str] = None,
        protect_content: bool = False,
        ttl_seconds: int = 0,
        max_views: int = 0,
    ) -> dict[str, Any]:
        assert self.conn is not None
        now = int(time.time())
        token = _gen_token(10)
        pin_h = hash_pin(pin) if (pin and pin.strip()) else ""
        exp = (now + ttl_seconds) if ttl_seconds > 0 else None
        cur = await self.conn.execute(
            """
            INSERT INTO packs (
                vault_id, creator_user_id, title, description, share_token,
                pin_hash, protect_content, expires_at, max_views, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                vault_id,
                creator_user_id,
                title.strip() or "File Pack",
                description.strip()[:300],
                token,
                pin_h,
                1 if protect_content else 0,
                exp,
                max(0, int(max_views)),
                now,
            ),
        )
        pack_id = cur.lastrowid
        seen: set[int] = set()
        for idx, fid in enumerate(file_ids):
            if fid in seen:
                continue
            seen.add(fid)
            await self.conn.execute(
                "INSERT OR IGNORE INTO pack_items (pack_id, file_id, pos) VALUES (?, ?, ?)",
                (pack_id, fid, idx),
            )
        await self.conn.commit()
        res = await self.get_pack(pack_id)
        assert res is not None
        return res

    async def append_files_to_pack(
        self, pack_id: int, file_ids: list[int]
    ) -> Optional[dict[str, Any]]:
        assert self.conn is not None
        cur = await self.conn.execute(
            "SELECT COALESCE(MAX(pos), -1) FROM pack_items WHERE pack_id = ?",
            (pack_id,),
        )
        base_pos = int((await cur.fetchone())[0]) + 1
        for offset, fid in enumerate(file_ids):
            await self.conn.execute(
                "INSERT OR IGNORE INTO pack_items (pack_id, file_id, pos) VALUES (?, ?, ?)",
                (pack_id, fid, base_pos + offset),
            )
        await self.conn.commit()
        return await self.get_pack(pack_id)

    async def remove_file_from_pack(self, pack_id: int, file_id: int) -> bool:
        assert self.conn is not None
        cur = await self.conn.execute(
            "DELETE FROM pack_items WHERE pack_id = ? AND file_id = ?",
            (pack_id, file_id),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def rename_pack(self, pack_id: int, new_title: str) -> None:
        assert self.conn is not None
        title = new_title.strip()[:80] or "File Pack"
        await self.conn.execute(
            "UPDATE packs SET title = ? WHERE id = ?", (title, pack_id)
        )
        await self.conn.commit()

    async def set_pack_description(self, pack_id: int, description: str) -> None:
        assert self.conn is not None
        await self.conn.execute(
            "UPDATE packs SET description = ? WHERE id = ?",
            (description.strip()[:300], pack_id),
        )
        await self.conn.commit()

    async def get_pack(self, pack_id: int) -> Optional[dict[str, Any]]:
        assert self.conn is not None
        cur = await self.conn.execute(
            """
            SELECT p.*,
                   (SELECT COUNT(*) FROM pack_items pi
                    JOIN files f ON f.id = pi.file_id
                    WHERE pi.pack_id = p.id AND COALESCE(f.is_trashed, 0) = 0) AS item_count,
                   (SELECT COALESCE(SUM(f.file_size), 0) FROM pack_items pi
                    JOIN files f ON f.id = pi.file_id
                    WHERE pi.pack_id = p.id AND COALESCE(f.is_trashed, 0) = 0) AS total_size
            FROM packs p
            WHERE p.id = ?
            """,
            (pack_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_pack_by_token(
        self, token: str, *, include_unavailable: bool = False
    ) -> Optional[dict[str, Any]]:
        assert self.conn is not None
        if not token or not token.strip():
            return None
        now = int(time.time())
        cur = await self.conn.execute(
            """
            SELECT p.*,
                   (SELECT COUNT(*) FROM pack_items pi
                    JOIN files f ON f.id = pi.file_id
                    WHERE pi.pack_id = p.id AND COALESCE(f.is_trashed, 0) = 0) AS item_count,
                   (SELECT COALESCE(SUM(f.file_size), 0) FROM pack_items pi
                    JOIN files f ON f.id = pi.file_id
                    WHERE pi.pack_id = p.id AND COALESCE(f.is_trashed, 0) = 0) AS total_size
            FROM packs p
            WHERE p.share_token = ?
            """,
            (token.strip(),),
        )
        row = await cur.fetchone()
        if not row:
            return None
        data = dict(row)
        if include_unavailable:
            return data
        if data.get("is_paused"):
            return None
        exp = data.get("expires_at")
        if exp and int(exp) > 0 and int(exp) < now:
            return None
        max_v = int(data.get("max_views") or 0)
        cur_v = int(data.get("views") or 0)
        if max_v > 0 and cur_v >= max_v:
            return None
        return data

    async def get_pack_files(self, pack_id: int) -> list[dict[str, Any]]:
        assert self.conn is not None
        cur = await self.conn.execute(
            """
            SELECT f.*, COALESCE(g.name, '') AS group_name
            FROM pack_items pi
            JOIN files f ON f.id = pi.file_id
            LEFT JOIN groups g ON g.id = f.group_id
            WHERE pi.pack_id = ? AND COALESCE(f.is_trashed, 0) = 0
            ORDER BY pi.pos ASC, f.id ASC
            """,
            (pack_id,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def list_vault_packs(
        self, vault_id: int, limit: int = 20, offset: int = 0
    ) -> list[dict[str, Any]]:
        assert self.conn is not None
        cur = await self.conn.execute(
            """
            SELECT p.*,
                   (SELECT COUNT(*) FROM pack_items pi
                    JOIN files f ON f.id = pi.file_id
                    WHERE pi.pack_id = p.id AND COALESCE(f.is_trashed, 0) = 0) AS item_count,
                   (SELECT COALESCE(SUM(f.file_size), 0) FROM pack_items pi
                    JOIN files f ON f.id = pi.file_id
                    WHERE pi.pack_id = p.id AND COALESCE(f.is_trashed, 0) = 0) AS total_size
            FROM packs p
            WHERE p.vault_id = ?
            ORDER BY p.created_at DESC
            LIMIT ? OFFSET ?
            """,
            (vault_id, limit, offset),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def count_vault_packs(self, vault_id: int) -> int:
        assert self.conn is not None
        cur = await self.conn.execute(
            "SELECT COUNT(*) FROM packs WHERE vault_id = ?", (vault_id,)
        )
        return int((await cur.fetchone())[0])

    async def set_pack_pin(self, pack_id: int, pin: Optional[str]) -> bool:
        assert self.conn is not None
        hashed = hash_pin(pin) if (pin and pin.strip()) else ""
        await self.conn.execute(
            "UPDATE packs SET pin_hash = ? WHERE id = ?", (hashed, pack_id)
        )
        await self.conn.commit()
        return bool(hashed)

    async def toggle_pack_protect(self, pack_id: int) -> bool:
        assert self.conn is not None
        p = await self.get_pack(pack_id)
        new_val = 0 if (p and p.get("protect_content")) else 1
        await self.conn.execute(
            "UPDATE packs SET protect_content = ? WHERE id = ?", (new_val, pack_id)
        )
        await self.conn.commit()
        return bool(new_val)

    async def cycle_pack_expiry(self, pack_id: int) -> Optional[int]:
        assert self.conn is not None
        p = await self.get_pack(pack_id)
        if not p:
            return None
        now = int(time.time())
        cur_exp = p.get("expires_at")
        remaining = max(0, int(cur_exp) - now) if cur_exp else 0
        if remaining == 0:
            nxt_ttl = 3600
        elif remaining <= 3600 + 60:
            nxt_ttl = 86400
        elif remaining <= 86400 + 60:
            nxt_ttl = 604800
        else:
            nxt_ttl = 0
        new_exp = (now + nxt_ttl) if nxt_ttl > 0 else None
        await self.conn.execute(
            "UPDATE packs SET expires_at = ? WHERE id = ?", (new_exp, pack_id)
        )
        await self.conn.commit()
        return new_exp

    async def toggle_pack_paused(self, pack_id: int) -> bool:
        assert self.conn is not None
        p = await self.get_pack(pack_id)
        new_val = 0 if (p and p.get("is_paused")) else 1
        await self.conn.execute(
            "UPDATE packs SET is_paused = ? WHERE id = ?", (new_val, pack_id)
        )
        await self.conn.commit()
        return bool(new_val)

    async def cycle_pack_max_views(self, pack_id: int) -> int:
        assert self.conn is not None
        p = await self.get_pack(pack_id)
        cur_val = int((p or {}).get("max_views") or 0)
        if cur_val in MAX_VIEWS_CYCLE:
            nxt = MAX_VIEWS_CYCLE[(MAX_VIEWS_CYCLE.index(cur_val) + 1) % len(MAX_VIEWS_CYCLE)]
        else:
            nxt = 0
        await self.conn.execute(
            "UPDATE packs SET max_views = ? WHERE id = ?", (nxt, pack_id)
        )
        await self.conn.commit()
        return nxt

    async def regenerate_pack_token(self, pack_id: int) -> str:
        assert self.conn is not None
        token = _gen_token(10)
        await self.conn.execute(
            "UPDATE packs SET share_token = ?, views = 0, is_paused = 0 WHERE id = ?",
            (token, pack_id),
        )
        await self.conn.commit()
        return token

    async def increment_pack_views(self, pack_id: int) -> None:
        assert self.conn is not None
        await self.conn.execute(
            "UPDATE packs SET views = views + 1 WHERE id = ?", (pack_id,)
        )
        await self.conn.commit()

    async def delete_pack(self, pack_id: int, vault_id: int) -> bool:
        assert self.conn is not None
        cur = await self.conn.execute(
            "DELETE FROM packs WHERE id = ? AND vault_id = ?", (pack_id, vault_id)
        )
        await self.conn.commit()
        return cur.rowcount > 0

    # -------------------- INLINE & VAULT SEARCH --------------------

    async def count_files(
        self,
        vault_id: int,
        *,
        media_type: Optional[str] = None,
        group_id_filter: Optional[int] = None,
        only_favorites: bool = False,
    ) -> int:
        assert self.conn is not None
        conditions = [
            "COALESCE(f.is_trashed, 0) = 0",
            "(f.vault_id = ? OR f.group_id IN (SELECT group_id FROM group_members WHERE vault_id = ?))",
        ]
        params: list[Any] = [vault_id, vault_id]
        if media_type:
            conditions.append("f.media_type = ?")
            params.append(media_type)
        if only_favorites:
            conditions.append("f.is_favorite = 1")
        if group_id_filter is not None:
            conditions.append("f.group_id = ?")
            params.append(group_id_filter)
        where_sql = " AND ".join(conditions)
        cur = await self.conn.execute(
            f"SELECT COUNT(*) FROM files f WHERE {where_sql}", params
        )
        return int((await cur.fetchone())[0])

    async def search_files(
        self,
        vault_id: int,
        *,
        text_query: str = "",
        media_type: Optional[str] = None,
        group_name_filter: Optional[str] = None,
        group_id_filter: Optional[int] = None,
        tag_filter: Optional[str] = None,
        only_favorites: bool = False,
        sort_by: str = "new",
        limit: int = 25,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        assert self.conn is not None
        conditions = [
            "COALESCE(f.is_trashed, 0) = 0",
            "(f.vault_id = ? OR f.group_id IN (SELECT group_id FROM group_members WHERE vault_id = ?))",
        ]
        params: list[Any] = [vault_id, vault_id]

        if media_type:
            conditions.append("f.media_type = ?")
            params.append(media_type)

        if only_favorites:
            conditions.append("f.is_favorite = 1")

        if group_id_filter is not None:
            conditions.append("f.group_id = ?")
            params.append(group_id_filter)

        if group_name_filter:
            conditions.append("LOWER(COALESCE(g.name, '')) LIKE ?")
            params.append(f"%{group_name_filter.lower()}%")

        if tag_filter:
            conditions.append("LOWER(f.tags) LIKE ?")
            params.append(f"%{tag_filter.lower()}%")

        cleaned_text = text_query.strip()
        if cleaned_text:
            fts_terms = [
                f'"{tok.replace(chr(34), "")}"*'
                for tok in cleaned_text.split()
                if tok.strip()
            ]
            like_pattern = f"%{cleaned_text.lower()}%"
            if fts_terms:
                fts_query = " AND ".join(fts_terms)
                conditions.append(
                    """(
                        f.id IN (SELECT file_id FROM files_fts WHERE files_fts MATCH ?)
                        OR LOWER(f.title) LIKE ?
                        OR LOWER(f.performer) LIKE ?
                        OR LOWER(f.file_name) LIKE ?
                        OR LOWER(f.tags) LIKE ?
                        OR LOWER(COALESCE(g.name, '')) LIKE ?
                    )"""
                )
                params.extend(
                    [
                        fts_query,
                        like_pattern,
                        like_pattern,
                        like_pattern,
                        like_pattern,
                        like_pattern,
                    ]
                )
            else:
                conditions.append(
                    "(LOWER(f.title) LIKE ? OR LOWER(f.file_name) LIKE ? OR LOWER(f.tags) LIKE ?)"
                )
                params.extend([like_pattern, like_pattern, like_pattern])

        order_clause = SORT_CLAUSES.get(sort_by, SORT_CLAUSES["new"])
        where_sql = " AND ".join(conditions)
        sql = f"""
            SELECT f.*, COALESCE(g.name, '') AS group_name
            FROM files f
            LEFT JOIN groups g ON g.id = f.group_id
            WHERE {where_sql}
            ORDER BY {order_clause}
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])
        cur = await self.conn.execute(sql, params)
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # -------------------- STATS & ADMIN --------------------

    async def get_vault_stats(self, vault_id: int) -> dict[str, Any]:
        assert self.conn is not None
        cur = await self.conn.execute(
            """
            SELECT
                COUNT(*) AS total_files,
                COALESCE(SUM(file_size), 0) AS total_size,
                COALESCE(SUM(CASE WHEN is_favorite = 1 THEN 1 ELSE 0 END), 0) AS fav_count,
                COALESCE(SUM(use_count), 0) AS total_uses
            FROM files
            WHERE vault_id = ? AND COALESCE(is_trashed, 0) = 0
            """,
            (vault_id,),
        )
        base = dict(await cur.fetchone())

        type_cur = await self.conn.execute(
            """
            SELECT media_type, COUNT(*) AS cnt, COALESCE(SUM(file_size), 0) AS sz
            FROM files
            WHERE vault_id = ? AND COALESCE(is_trashed, 0) = 0
            GROUP BY media_type
            """,
            (vault_id,),
        )
        by_type = {
            r["media_type"]: {"count": r["cnt"], "size": r["sz"]}
            for r in await type_cur.fetchall()
        }

        top_cur = await self.conn.execute(
            """
            SELECT id, title, media_type, use_count
            FROM files
            WHERE vault_id = ? AND COALESCE(is_trashed, 0) = 0 AND use_count > 0
            ORDER BY use_count DESC, created_at DESC
            LIMIT 3
            """,
            (vault_id,),
        )
        top_files = [dict(r) for r in await top_cur.fetchall()]

        grp_cur = await self.conn.execute(
            "SELECT COUNT(*) AS cnt FROM group_members WHERE vault_id = ?",
            (vault_id,),
        )
        groups_count = (await grp_cur.fetchone())["cnt"]

        acc_cur = await self.conn.execute(
            "SELECT COUNT(*) AS cnt FROM users WHERE vault_id = ?", (vault_id,)
        )
        linked_accounts = (await acc_cur.fetchone())["cnt"]

        packs_cnt = await self.count_vault_packs(vault_id)
        trash_cnt = await self.count_trashed_files(vault_id)

        base["by_type"] = by_type
        base["top_files"] = top_files
        base["groups_count"] = groups_count
        base["linked_accounts"] = linked_accounts
        base["packs_count"] = packs_cnt
        base["trash_count"] = trash_cnt
        return base

    async def get_global_stats(self) -> dict[str, Any]:
        assert self.conn is not None
        now = int(time.time())
        day_ago = now - 86400
        u = (await (await self.conn.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
        banned = (
            await (
                await self.conn.execute(
                    "SELECT COUNT(*) FROM users WHERE COALESCE(is_banned, 0) = 1"
                )
            ).fetchone()
        )[0]
        active_24h = (
            await (
                await self.conn.execute(
                    "SELECT COUNT(*) FROM users WHERE last_seen >= ?", (day_ago,)
                )
            ).fetchone()
        )[0]
        v = (await (await self.conn.execute("SELECT COUNT(*) FROM vaults")).fetchone())[0]
        f = (
            await (
                await self.conn.execute(
                    "SELECT COUNT(*) FROM files WHERE COALESCE(is_trashed, 0) = 0"
                )
            ).fetchone()
        )[0]
        trashed = (
            await (
                await self.conn.execute(
                    "SELECT COUNT(*) FROM files WHERE COALESCE(is_trashed, 0) = 1"
                )
            ).fetchone()
        )[0]
        g = (await (await self.conn.execute("SELECT COUNT(*) FROM groups")).fetchone())[0]
        p = (await (await self.conn.execute("SELECT COUNT(*) FROM packs")).fetchone())[0]
        s = (
            await (
                await self.conn.execute("SELECT COALESCE(SUM(file_size), 0) FROM files")
            ).fetchone()
        )[0]
        return {
            "users": u,
            "banned": banned,
            "active_24h": active_24h,
            "vaults": v,
            "files": f,
            "trashed": trashed,
            "groups": g,
            "packs": p,
            "total_size": s,
        }

    async def get_all_user_ids(self) -> list[int]:
        assert self.conn is not None
        cur = await self.conn.execute(
            "SELECT user_id FROM users WHERE COALESCE(is_banned, 0) = 0"
        )
        return [r[0] for r in await cur.fetchall()]

    # -------------------- UI BANNER ASSETS (DUMP CHANNEL CACHE) --------------------

    async def get_ui_asset(self, asset_key: str) -> Optional[dict[str, Any]]:
        assert self.conn is not None
        cur = await self.conn.execute(
            "SELECT * FROM ui_assets WHERE asset_key = ?", (asset_key,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def upsert_ui_asset(
        self,
        *,
        asset_key: str,
        file_id: str,
        file_unique_id: str = "",
        dump_message_id: int = 0,
        sha256: str = "",
    ) -> dict[str, Any]:
        assert self.conn is not None
        now = int(time.time())
        await self.conn.execute(
            """
            INSERT INTO ui_assets (asset_key, file_id, file_unique_id, dump_message_id, sha256, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(asset_key) DO UPDATE SET
                file_id = excluded.file_id,
                file_unique_id = excluded.file_unique_id,
                dump_message_id = excluded.dump_message_id,
                sha256 = excluded.sha256,
                updated_at = excluded.updated_at
            """,
            (asset_key, file_id, file_unique_id, dump_message_id, sha256, now),
        )
        await self.conn.commit()
        res = await self.get_ui_asset(asset_key)
        assert res is not None
        return res

    async def list_ui_assets(self) -> list[dict[str, Any]]:
        assert self.conn is not None
        cur = await self.conn.execute(
            "SELECT * FROM ui_assets ORDER BY asset_key ASC"
        )
        return [dict(r) for r in await cur.fetchall()]

    async def clear_ui_assets(self) -> int:
        assert self.conn is not None
        cur = await self.conn.execute("DELETE FROM ui_assets")
        await self.conn.commit()
        return cur.rowcount
