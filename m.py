import asyncio
import logging
import os
import aiosqlite
import datetime
import re
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.client.default import DefaultBotProperties

# --- КОНФИГУРАЦИЯ ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "8321504283:AAGfklTup3WR-FaccLQuIs1ST4Knrqd_hVY")
ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID", -1002752721634))
OWNER_ID = int(os.getenv("OWNER_ID", 6160978171))

if not BOT_TOKEN:
    exit("Ошибка: BOT_TOKEN не найден в .env")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
DB_NAME = "anon_chat.db"

# --- СОСТОЯНИЯ ---
class BroadcastState(StatesGroup):
    waiting_for_message = State()

# --- БАЗА ДАННЫХ ---
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                warns INTEGER DEFAULT 0,
                is_banned BOOLEAN DEFAULT 0,
                reg_date TEXT
            )
        """)
        await db.commit()

async def get_user_by_id(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        # Используем Row для обращения по именам колонок
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            return await cursor.fetchone()

async def create_user(user_id):
    reg_date = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, reg_date) VALUES (?, ?)", 
            (user_id, reg_date)
        )
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

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def extract_user_id(text: str):
    if not text: return None
    match = re.search(r"ID: (\d+)", text)
    return int(match.group(1)) if match else None

# --- ХЕНДЛЕРЫ ПОЛЬЗОВАТЕЛЕЙ ---

# Сначала обрабатываем команду Старт
@dp.message(F.chat.type == "private", CommandStart())
async def cmd_start(message: types.Message):
    await create_user(message.from_user.id)
    user = await get_user_by_id(message.from_user.id)

    if user and user['is_banned']:
        return # Ничего не отвечаем забаненным

    photo_url = "https://i.yapx.ru/dXrv6.png"
    text = (
        '⊹ ࣪ ˖ || Добро пожаловать в бот поддержки "Шⲩⲙ Кⲟⲥⲙⲟⲥⲁ"!✨ \n━━━━━━━━━━━━━━━━━━━━━━\n📝 Наш бот всегда готов помочь вам:\n• Ответим на ваши вопросы;\n• Поддержим в трудную минуту;\n• Предложим админа для общения если вам просто скучно.\n━━━━━━━━━━━━━━━━━━━━━━\n🔗 Наш канал: @Canal_BotRuEs\n🛠 Тех поддержка: @ApoyoTecnico_RuEsBot\n━━━━━━━━━━━━━━━━━━━━━━\n💌 Прочитайте правила и напишите ваше сообщение, мы обязательно ответим! Учтите, незнание правил не освобождает от ответственности.'
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
    except Exception as e:
        logger.error(f"Ошибка в cmd_start: {e}")
        await message.answer(text, reply_markup=keyboard)

# Затем обрабатываем все остальные сообщения в ЛС
@dp.message(F.chat.type == "private")
async def user_message(message: types.Message):
    # Если это любая другая команда, не обрабатываем её здесь
    if message.text and message.text.startswith("/"):
        return

    user = await get_user_by_id(message.from_user.id)
    if not user:
        await create_user(message.from_user.id)
        user = await get_user_by_id(message.from_user.id)

    if user['is_banned']:
        return

    # Заголовок сообщения для админов (важно: кавычки в f-строке)
    header = (
        f'<tg-emoji emoji-id="5429226690964374907">⭐️</tg-emoji> <b>Новое сообщение</b>\n'
        f'<tg-emoji emoji-id="5467538555158943525">💭</tg-emoji> От: {message.from_user.mention_html()}\n'
        f'<tg-emoji emoji-id="5206607081334906820">✔️</tg-emoji> ID: <code>{message.from_user.id}</code>\n'
        f'<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> Варнов: {user["warns"]}/3\n'
        f'--------------------------'
    )

    try:
        info_msg = await bot.send_message(ADMIN_GROUP_ID, header)
        await message.copy_to(chat_id=ADMIN_GROUP_ID, reply_to_message_id=info_msg.message_id)

        sent_confirm = await message.answer('<tg-emoji emoji-id="5206607081334906820">✔️</tg-emoji> Отправлено!')
        await asyncio.sleep(3)
        await sent_confirm.delete()
    except Exception as e:
        logger.error(f"Ошибка пересылки: {e}")

# --- ХЕНДЛЕРЫ АДМИНОВ ---

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("ban"))
async def cmd_ban(message: types.Message):
    if not message.reply_to_message:
        return await message.reply("Ответьте на сообщение пользователя!")

    user_id = extract_user_id(message.reply_to_message.text or message.reply_to_message.caption or "")
    if not user_id: return

    await update_ban(user_id, True)
    await message.reply(f'<tg-emoji emoji-id="5240241223632954241">🚫</tg-emoji> Юзер <code>{user_id}</code> <b>ЗАБАНЕН</b>.')
    try:
        await bot.send_message(user_id, '<tg-emoji emoji-id="5240241223632954241">🚫</tg-emoji> Вы заблокированы.')
    except:
        pass

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("warn"))
async def cmd_warn(message: types.Message):
    if not message.reply_to_message: return
    user_id = extract_user_id(message.reply_to_message.text or message.reply_to_message.caption or "")
    if not user_id: return

    user = await get_user_by_id(user_id)
    new_warns = user['warns'] + 1
    await update_warns(user_id, new_warns)

    if new_warns >= 3:
        await update_ban(user_id, True)
        await message.reply(f'<tg-emoji emoji-id="5240241223632954241">🚫</tg-emoji> 3/3 варна. Юзер <code>{user_id}</code> забанен.')
    else:
        await message.reply(f'<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> Варн ({new_warns}/3) для <code>{user_id}</code>')

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("stats"))
async def cmd_stats(message: types.Message):
    if message.from_user.id != OWNER_ID: return
    total, banned = await get_stats_data()
    await message.reply(f'<tg-emoji emoji-id="5467538555158943525">💭</tg-emoji> <b>Статистика</b>\nЮзеров: {total}\nВ бане: {banned}')

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("broadcast"))
async def cmd_broadcast(message: types.Message, state: FSMContext):
    if message.from_user.id != OWNER_ID: return
    await message.reply('Введите текст рассылки:')
    await state.set_state(BroadcastState.waiting_for_message)

@dp.message(F.chat.id == ADMIN_GROUP_ID, BroadcastState.waiting_for_message)
async def process_broadcast(message: types.Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        return await message.reply("Отмена.")

    users = await get_all_users_ids()
    good, bad = 0, 0
    for u in users:
        try:
            await message.copy_to(chat_id=u[0])
            good += 1
            await asyncio.sleep(0.05)
        except:
            bad += 1
    await message.reply(f'<tg-emoji emoji-id="5206607081334906820">✔️</tg-emoji> Успешно: {good}, Ошибок: {bad}')
    await state.clear()

@dp.message(F.chat.id == ADMIN_GROUP_ID, F.reply_to_message)
async def admin_reply(message: types.Message):
    if message.text and message.text.startswith("/"):
        return 

    target_text = message.reply_to_message.text or message.reply_to_message.caption or ""
    user_id = extract_user_id(target_text)

    if not user_id:
        return 

    try:
        await message.copy_to(chat_id=user_id)
        await message.react([types.ReactionTypeEmoji(emoji="🕊")])
    except Exception:
        await message.reply('<tg-emoji emoji-id="5416076321442777828">❌</tg-emoji> Не удалось доставить.')

# --- ЗАПУСК ---
async def main():
    await init_db()
    # Очищаем очередь обновлений, чтобы бот не «захлебнулся» старыми нажатиями Старт
    await bot.delete_webhook(drop_pending_updates=True)
    print("Бот успешно запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен")