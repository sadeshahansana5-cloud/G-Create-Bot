import os
import logging
import asyncio
import re
import requests
import threading
from datetime import datetime
from flask import Flask
from pymongo import MongoClient, errors
from bson.objectid import ObjectId
from telegram import (
    Update, 
    InlineKeyboardButton, 
    InlineKeyboardMarkup, 
    ParseMode
)
from telegram.ext import (
    Application, 
    CommandHandler, 
    MessageHandler, 
    CallbackQueryHandler, 
    filters, 
    ContextTypes,
    JobQueue
)

# --- 1. LOGGING SETUP ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- 2. CONFIGURATION ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
TMDB_API_KEY = os.environ.get("TMDB_API_KEY")
MONGO_URI = os.environ.get("MONGO_URI")
ADMIN_CHANNEL_ID = os.environ.get("ADMIN_CHANNEL_ID")
ALLOWED_GROUP_ID = os.environ.get("ALLOWED_GROUP_ID")
TARGET_GROUP_LINK = os.environ.get("TARGET_GROUP_LINK", "https://t.me/your_group_link")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "YourBotUsername")

# --- 3. DATABASE INITIALIZATION ---
try:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    client.server_info() # Connection test
    db = client['autofilter']
    files_col = db['royal_files']
    req_col = db['requests']
    logger.info("✅ Database Connected: autofilter -> royal_files & requests")
except errors.ServerSelectionTimeoutError as err:
    logger.error(f"❌ DB Connection Error: {err}")
    exit(1)

# --- 4. FLASK SERVER FOR UPTIME ---
app = Flask(__name__)
@app.route('/')
def home(): return "<h1>Bot is Running</h1>"
def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

# --- 5. TMDB & SEARCH LOGIC ---

def get_movie_details(tmdb_id, m_type):
    """TMDB වෙතින් සම්පූර්ණ විස්තර ලබා ගැනීම"""
    try:
        url = f"https://api.themoviedb.org/3/{m_type}/{tmdb_id}?api_key={TMDB_API_KEY}&append_to_response=credits,release_dates"
        res = requests.get(url, timeout=10).json()
        
        # Crew details extract
        director = next((m['name'] for m in res.get('credits', {}).get('crew', []) if m['job'] == 'Director'), "N/A")
        cast = ", ".join([m['name'] for m in res.get('credits', {}).get('cast', [])[:8]])
        genres = ", ".join([g['name'] for g in res.get('genres', [])])
        countries = ", ".join([c['name'] for c in res.get('production_countries', [])])
        languages = ", ".join([l['english_name'] for l in res.get('spoken_languages', [])])
        
        # Rating (Age) logic
        rating = "PG-13" # Default
        for r in res.get('release_dates', {}).get('results', []):
            if r['iso_3166_1'] == 'US':
                rating = r['release_dates'][0]['certification'] or "N/A"

        return {
            "title": res.get('title') or res.get('name'),
            "year": (res.get('release_date') or res.get('first_air_date') or "0000")[:4],
            "full_date": res.get('release_date') or res.get('first_air_date') or "N/A",
            "plot": res.get('overview') or "No plot summary available.",
            "rating": rating,
            "tmdb_score": res.get('vote_average', 'N/A'),
            "poster": f"https://image.tmdb.org/t/p/w500{res.get('poster_path')}" if res.get('poster_path') else None,
            "runtime": f"{res.get('runtime') or 'N/A'} min",
            "director": director,
            "cast": cast,
            "genres": genres,
            "countries": countries,
            "languages": languages
        }
    except Exception as e:
        logger.error(f"TMDB Fetch Error: {e}")
        return None

def strict_file_search(title, year):
    """
    Search Logic: 
    1. නම හරියටම වචනයක් ලෙස තිබිය යුතුය (\bword\b).
    2. වර්ෂය එම string එකේම තිබිය යුතුය.
    """
    if not title: return False
    
    # Clean title for regex
    clean_title = re.escape(title)
    # Regex: word boundary for start and end of the name to avoid partial matches like Maargan
    # It also checks if the year exists anywhere in the same filename
    pattern = rf"(?i).*\b{clean_title}\b.*{year}.*"
    
    query = {"file_name": {"$regex": pattern}}
    match = files_col.find_one(query)
    
    if match:
        logger.info(f"✅ Match Found: {match['file_name']} for {title} {year}")
        return True
    return False

# --- 6. CORE BOT HANDLERS ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Private Start Handler"""
    user = update.effective_user
    welcome_text = (
        f"👋 **Hello {user.first_name}!**\n\n"
        "මම තමයි Movie Search Bot. ඔබට අවශ්‍ය චිත්‍රපට සමූහය තුලදී "
        "සොයාගත නොහැකි නම් මම හරහා විස්තර බලාගෙන Request කළ හැකියි.\n\n"
        "ℹ️ **භාවිතා කරන ආකාරය:**\n"
        "1. සමූහයට ගොස් චිත්‍රපටයේ නම Type කරන්න.\n"
        "2. ලැබෙන Button වලින් අවශ්‍ය එක තෝරන්න.\n"
        "3. විස්තර බැලීමට 'View Details' ඔබන්න."
    )
    await update.message.reply_text(welcome_text, parse_mode='Markdown')

async def group_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Group Search logic with 10 buttons"""
    if str(update.effective_chat.id) != str(ALLOWED_GROUP_ID):
        return

    query_text = update.message.text
    if len(query_text) < 2: return

    try:
        search_url = f"https://api.themoviedb.org/3/search/multi?api_key={TMDB_API_KEY}&query={query_text}"
        res = requests.get(search_url).json().get('results', [])[:10]

        if not res: return

        buttons = []
        for item in res:
            name = item.get('title') or item.get('name')
            date = item.get('release_date') or item.get('first_air_date') or "0000"
            year = date[:4]
            m_type = item.get('media_type', 'movie')
            
            if name:
                # view|id|type|year
                cb_data = f"view|{item['id']}|{m_type}|{year}"
                buttons.append([InlineKeyboardButton(f"🎬 {name} ({year})", callback_data=cb_data)])

        await update.message.reply_text(
            f"🔎 Search Results for: **{query_text}**",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Search error: {e}")

async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """සියලුම Button වල ක්‍රියාකාරීත්වය පාලනය"""
    query = update.callback_query
    data = query.data.split("|")
    action = data[0]

    # --- VIEW DETAILS ---
    if action == "view":
        tmdb_id, m_type, year = data[1], data[2], data[3]
        info = get_movie_details(tmdb_id, m_type)
        
        if not info:
            await query.answer("Could not fetch details from TMDB.", show_alert=True)
            return

        is_found = strict_file_search(info['title'], year)

        # Build Detail Card
        card = (
            f"🎬 **{info['title']} ({info['year']})**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⭐ **Rating:** {info['tmdb_score']}/10 | 🔞 **Rated:** {info['rating']}\n"
            f"🗓️ **Release:** {info['full_date']}\n"
            f"⏳ **Runtime:** {info['runtime']}\n"
            f"🎭 **Genres:** {info['genres']}\n"
            f"🌍 **Country:** {info['countries']}\n"
            f"🔊 **Languages:** {info['languages']}\n\n"
            f"👨‍💼 **Director:** {info['director']}\n"
            f"🌟 **Cast:** {info['cast']}\n\n"
            f"📖 **Plot:** {info['plot'][:450]}...\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
        )

        kb = []
        if is_found:
            card += "✅ **Status:** Already Available in Group!"
            kb.append([InlineKeyboardButton("📥 Download Movie", url=TARGET_GROUP_LINK)])
        else:
            card += "❌ **Status:** Not Found in our Database."
            kb.append([InlineKeyboardButton("🗳️ Request This Movie", callback_data=f"req|{tmdb_id}|{m_type}|{year}")])

        try:
            # Send to PM
            await context.bot.send_photo(
                chat_id=query.from_user.id,
                photo=info['poster'] or "https://via.placeholder.com/500",
                caption=card,
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(kb)
            )
            await query.answer("✅ Detailed info sent to your PM!")
        except Exception:
            # If user hasn't started the bot
            await query.answer("⚠️ Please Start the bot in PM first!", show_alert=True)
            join_btn = [[InlineKeyboardButton("🚀 Start Bot Now", url=f"https://t.me/{BOT_USERNAME}?start=true")]]
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"❌ @{query.from_user.username}, කරුණාකර මුලින්ම බොට්ව Start කර ඉන්පසු Button එක ඔබන්න.",
                reply_markup=InlineKeyboardMarkup(join_btn)
            )

    # --- REQUEST LOGIC ---
    elif action == "req":
        tmdb_id, m_type, year = data[1], data[2], data[3]
        user_id = query.from_user.id
        
        # Check current pending requests
        pending = req_col.count_documents({"user_id": user_id, "status": "pending"})
        if pending >= 3:
            await query.answer("⚠️ Limit Reached! ඔබට උපරිම ඉල්ලීම් 3ක් පමණි.", show_alert=True)
            return

        info = get_movie_details(tmdb_id, m_type)
        req_obj = {
            "user_id": user_id,
            "user_name": query.from_user.full_name,
            "title": info['title'],
            "year": year,
            "tmdb_id": tmdb_id,
            "m_type": m_type,
            "status": "pending",
            "date": datetime.now()
        }
        inserted_id = req_col.insert_one(req_obj).inserted_id

        # Notify Admin Channel
        admin_kb = [[
            InlineKeyboardButton("✅ Done", callback_data=f"adm|done|{inserted_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"adm|cncl|{inserted_id}")
        ]]
        admin_msg = await context.bot.send_message(
            chat_id=ADMIN_CHANNEL_ID,
            text=(f"🗳️ **NEW REQUEST**\n\n"
                  f"🎬 **{info['title']} ({year})**\n"
                  f"👤 User: {query.from_user.full_name}\n"
                  f"🆔 ID: `{user_id}`\n"
                  f"🔗 [TMDB Link](https://www.themoviedb.org/{m_type}/{tmdb_id})"),
            reply_markup=InlineKeyboardMarkup(admin_kb),
            parse_mode='Markdown',
            disable_web_page_preview=True
        )
        # Store message ID to edit later
        req_col.update_one({"_id": inserted_id}, {"$set": {"admin_msg_id": admin_msg.message_id}})
        await query.answer("✅ Request Sent Successfully!", show_alert=True)

    # --- ADMIN ACTIONS ---
    elif action == "adm":
        sub_action, req_db_id = data[1], data[2]
        request = req_col.find_one({"_id": ObjectId(req_db_id)})
        
        if not request:
            await query.answer("Request not found in Database.")
            return

        if sub_action == "done":
            await process_done_request(context, request)
            await query.message.edit_text(f"✅ **Request Completed**\n🎬 {request['title']} ({request['year']})", parse_mode='Markdown')
        
        elif sub_action == "cncl":
            req_col.delete_one({"_id": ObjectId(req_db_id)})
            await query.message.edit_text(f"❌ **Request Cancelled**\n🎬 {request['title']}", parse_mode='Markdown')
            try:
                await context.bot.send_message(request['user_id'], f"❌ Your request for **{request['title']}** was declined by admins.")
            except: pass

# --- 7. AUTO SYSTEM LOGIC ---

async def process_done_request(context, req_doc):
    """Request එකක් අවසන් වූ පසු ක්‍රියාත්මක වන ප්‍රධාන function එක"""
    # Update Status
    req_col.update_one({"_id": req_doc['_id']}, {"$set": {"status": "completed"}})
    
    # Notify User
    success_text = (
        f"✅ **Request Fulfilled!**\n\n"
        f"🎬 **{req_doc['title']} ({req_doc['year']})**\n"
        f"ඔබ ඉල්ලූ චිත්‍රපටය දැන් සමූහයේ පවතියි. පහත බොත්තමෙන් ලබාගන්න."
    )
    kb = [[InlineKeyboardButton("📥 Get Movie Now", url=TARGET_GROUP_LINK)]]
    
    try:
        await context.bot.send_message(chat_id=req_doc['user_id'], text=success_text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
    except Exception as e:
        logger.warning(f"Could not notify user {req_doc['user_id']}: {e}")

    # Edit Admin Channel Msg
    try:
        if 'admin_msg_id' in req_doc:
            await context.bot.edit_message_text(
                chat_id=ADMIN_CHANNEL_ID,
                message_id=req_doc['admin_msg_id'],
                text=f"✅ **COMPLETED & UPLOADED**\n🎬 {req_doc['title']} ({req_doc['year']})\nProcessed at: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            )
    except Exception as e:
        logger.error(f"Admin Msg Edit Error: {e}")

async def auto_check_job(context: ContextTypes.DEFAULT_TYPE):
    """
    පසුබිමින් ක්‍රියාත්මක වන සේවාව. 
    Pending requests සියල්ල පරීක්ෂා කර, ෆයිල් එකක් DB එකට වැටී ඇත්නම් Auto Done කරයි.
    """
    pending_requests = req_col.find({"status": "pending"})
    
    for req in pending_requests:
        # DB එකේ ෆයිල් එක දැන් තියෙනවද බලන්න
        if strict_file_search(req['title'], req['year']):
            logger.info(f"🤖 Auto-Detect: {req['title']} found. Marking as Done.")
            await process_done_request(context, req)

# --- 8. MAIN STARTUP ---

def main():
    # Start Keep-Alive Server
    threading.Thread(target=run_flask, daemon=True).start()

    # Create Bot Application
    application = Application.builder().token(BOT_TOKEN).build()

    # Registration of Handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.TEXT & filters.ChatType.GROUPS, group_message_handler))
    application.add_handler(CallbackQueryHandler(callback_query_handler))

    # Job Queue for Auto Check (Runs every 15 minutes)
    job_queue = application.job_queue
    job_queue.run_repeating(auto_check_job, interval=900, first=30)

    logger.info("🚀 Bot is Fully Operational!")
    application.run_polling()

if __name__ == '__main__':
    main()
