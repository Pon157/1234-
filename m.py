import asyncio
import logging
import os
import aiosqlite
import datetime
from dotenv import load_dotenv

import aiohttp
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError

# ──────────────────────────────────────────────────────────────
# Прокси и Сессия
# ──────────────────────────────────────────────────────────────
try:
    from aiohttp_socks import ProxyConnector
    _SOCKS_OK = True
except ImportError:
    _SOCKS_OK = False

class _CustomProxySession(AiohttpSession):
    """Кастомная сессия с SOCKS5-прокси и удаленным DNS."""
    def __init__(self, connector: aiohttp.BaseConnector):
        super().__init__(timeout=40.0)
        self._connector = connector

    async def create_session(self) -> aiohttp.ClientSession:
        return aiohttp.ClientSession(
            connector=self._connector,
            json_serialize=self.json_dumps,
            timeout=aiohttp.ClientTimeout(total=40, connect=15),
        )

def _make_session() -> AiohttpSession:
    proxy_url = os.getenv("TG_PROXY_URL", "").strip()

    if not proxy_url or not _SOCKS_OK:
        if proxy_url and not _SOCKS_OK:
            logging.warning("Библиотека aiohttp-socks не найдена. Прокси отключен.")
        return AiohttpSession(timeout=40.0)

    try:
        # aiohttp-socks не понимает схему 'socks5h',
        # но флаг rdns=True делает ровно то же самое (удаленный DNS)
        clean_url = proxy_url.replace("socks5h://", "socks5://")

        # Если в URL забыли указать схему вообще
        if "://" not in clean_url:
            clean_url = f"socks5://{clean_url}"

        connector = ProxyConnector.from_url(
            clean_url,
            rdns=True  # ЭТОТ ФЛАГ заменяет 'socks5h'
        )

        logging.info(f"Прокси настроен: {clean_url.split('@')[-1]} (RDNS: On)")
        return _CustomProxySession(connector)
    except Exception as e:
        logging.error(f"Ошибка конфигурации прокси: {e}")
        return AiohttpSession(timeout=40.0)

# ──────────────────────────────────────────────────────────────
# Конфигурация
# ──────────────────────────────────────────────────────────────
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID", -1002752721634))
OWNER_ID = int(os.getenv("OWNER_ID"))

if not BOT_TOKEN:
    exit("Ошибка: BOT_TOKEN не найден в .env")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

dp = Dispatcher()
bot: Bot = None
DB_NAME = "anon_chat.db"

class BroadcastState(StatesGroup):
    waiting_for_message = State()

# ──────────────────────────────────────────────────────────────
# База данных
# ──────────────────────────────────────────────────────────────
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                topic_id INTEGER UNIQUE,
                warns INTEGER DEFAULT 0,
                is_banned BOOLEAN DEFAULT 0,
                reg_date TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                user_msg_id INTEGER,
                admin_msg_id INTEGER
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_user_msg ON messages(user_msg_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_admin_msg ON messages(admin_msg_id)")
        await db.commit()

async def get_user_by_id(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            res = await cursor.fetchone()
            return dict(res) if res else None

async def get_user_by_topic(topic_id):
    if not topic_id: return None
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE topic_id = ?", (topic_id,)) as cursor:
            res = await cursor.fetchone()
            return dict(res) if res else None

async def create_user(user_id):
    reg_date = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id, reg_date) VALUES (?, ?)", (user_id, reg_date))
        await db.commit()

async def update_user_topic(user_id, topic_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET topic_id = ? WHERE user_id = ?", (topic_id, user_id))
        await db.commit()

async def update_ban(user_id, is_banned):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET is_banned = ? WHERE user_id = ?", (int(is_banned), user_id))
        await db.commit()

async def update_warns(user_id, count):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET warns = ? WHERE user_id = ?", (count, user_id))
        await db.commit()

async def get_stats_data():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT count(*) FROM users") as c:
            total = (await c.fetchone())[0]
        async with db.execute("SELECT count(*) FROM users WHERE is_banned=1") as c:
            banned = (await c.fetchone())[0]
    return total, banned

async def get_all_users_ids():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id FROM users") as c:
            return await c.fetchall()

async def save_msg_map(user_id, user_msg_id, admin_msg_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO messages (user_id, user_msg_id, admin_msg_id) VALUES (?, ?, ?)",
                         (user_id, user_msg_id, admin_msg_id))
        await db.commit()

async def get_map_by_user_msg(user_msg_id):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM messages WHERE user_msg_id = ?", (user_msg_id,)) as c:
            res = await c.fetchone()
            return dict(res) if res else None

async def get_map_by_admin_msg(admin_msg_id):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM messages WHERE admin_msg_id = ?", (admin_msg_id,)) as c:
            res = await c.fetchone()
            return dict(res) if res else None

# ──────────────────────────────────────────────────────────────
# Логика
# ──────────────────────────────────────────────────────────────
async def ensure_topic(user_id, bot_instance: Bot):
    user = await get_user_by_id(user_id)
    if user and user.get('topic_id'):
        return user['topic_id']
    try:
        topic = await bot_instance.create_forum_topic(ADMIN_GROUP_ID, name=f"Анонимный диалог")
        await update_user_topic(user_id, topic.message_thread_id)
        await bot_instance.send_message(
            ADMIN_GROUP_ID,
            "<b>Создан новый диалог</b>\nИспользуйте /info",
            message_thread_id=topic.message_thread_id
        )
        return topic.message_thread_id
    except Exception as e:
        logger.error(f"Topic error: {e}")
        return None

# ──────────────────────────────────────────────────────────────
# ХЕНДЛЕРЫ
# ──────────────────────────────────────────────────────────────

@dp.message(F.chat.type == "private", CommandStart())
async def cmd_start(message: types.Message, bot: Bot):
    await create_user(message.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Канал", url="https://t.me/Canal_BotRuEs"),
        InlineKeyboardButton(text="Правила", url="https://t.me/ApoyoTecnico_RuEsBot")
    ]])
    text = '⊹ ࣪ ˖ || Добро пожаловать в поддержку!\nНапишите ваше сообщение.'
    try:
        sent = await message.answer_photo(photo="https://i.yapx.ru/dY3rO.png", caption=text, reply_markup=kb)
        await bot.pin_chat_message(message.chat.id, sent.message_id)
    except:
        await message.answer(text, reply_markup=kb)

@dp.message(F.chat.type == "private")
async def user_message(message: types.Message, bot: Bot):
    # Игнорируем команды от обычных пользователей, если это не /start
    if message.text and message.text.startswith("/"): return
    
    # Игнорируем сообщения, если юзер пишет боту, находясь в состоянии рассылки
    current_state = await dp.fsm.get_context(bot, message.chat.id, message.from_user.id).get_state()
    if current_state == BroadcastState.waiting_for_message.state:
        return

    user = await get_user_by_id(message.from_user.id)
    if not user:
        await create_user(message.from_user.id)
        user = await get_user_by_id(message.from_user.id)
    if user['is_banned']: return

    t_id = await ensure_topic(message.from_user.id, bot)
    if not t_id: return await message.answer("Ошибка связи.")

    reply_id = None
    if message.reply_to_message:
        m_map = await get_map_by_user_msg(message.reply_to_message.message_id)
        if m_map: reply_id = m_map['admin_msg_id']

    try:
        sent = await message.copy_to(ADMIN_GROUP_ID, message_thread_id=t_id, reply_to_message_id=reply_id)
        await save_msg_map(message.from_user.id, message.message_id, sent.message_id)
        confirm = await message.answer('✔️ Отправлено!')
        await asyncio.sleep(2); await confirm.delete()
    except Exception as e: logger.error(f"Forward error: {e}")

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("stats"))
async def cmd_stats(message: types.Message):
    if message.from_user.id != OWNER_ID: return
    t, b = await get_stats_data()
    await message.reply(f"Юзеров: {t}\nВ бане: {b}")

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("info"))
async def cmd_info(message: types.Message):
    u = await get_user_by_topic(message.message_thread_id)
    if not u: return await message.reply("Юзер не найден.")
    await message.reply(f"ID: <code>{u['user_id']}</code>\nВарны: {u['warns']}/3\nБан: {u['is_banned']}")

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("ban"))
async def cmd_ban(message: types.Message, bot: Bot):
    u = await get_user_by_topic(message.message_thread_id)
    if u:
        await update_ban(u['user_id'], True)
        await message.reply("Пользователь забанен.")
        try: await bot.send_message(u['user_id'], "🚫 Вы заблокированы.")
        except: pass

# ──────────────────────────────────────────────────────────────
# РАССЫЛКА
# ──────────────────────────────────────────────────────────────
@dp.message(Command("broadcast"))
async def start_broadcast(message: types.Message, state: FSMContext):
    # Строгая проверка: только OWNER_ID
    if message.from_user.id != OWNER_ID:
        return
    
    await message.reply("Введите сообщение для рассылки (текст, фото, видео и т.д.) или /cancel для отмены:")
    await state.set_state(BroadcastState.waiting_for_message)

@dp.message(BroadcastState.waiting_for_message)
async def perform_broadcast(message: types.Message, state: FSMContext, bot: Bot):
    if message.text == "/cancel":
        await state.clear()
        return await message.reply("Рассылка отменена.")

    # Если вдруг сюда попал не овнер, подстраховываемся
    if message.from_user.id != OWNER_ID:
        await state.clear()
        return

    users = await get_all_users_ids()
    count = 0
    errors = 0
    
    await message.reply(f"Начинаю рассылку на {len(users)} пользователей...")

    for (user_id,) in users:
        try:
            # Копируем сообщение любого типа
            await message.copy_to(user_id)
            count += 1
            # Небольшая задержка, чтобы Telegram не забанил за спам
            await asyncio.sleep(0.05) 
        except Exception:
            errors += 1

    await state.clear()
    await message.reply(f"Рассылка завершена!\n✅ Успешно: {count}\n❌ Ошибок: {errors}")

# ──────────────────────────────────────────────────────────────
# ОБРАБОТКА ОТВЕТОВ АДМИНА
# ──────────────────────────────────────────────────────────────
@dp.message(F.chat.id == ADMIN_GROUP_ID)
async def admin_reply(message: types.Message, bot: Bot):
    if (message.text and message.text.startswith("/")) or not message.message_thread_id: return
    u = await get_user_by_topic(message.message_thread_id)
    if not u or u['is_banned']: return

    reply_id = None
    if message.reply_to_message:
        m_map = await get_map_by_admin_msg(message.reply_to_message.message_id)
        if m_map: reply_id = m_map['user_msg_id']

    try:
        sent = await message.copy_to(u['user_id'], reply_to_message_id=reply_id)
        await save_msg_map(u['user_id'], sent.message_id, message.message_id)
    except: await message.reply("❌ Ошибка доставки.")

@dp.edited_message()
async def edit_sync(message: types.Message, bot: Bot):
    if message.chat.type == "private":
        m_map = await get_map_by_user_msg(message.message_id)
        if m_map:
            try:
                if message.text: await bot.edit_message_text(message.text, ADMIN_GROUP_ID, m_map['admin_msg_id'])
                elif message.caption: await bot.edit_message_caption(ADMIN_GROUP_ID, m_map['admin_msg_id'], caption=message.caption)
            except: pass
    elif message.chat.id == ADMIN_GROUP_ID:
        m_map = await get_map_by_admin_msg(message.message_id)
        if m_map:
            try:
                if message.text: await bot.edit_message_text(message.text, m_map['user_id'], m_map['user_msg_id'])
                elif message.caption: await bot.edit_message_caption(m_map['user_id'], m_map['user_msg_id'], caption=message.caption)
            except: pass

# ──────────────────────────────────────────────────────────────
# ЗАПУСК
# ──────────────────────────────────────────────────────────────
async def main():
    session = _make_session()

    global bot
    bot = Bot(
        token=BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode="HTML")
    )

    await init_db()

    try:
        me = await bot.get_me()
        logger.info(f"Бот @{me.username} успешно запущен!")
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"Критическая ошибка при запуске: {e}")
    finally:
        await session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен")
