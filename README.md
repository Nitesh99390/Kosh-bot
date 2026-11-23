# KoshBox — Pro Telegram Cloud Vault, Pack-Store & Inline Bot

A fast, lightweight, Oracle Cloud–ready Telegram bot inspired by **Linear / Raycast UI** and flagship open-source **Telegram FileStore + Inline Vault** architectures (`Codeflix-Bots/FileStore`, `VJ-File-Store`, `PostBot`, `aiogram-sentinel`).

Archive any file, album, or media into your private Dump Channel once — then search & send from **any chat** via Inline Mode (`@YourBot query`) or share **PIN-locked, expiring, forward-protected File & Multi-File Pack links**.

---

## Key Pro Capabilities & Ultra-Short UI/UX

- **Ultra-Short, Crisp Card Design (Zero Clutter)**:
  - Compact 3–4 line scannable headers with native `<blockquote>` cards, subtle breadcrumbs (`Vault › Files`), and clean 2-column button grids.
  - Native Telegram Bot API 9.4 styling (`style="primary"`, `style="success"`, `style="danger"`), one-tap `CopyTextButton` (`📋 Copy Link`, `📋 Copy Code`), and native Telegram Share Sheet (`↗️ Share`).
- **One-Time Dump Channel 3D Banner Cache**:
  - Custom 3D mascot banners (`assets/banners/*.jpg`) upload **once** to `DUMP_CHANNEL_ID` on startup and reuse cached Telegram `file_id` + `SHA-256` forever with zero repeated server uploads.
- **Multi-File Packs / Batch Link Builder (`/packs`, `/batch`)**:
  - Bundle multiple files into **one shareable link** (`?start=pack_<TOKEN>`).
  - Create packs interactively via `/batch` + `/done`, **1-tap from any uploaded Media Album** (`🎒 Create Pack Link`), or **1-tap from any Folder** (`🎒 Pack All into Link`).
  - Inline Pack sharing: type `@YourBot packs` in any chat to send interactive Pack cards!
- **Advanced Link Security (PIN Lock, No-Forward, Auto-Expiry)**:
  - Protect both **Single-File Links** and **Multi-File Packs** with:
    - `🔑 PIN Lock` (salted SHA-256 + constant-time `hmac` check; auto-deletes user PIN messages).
    - `🛡️ No-Fwd` (`protect_content=True` — blocks forwarding, saving, and screenshots in Telegram).
    - `⏳ Expiry Timer` (`1h` / `24h` / `7d` / `Never`).
- **Auto-Delete Delivery Timer (`/settings`)**:
  - Optional self-destruct timer (`Off` / `5m` / `15m` / `1h` / `24h`) that automatically cleans up delivered files and packs from user chats.
- **Outer Guard & Anti-Flood Middleware**:
  - Built-in rate-limiting (`RATE_LIMIT_INTERVAL`, album-aware so multi-file drops never throttle), user Ban/Unban guard, and optional **Force-Subscribe Channel Gate** (`FORCE_SUB_CHANNEL`).
- **Hybrid FTS5 Instant Inline Search (`@YourBot`)**:
  - Supports `t:audio`, `t:video`, `t:doc`, `t:photo`, `t:voice`, `t:gif`, `t:sticker`, `g:folder`, `#tag`, `fav`, `top`, `packs`, `s:az`, `s:size`.
- **Folders, Shared Collaborative Groups & Multi-Account Sync**:
  - 2-column folder grid, invite-code collaborative groups (`/groups`), safe Recycle Trash with 1-tap Undo (`/trash`), and real-time multi-account vault merging (`/accounts`, `/link`, `/verify`).

---

## Step 1: Telegram & BotFather Setup

1. Create a bot in [@BotFather](https://t.me/BotFather) with `/newbot` → copy `BOT_TOKEN`.
2. **Enable Inline Mode**:
   - Send `/setinline` to `@BotFather` → select your bot → placeholder: `Search vault (t:audio, g:folder, fav, packs)…`
   - Send `/setinlinefeedback` → choose `100%` so popularity (`top`) counters update automatically.
3. **Create a Private Dump Channel**:
   - Create a private Telegram Channel, add your bot as **Administrator** (`Post Messages`), and copy its ID (`-100...`).

---

## Step 2: Deploy (Oracle Cloud / Any Linux VPS)

1. Configure `.env`:
   ```bash
   cp .env.example .env
   nano .env
   ```
   Set `BOT_TOKEN`, `DUMP_CHANNEL_ID`, and `ADMIN_IDS` (optional: `FORCE_SUB_CHANNEL`, `RATE_LIMIT_INTERVAL`).
2. Run the automated installer (`.venv` + `systemd` 24/7 service):
   ```bash
   chmod +x deploy_oracle.sh
   ./deploy_oracle.sh
   ```
3. Manage service:
   ```bash
   sudo systemctl status koshbox
   journalctl -u koshbox -f
   ```
