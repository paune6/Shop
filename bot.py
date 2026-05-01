import asyncio
import logging
import aiosqlite
import io
from datetime import datetime
from PIL import Image

# Библиотеки ИИ
from google import genai
from google.genai import types
from duckduckgo_search import DDGS

# Aiogram
from aiogram import Bot, Dispatcher, types as aiog_types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.chat_action import ChatActionSender

# ========== КОНФИГУРАЦИЯ ==========
API_KEYS = {
    "TG": "8757845092:AAHK3sKNbzy-w7_4oBgHF7p9sYeNGxIXEic",
    "GEMINI": "AIzaSyDJAiv1aUdtqWv8qbMyvuXhfztCJTSQBtU",
    "TAVILY": "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"
}

DB_PATH = "simul_core.db"
GEMINI_MODEL = "gemini-1.5-flash"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("SimulBM100")

# ========== КЛИЕНТЫ ==========
client = genai.Client(api_key=API_KEYS["GEMINI"])

try:
    from tavily import AsyncTavilyClient
    tavily_api = AsyncTavilyClient(api_key=API_KEYS["TAVILY"])
except:
    tavily_api = None

# ========== БАЗА ДАННЫХ ==========
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY, 
                name TEXT, 
                history TEXT, 
                search_on INTEGER DEFAULT 0
            )""")
        await db.commit()

async def get_user_data(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT name, history, search_on FROM users WHERE id=?", (uid,)) as c:
            row = await c.fetchone()
            if row:
                return {"name": row[0], "history": row[1] or "", "search_on": bool(row[2])}
            return None

# ========== МОДУЛЬ ПОИСКА ==========
def sync_ddg_search(query):
    """Синхронный поиск DuckDuckGo (запускается в потоке)"""
    try:
        with DDGS() as ddgs:
            results = [r for r in ddgs.text(query, max_results=3)]
            return "\n".join([f"- {r['title']}: {r['body']}" for r in results])
    except:
        return ""

async def run_combined_search(query: str):
    """Сначала Tavily, потом DDG"""
    search_text = ""
    # 1. Пробуем официальный API Tavily
    if tavily_api:
        try:
            t_res = await tavily_api.search(query=query, search_depth="basic", max_results=3)
            for r in t_res.get("results", []):
                search_text += f"• {r['title']}: {r['content']}\n"
        except: pass
    
    # 2. Если пусто, пробуем DDG
    if not search_text:
        search_text = await asyncio.to_thread(sync_ddg_search, query)
    
    return search_text if search_text else "Информация в сети не найдена."

# ========== КЛАВИАТУРА ==========
def main_kb(search_status: bool):
    btn_search = "⏹ Поиск: ВЫКЛ" if search_status else "🔍 Поиск: ВКЛ"
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="ℹ️ Инфо"), KeyboardButton(text="🗑 Очистить память")],
            [KeyboardButton(text=btn_search)],
            [KeyboardButton(text="⚙️ Изменить имя")]
        ], resize_keyboard=True
    )

# ========== ЛОГИКА АЙОГРАМ ==========
bot = Bot(token=API_KEYS["TG"])
dp = Dispatcher(storage=MemoryStorage())

class Reg(StatesGroup):
    name = State()

@dp.message(Command("start"))
async def cmd_start(message: aiog_types.Message, state: FSMContext):
    await state.clear()
    user = await get_user_data(message.from_user.id)
    if user:
        await message.answer(f"🦾 Simul онлайн. Привет, {user['name']}!", reply_markup=main_kb(user['search_on']))
    else:
        await message.answer("🦾 Протокол инициализации Simul.\nВведите ваше имя:")
        await state.set_state(Reg.name)

@dp.message(Reg.name)
async def process_reg(message: aiog_types.Message, state: FSMContext):
    name = message.text.strip()[:20] if message.text else "User"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO users (id, name, history, search_on) VALUES (?, ?, '', 0) ON CONFLICT(id) DO UPDATE SET name=?", (message.from_user.id, name, name))
        await db.commit()
    await state.clear()
    await message.answer(f"✅ Принято, {name}. Я готов к работе.", reply_markup=main_kb(False))

@dp.message(F.text.contains("Поиск:"))
async def toggle_search(message: aiog_types.Message):
    user = await get_user_data(message.from_user.id)
    if not user: return
    new_val = 0 if user['search_on'] else 1
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET search_on=? WHERE id=?", (new_val, message.from_user.id))
        await db.commit()
    await message.answer(f"✅ Поиск {'ВКЛЮЧЕН' if new_val else 'ВЫКЛЮЧЕН'}.", reply_markup=main_kb(bool(new_val)))

@dp.message(F.text == "🗑 Очистить память")
async def clear_hist(message: aiog_types.Message):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET history='' WHERE id=?", (message.from_user.id,))
        await db.commit()
    await message.answer("🧠 Память очищена.")

@dp.message()
async def universal_handler(message: aiog_types.Message, state: FSMContext):
    uid = message.from_user.id
    user = await get_user_data(uid)
    if not user: return
    
    # Обработка кнопок Инфо и Имя
    if message.text == "ℹ️ Инфо":
        return await message.answer(f"🤖 Simul BM-100\n👤 Имя: {user['name']}\n🧠 Память: {len(user['history'])} симв.")
    if message.text == "⚙️ Изменить имя":
        await message.answer("Введите новое имя:")
        return await state.set_state(Reg.name)

    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        user_input = message.text or message.caption or ""
        search_data = ""
        
        # Умный запуск поиска
        if user['search_on'] or any(word in user_input.lower() for word in ["кто", "что", "где", "когда", "новости", "курс"]):
            if user_input: search_data = await run_combined_search(user_input)

        # Инструкция для ИИ
        sys_prompt = f"Ты — Simul, ассистент пользователя {user['name']}. Будь полезен и точен."
        full_context = f"{sys_prompt}\n\nИстория:\n{user['history']}\n\nДАННЫЕ ИЗ СЕТИ:\n{search_data}\n\nЗапрос: {user_input}\nОтвет:"

        try:
            # Если ФОТО
            if message.photo:
                photo = message.photo[-1]
                p_file = await bot.get_file(photo.file_id)
                p_data = await bot.download_file(p_file.file_path)
                img = Image.open(p_data).convert("RGB")
                img.thumbnail((1024, 1024))
                img_io = io.BytesIO()
                img.save(img_io, format="JPEG", quality=80)
                
                res = await client.aio.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=[f"{sys_prompt}\n{user_input}", types.Part.from_bytes(data=img_io.getvalue(), mime_type="image/jpeg")]
                )
                ai_reply = res.text
            # Если ТЕКСТ
            else:
                res = await client.aio.models.generate_content(model=GEMINI_MODEL, contents=full_context)
                ai_reply = res.text
        except Exception as e:
            logger.error(f"AI Error: {e}")
            ai_reply = "⚠️ Модель временно недоступна. Попробуйте переформулировать запрос."

        if ai_reply:
            # Сохранение истории (последние 3000 симв)
            new_h = (user['history'] + f"\nU: {user_input}\nA: {ai_reply}")[-3000:]
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE users SET history=? WHERE id=?", (new_h, uid))
                await db.commit()
            
            # Отправка
            final_resp = ("🌐 [SEARCH MODE]\n\n" + ai_reply) if search_data else ai_reply
            for i in range(0, len(final_resp), 4000):
                await message.answer(final_resp[i:i+4000], reply_markup=main_kb(user['search_on']))

async def main():
    await init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("--- БОТ ЗАПУЩЕН ---")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except:
        pass