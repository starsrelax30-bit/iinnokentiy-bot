import os
import time
import sqlite3
import threading
import requests
import io
from datetime import datetime, timedelta
from urllib.parse import quote
from flask import Flask
from waitress import serve
import telebot
from telebot import types
from groq import Groq

# Получаем секретные ключи из переменных окружения
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))

SUPPORT_ADMIN = os.environ.get("SUPPORT_ADMIN", "@admin")
SUPPORT_CHANNEL = os.environ.get("SUPPORT_CHANNEL", "https://t.me/telegram")

# Инициализируем клиента нейросети и самого бота
client = Groq(api_key=GROQ_API_KEY)
bot = telebot.TeleBot(TELEGRAM_TOKEN)
# --- 2. ВЕБ-СЕРВЕР ДЛЯ RENDER (24/7) ---
app = Flask(__name__)

@app.route('/')
def home():
    return "ИИННОКЕНТИЙ работает 24/7!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    serve(app, host='0.0.0.0', port=port)

# --- 3. БАЗА ДАННЫХ SQLite И ПРОВЕРКА СТАТУСОВ ---
DB_FILE = "bot_database.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # Таблица пользователей
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            has_text_vip INTEGER DEFAULT 0,
            has_image_vip INTEGER DEFAULT 0,
            vip_until TEXT,
            ai_mode TEXT DEFAULT 'general',
            referred_by INTEGER DEFAULT 0,
            referrals_count INTEGER DEFAULT 0
        )
    ''')
    # Таблица промокодов
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS promo_codes (
            code TEXT PRIMARY KEY,
            reward_type TEXT,
            uses_left INTEGER DEFAULT 1
        )
    ''')
    conn.commit()
    conn.close()

def check_and_update_vip(user_id):
    """Проверяет срок действия VIP и сбрасывает его при истечении"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT vip_until, has_text_vip, has_image_vip FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    
    if row and row[0]:
        vip_until_dt = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
        if datetime.now() > vip_until_dt:
            cursor.execute("UPDATE users SET has_text_vip = 0, has_image_vip = 0, vip_until = NULL WHERE user_id = ?", (user_id,))
            conn.commit()
            conn.close()
            return False, False, None
        return bool(row[1]), bool(row[2]), row[0]
        
    conn.close()
    return bool(row[1]) if row else False, bool(row[2]) if row else False, None

def get_user(user_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT ai_mode, referrals_count, vip_until FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    
    has_text_vip, has_image_vip, vip_until = check_and_update_vip(user_id)
    
    if row:
        return {
            "text_vip": has_text_vip,
            "image_vip": has_image_vip,
            "vip_until": vip_until,
            "mode": row[0],
            "referrals_count": row[1]
        }
    return {"text_vip": False, "image_vip": False, "vip_until": None, "mode": "general", "referrals_count": 0}

def register_user_if_new(user_id, username, referred_by=None):
    """Регистрирует пользователя и выдает бесплатный 2-дневный VIP при старте"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    
    is_new = False
    if row is None:
        is_new = True
        vip_until = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        ref_b = referred_by if referred_by else 0
        
        cursor.execute(
            "INSERT INTO users (user_id, username, has_text_vip, has_image_vip, vip_until, referred_by) VALUES (?, ?, 1, 1, ?, ?)",
            (user_id, username, vip_until, ref_b)
        )
        
        if ref_b and ref_b != user_id:
            cursor.execute("UPDATE users SET referrals_count = referrals_count + 1 WHERE user_id = ?", (ref_b,))
    else:
        cursor.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user_id))
        
    conn.commit()
    conn.close()
    return is_new

def add_days_to_vip(user_id, days, text_vip=True, image_vip=True):
    """Продлевает VIP-статус на указанное количество дней"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT vip_until FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    
    now = datetime.now()
    if row and row[0]:
        current_until = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
        start_date = max(now, current_until)
    else:
        start_date = now
        
    new_until = (start_date + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    
    t_val = 1 if text_vip else 0
    i_val = 1 if image_vip else 0
    
    cursor.execute(
        "UPDATE users SET has_text_vip = ?, has_image_vip = ?, vip_until = ? WHERE user_id = ?",
        (t_val, i_val, new_until, user_id)
    )
    conn.commit()
    conn.close()

def get_all_users():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users")
    rows = cursor.fetchall()
    conn.close()
    return [r[0] for r in rows]

init_db()
# --- 4. РАБОТА С НЕЙРОСЕТЬЮ GROQ И КОНТЕКСТОМ ---

# Память бота (хранит историю диалогов в оперативной памяти)
user_context = {}

# Системные промпты для разных режимов работы бота
SYSTEM_PROMPTS = {
    "general": (
        "Ты — ИИННОКЕНТИЙ, мудрый, добрый и отзывчивый ИИ-помощник. "
        "Отвечай понятно, вежливо и информативно. "
        "Используй форматирование Markdown там, где это уместно."
    ),
    "code": (
        "Ты — профессиональный senior-разработчик и эксперт по программированию. "
        "Пиши чистый, оптимизированный и хорошо прокомментированный код. "
        "Объясняй сложные концепции простыми словами."
    ),
    "creative": (
        "Ты — творческий писатель, копирайтер и креативный мыслитель. "
        "Твой стиль ответов — яркий, метафоричный и увлекательный. "
        "Помогай генерировать уникальные идеи, тексты и сценарии."
    ),
    "expert": (
        "Ты — аналитик и ученый-эксперт. "
        "Давай максимально точные, структурированные, объективные и логичные ответы. "
        "Опирайся на факты и давай глубокий анализ."
    )
}

def get_ai_response(user_id, text, is_vip, mode="general"):
    """
    Отправляет запрос к API Groq с учетом контекста диалога и текущего режима
    """
    if user_id not in user_context:
        user_context[user_id] = []

    # Определяем системную роль ИИ в зависимости от режима
    system_instruction = SYSTEM_PROMPTS.get(mode, SYSTEM_PROMPTS["general"])
    
    # Для VIP-пользователей выбираем более мощную модель
    model_name = "llama-3.3-70b-versatile" if is_vip else "llama-3.1-8b-instant"

    # Формируем историю сообщений для отправки в Groq
    messages = [{"role": "system", "content": system_instruction}]
    
    # Ограничиваем историю: последние 10 сообщений для VIP, последние 4 для обычных
    max_history = 10 if is_vip else 4
    for item in user_context[user_id][-max_history:]:
        messages.append(item)

    # Добавляем новое сообщение пользователя
    messages.append({"role": "user", "content": text})

    try:
        completion = client.chat.completions.create(
            model=model_name,
            messages=messages,
            temperature=0.7,
            max_tokens=2048 if is_vip else 1024
        )
        answer = completion.choices[0].message.content

        # Сохраняем диалог в память
        user_context[user_id].append({"role": "user", "content": text})
        user_context[user_id].append({"role": "assistant", "content": answer})

        # Храним в памяти не более 20 последних сообщений на пользователя
        if len(user_context[user_id]) > 20:
            user_context[user_id] = user_context[user_id][-20:]

        return answer

    except Exception as e:
        return f"⚠️ Произошла ошибка при обращении к нейросети: {str(e)}"

# --- 5. ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЙ (POLLINATIONS AI) ---

def generate_image_url(prompt):
    """
    Генерирует прямой URL на изображение через Pollinations AI
    """
    encoded_prompt = quote(prompt)
    # Используем модель FLUX для генерации качественного изображения
    url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?model=flux&width=1024&height=1024&nologo=true"
    return url
# --- 6. КЛАВИАТУРЫ И ОБРАБОТЧИКИ КОМАНД ---

def get_main_keyboard(user_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    btn_ai = types.KeyboardButton("🤖 Задать вопрос ИИ")
    btn_image = types.KeyboardButton("🖼 Создать картинку")
    btn_modes = types.KeyboardButton("🎭 Режимы ИИ")
    btn_clear = types.KeyboardButton("🧹 Очистить диалог")
    btn_buy = types.KeyboardButton("⭐ VIP / Stars")
    btn_ref = types.KeyboardButton("👥 Рефералы")
    btn_info = types.KeyboardButton("ℹ️ Статус")
    btn_support = types.KeyboardButton("🛠 Поддержка")
    
    markup.add(btn_ai, btn_image)
    markup.add(btn_modes, btn_clear)
    markup.add(btn_buy, btn_ref)
    markup.add(btn_info, btn_support)
    
    if user_id == ADMIN_ID:
        markup.add(types.KeyboardButton("👑 Админ Панель"))
    return markup

@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.from_user.id
    username = message.from_user.username or "Anonymous"
    
    args = message.text.split()
    referred_by = None
    if len(args) > 1 and args[1].isdigit():
        ref_id = int(args[1])
        if ref_id != user_id:
            referred_by = ref_id

    is_new = register_user_if_new(user_id, username, referred_by=referred_by)

    if is_new:
        welcome_text = (
            f"Привет, {message.from_user.first_name}! 👋\n\n"
            "🎁 **Вам зачислен подарок!** Тестовый **VIP-доступ на 2 дня** активирован автоматически!\n\n"
            "Вам открыта самая сильная модель Llama 3.3 (70B) и генерация картинок 🎨\n\n"
            "Выбирайте раздел в меню ниже 👇"
        )
        if referred_by:
            try:
                bot.send_message(referred_by, "🎉 По вашей ссылке зарегистрировался новый пользователь!")
            except Exception:
                pass
    else:
        welcome_text = (
            f"С возвращением, {message.from_user.first_name}! 👋\n\n"
            "Я **ИИННОКЕНТИЙ** — твой личный ИИ-помощник.\n"
            "Задай мне любой вопрос или используй меню ниже 👇"
        )

    bot.send_message(message.chat.id, welcome_text, reply_markup=get_main_keyboard(user_id), parse_mode="Markdown")

@bot.message_handler(func=lambda message: True)
def handle_all_messages(message):
    user_id = message.from_user.id
    text = message.text
    user_data = get_user(user_id)

    if text == "🧹 Очистить диалог":
        if user_id in user_context:
            del user_context[user_id]
        bot.send_message(message.chat.id, "🧹 Память диалога очищена!")

    elif text == "🎭 Режимы ИИ":
        markup = types.InlineKeyboardMarkup()
        btn1 = types.InlineKeyboardButton("🌐 Обычный", callback_data="set_mode_general")
        btn2 = types.InlineKeyboardButton("💻 Программист", callback_data="set_mode_code")
        btn3 = types.InlineKeyboardButton("✍️ Копирайтер", callback_data="set_mode_creative")
        btn4 = types.InlineKeyboardButton("🔬 Эксперт", callback_data="set_mode_expert")
        markup.add(btn1, btn2)
        markup.add(btn3, btn4)
        bot.send_message(message.chat.id, f"Текущий режим: **{user_data['mode']}**\nВыберите новый:", reply_markup=markup, parse_mode="Markdown")

    elif text == "⭐ VIP / Stars":
        markup = types.InlineKeyboardMarkup()
        btn_text = types.InlineKeyboardButton("⭐️ VIP Доступ (30 дней) - 50 Stars", callback_data="buy_vip_30")
        markup.add(btn_text)
        bot.send_message(message.chat.id, "Продлить VIP-доступ через **Telegram Stars** (или введите промокод в профиле):", reply_markup=markup, parse_mode="Markdown")

    elif text == "👥 Рефералы":
        bot_info = bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start={user_id}"
        refs_count = user_data["referrals_count"]
        
        ref_text = (
            "👥 **Реферальная система**\n\n"
            "Приглашайте друзей по вашей ссылке:\n"
            f"• Приглашено друзей: **{refs_count} / 3**\n\n"
            f"🔗 Ваша ссылка:\n`{ref_link}`\n\n"
            "_За каждых 3 приглашенных друзей вы получаете +7 дней VIP-доступа!_"
        )
        bot.send_message(message.chat.id, ref_text, parse_mode="Markdown")

    elif text == "ℹ️ Статус":
        if user_data["text_vip"]:
            until_str = user_data["vip_until"] or "Бессрочно"
            status_text = f"✅ **Активирован** (до `{until_str}`)"
        else:
            status_text = "❌ **Истек (базовый тариф 8B)**"
            
        status_image = "✅ **Открыт**" if user_data["image_vip"] else "❌ **Закрыт**"
        
        info_text = (
            "ℹ️ **Ваш профиль**\n\n"
            f"• VIP Статус: {status_text}\n"
            f"• Доступ к картинкам: {status_image}\n"
            f"• Режим ИИ: `{user_data['mode']}`\n\n"
            f"🆔 Ваш ID: `{user_id}`"
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🎟 Ввести промокод", callback_data="enter_promo"))
        bot.send_message(message.chat.id, info_text, parse_mode="Markdown", reply_markup=markup)

    elif text == "👑 Админ Панель" and user_id == ADMIN_ID:
        users_count = len(get_all_users())
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📢 Сделать рассылку", callback_data="admin_broadcast"))
        markup.add(types.InlineKeyboardButton("🎟 Управление промокодами", callback_data="admin_promos"))
        bot.send_message(message.chat.id, f"👑 **Админ Панель**\n\nВсего пользователей: `{users_count}`", parse_mode="Markdown", reply_markup=markup)

    elif text == "🛠 Поддержка":
        msg = bot.send_message(message.chat.id, "💬 Опишите ваш вопрос одним сообщением:", parse_mode="Markdown")
        bot.register_next_step_handler(msg, send_support_request)

    elif text == "🖼 Создать картинку":
        if user_data["image_vip"]:
            msg = bot.send_message(message.chat.id, "🖌 **Опишите картинку:**", parse_mode="Markdown")
            bot.register_next_step_handler(msg, process_art_generation)
        else:
            bot.send_message(message.chat.id, "🔒 Ваш тестовый VIP истек. Продлите его в разделе **⭐ VIP / Stars** или введите промокод!", parse_mode="Markdown")

    elif text == "🤖 Задать вопрос ИИ":
        bot.send_message(message.chat.id, "Задавай любой вопрос прямо в чат!")

    else:
        try:
            bot.send_chat_action(message.chat.id, 'typing')
            answer = get_ai_response(user_id, text, user_data["text_vip"], user_data["mode"])
            prefix = "⭐️ [VIP ИИннокентий]:\n\n" if user_data["text_vip"] else "👴 [ИИннокентий]:\n\n"
            bot.reply_to(message, prefix + answer)
        except Exception as e:
            bot.reply_to(message, f"⚠️ Ошибка API: `{str(e)}`", parse_mode="Markdown")

# --- 7. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ И CALLBACK-ХЕНДЛЕРЫ ---

def send_support_request(message):
    user_id = message.from_user.id
    if ADMIN_ID == 0:
        bot.send_message(message.chat.id, "⚠️ Ошибка: ADMIN_ID не настроен.")
        return

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✉️ Ответить", callback_data=f"reply_to_{user_id}"))

    admin_msg = f"📩 **Новое обращение!**\n\n👤 Игрок: `{user_id}`\n💬 Сообщение:\n{message.text}"
    try:
        bot.send_message(ADMIN_ID, admin_msg, parse_mode="Markdown", reply_markup=markup)
        bot.send_message(message.chat.id, "✅ Ваше сообщение отправлено поддержке!")
    except Exception:
        bot.send_message(message.chat.id, "❌ Ошибка отправки сообщения администратору.")

def process_admin_reply(message, target_user_id):
    try:
        bot.send_message(target_user_id, f"🛠 **Ответ от Поддержки:**\n\n{message.text}", parse_mode="Markdown")
        bot.send_message(message.chat.id, "✅ Ответ успешно отправлен!")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка: `{str(e)}`", parse_mode="Markdown")

def admin_ask_promo_code(message):
    code = message.text.strip().upper()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO promo_codes (code, reward_type, uses_left) VALUES (?, 'vip_7_days', 10)", (code,))
    conn.commit()
    conn.close()
    bot.send_message(message.chat.id, f"✅ Промокод `{code}` успешно создан! Дает +7 дней VIP (10 активаций).", parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    user_id = call.from_user.id
    
    if call.data.startswith("set_mode_"):
        new_mode = call.data.replace("set_mode_", "")
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET ai_mode = ? WHERE user_id = ?", (new_mode, user_id))
        conn.commit()
        conn.close()
        
        if user_id in user_context:
            del user_context[user_id]
        bot.answer_callback_query(call.id, "Режим изменен!")
        bot.edit_message_text(f"✅ Режим изменен на: **{new_mode}**", call.message.chat.id, call.message.message_id, parse_mode="Markdown")

    elif call.data == "enter_promo":
        msg = bot.send_message(call.message.chat.id, "🎟 Введите ваш промокод:")
        bot.register_next_step_handler(msg, process_promo_code)

    elif call.data == "admin_broadcast" and user_id == ADMIN_ID:
        msg = bot.send_message(call.message.chat.id, "📢 Введите текст рассылки:")
        bot.register_next_step_handler(msg, process_broadcast)

    elif call.data == "admin_promos" and user_id == ADMIN_ID:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT code, reward_type, uses_left FROM promo_codes")
        promos = cursor.fetchall()
        conn.close()

        text = "🎟 **Активные промокоды:**\n\n"
        if promos:
            for p in promos:
                text += f"• `{p[0]}` — (Осталось активаций: {p[2]})\n"
        else:
            text += "Пока нет активных промокодов.\n"

        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("➕ Создать промокод (+7 дней)", callback_data="admin_new_promo"))
        bot.send_message(call.message.chat.id, text, parse_mode="Markdown", reply_markup=markup)

    elif call.data == "admin_new_promo" and user_id == ADMIN_ID:
        msg = bot.send_message(call.message.chat.id, "✍️ Введите название промокода (например, `GIFT7`):")
        bot.register_next_step_handler(msg, admin_ask_promo_code)

    elif call.data.startswith("reply_to_") and user_id == ADMIN_ID:
        target_user_id = int(call.data.replace("reply_to_", ""))
        msg = bot.send_message(call.message.chat.id, f"✍️ Напишите ответ для `{target_user_id}`:", parse_mode="Markdown")
        bot.register_next_step_handler(msg, process_admin_reply, target_user_id)

    elif call.data == "buy_vip_30":
        send_stars_invoice(call)

def process_broadcast(message):
    users = get_all_users()
    success = 0
    bot.send_message(message.chat.id, "⏳ Рассылка запущена...")
    for uid in users:
        try:
            bot.send_message(uid, f"📢 **Сообщение от администрации:**\n\n{message.text}", parse_mode="Markdown")
            success += 1
        except Exception:
            pass
    bot.send_message(message.chat.id, f"✅ Доставлено: {success}/{len(users)}")

def process_promo_code(message):
    user_id = message.from_user.id
    user_code = message.text.strip().upper()

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT reward_type, uses_left FROM promo_codes WHERE code = ?", (user_code,))
    promo = cursor.fetchone()

    if promo and promo[1] > 0:
        add_days_to_vip(user_id, days=7, text_vip=True, image_vip=True)
        cursor.execute("UPDATE promo_codes SET uses_left = uses_left - 1 WHERE code = ?", (user_code,))
        conn.commit()
        bot.send_message(message.chat.id, "🎉 Промокод активирован! Вам добавлено **+7 дней VIP-доступа**.")
    else:
        bot.send_message(message.chat.id, "❌ Неверный или уже исчерпанный промокод.")
    conn.close()

def send_stars_invoice(call):
    bot.send_invoice(
        call.message.chat.id, title="VIP Доступ (30 дней)", description="Доступ к Llama 70B и генерации картинок.",
        invoice_payload="payload_vip_30", provider_token="", currency="XTR",
        prices=[types.LabeledPrice(label="VIP 30 дней", amount=50)], start_parameter="vip-buy"
    )

@bot.pre_checkout_query_handler(func=lambda query: True)
def checkout(pre_checkout_query):
    bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@bot.message_handler(content_types=['successful_payment'])
def got_payment(message):
    user_id = message.from_user.id
    add_days_to_vip(user_id, days=30, text_vip=True, image_vip=True)
    bot.send_message(message.chat.id, "🎉 Оплата прошла успешно! VIP-доступ продлен на 30 дней.")

def process_art_generation(message):
    user_id = message.from_user.id
    prompt = message.text.strip()
    user_data = get_user(user_id)

    if not user_data["image_vip"]:
        bot.send_message(message.chat.id, "❌ Ваш VIP-период завершен.")
        return

    wait_msg = bot.send_message(message.chat.id, "🎨 **ИИннокентий рисует...**\n_(Это занимает 5-10 секунд)_", parse_mode="Markdown")
    bot.send_chat_action(message.chat.id, 'upload_photo')

    try:
        image_url = generate_image_url(prompt)
        response = requests.get(image_url, timeout=30)
        
        if response.status_code == 200:
            photo = io.BytesIO(response.content)
            bot.send_photo(message.chat.id, photo, caption=f"🖌 **Ваш арт:** `{prompt}`", parse_mode="Markdown")
            bot.delete_message(message.chat.id, wait_msg.message_id)
        else:
            bot.edit_message_text("⚠️ Сервер обработки изображений перегружен.", message.chat.id, wait_msg.message_id)
    except Exception as e:
        bot.edit_message_text(f"⚠️ Ошибка: `{str(e)}`", message.chat.id, wait_msg.message_id, parse_mode="Markdown")

# --- 8. ЗАПУСК ВЕБ-СЕРВЕРА И ПОЛЛИНГА ---
if __name__ == "__main__":
    t = threading.Thread(target=run_flask)
    t.daemon = True
    t.start()
    
    while True:
        try:
            bot.remove_webhook()
            time.sleep(1)
            print("Бот ИИннокентий запущен!")
            bot.polling(none_stop=True, interval=0, timeout=20)
        except telebot.apihelper.ApiTelegramException as e:
            if e.error_code == 409:
                time.sleep(5)
            else:
                time.sleep(3)
        except Exception:
            time.sleep(3)
