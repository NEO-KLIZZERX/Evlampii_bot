import os
import random
import re
import textwrap
import asyncio
from datetime import datetime, timezone
from io import BytesIO

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message,
    BufferedInputFile,
    BotCommand,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    ReactionTypeEmoji,
)
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

import aiosqlite
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from dotenv import load_dotenv

load_dotenv()

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
MAX_WRAP = 34

FONT_CANDIDATES = [
    "impact.ttf",
    "Impact.ttf",
    "arialbd.ttf",
    "Arial Black.ttf",
    "arial.ttf",
    "/usr/share/fonts/truetype/msttcorefonts/Impact.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9]+", re.UNICODE)

def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))

def load_font(size: int):
    for p in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(p, size=size)
        except Exception:
            continue
    return ImageFont.load_default()

def kb_panel(chat_id: int, enabled: int, chance: int, cd_min: int, cd_max: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=("✅ Включен" if enabled else "⛔ Выключен"),
                callback_data=f"p|toggle|{chat_id}"
            )
        ],
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
        [
            InlineKeyboardButton(text="🔄 Обновить", callback_data=f"p|refresh|{chat_id}")
        ],
    ])

async def open_db():
    db = await aiosqlite.connect(DB_PATH, timeout=30)
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA synchronous=NORMAL")
    return db

async def table_columns(db: aiosqlite.Connection, table: str) -> set[str]:
    cols = set()
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        rows = await cur.fetchall()
    for r in rows:
        cols.add(str(r[1]))
    return cols

async def ensure_columns(db: aiosqlite.Connection, table: str, required: dict[str, str]):
    existing = await table_columns(db, table)
    for col, ddl in required.items():
        if col not in existing:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

async def init_db():
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

async def ensure_chat_row(chat_id: int):
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

async def get_chat_settings(chat_id: int):
    db = await open_db()
    try:
        async with db.execute("SELECT enabled, chance, cd_min, cd_max FROM chats WHERE chat_id=?", (chat_id,)) as cur:
            row = await cur.fetchone()
            if not row:
                return DEFAULT_ENABLED, DEFAULT_CHANCE, DEFAULT_CD_MIN, DEFAULT_CD_MAX
            return int(row[0]), int(row[1]), int(row[2]), int(row[3])
    finally:
        await db.close()

async def set_chat_settings(chat_id: int, *, enabled=None, chance=None, cd_min=None, cd_max=None):
    cur_enabled, cur_chance, cur_cd_min, cur_cd_max = await get_chat_settings(chat_id)

    new_enabled = cur_enabled if enabled is None else int(enabled)
    new_chance = cur_chance if chance is None else int(chance)
    new_cd_min = cur_cd_min if cd_min is None else int(cd_min)
    new_cd_max = cur_cd_max if cd_max is None else int(cd_max)

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

    return new_enabled, new_chance, new_cd_min, new_cd_max

async def get_enabled_chats() -> list[int]:
    db = await open_db()
    try:
        async with db.execute("SELECT chat_id FROM chats WHERE enabled=1") as cur:
            rows = await cur.fetchall()
            return [int(r[0]) for r in rows]
    finally:
        await db.close()

def is_good_quote(text: str) -> bool:
    t = text.strip()
    if not t or t.startswith("/"):
        return False
    if len(t) < MIN_TEXT_LEN or len(t) > MAX_TEXT_LEN:
        return False
    if "http://" in t or "https://" in t or "t.me/" in t:
        return False
    return True

async def prune_quotes(db: aiosqlite.Connection, chat_id: int):
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

async def prune_photos(db: aiosqlite.Connection, chat_id: int):
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

async def save_quote(chat_id: int, user_id: int | None, text: str):
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

async def save_photo(chat_id: int, file_id: str):
    db = await open_db()
    try:
        await db.execute(
            "INSERT INTO photos(chat_id, file_id, created_at) VALUES(?,?,?)",
            (chat_id, file_id, now_utc_iso())
        )
        await prune_photos(db, chat_id)
        await db.commit()
    finally:
        await db.close()

async def get_random_quote(chat_id: int) -> str | None:
    db = await open_db()
    try:
        async with db.execute(
            "SELECT text FROM quotes WHERE chat_id=? ORDER BY RANDOM() LIMIT 1",
            (chat_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None
    finally:
        await db.close()

async def get_random_photo_file_id(chat_id: int) -> str | None:
    db = await open_db()
    try:
        async with db.execute(
            "SELECT file_id FROM photos WHERE chat_id=? ORDER BY RANDOM() LIMIT 1",
            (chat_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None
    finally:
        await db.close()

async def get_word_mash(chat_id: int) -> str | None:
    db = await open_db()
    try:
        async with db.execute(
            "SELECT text FROM quotes WHERE chat_id=? ORDER BY id DESC LIMIT 120",
            (chat_id,)
        ) as cur:
            rows = await cur.fetchall()
    finally:
        await db.close()

    if not rows:
        return None

    pool = []
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
    if not s:
        return None
    if len(s) > 120:
        s = s[:120].rstrip()
    return s

def shadow_text(draw: ImageDraw.ImageDraw, xy, text, font, fill, shadow=(0, 0, 0), r=2):
    x, y = xy
    for dx in range(-r, r + 1):
        for dy in range(-r, r + 1):
            if dx == 0 and dy == 0:
                continue
            draw.text((x + dx, y + dy), text, font=font, fill=shadow)
    draw.text((x, y), text, font=font, fill=fill)

def make_vignette_mask(w: int, h: int) -> Image.Image:
    mask = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(mask)
    d.ellipse((-w * 0.25, -h * 0.15, w * 1.25, h * 1.15), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(70))
    return mask

async def make_demotivator(image_bytes: bytes, caption: str) -> bytes:
    img = Image.open(BytesIO(image_bytes)).convert("RGB")

    W, H = 980, 1180
    frame_margin = 70
    caption_area_h = 300

    base = Image.new("RGB", (W, H), (0, 0, 0))
    draw = ImageDraw.Draw(base)

    photo_max_w = W - frame_margin * 2
    photo_max_h = H - caption_area_h - frame_margin * 2

    img.thumbnail((photo_max_w, photo_max_h))
    px = (W - img.width) // 2
    py = frame_margin

    pad = 14
    border_w = 3
    draw.rectangle((px - pad, py - pad, px + img.width + pad, py + img.height + pad), outline=(245, 245, 245), width=border_w)
    base.paste(img, (px, py))

    mask = make_vignette_mask(W, H)
    dark = Image.new("RGB", (W, H), (0, 0, 0))
    overlay = Image.composite(dark, base, mask)
    base = Image.blend(base, overlay, 0.25)
    draw = ImageDraw.Draw(base)

    caption = (caption or "").strip() or "..."
    lines = textwrap.wrap(caption, width=MAX_WRAP)[:4]
    title = lines[0] if lines else caption
    sub = "\n".join(lines[1:]) if len(lines) > 1 else ""

    title = title.strip()
    if len(title) <= 18:
        title = title.upper()

    font_title = load_font(58)
    font_sub = load_font(32)

    y_text = py + img.height + 78
    w_title = draw.textlength(title, font=font_title)
    shadow_text(draw, ((W - w_title) / 2, y_text), title, font_title, (255, 255, 255), shadow=(0, 0, 0), r=2)

    if sub:
        y_sub = y_text + 86
        for sline in sub.split("\n"):
            w_sub = draw.textlength(sline, font=font_sub)
            shadow_text(draw, ((W - w_sub) / 2, y_sub), sline, font_sub, (210, 210, 210), shadow=(0, 0, 0), r=2)
            y_sub += 46

    out = BytesIO()
    base.save(out, format="JPEG", quality=93)
    return out.getvalue()

msg_since_bot: dict[int, int] = {}
next_threshold: dict[int, int] = {}

def init_threshold(chat_id: int, cd_min: int, cd_max: int):
    next_threshold[chat_id] = random.randint(cd_min, cd_max)
    msg_since_bot[chat_id] = 0

async def maybe_react_to_message(bot: Bot, message: Message) -> bool:
    enabled, _, cd_min, cd_max = await get_chat_settings(message.chat.id)
    if not enabled:
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
        init_threshold(message.chat.id, cd_min, cd_max)
        return True
    except Exception:
        return False

async def maybe_auto_post(bot: Bot, chat_id: int):
    enabled, chance, cd_min, cd_max = await get_chat_settings(chat_id)
    if not enabled:
        return

    if chat_id not in next_threshold:
        init_threshold(chat_id, cd_min, cd_max)

    msg_since_bot[chat_id] = msg_since_bot.get(chat_id, 0) + 1
    if msg_since_bot[chat_id] < next_threshold[chat_id]:
        return

    roll = random.randint(1, 100)
    if roll <= chance:
        post_text = None
        if random.randint(1, 100) <= MASH_CHANCE_ON_POST:
            post_text = await get_word_mash(chat_id)
        if not post_text:
            post_text = await get_random_quote(chat_id)

        if post_text:
            try:
                await bot.send_message(chat_id, post_text)
            except Exception:
                pass

    init_threshold(chat_id, cd_min, cd_max)

async def periodic_poster(bot: Bot):
    while True:
        await asyncio.sleep(POST_EVERY_SECONDS)
        enabled_chats = await get_enabled_chats()
        for chat_id in enabled_chats:
            roll = random.randint(1, 100)
            if roll > POST_CHANCE_TIMER:
                continue
            q = await get_random_quote(chat_id)
            if q:
                try:
                    await bot.send_message(chat_id, q)
                except Exception:
                    pass

def fmt_panel(enabled: int, chance: int, cd_min: int, cd_max: int) -> str:
    status = "✅ <b>Включен</b>" if enabled else "⛔ <b>Выключен</b>"
    return (
        f"🎛️ <b>Панель Евлампия</b>\n"
        f"{status}\n\n"
        f"🎲 <b>Шанс:</b> <code>{chance}%</code>\n"
        f"⏳ <b>Кулдаун:</b> <code>{cd_min}–{cd_max}</code> сообщений\n\n"
        f"🧠 <i>Лимиты: фразы {MAX_QUOTES_PER_CHAT}/чат, фото {MAX_PHOTOS_PER_CHAT}/чат</i>"
    )

async def cmd_panel(message: Message):
    await ensure_chat_row(message.chat.id)
    enabled, chance, cd_min, cd_max = await get_chat_settings(message.chat.id)
    await message.answer(
        fmt_panel(enabled, chance, cd_min, cd_max),
        reply_markup=kb_panel(message.chat.id, enabled, chance, cd_min, cd_max)
    )

async def cmd_quote(message: Message):
    await ensure_chat_row(message.chat.id)
    q = await get_random_quote(message.chat.id)
    if not q:
        return
    await message.reply(q)

async def cmd_mash(message: Message):
    await ensure_chat_row(message.chat.id)
    s = await get_word_mash(message.chat.id)
    if not s:
        return
    await message.reply(s)

async def cmd_dem(message: Message, bot: Bot):
    await ensure_chat_row(message.chat.id)

    file_id = await get_random_photo_file_id(message.chat.id)
    if not file_id:
        await message.reply("📷 В этом чате ещё нет сохранённых фоток.")
        return

    caption = await get_random_quote(message.chat.id) or "..."

    file = await bot.get_file(file_id)
    img_bytes = await bot.download_file(file.file_path)

    result_bytes = await make_demotivator(img_bytes.read(), caption)

    await message.reply_photo(
        BufferedInputFile(result_bytes, filename="evlampiy_demotivator.jpg"),
        caption="🖼️ <b>Демотиватор из чата</b>",
    )

    enabled, _, cd_min, cd_max = await get_chat_settings(message.chat.id)
    if enabled:
        init_threshold(message.chat.id, cd_min, cd_max)

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

    enabled, chance, cd_min, cd_max = await get_chat_settings(message.chat.id)
    txt = (
        f"📊 <b>Статистика</b>\n\n"
        f"🗣️ Фраз: <code>{qn}</code> / <code>{MAX_QUOTES_PER_CHAT}</code>\n"
        f"🖼️ Фото: <code>{pn}</code> / <code>{MAX_PHOTOS_PER_CHAT}</code>\n\n"
        f"🎲 Шанс: <code>{chance}%</code>\n"
        f"⏳ Кулдаун: <code>{cd_min}–{cd_max}</code>\n"
        f"🔌 Статус: {'✅' if enabled else '⛔'}\n"
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
        enabled, chance, cd_min, cd_max = await set_chat_settings(message.chat.id, enabled=1, chance=20, cd_min=10, cd_max=18)
    elif mode == "normal":
        enabled, chance, cd_min, cd_max = await set_chat_settings(message.chat.id, enabled=1, chance=55, cd_min=3, cd_max=5)
    else:
        enabled, chance, cd_min, cd_max = await set_chat_settings(message.chat.id, enabled=1, chance=85, cd_min=2, cd_max=3)

    init_threshold(message.chat.id, cd_min, cd_max)
    await message.answer("Готово ✅")

async def cmd_help(message: Message):
    txt = (
        "🧾 <b>Команды Евлампия</b>\n\n"
        "🎛️ /panel — панель управления\n"
        "🗣️ /quote — случайная фраза из чата\n"
        "🧩 /mash — склейка слов из чата\n"
        "🖼️ /dem — демотиватор: фото+фраза\n"
        "📊 /stats — статистика\n"
        "🎚️ /mode quiet|normal|chaos — режим\n\n"
        "✨ Иногда Евлампий ставит реакцию на сообщения."
    )
    await message.answer(txt)

async def on_any_message(message: Message, bot: Bot):
    await ensure_chat_row(message.chat.id)

    if message.photo:
        await save_photo(message.chat.id, message.photo[-1].file_id)

    if message.text and is_good_quote(message.text):
        await save_quote(message.chat.id, message.from_user.id if message.from_user else None, message.text)

    if message.text and message.text.startswith("/"):
        return

    reacted = await maybe_react_to_message(bot, message)
    if reacted:
        return

    await maybe_auto_post(bot, message.chat.id)

async def on_panel_callback(call: CallbackQuery):
    data = call.data or ""
    if not data.startswith("p|"):
        await call.answer()
        return

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
    enabled, chance, cd_min, cd_max = await get_chat_settings(chat_id)

    if action == "toggle":
        enabled = 0 if enabled else 1
        enabled, chance, cd_min, cd_max = await set_chat_settings(chat_id, enabled=enabled)
        init_threshold(chat_id, cd_min, cd_max)
        await call.answer("Ок")
    elif action == "ch":
        delta = int(payload[0].replace("+", ""))
        enabled, chance, cd_min, cd_max = await set_chat_settings(chat_id, chance=chance + delta)
        await call.answer(f"{chance}%")
    elif action == "cd":
        delta = int(payload[0].replace("+", ""))
        enabled, chance, cd_min, cd_max = await set_chat_settings(chat_id, cd_min=cd_min + delta, cd_max=cd_max + delta)
        init_threshold(chat_id, cd_min, cd_max)
        await call.answer(f"{cd_min}–{cd_max}")
    elif action == "mode":
        mode = payload[0]
        if mode == "quiet":
            enabled, chance, cd_min, cd_max = await set_chat_settings(chat_id, enabled=1, chance=20, cd_min=10, cd_max=18)
        elif mode == "normal":
            enabled, chance, cd_min, cd_max = await set_chat_settings(chat_id, enabled=1, chance=55, cd_min=3, cd_max=5)
        else:
            enabled, chance, cd_min, cd_max = await set_chat_settings(chat_id, enabled=1, chance=85, cd_min=2, cd_max=3)
        init_threshold(chat_id, cd_min, cd_max)
        await call.answer("Режим")
    elif action == "refresh":
        await call.answer("Обновлено")
    else:
        await call.answer()

    try:
        await call.message.edit_text(
            fmt_panel(enabled, chance, cd_min, cd_max),
            reply_markup=kb_panel(chat_id, enabled, chance, cd_min, cd_max),
        )
    except Exception:
        try:
            await call.message.answer(
                fmt_panel(enabled, chance, cd_min, cd_max),
                reply_markup=kb_panel(chat_id, enabled, chance, cd_min, cd_max),
            )
        except Exception:
            pass

async def set_commands(bot: Bot):
    commands = [
        BotCommand(command="panel", description="Панель управления"),
        BotCommand(command="quote", description="Случайная фраза из чата"),
        BotCommand(command="mash", description="Склейка слов из чата"),
        BotCommand(command="dem", description="Демотиватор: фото+фраза"),
        BotCommand(command="stats", description="Статистика"),
        BotCommand(command="mode", description="Режим: quiet/normal/chaos"),
        BotCommand(command="help", description="Помощь"),
    ]
    await bot.set_my_commands(commands)

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан в .env")

    await init_db()

    bot = Bot(
        BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
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
