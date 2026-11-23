# KoshBox — Flagship Multi-User Cloud Vault & Permanent Trial Delivery Platform

A fast, ultra-low-RAM, Oracle Cloud–ready Telegram bot inspired by **Linear / Raycast / GramIO minimalism** and native **Telegram Bot API 9.4+**.

Archive any file, album, or media into your private Dump Channel once — then search & send from **any chat** via Inline Mode (`@YourBot query`) or publish **permanent, group-safe Trial Links** with **1-Time Per-User Claim Lock**, **Dynamic Per-Owner Contact Support**, **100-in-1 Native Batch Delivery (`copy_messages`)**, and **Restart-Safe Batch Auto-Delete (`delete_messages`)**.

---

## Flagship Capabilities & Bot API 9.4+ UI/UX

- **Permanent Group-Safe Trial System + 1-Time Per-User Claim Lock (`👤 1×/User`)**:
  - **Never-Expiring Group-Safe Links by Default**: File Links (`?start=file_...`) and Pack Links (`?start=pack_...`) stay active forever (`Never Expire`, `∞` total visitors) unless paused or revoked.
  - **Atomic 1-Time Per-User Claim Lock (`link_user_claims`)**: Enable `👤 1×/User: ON` per link or globally in `/settings`. First open atomically claims the user's slot (`INSERT OR IGNORE`) before sending media. Repeat opens send **zero files** and display a sleek `🔒 Trial Access Ended` card with the owner's `💬 Contact Owner` CTA.
  - **Owner Preview Bypass**: Link creators/vault members opening their own link are never blocked and never consume claims or quota. `🔄 Reset` clears `link_user_claims` and generates a fresh token.
- **Dynamic Per-Owner Contact Support (`💬 Support Contact`)**:
  - Each creator sets their own `@username` or `https://t.me/...` sales/support handle in `/settings` (automatically falls back to their Telegram `@username`, or omits cleanly if unset).
  - Attached as a primary `💬 Contact Owner` button on both the **post-trial auto-delete expiry notice** and the **repeat-open (`🔒 Trial Access Ended`) card**.
- **Restart-Safe, Zero-RAM-Bloat Batch Auto-Delete (`scheduled_deletes` + `bot.delete_messages()`)**:
  - Optional self-destruct timer (`Off` / `5m` / `15m` / `1h` / `2h` / `24h`) persisted in SQLite (`scheduled_deletes`) and swept every 5s in 100-message native batches via `bot.delete_messages()` — zero sleeping tasks per user and 100% safe across `systemd` restarts.
- **Native 100-in-1 Batch Pack Delivery (`bot.copy_messages()`)**:
  - Delivers up to 100 files from `DUMP_CHANNEL_ID` in a **single Telegram API call** (`copy_messages`) with `protect_content=True` (No-Forward / No-Save) and graceful fallback, eliminating 429 FloodWaits.
- **Telegram Bot API 9.4+ Visual Polish (360px Mobile-First)**:
  - Native full-screen Message Effects (`🎉` Party, `🔥` Fire, `👍` Thumbs Up) with automatic fallback, `<blockquote expandable>` collapsible previews on large packs, colored `style="primary"|"success"|"danger"` buttons, one-tap `CopyTextButton`, and strict max-2-buttons-per-row mobile ergonomics.
- **One-Time Dump Channel 3D Banner Cache**:
  - Custom 3D mascot banners (`assets/banners/*.jpg`) upload **once** to `DUMP_CHANNEL_ID` on startup and reuse cached Telegram `file_id` + `SHA-256` forever.
- **Smart Multi-Drop Auto-Pack & Zero-Spam Builder (`/packs`, `/batch`)**:
  - Drop **2+ files together** (up to 100+ files at once) to auto-bundle into **1 Pack Link** with a single celebration card, or use `/batch` for live in-place status updates.
- **Advanced Link Security (PIN Lock, Max Opens, Pause, No-Forward, Expiry)**:
  - Salted SHA-256 `🔑 PIN Lock` with constant-time `hmac` verification + 5-attempt lockout, `👁 Access Limit`, `⏸ Pause / ▶️ Resume`, `🛡️ No-Fwd`, and `⏳ Expiry Timer`.
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
