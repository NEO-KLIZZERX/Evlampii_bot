import os
import random
import re
import textwrap
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Optional

import aiosqlite
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    Message,
    BufferedInputFile,
    BotCommand,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    ReactionTypeEmoji,
)

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("evlampiy")

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = "bot.db"

MIN_TEXT_LEN = 2
MAX_TEXT_LEN = 240

MAX_QUOTES_PER_CHAT = 5000
MAX_PHOTOS_PER_CHAT = 150

DEFAULT_ENABLED = 1
DEFAULT_CHANCE = 55
DEFAULT_CD_MIN = 3
DEFAULT_CD_MAX = 5

POST_EVERY_SECONDS = 8 * 60
POST_CHANCE_TIMER = 10

REACTION_CHANCE_PER_MESSAGE = 2
MASH_CHANCE_ON_POST = 15

REACTION_EMOJIS = ["👍", "😂", "🔥", "🤡", "👀", "🤨", "😈", "💀", "🥴"]

WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9]+", re.UNICODE)

BASE_DIR = Path(__file__).resolve().parent

FONT_CANDIDATES = [
    BASE_DIR / "fonts" / "Impact.ttf",
    BASE_DIR / "fonts" / "DejaVuSans-Bold.ttf",
    BASE_DIR / "fonts" / "DejaVuSans.ttf",
    "impact.ttf",
    "Impact.ttf",
    "arialbd.ttf",
    "Arial Black.ttf",
    "arial.ttf",
    "/usr/share/fonts/truetype/msttcorefonts/Impact.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
]


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))


def load_font(size: int) -> ImageFont.ImageFont:
    for p in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(str(p), size=size)
        except Exception:
            continue
    return ImageFont.load_default()


@dataclass(frozen=True)
class ChatSettings:
    enabled: int = DEFAULT_ENABLED
    chance: int = DEFAULT_CHANCE
    cd_min: int = DEFAULT_CD_MIN
    cd_max: int = DEFAULT_CD_MAX


def kb_panel(chat_id: int, s: ChatSettings) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=("✅ Включен" if s.enabled else "⛔ Выключен"), callback_data=f"p|toggle|{chat_id}")],
        [
            InlineKeyboardButton(text="➖ Шанс", callback_data=f"p|ch|-5|{chat_id}"),
            InlineKeyboardButton(text="➕ Шанс", callback_data=f"p|ch|+5|{chat_id}")
        ],
        [
            InlineKeyboardButton(text="➖ Кулдаун", callback_data=f"p|cd|-1|{chat_id}"),
            InlineKeyboardButton(text="➕ Кулдаун", callback_data=f"p|cd|+1|{chat_id}")
        ],
        [
            InlineKeyboardButton(text="😶 Тихо", callback_data=f"p|mode|quiet|{chat_id}"),
            InlineKeyboardButton(text="🙂 Норма", callback_data=f"p|mode|normal|{chat_id}"),
            InlineKeyboardButton(text="🤪 Хаос", callback_data=f"p|mode|chaos|{chat_id}")
        ],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"p|refresh|{chat_id}")]
    ])


def fmt_panel(s: ChatSettings) -> str:
    status = "✅ <b>Включен</b>" if s.enabled else "⛔ <b>Выключен</b>"
    return (
        f"🎛️ <b>Панель Евлампия</b>\n"
        f"{status}\n\n"
        f"🎲 <b>Шанс:</b> <code>{s.chance}%</code>\n"
        f"⏳ <b>Кулдаун:</b> <code>{s.cd_min}–{s.cd_max}</code> сообщений\n\n"
        f"🧠 <i>Лимиты: фразы {MAX_QUOTES_PER_CHAT}/чат, фото {MAX_PHOTOS_PER_CHAT}/чат</i>"
    )


def is_good_quote(text: str) -> bool:
    t = (text or "").strip()
    if not t or t.startswith("/"):
        return False
    if len(t) < MIN_TEXT_LEN or len(t) > MAX_TEXT_LEN:
        return False
    if "http://" in t or "https://" in t or "t.me/" in t:
        return False
    return True


async def open_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH, timeout=30)
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA synchronous=NORMAL")
    return db


async def table_columns(db: aiosqlite.Connection, table: str) -> set[str]:
    cols: set[str] = set()
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        rows = await cur.fetchall()
    for r in rows:
        cols.add(str(r[1]))
    return cols


async def ensure_columns(db: aiosqlite.Connection, table: str, required: dict[str, str]) -> None:
    existing = await table_columns(db, table)
    for col, ddl in required.items():
        if col not in existing:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


async def init_db() -> None:
    db = await open_db()
    try:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS quotes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_id INTEGER,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_quotes_chat ON quotes(chat_id)")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            file_id TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_photos_chat ON photos(chat_id)")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            chat_id INTEGER PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL
        )
        """)
        await ensure_columns(db, "chats", {
            "chance": "chance INTEGER NOT NULL DEFAULT 55",
            "cd_min": "cd_min INTEGER NOT NULL DEFAULT 3",
            "cd_max": "cd_max INTEGER NOT NULL DEFAULT 5",
        })
        await db.commit()
    finally:
        await db.close()


async def ensure_chat_row(chat_id: int) -> None:
    db = await open_db()
    try:
        await db.execute("""
        INSERT INTO chats(chat_id, enabled, chance, cd_min, cd_max, updated_at)
        VALUES(?, ?, ?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET updated_at=excluded.updated_at
        """, (chat_id, DEFAULT_ENABLED, DEFAULT_CHANCE, DEFAULT_CD_MIN, DEFAULT_CD_MAX, now_utc_iso()))
        await db.commit()
    finally:
        await db.close()


async def get_chat_settings(chat_id: int) -> ChatSettings:
    db = await open_db()
    try:
        async with db.execute("SELECT enabled, chance, cd_min, cd_max FROM chats WHERE chat_id=?", (chat_id,)) as cur:
            row = await cur.fetchone()
            if not row:
                return ChatSettings()
            return ChatSettings(int(row[0]), int(row[1]), int(row[2]), int(row[3]))
    finally:
        await db.close()


async def set_chat_settings(
    chat_id: int,
    *,
    enabled: Optional[int] = None,
    chance: Optional[int] = None,
    cd_min: Optional[int] = None,
    cd_max: Optional[int] = None
) -> ChatSettings:
    cur = await get_chat_settings(chat_id)

    new_enabled = cur.enabled if enabled is None else int(enabled)
    new_chance = cur.chance if chance is None else int(chance)
    new_cd_min = cur.cd_min if cd_min is None else int(cd_min)
    new_cd_max = cur.cd_max if cd_max is None else int(cd_max)

    new_chance = clamp(new_chance, 1, 99)
    new_cd_min = clamp(new_cd_min, 1, 50)
    new_cd_max = clamp(new_cd_max, 1, 50)
    if new_cd_min > new_cd_max:
        new_cd_min, new_cd_max = new_cd_max, new_cd_min

    db = await open_db()
    try:
        await db.execute("""
        INSERT INTO chats(chat_id, enabled, chance, cd_min, cd_max, updated_at)
        VALUES(?, ?, ?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            enabled=excluded.enabled,
            chance=excluded.chance,
            cd_min=excluded.cd_min,
            cd_max=excluded.cd_max,
            updated_at=excluded.updated_at
        """, (chat_id, new_enabled, new_chance, new_cd_min, new_cd_max, now_utc_iso()))
        await db.commit()
    finally:
        await db.close()

    return ChatSettings(new_enabled, new_chance, new_cd_min, new_cd_max)


async def get_enabled_chats() -> list[int]:
    db = await open_db()
    try:
        async with db.execute("SELECT chat_id FROM chats WHERE enabled=1") as cur:
            rows = await cur.fetchall()
            return [int(r[0]) for r in rows]
    finally:
        await db.close()


async def prune_quotes(db: aiosqlite.Connection, chat_id: int) -> None:
    await db.execute("""
    DELETE FROM quotes
    WHERE chat_id=?
      AND id NOT IN (
        SELECT id FROM quotes
        WHERE chat_id=?
        ORDER BY id DESC
        LIMIT ?
      )
    """, (chat_id, chat_id, MAX_QUOTES_PER_CHAT))


async def prune_photos(db: aiosqlite.Connection, chat_id: int) -> None:
    await db.execute("""
    DELETE FROM photos
    WHERE chat_id=?
      AND id NOT IN (
        SELECT id FROM photos
        WHERE chat_id=?
        ORDER BY id DESC
        LIMIT ?
      )
    """, (chat_id, chat_id, MAX_PHOTOS_PER_CHAT))


async def save_quote(chat_id: int, user_id: Optional[int], text: str) -> None:
    db = await open_db()
    try:
        await db.execute(
            "INSERT INTO quotes(chat_id, user_id, text, created_at) VALUES(?,?,?,?)",
            (chat_id, user_id, text.strip(), now_utc_iso())
        )
        await prune_quotes(db, chat_id)
        await db.commit()
    finally:
        await db.close()


async def save_photo(chat_id: int, file_id: str) -> None:
    db = await open_db()
    try:
        await db.execute("INSERT INTO photos(chat_id, file_id, created_at) VALUES(?,?,?)", (chat_id, file_id, now_utc_iso()))
        await prune_photos(db, chat_id)
        await db.commit()
    finally:
        await db.close()


async def get_random_quote(chat_id: int) -> Optional[str]:
    db = await open_db()
    try:
        async with db.execute("SELECT text FROM quotes WHERE chat_id=? ORDER BY RANDOM() LIMIT 1", (chat_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None
    finally:
        await db.close()


async def get_random_photo_file_id(chat_id: int) -> Optional[str]:
    db = await open_db()
    try:
        async with db.execute("SELECT file_id FROM photos WHERE chat_id=? ORDER BY RANDOM() LIMIT 1", (chat_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None
    finally:
        await db.close()


async def get_word_mash(chat_id: int) -> Optional[str]:
    db = await open_db()
    try:
        async with db.execute("SELECT text FROM quotes WHERE chat_id=? ORDER BY id DESC LIMIT 120", (chat_id,)) as cur:
            rows = await cur.fetchall()
    finally:
        await db.close()

    if not rows:
        return None

    pool: list[str] = []
    for (txt,) in rows:
        for w in WORD_RE.findall(txt or ""):
            w2 = w.strip()
            if len(w2) >= 3:
                pool.append(w2)

    if len(pool) < 4:
        return None

    n = random.randint(2, 5)
    words = random.sample(pool, k=min(n, len(pool)))
    s = " ".join(words).strip()
    return s[:120].rstrip() if s else None


def _text_bbox(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _wrap_by_pixels(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_w: int) -> list[str]:
    words = (text or "").split()
    if not words:
        return ["..."]
    lines: list[str] = []
    cur = words[0]
    for w in words[1:]:
        candidate = f"{cur} {w}"
        cw, _ = _text_bbox(draw, candidate, font)
        if cw <= max_w:
            cur = candidate
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines


def _shadow_text(draw: ImageDraw.ImageDraw, x: float, y: float, text: str, font, fill, shadow=(0, 0, 0), r=2) -> None:
    for dx in range(-r, r + 1):
        for dy in range(-r, r + 1):
            if dx == 0 and dy == 0:
                continue
            draw.text((x + dx, y + dy), text, font=font, fill=shadow)
    draw.text((x, y), text, font=font, fill=fill)


def _make_vignette_mask(w: int, h: int) -> Image.Image:
    mask = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(mask)
    d.ellipse((-w * 0.25, -h * 0.15, w * 1.25, h * 1.15), fill=255)
    return mask.filter(ImageFilter.GaussianBlur(70))


def _resampling_lanczos():
    return getattr(getattr(Image, "Resampling", Image), "LANCZOS", getattr(Image, "LANCZOS", 1))


async def make_demotivator(image_bytes: bytes, caption: str) -> bytes:
    img = Image.open(BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img).convert("RGB")

    W, H = 980, 1180
    frame_margin = 70
    caption_area_h = 300

    base = Image.new("RGB", (W, H), (0, 0, 0))
    draw = ImageDraw.Draw(base)

    photo_max_w = W - frame_margin * 2
    photo_max_h = H - caption_area_h - frame_margin * 2

    img.thumbnail((photo_max_w, photo_max_h), _resampling_lanczos())

    px = (W - img.width) // 2
    py = frame_margin

    pad = 14
    border_w = 3
    draw.rectangle((px - pad, py - pad, px + img.width + pad, py + img.height + pad), outline=(245, 245, 245), width=border_w)
    base.paste(img, (px, py))

    mask = _make_vignette_mask(W, H)
    dark = Image.new("RGB", (W, H), (0, 0, 0))
    overlay = Image.composite(dark, base, mask)
    base = Image.blend(base, overlay, 0.25)
    draw = ImageDraw.Draw(base)

    caption = (caption or "").strip() or "..."
    raw_lines = textwrap.wrap(caption, width=120)[:6]
    title_text = (raw_lines[0] if raw_lines else caption).strip()
    rest_text = " ".join(raw_lines[1:]).strip()

    if len(title_text) <= 18:
        title_text = title_text.upper()

    cap_top = py + img.height + 60
    cap_bottom = H - 40
    cap_h = max(120, cap_bottom - cap_top)
    max_text_w = W - 120

    title_size = 62
    sub_size = 34

    while True:
        font_title = load_font(title_size)
        font_sub = load_font(sub_size)

        title_lines = _wrap_by_pixels(draw, title_text, font_title, max_text_w)[:2]
        sub_lines = _wrap_by_pixels(draw, rest_text, font_sub, max_text_w)[:3] if rest_text else []

        title_total_h = sum(_text_bbox(draw, ln, font_title)[1] for ln in title_lines) + (10 * (len(title_lines) - 1))
        sub_total_h = sum(_text_bbox(draw, ln, font_sub)[1] for ln in sub_lines) + (8 * (len(sub_lines) - 1)) if sub_lines else 0
        total_h = title_total_h + (30 if sub_lines else 0) + sub_total_h

        if total_h <= cap_h or title_size <= 36:
            break

        title_size -= 2
        sub_size = max(22, sub_size - 1)

    y = cap_top
    for ln in title_lines:
        lw, lh = _text_bbox(draw, ln, font_title)
        _shadow_text(draw, (W - lw) / 2, y, ln, font_title, (255, 255, 255), shadow=(0, 0, 0), r=2)
        y += lh + 10

    if sub_lines:
        y += 18
        for ln in sub_lines:
            lw, lh = _text_bbox(draw, ln, font_sub)
            _shadow_text(draw, (W - lw) / 2, y, ln, font_sub, (210, 210, 210), shadow=(0, 0, 0), r=2)
            y += lh + 8

    out = BytesIO()
    base.save(out, format="JPEG", quality=93, optimize=True)
    return out.getvalue()


msg_since_bot: dict[int, int] = {}
next_threshold: dict[int, int] = {}


def init_threshold(chat_id: int, cd_min: int, cd_max: int) -> None:
    next_threshold[chat_id] = random.randint(cd_min, cd_max)
    msg_since_bot[chat_id] = 0


async def maybe_react_to_message(bot: Bot, message: Message) -> bool:
    s = await get_chat_settings(message.chat.id)
    if not s.enabled:
        return False
    if random.randint(1, 100) > REACTION_CHANCE_PER_MESSAGE:
        return False
    try:
        emoji = random.choice(REACTION_EMOJIS)
        await bot.set_message_reaction(
            chat_id=message.chat.id,
            message_id=message.message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
            is_big=False
        )
        init_threshold(message.chat.id, s.cd_min, s.cd_max)
        return True
    except Exception:
        log.exception("reaction_failed")
        return False


async def maybe_auto_post(bot: Bot, chat_id: int) -> None:
    s = await get_chat_settings(chat_id)
    if not s.enabled:
        return

    if chat_id not in next_threshold:
        init_threshold(chat_id, s.cd_min, s.cd_max)

    msg_since_bot[chat_id] = msg_since_bot.get(chat_id, 0) + 1
    if msg_since_bot[chat_id] < next_threshold[chat_id]:
        return

    if random.randint(1, 100) <= s.chance:
        post_text = None
        if random.randint(1, 100) <= MASH_CHANCE_ON_POST:
            post_text = await get_word_mash(chat_id)
        if not post_text:
            post_text = await get_random_quote(chat_id)
        if post_text:
            try:
                await bot.send_message(chat_id, post_text)
            except Exception:
                log.exception("auto_post_failed")

    init_threshold(chat_id, s.cd_min, s.cd_max)


async def periodic_poster(bot: Bot) -> None:
    while True:
        await asyncio.sleep(POST_EVERY_SECONDS)
        for chat_id in await get_enabled_chats():
            if random.randint(1, 100) > POST_CHANCE_TIMER:
                continue
            q = await get_random_quote(chat_id)
            if q:
                try:
                    await bot.send_message(chat_id, q)
                except Exception:
                    log.exception("periodic_post_failed")


async def cmd_panel(message: Message):
    await ensure_chat_row(message.chat.id)
    s = await get_chat_settings(message.chat.id)
    await message.answer(fmt_panel(s), reply_markup=kb_panel(message.chat.id, s))


async def cmd_quote(message: Message):
    await ensure_chat_row(message.chat.id)
    q = await get_random_quote(message.chat.id)
    if q:
        await message.reply(q)


async def cmd_mash(message: Message):
    await ensure_chat_row(message.chat.id)
    s = await get_word_mash(message.chat.id)
    if s:
        await message.reply(s)


async def cmd_dem(message: Message, bot: Bot):
    await ensure_chat_row(message.chat.id)

    file_id = await get_random_photo_file_id(message.chat.id)
    if not file_id:
        await message.reply("📷 В этом чате ещё нет сохранённых фоток.")
        return

    caption = await get_random_quote(message.chat.id) or "..."

    try:
        tg_file = await bot.get_file(file_id)
        buf = BytesIO()
        await bot.download_file(tg_file.file_path, destination=buf)
        img_bytes = buf.getvalue()
        result_bytes = await make_demotivator(img_bytes, caption)

        await message.reply_photo(
            BufferedInputFile(result_bytes, filename="evlampiy_demotivator.jpg"),
            caption="🖼️ <b>Демотиватор из чата</b>",
        )

        s = await get_chat_settings(message.chat.id)
        if s.enabled:
            init_threshold(message.chat.id, s.cd_min, s.cd_max)

    except Exception:
        log.exception("demotivator_failed")
        await message.reply("⚠️ Не смог сделать демотиватор. Проверь наличие шрифтов и попробуй ещё раз.")


async def cmd_stats(message: Message):
    await ensure_chat_row(message.chat.id)

    db = await open_db()
    try:
        async with db.execute("SELECT COUNT(*) FROM quotes WHERE chat_id=?", (message.chat.id,)) as c1:
            qn = int((await c1.fetchone())[0])
        async with db.execute("SELECT COUNT(*) FROM photos WHERE chat_id=?", (message.chat.id,)) as c2:
            pn = int((await c2.fetchone())[0])
    finally:
        await db.close()

    s = await get_chat_settings(message.chat.id)
    txt = (
        f"📊 <b>Статистика</b>\n\n"
        f"🗣️ Фраз: <code>{qn}</code> / <code>{MAX_QUOTES_PER_CHAT}</code>\n"
        f"🖼️ Фото: <code>{pn}</code> / <code>{MAX_PHOTOS_PER_CHAT}</code>\n\n"
        f"🎲 Шанс: <code>{s.chance}%</code>\n"
        f"⏳ Кулдаун: <code>{s.cd_min}–{s.cd_max}</code>\n"
        f"🔌 Статус: {'✅' if s.enabled else '⛔'}\n"
        f"🧩 Склейка слов: <code>{MASH_CHANCE_ON_POST}%</code> от автопостов\n"
        f"✨ Реакции: <code>{REACTION_CHANCE_PER_MESSAGE}%</code> на сообщение"
    )
    await message.answer(txt)


async def cmd_mode(message: Message):
    await ensure_chat_row(message.chat.id)
    parts = (message.text or "").split(maxsplit=1)
    mode = parts[1].strip().lower() if len(parts) > 1 else ""

    if mode not in {"quiet", "normal", "chaos"}:
        await message.answer("Используй: <code>/mode quiet</code> | <code>/mode normal</code> | <code>/mode chaos</code>")
        return

    if mode == "quiet":
        s = await set_chat_settings(message.chat.id, enabled=1, chance=20, cd_min=10, cd_max=18)
    elif mode == "normal":
        s = await set_chat_settings(message.chat.id, enabled=1, chance=55, cd_min=3, cd_max=5)
    else:
        s = await set_chat_settings(message.chat.id, enabled=1, chance=85, cd_min=2, cd_max=3)

    init_threshold(message.chat.id, s.cd_min, s.cd_max)
    await message.answer("Готово ✅")


async def cmd_help(message: Message):
    await message.answer(
        "🧾 <b>Команды Евлампия</b>\n\n"
        "🎛️ /panel — панель управления\n"
        "🗣️ /quote — случайная фраза из чата\n"
        "🧩 /mash — склейка слов из чата\n"
        "🖼️ /dem — демотиватор: фото+фраза\n"
        "📊 /stats — статистика\n"
        "🎚️ /mode quiet|normal|chaos — режим\n\n"
        "✨ Иногда Евлампий ставит реакцию на сообщения."
    )


async def on_any_message(message: Message, bot: Bot):
    await ensure_chat_row(message.chat.id)

    if message.photo:
        try:
            await save_photo(message.chat.id, message.photo[-1].file_id)
        except Exception:
            log.exception("save_photo_failed")

    if message.text and is_good_quote(message.text):
        try:
            await save_quote(message.chat.id, message.from_user.id if message.from_user else None, message.text)
        except Exception:
            log.exception("save_quote_failed")

    if message.text and message.text.startswith("/"):
        return

    reacted = await maybe_react_to_message(bot, message)
    if reacted:
        return

    await maybe_auto_post(bot, message.chat.id)


async def on_panel_callback(call: CallbackQuery):
    data = call.data or ""
    parts = data.split("|")
    if len(parts) < 3:
        await call.answer()
        return

    action = parts[1]
    payload = parts[2:]

    if call.message is None:
        await call.answer()
        return

    try:
        chat_id = int(payload[-1])
    except Exception:
        await call.answer()
        return

    if call.message.chat.id != chat_id:
        await call.answer("Панель не из этого чата.")
        return

    await ensure_chat_row(chat_id)
    s = await get_chat_settings(chat_id)

    if action == "toggle":
        s = await set_chat_settings(chat_id, enabled=0 if s.enabled else 1)
        init_threshold(chat_id, s.cd_min, s.cd_max)
        await call.answer("Ок")
    elif action == "ch":
        delta = int(payload[0].replace("+", ""))
        s = await set_chat_settings(chat_id, chance=s.chance + delta)
        await call.answer(f"{s.chance}%")
    elif action == "cd":
        delta = int(payload[0].replace("+", ""))
        s = await set_chat_settings(chat_id, cd_min=s.cd_min + delta, cd_max=s.cd_max + delta)
        init_threshold(chat_id, s.cd_min, s.cd_max)
        await call.answer(f"{s.cd_min}–{s.cd_max}")
    elif action == "mode":
        mode = payload[0]
        if mode == "quiet":
            s = await set_chat_settings(chat_id, enabled=1, chance=20, cd_min=10, cd_max=18)
        elif mode == "normal":
            s = await set_chat_settings(chat_id, enabled=1, chance=55, cd_min=3, cd_max=5)
        else:
            s = await set_chat_settings(chat_id, enabled=1, chance=85, cd_min=2, cd_max=3)
        init_threshold(chat_id, s.cd_min, s.cd_max)
        await call.answer("Режим")
    elif action == "refresh":
        await call.answer("Обновлено")
    else:
        await call.answer()

    try:
        await call.message.edit_text(fmt_panel(s), reply_markup=kb_panel(chat_id, s))
    except Exception:
        try:
            await call.message.answer(fmt_panel(s), reply_markup=kb_panel(chat_id, s))
        except Exception:
            pass


async def set_commands(bot: Bot):
    await bot.set_my_commands([
        BotCommand(command="panel", description="Панель управления"),
        BotCommand(command="quote", description="Случайная фраза из чата"),
        BotCommand(command="mash", description="Склейка слов из чата"),
        BotCommand(command="dem", description="Демотиватор: фото+фраза"),
        BotCommand(command="stats", description="Статистика"),
        BotCommand(command="mode", description="Режим: quiet/normal/chaos"),
        BotCommand(command="help", description="Помощь"),
    ])


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан в .env")

    await init_db()

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await set_commands(bot)

    dp = Dispatcher()

    dp.message.register(cmd_help, Command("help"))
    dp.message.register(cmd_panel, Command("panel"))
    dp.message.register(cmd_quote, Command("quote"))
    dp.message.register(cmd_mash, Command("mash"))
    dp.message.register(cmd_dem, Command("dem"))
    dp.message.register(cmd_stats, Command("stats"))
    dp.message.register(cmd_mode, Command("mode"))

    dp.callback_query.register(on_panel_callback, F.data.startswith("p|"))

    dp.message.register(on_any_message)

    asyncio.create_task(periodic_poster(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
