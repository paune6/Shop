import asyncio
import logging
import aiosqlite
import io
import time
from PIL import Image
from google import genai
from google.genai import types
from openai import AsyncOpenAI

try:
    from duckduckgo_search import DDGS
except ImportError:
    logging.error("duckduckgo_search not installed")
    DDGS = None

from aiogram import Bot, Dispatcher, types as aiog_types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton
)
from aiogram.utils.chat_action import ChatActionSender

TG_TOKEN = "8757845092:AAHK3sKNbzy-w7_4oBgHF7p9sYeNGxIXEic"
GEMINI_API_KEY = "AIzaSyDJAiv1aUdtqWv8qbMyvuXhfztCJTSQBtU"
DEEPSEEK_API_KEY = "sk-4c6ad799713d41d8b22f614be5e02264"
DB_PATH = "simul_core.db"
MAX_IMAGE_SIZE_MB = 20

logging.basicConfig(level=logging.INFO)

gemini_client = genai.Client(api_key=GEMINI_API_KEY)
deepseek_client = AsyncOpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com/v1"
)

class Registration(StatesGroup):
    waiting_for_bot_name = State()

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, bot_name TEXT, history TEXT)"
        )
        await db.commit()

async def get_user_data(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT bot_name, history FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        if row:
            return {"bot_name": row[0], "history": row[1] or ""}
        return None

async def update_user(user_id, bot_name=None, history=None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, bot_name, history) VALUES (?, '', '')",
            (user_id,)
        )
        if bot_name is not None:
            await db.execute("UPDATE users SET bot_name = ? WHERE user_id = ?", (bot_name, user_id))
        if history is not None:
            await db.execute("UPDATE users SET history = ? WHERE user_id = ?", (history, user_id))
        await db.commit()

def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="ℹ️ Информация"), KeyboardButton(text="🗑 Очистить память")],
            [KeyboardButton(text="⚙️ Изменить имя"), KeyboardButton(text="❓ Помощь")],
            [KeyboardButton(text="Начать поиск"), KeyboardButton(text="Закончить поиск")],
            [KeyboardButton(text="📊 Статус системы")]
        ],
        resize_keyboard=True,
        one_time_keyboard=False
    )

bot = Bot(token=TG_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

search_users = set()

async def search_internet(query):
    """Поиск информации в интернете через DuckDuckGo"""
    if not DDGS:
        logging.warning("DDGS not available")
        return ""
    
    try:
        def perform_search():
            try:
                ddgs = DDGS()
                # Ограничиваем время поиска
                results = []
                for i, result in enumerate(ddgs.text(query, max_results=3)):
                    if i >= 3:
                        break
                    results.append(result)
                return results
            except Exception as e:
                logging.error(f"DDGS search error: {e}")
                return []
        
        results = await asyncio.to_thread(perform_search)
        
        if not results:
            return ""
        
        search_context = "📱 Информация из интернета:\n"
        count = 0
        
        for result in results:
            try:
                if not isinstance(result, dict):
                    continue
                    
                title = str(result.get('title', '')).strip()[:100]
                body = str(result.get('body', '')).strip()[:150]
                
                if not title or not body:
                    continue
                
                search_context += f"• {title}\n  {body}\n\n"
                count += 1
                
            except Exception as e:
                logging.error(f"Error processing search result: {e}")
                continue
        
        if count == 0:
            return ""
            
        return search_context
        
    except Exception as e:
        logging.error(f"Search internet error: {e}")
        return ""

async def ask_gemini(contents):
    try:
        def call_gemini():
            try:
                response = gemini_client.models.generate_content(
                    model="gemini-1.5-flash",
                    contents=contents
                )
                return response.text.strip() if response.text else None
            except Exception as e:
                logging.error(f"Gemini API error: {e}")
                return None
        
        result = await asyncio.to_thread(call_gemini)
        return result
    except Exception as e:
        logging.error(f"Gemini wrapper error: {e}")
        return None

async def ask_deepseek(messages):
    try:
        response = await asyncio.wait_for(
            deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=messages,
                temperature=0.7,
                max_tokens=1000
            ),
            timeout=10.0
        )
        return response.choices[0].message.content.strip() if response.choices else None
    except asyncio.TimeoutError:
        logging.error("DeepSeek request timed out")
        return None
    except Exception as e:
        logging.error(f"DeepSeek error: {e}")
        return None

@dp.message(Command("start"))
async def cmd_start(message: aiog_types.Message, state: FSMContext):
    user_data = await get_user_data(message.from_user.id)
    if user_data:
        await message.answer(
            f"Simul - BM 100\nВаш персональный ассистент {user_data['bot_name']} готов к выполнению задач.",
            reply_markup=get_main_keyboard()
        )
    else:
        await message.answer(
            "🦾 Протокол Инициализации Simul\n\nОтправьте имя, под которым вы хотите общаться с системой:"
        )
        await state.set_state(Registration.waiting_for_bot_name)

@dp.message(Command("help"))
async def cmd_help(message: aiog_types.Message):
    help_text = (
        "🤖 Справка Simul - BM 100\n\n"
        "/start – запуск\n/help – команды\n\n"
        "Кнопки:\n"
        "• Информация – статус\n"
        "• Очистить память – сброс истории\n"
        "• Изменить имя – задать новое имя\n"
        "• Помощь – эта справка\n"
        "• Начать/Закончить поиск – режим поиска\n"
        "• Статус системы – состояние ядра"
    )
    await message.answer(help_text)

@dp.message(Registration.waiting_for_bot_name)
async def process_reg(message: aiog_types.Message, state: FSMContext):
    if message.text:
        name = message.text.strip()[:20]
        await update_user(message.from_user.id, bot_name=name)
        await state.clear()
        await message.answer(
            f"✅ Синхронизация завершена.\n\nSimul - BM 100\nЯ — {name}.",
            reply_markup=get_main_keyboard()
        )
    else:
        await message.answer("Пожалуйста, отправьте текст для имени.")

@dp.message(F.text == "ℹ️ Информация")
async def btn_info(message: aiog_types.Message):
    user_data = await get_user_data(message.from_user.id)
    if user_data:
        await message.answer(
            f"🟢 Simul онлайн\nМодель: BM 100\nИмя: {user_data['bot_name']}\n"
            f"Память: {len(user_data['history'])} симв.",
            reply_markup=get_main_keyboard()
        )
    else:
        await message.answer("Система не инициализирована. Используйте /start.", reply_markup=get_main_keyboard())

@dp.message(F.text == "🗑 Очистить память")
async def btn_reset(message: aiog_types.Message):
    await update_user(message.from_user.id, history="")
    await message.answer("🧠 Память очищена.", reply_markup=get_main_keyboard())

@dp.message(F.text == "⚙️ Изменить имя")
async def btn_change_name(message: aiog_types.Message, state: FSMContext):
    await message.answer("Введите новое имя:", reply_markup=aiog_types.ReplyKeyboardRemove())
    await state.set_state(Registration.waiting_for_bot_name)

@dp.message(F.text == "❓ Помощь")
async def btn_help(message: aiog_types.Message):
    await cmd_help(message)

@dp.message(F.text == "📊 Статус системы")
async def btn_sys_status(message: aiog_types.Message):
    user_data = await get_user_data(message.from_user.id)
    if user_data:
        await message.answer(
            f"🟢 Simul онлайн\nМодель: BM 100\nИмя: {user_data['bot_name']}\n"
            f"Память: {len(user_data['history'])} симв.",
            reply_markup=get_main_keyboard()
        )
    else:
        await message.answer("Система не инициализирована. /start", reply_markup=get_main_keyboard())

@dp.message(F.text == "Начать поиск")
async def btn_start_search(message: aiog_types.Message):
    search_users.add(message.from_user.id)
    await message.answer(
        "🔍 Режим поиска активирован. Все ваши запросы теперь будут обрабатываться как поисковые.",
        reply_markup=get_main_keyboard()
    )

@dp.message(F.text == "Закончить поиск")
async def btn_end_search(message: aiog_types.Message):
    search_users.discard(message.from_user.id)
    await message.answer(
        "⏹ Режим поиска остановлен. Вы снова в обычном диалоге.",
        reply_markup=get_main_keyboard()
    )

@dp.message()
async def universal_handler(message: aiog_types.Message, state: FSMContext):
    if not message.photo and not message.text:
        return

    user_id = message.from_user.id
    user_data = await get_user_data(user_id)

    if not user_data:
        await message.answer(
            "⚠️ Сессия утеряна. Отправьте имя ассистента заново:",
            reply_markup=aiog_types.ReplyKeyboardRemove()
        )
        await state.set_state(Registration.waiting_for_bot_name)
        return

    bot_name = user_data["bot_name"]
    history = user_data["history"]
    in_search = user_id in search_users

    persona = f"Ты — Simul, персональный ассистент с именем {bot_name}."
    if in_search:
        persona += " Отвечай кратко и только по существу запроса."

    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        try:
            if message.photo:
                photo = message.photo[-1]
                file_info = await bot.get_file(photo.file_id)
                if file_info.file_size and file_info.file_size > MAX_IMAGE_SIZE_MB * 1024 * 1024:
                    await message.answer(f"❌ Изображение больше {MAX_IMAGE_SIZE_MB} МБ. Уменьшите размер.")
                    return

                file_io = io.BytesIO()
                await bot.download_file(file_info.file_path, file_io)
                file_io.seek(0)
                try:
                    img = Image.open(file_io)
                except Exception as e:
                    logging.error(f"Image processing error: {e}")
                    await message.answer("❌ Неподдерживаемый формат изображения.")
                    return

                user_text = message.caption if message.caption else "Что на этом фото?"
                context_text = (
                    f"{persona}\n\nИстория диалога:\n{history}\n\n"
                    f"Пользователь прислал фото и спрашивает: {user_text}"
                )
                contents = [context_text, types.Part.from_image(img)]
                ai_reply = await ask_gemini(contents)
                
                if not ai_reply:
                    ai_reply = "Извините, не удалось обработать изображение. Попробуйте позже."
                
                await message.answer(ai_reply, reply_markup=get_main_keyboard())
            else:
                user_text = message.text
                
                # Автоматический поиск в интернете для информационных запросов
                search_context = ""
                keywords = ["что", "как", "где", "когда", "почему", "какой", "новости", "информация", "событие", "последние", "актуальное", "сейчас"]
                should_search = any(keyword in user_text.lower() for keyword in keywords)
                
                if should_search or in_search:
                    try:
                        search_context = await search_internet(user_text)
                    except Exception as e:
                        logging.error(f"Search failed silently: {e}")
                        search_context = ""
                
                # Формируем контекст с поисковой информацией
                context_text = f"{persona}\n\n"
                
                if search_context:
                    context_text += f"{search_context}\n"
                
                context_text += (
                    f"История диалога:\n{history}\n\n"
                    f"Пользователь: {user_text}"
                )

                ai_reply = None
                
                # Пытаемся Gemini
                try:
                    ai_reply = await ask_gemini([context_text])
                except Exception as e:
                    logging.error(f"Gemini call failed: {e}")
                
                # Если Gemini не сработал, пытаемся DeepSeek
                if not ai_reply:
                    try:
                        messages = [
                            {"role": "system", "content": persona},
                        ]
                        if search_context:
                            messages.append({"role": "system", "content": search_context})
                        messages.append({"role": "user", "content": user_text})
                        
                        ai_reply = await ask_deepseek(messages)
                    except Exception as e:
                        logging.error(f"DeepSeek call failed: {e}")
                
                # Если оба не сработали, даем простой ответ
                if not ai_reply or ai_reply.strip() == "":
                    ai_reply = f"Привет! Я {bot_name}. Сейчас я обрабатываю вашу информацию. Пожалуйста, попробуйте ещё раз."

                # Сохраняем в историю только если не в режиме поиска
                if not in_search:
                    try:
                        new_history = (history + f"\nU: {user_text}\nA: {ai_reply}")[-4000:]
                        await update_user(user_id, history=new_history)
                    except Exception as e:
                        logging.error(f"History update failed: {e}")

                await message.answer(ai_reply, reply_markup=get_main_keyboard())

        except Exception as e:
            logging.exception(f"Error in universal handler: {e}")
            await message.answer(
                "⚠️ Произошла небольшая ошибка. Система восстанавливается...",
                reply_markup=get_main_keyboard()
            )

async def main():
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
