import asyncio
import logging
import aiosqlite
import io
import os
from datetime import datetime
from PIL import Image

# Библиотеки
from google import genai
from google.genai import types
from tavily import AsyncTavilyClient

# Aiogram 3.x
from aiogram import Bot, Dispatcher, types as aiog_types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.chat_action import ChatActionSender

# ========== КОНФИГУРАЦИЯ ==========
KEYS = {
    "TG": "8757845092:AAHK3sKNbzy-w7_4oBgHF7p9sYeNGxIXEic",
    "GEMINI": "AIzaSyDJAiv1aUdtqWv8qbMyvuXhfztCJTSQBtU",
    "TAVILY": "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"
}

DB_PATH = "simul_core.db"
MODEL_ID = "gemini-1.5-flash"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("SimulHybrid")

# ========== КЛИЕНТЫ ==========
gemini = genai.Client(api_key=KEYS["GEMINI"])
tavily = AsyncTavilyClient(api_key=KEYS["TAVILY"])

# ========== УМНЫЙ ПОИСК ==========
async def fetch_web_data(query: str) -> str:
    """Пытается найти данные. Если ошибка или пусто — возвращает пустую строку."""
    try:
        logger.info(f"🔎 Поиск в Tavily: {query}")
        # Таймаут 10 секунд, чтобы бот не завис на хостинге
        search = await asyncio.wait_for(
            tavily.search(query=query, search_depth="basic", max_results=3),
            timeout=10.0
        )
        results = search.get("results", [])
        if not results:
            return ""
        
        context = "\n--- [ДАННЫЕ ИЗ ИНТЕРНЕТА] ---\n"
        for r in results:
            context += f"• {r['title']}: {r['content']}\n"
        return context
    except Exception as e:
        logger.error(f"Tavily не ответил: {e}")
        return "" # Если Tavily упал, просто возвращаем пустоту

# ========== БАЗА ДАННЫХ ==========
async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY, 
                name TEXT, 
                history TEXT, 
                search_on INTEGER DEFAULT 1
            )""")
        await db.commit()

async def get_user_info(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT name, history, search_on FROM users WHERE id=?", (uid,)) as c:
            row = await c.fetchone()
            if row: return {"name": row[0], "history": row[1] or "", "search": bool(row[2])}
            return None

# ========== КЛАВИАТУРА ==========
def main_kb(search_active: bool):
    s_btn = "⏹ Поиск: ВЫКЛ" if search_active else "🔍 Поиск: ВКЛ"
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="ℹ️ Информация"), KeyboardButton(text="🗑 Сброс памяти")],
            [KeyboardButton(text=s_btn)],
            [KeyboardButton(text="⚙️ Сменить имя")]
        ], resize_keyboard=True
    )

bot = Bot(token=KEYS["TG"])
dp = Dispatcher(storage=MemoryStorage())

class Reg(StatesGroup):
    name = State()

# ========== ОБРАБОТЧИКИ ==========
@dp.message(Command("start"))
async def cmd_start(message: aiog_types.Message, state: FSMContext):
    await state.clear()
    user = await get_user_info(message.from_user.id)
    if user:
        await message.answer(f"🦾 Simul на связи. Я готов отвечать.", reply_markup=main_kb(user['search']))
    else:
        await message.answer("🦾 Протокол инициализации Simul.\nВведите ваше имя:")
        await state.set_state(Reg.name)

@dp.message(Reg.name)
async def process_reg(message: aiog_types.Message, state: FSMContext):
    name = message.text.strip()[:20] if message.text else "Командор"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO users (id, name, history, search_on) VALUES (?, ?, '', 1) ON CONFLICT(id) DO UPDATE SET name=?", 
                         (message.from_user.id, name, name))
        await db.commit()
    await state.clear()
    await message.answer(f"✅ Система активирована. Приветствую, {name}.", reply_markup=main_kb(True))

@dp.message(F.text.contains("Поиск:"))
async def toggle_search(message: aiog_types.Message):
    user = await get_user_info(message.from_user.id)
    new_v = 0 if user['search'] else 1
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET search_on=? WHERE id=?", (new_v, message.from_user.id))
        await db.commit()
    await message.answer(f"✅ Поиск {'ВКЛ' if new_v else 'ВЫКЛ'}.", reply_markup=main_kb(bool(new_v)))

@dp.message(F.text == "🗑 Сброс памяти")
async def clear_mem(message: aiog_types.Message):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET history='' WHERE id=?", (message.from_user.id,))
        await db.commit()
    await message.answer("🧠 Моя память очищена.")

@dp.message()
async def main_handler(message: aiog_types.Message, state: FSMContext):
    uid = message.from_user.id
    user = await get_user_info(uid)
    if not user: return

    # Игнор кнопок меню
    if message.text in ["ℹ️ Информация", "⚙️ Сменить имя"]:
        if message.text == "ℹ️ Информация":
            await message.answer(f"👤 Твое имя: {user['name']}\n🌍 Статус поиска: {'Активен' if user['search'] else 'Отключен'}")
        return

    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        query = message.text or message.caption or ""
        web_data = ""
        
        # 1. ТРИГГЕР ПОИСКА
        search_triggers = ["кто", "что", "где", "когда", "курс", "новости", "сколько"]
        if (user['search'] or any(t in query.lower() for t in search_triggers)) and query:
            web_data = await fetch_web_data(query)

        # 2. ИНСТРУКЦИЯ ДЛЯ ГИБРИДНОЙ ЛОГИКИ
        # Если web_data пустая, ИИ поймет это и ответит сам.
        sys_instr = (
            f"Ты — Simul, мощный ИИ-ассистент пользователя {user['name']}. "
            f"Сегодня: {datetime.now().strftime('%d.%m.%Y')}. "
            "ПРАВИЛО ОТВЕТА: Если ниже предоставлен блок 'ДАННЫЕ ИЗ ИНТЕРНЕТА', используй их. "
            "Если этот блок ПУСТОЙ или данных там недостаточно — отвечай максимально точно, используя свои собственные встроенные знания."
        )

        final_prompt = f"{sys_instr}\n\n[ДАННЫЕ ИЗ ИНТЕРНЕТА]:\n{web_data}\n\n[ИСТОРИЯ]:\n{user['history']}\n\n[ЗАПРОС]: {query}"

        try:
            # 3. ОБРАБОТКА ФОТО
            if message.photo:
                photo = message.photo[-1]
                p_file = await bot.get_file(photo.file_id)
                p_data = await bot.download_file(p_file.file_path)
                img = Image.open(p_data).convert("RGB")
                img.thumbnail((1024, 1024))
                img_io = io.BytesIO()
                img.save(img_io, format="JPEG", quality=85)
                
                resp = await gemini.aio.models.generate_content(
                    model=MODEL_ID,
                    contents=[f"{sys_instr}\n{query}", types.Part.from_bytes(data=img_io.getvalue(), mime_type="image/jpeg")]
                )
                answer = resp.text
            
            # 4. ОБРАБОТКА ТЕКСТА
            else:
                resp = await gemini.aio.models.generate_content(
                    model=MODEL_ID,
                    contents=final_prompt
                )
                answer = resp.text
        except Exception as e:
            logger.error(f"AI error: {e}")
            answer = "⚠️ Произошел технический сбой. Попробуйте еще раз."

        if answer:
            # Обновление истории
            new_h = (user['history'] + f"\nU: {query}\nA: {answer}")[-3000:]
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE users SET history=? WHERE id=?", (new_h, uid))
                await db.commit()
            
            # Если поиск реально что-то нашел — добавим маркер
            if web_data.strip():
                answer = "🌐 [Simul Search: Данные сети]\n\n" + answer
            else:
                answer = "🧠 [Simul Core: Внутренние знания]\n\n" + answer

            for i in range(0, len(answer), 4000):
                await message.answer(answer[i:i+4000], reply_markup=main_kb(user['search']))

async def main():
    await db_init()
    # Сброс вебхука для стабильности на хостинге
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("--- SIMUL HYBRID ENGINE STARTED ---")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())