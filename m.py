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

class LazyProxySession(AiohttpSession):
    """Ленивая сессия с SOCKS5-прокси. Коннектор создается только внутри работающего event loop."""
    def __init__(self, proxy_url: str):
        super().__init__(timeout=40.0)
        self._proxy_url = proxy_url
        self._connector = None

    async def create_session(self) -> aiohttp.ClientSession:
        # Инициализация происходит уже внутри асинхронного контекста
        if self._session is None or self._session.closed:
            clean = self._proxy_url.replace("socks5h://", "socks5://")
            self._connector = ProxyConnector.from_url(clean, rdns=True)
            self._session = aiohttp.ClientSession(
                connector=self._connector,
                json_serialize=self.json_dumps,
                timeout=aiohttp.ClientTimeout(total=40, connect=15),
            )
        return self._session

    async def close(self):
        """Закрываем сессию и коннектор"""
        if self._session and not self._session.closed:
            await self._session.close()
        if self._connector and hasattr(self._connector, 'close'):
            await self._connector.close()

def create_bot_session() -> AiohttpSession:
    """Синхронная обертка для создания нужного типа сессии."""
    proxy_url = os.getenv("TG_PROXY_URL", "").strip()
    
    if not proxy_url or not _SOCKS_OK:
        if proxy_url and not _SOCKS_OK:
            logging.warning("aiohttp-socks не установлен, прокси отключён.")
        return AiohttpSession(timeout=40.0)
    
    return LazyProxySession(proxy_url)

# ──────────────────────────────────────────────────────────────
# Конфигурация Бота
# ──────────────────────────────────────────────────────────────
load_dotenv()

# Токен и другие чувствительные данные берутся строго из .env
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID", -1002752721634))
OWNER_ID = int(os.getenv("OWNER_ID", 6160978171)) 

if not BOT_TOKEN:
    exit("Ошибка: BOT_TOKEN не найден в файле .env")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(
    token=BOT_TOKEN, 
    session=create_bot_session(), 
    default=DefaultBotProperties(parse_mode="HTML")
)
dp = Dispatcher()
DB_NAME = "anon_chat.db"

# Инициализация состояний для рассылки (ИСПРАВЛЕНИЕ ОШИБКИ)
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
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, reg_date) VALUES (?, ?)", 
            (user_id, reg_date)
        )
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
        await db.execute(
            "INSERT INTO messages (user_id, user_msg_id, admin_msg_id) VALUES (?, ?, ?)",
            (user_id, user_msg_id, admin_msg_id)
        )
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
# Логика работы с топиками
# ──────────────────────────────────────────────────────────────
async def ensure_topic(user_id):
    user = await get_user_by_id(user_id)
    if user and user.get('topic_id'):
        return user['topic_id']
    
    try:
        topic = await bot.create_forum_topic(ADMIN_GROUP_ID, name=f"Анонимный диалог")
        topic_id = topic.message_thread_id
        await update_user_topic(user_id, topic_id)
        
        header = (
            f'<tg-emoji emoji-id="5429226690964374907">⭐️</tg-emoji> <b>Создан новый диалог</b>\n'
            f'Для информации об авторе используйте команду /info'
        )
        await bot.send_message(ADMIN_GROUP_ID, header, message_thread_id=topic_id)
        return topic_id
    except Exception as e:
        logger.error(f"Не удалось создать топик: {e}")
        return None


# ──────────────────────────────────────────────────────────────
# ХЕНДЛЕРЫ ПОЛЬЗОВАТЕЛЕЙ (ЛС)
# ──────────────────────────────────────────────────────────────
@dp.message(F.chat.type == "private", CommandStart())
async def cmd_start(message: types.Message):
    await create_user(message.from_user.id)
    user = await get_user_by_id(message.from_user.id)
    if user and user['is_banned']:
        return 

    photo_url = "https://i.yapx.ru/dY3rO.png"
    text = (
        '⊹ ࣪ ˖ || Добро пожаловать в бот поддержки "Шⲩⲙ Кⲟⲥⲙⲟⲥⲁ"!✨ \n━━━━━━━━━━━━━━━━━━━━━━\n'
        '📝 Наш бот всегда готов помочь вам:\n• Ответим на ваши вопросы;\n'
        '• Поддержим в трудную минуту;\n• Предложим админа для общения если вам просто скучно.\n'
        '━━━━━━━━━━━━━━━━━━━━━━\n💌 Прочитайте правила и напишите ваше сообщение!'
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Канал", url="https://t.me/Canal_BotRuEs"),
            InlineKeyboardButton(text="Правила", url="https://t.me/ApoyoTecnico_RuEsBot")
        ]
    ])

    try:
        sent_msg = await message.answer_photo(photo=photo_url, caption=text, reply_markup=keyboard)
        await bot.pin_chat_message(chat_id=message.chat.id, message_id=sent_msg.message_id)
    except Exception:
        await message.answer(text, reply_markup=keyboard)


@dp.message(F.chat.type == "private")
async def user_message(message: types.Message):
    if message.text and message.text.startswith("/"):
        return

    user = await get_user_by_id(message.from_user.id)
    if not user:
        await create_user(message.from_user.id)
        user = await get_user_by_id(message.from_user.id)
    if user['is_banned']: return

    topic_id = await ensure_topic(message.from_user.id)
    if not topic_id:
        return await message.answer("Ошибка связи с сервером поддержки.")

    reply_to_admin_msg_id = None
    if message.reply_to_message:
        msg_map = await get_map_by_user_msg(message.reply_to_message.message_id)
        if msg_map:
            reply_to_admin_msg_id = msg_map['admin_msg_id']

    try:
        sent_msg = await message.copy_to(
            chat_id=ADMIN_GROUP_ID, 
            message_thread_id=topic_id, 
            reply_to_message_id=reply_to_admin_msg_id
        )
        await save_msg_map(message.from_user.id, message.message_id, sent_msg.message_id)
        
        sent_confirm = await message.answer('<tg-emoji emoji-id="5206607081334906820">✔️</tg-emoji> Отправлено!')
        await asyncio.sleep(2)
        await sent_confirm.delete()
    except Exception as e:
        logger.error(f"Ошибка пересылки: {e}")


# ──────────────────────────────────────────────────────────────
# ХЕНДЛЕРЫ АДМИНОВ (Команды - ДОЛЖНЫ БЫТЬ ВЫШЕ обычных сообщений)
# ──────────────────────────────────────────────────────────────

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("stats"))
async def cmd_stats(message: types.Message):
    if message.from_user.id != OWNER_ID: 
        return await message.reply("У вас нет прав для этой команды.")
    
    total, banned = await get_stats_data()
    await message.reply(f'<tg-emoji emoji-id="5467538555158943525">💭</tg-emoji> <b>Статистика</b>\nЮзеров: {total}\nВ бане: {banned}')


@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("broadcast"))
async def cmd_broadcast(message: types.Message, state: FSMContext):
    if message.from_user.id != OWNER_ID: 
        return await message.reply("У вас нет прав для этой команды.")
    
    await message.reply('Введите текст рассылки (для отмены введите /cancel):')
    await state.set_state(BroadcastState.waiting_for_message)


@dp.message(F.chat.id == ADMIN_GROUP_ID, BroadcastState.waiting_for_message)
async def process_broadcast(message: types.Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        return await message.reply("Рассылка отменена.")

    users = await get_all_users_ids()
    good, bad = 0, 0
    await message.reply("Начинаю рассылку...")
    
    for u in users:
        try:
            await message.copy_to(chat_id=u[0])
            good += 1
            await asyncio.sleep(0.05) # Защита от флудлимита
        except Exception:
            bad += 1
            
    await message.reply(f'<tg-emoji emoji-id="5206607081334906820">✔️</tg-emoji> Успешно: {good}, Ошибок: {bad}')
    await state.clear()


@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("info"))
async def cmd_info(message: types.Message):
    user = await get_user_by_topic(message.message_thread_id)
    if not user:
        return await message.reply("Это не топик пользователя или он не найден.")
    
    info_text = (
        f"<b>Информация об анониме:</b>\n\n"
        f"ID: <code>{user['user_id']}</code>\n"
        f"Предупреждения (Варны): {user['warns']}/3\n"
        f"Бан: {'Да' if user['is_banned'] else 'Нет'}\n"
        f"Дата начала: {user['reg_date']}"
    )
    await message.reply(info_text)


@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("ban"))
async def cmd_ban(message: types.Message):
    user = await get_user_by_topic(message.message_thread_id)
    if not user: return await message.reply("Используйте в топике юзера.")

    await update_ban(user['user_id'], True)
    await message.reply(f'<tg-emoji emoji-id="5240241223632954241">🚫</tg-emoji> Этот пользователь <b>ЗАБАНЕН</b>.')
    try:
        await bot.send_message(user['user_id'], '<tg-emoji emoji-id="5240241223632954241">🚫</tg-emoji> Вы заблокированы администрацией.')
    except: pass


@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("unban"))
async def cmd_unban(message: types.Message):
    user = await get_user_by_topic(message.message_thread_id)
    if not user: return await message.reply("Используйте в топике юзера.")

    await update_ban(user['user_id'], False)
    await update_warns(user['user_id'], 0)
    
    await message.reply(f'<tg-emoji emoji-id="5206607081334906820">✔️</tg-emoji> Пользователь <b>РАЗБЛОКИРОВАН</b>, варны обнулены.')
    try:
        await bot.send_message(user['user_id'], '<tg-emoji emoji-id="5206607081334906820">✔️</tg-emoji> Вы были разблокированы!')
    except: pass


@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("warn"))
async def cmd_warn(message: types.Message):
    user = await get_user_by_topic(message.message_thread_id)
    if not user: return await message.reply("Используйте в топике юзера.")

    new_warns = user['warns'] + 1
    await update_warns(user['user_id'], new_warns)

    if new_warns >= 3:
        await update_ban(user['user_id'], True)
        await message.reply(f'<tg-emoji emoji-id="5240241223632954241">🚫</tg-emoji> 3/3 варна. Пользователь автоматически забанен.')
        try:
            await bot.send_message(user['user_id'], '🚫 Вы получили 3-е предупреждение и были заблокированы.')
        except: pass
    else:
        await message.reply(f'<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> Выдан варн ({new_warns}/3)')
        try:
            await bot.send_message(user['user_id'], f'⚠️ Вам выдано предупреждение ({new_warns}/3). Будьте вежливы!')
        except: pass


@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("unwarn"))
async def cmd_unwarn(message: types.Message):
    user = await get_user_by_topic(message.message_thread_id)
    if not user: return await message.reply("Используйте в топике юзера.")

    if user['warns'] == 0:
        return await message.reply("У пользователя 0 варнов.")

    new_warns = user['warns'] - 1
    await update_warns(user['user_id'], new_warns)
    
    if user['is_banned'] and new_warns < 3:
        await update_ban(user['user_id'], False)
        status = "и РАЗБАНЕН"
    else:
        status = ""

    await message.reply(f'✅ Варн снят ({new_warns}/3) {status}')
    try:
        await bot.send_message(user['user_id'], f'✅ С вас сняли одно предупреждение ({new_warns}/3).')
    except: pass


@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("del"))
async def cmd_delete_msg(message: types.Message):
    """Удаление конкретного сообщения у юзера через ответ на него в топике."""
    if not message.reply_to_message: 
        return await message.reply("Ответьте на сообщение, которое нужно удалить.")
        
    msg_map = await get_map_by_admin_msg(message.reply_to_message.message_id)
    if msg_map:
        try:
            await bot.delete_message(msg_map['user_id'], msg_map['user_msg_id'])
            await message.reply_to_message.delete()
            await message.delete()
        except TelegramAPIError:
            await message.reply("Не удалось удалить у пользователя (возможно, прошло более 48 часов).")
    else:
        await message.reply("Сообщение не найдено в базе данных.")


# ──────────────────────────────────────────────────────────────
# ХЕНДЛЕР-ЛОВЕЦ (Обычные ответы админов — должен быть ВНИЗУ)
# ──────────────────────────────────────────────────────────────

@dp.message(F.chat.id == ADMIN_GROUP_ID)
async def admin_reply(message: types.Message, state: FSMContext):
    """Пересылка обычных сообщений из топика пользователю в ЛС."""
    # Защита: если это команда, которую мы не обработали выше (например, опечатка), просто игнорим
    if message.text and message.text.startswith("/"): return 
    if not message.message_thread_id: return 

    user = await get_user_by_topic(message.message_thread_id)
    if not user: return 
    
    if user['is_banned']: 
        return await message.reply("Пользователь заблокирован. Команды: /unban или /unwarn")

    reply_to_user_msg_id = None
    if message.reply_to_message:
        msg_map = await get_map_by_admin_msg(message.reply_to_message.message_id)
        if msg_map:
            reply_to_user_msg_id = msg_map['user_msg_id']

    try:
        sent_msg = await message.copy_to(
            chat_id=user['user_id'],
            reply_to_message_id=reply_to_user_msg_id
        )
        await save_msg_map(user['user_id'], sent_msg.message_id, message.message_id)
    except Exception:
        await message.reply('❌ Не удалось доставить сообщение. Возможно, пользователь заблокировал бота.')


# ──────────────────────────────────────────────────────────────
# РЕДАКТИРОВАНИЕ И РЕАКЦИИ (Синхронизация)
# ──────────────────────────────────────────────────────────────
@dp.edited_message()
async def edit_sync(message: types.Message):
    if message.chat.type == "private":
        msg_map = await get_map_by_user_msg(message.message_id)
        if msg_map:
            try:
                if message.text:
                    await bot.edit_message_text(message.text, ADMIN_GROUP_ID, msg_map['admin_msg_id'], entities=message.entities)
                elif message.caption:
                    await bot.edit_message_caption(ADMIN_GROUP_ID, msg_map['admin_msg_id'], caption=message.caption, caption_entities=message.caption_entities)
            except TelegramAPIError: pass
    elif message.chat.id == ADMIN_GROUP_ID:
        msg_map = await get_map_by_admin_msg(message.message_id)
        if msg_map:
            try:
                if message.text:
                    await bot.edit_message_text(message.text, msg_map['user_id'], msg_map['user_msg_id'], entities=message.entities)
                elif message.caption:
                    await bot.edit_message_caption(msg_map['user_id'], msg_map['user_msg_id'], caption=message.caption, caption_entities=message.caption_entities)
            except TelegramAPIError: pass


@dp.message_reaction()
async def reaction_sync(reaction: types.MessageReactionUpdated):
    if reaction.chat.type == "private":
        msg_map = await get_map_by_user_msg(reaction.message_id)
        if msg_map:
            try:
                await bot.set_message_reaction(ADMIN_GROUP_ID, msg_map['admin_msg_id'], reaction.new_reaction)
            except TelegramAPIError: pass
    elif reaction.chat.id == ADMIN_GROUP_ID:
        msg_map = await get_map_by_admin_msg(reaction.message_id)
        if msg_map:
            try:
                await bot.set_message_reaction(msg_map['user_id'], msg_map['user_msg_id'], reaction.new_reaction)
            except TelegramAPIError: pass


# ──────────────────────────────────────────────────────────────
# ЗАПУСК
# ──────────────────────────────────────────────────────────────
async def main():
    await init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    print("Бот успешно запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен")
