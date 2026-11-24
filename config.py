import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    bot_token: str
    dump_channel_id: int
    admin_ids: tuple[int, ...]
    db_path: str
    inline_page_size: int
    link_code_ttl_seconds: int
    force_sub_channel: str = ""
    force_sub_url: str = ""
    rate_limit_interval: float = 0.35


def load_config() -> Config:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise ValueError("BOT_TOKEN is missing in .env")

    raw_dump_id = os.getenv("DUMP_CHANNEL_ID", "").strip()
    if not raw_dump_id:
        raise ValueError("DUMP_CHANNEL_ID is missing in .env")
    try:
        dump_channel_id = int(raw_dump_id)
    except ValueError as exc:
        raise ValueError("DUMP_CHANNEL_ID must be an integer (e.g. -1001234567890)") from exc

    raw_admins = os.getenv("ADMIN_IDS", "").strip()
    admin_ids: list[int] = []
    if raw_admins:
        for item in raw_admins.split(","):
            item = item.strip()
            if item:
                try:
                    admin_ids.append(int(item))
                except ValueError as exc:
                    raise ValueError(f"ADMIN_IDS contains non-integer entry: {item!r}") from exc

    db_path = (os.getenv("DB_PATH") or "").strip() or "data/koshbox.db"

    raw_inline_ps = (os.getenv("INLINE_PAGE_SIZE") or "").strip() or "25"
    try:
        inline_page_size = min(max(int(raw_inline_ps), 5), 50)
    except ValueError as exc:
        raise ValueError(f"INLINE_PAGE_SIZE must be an integer, got {raw_inline_ps!r}") from exc

    raw_link_ttl = (os.getenv("LINK_CODE_TTL_SECONDS") or "").strip() or "300"
    try:
        link_code_ttl = max(int(raw_link_ttl), 60)
    except ValueError as exc:
        raise ValueError(f"LINK_CODE_TTL_SECONDS must be an integer, got {raw_link_ttl!r}") from exc

    force_sub_channel = (os.getenv("FORCE_SUB_CHANNEL") or "").strip()
    force_sub_url = (os.getenv("FORCE_SUB_URL") or "").strip()

    raw_rl = (os.getenv("RATE_LIMIT_INTERVAL") or "").strip() or "0.35"
    try:
        rate_limit_interval = max(float(raw_rl), 0.0)
    except ValueError as exc:
        raise ValueError(f"RATE_LIMIT_INTERVAL must be a number, got {raw_rl!r}") from exc

    return Config(
        bot_token=bot_token,
        dump_channel_id=dump_channel_id,
        admin_ids=tuple(admin_ids),
        db_path=db_path,
        inline_page_size=inline_page_size,
        link_code_ttl_seconds=link_code_ttl,
        force_sub_channel=force_sub_channel,
        force_sub_url=force_sub_url,
        rate_limit_interval=rate_limit_interval,
    )
