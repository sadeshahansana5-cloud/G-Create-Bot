import os
import logging
import asyncio
import re
from datetime import datetime
from threading import Thread
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from pymongo import MongoClient
import requests

# --- CONFIGURATION (Load from Env Vars) ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
TMDB_API_KEY = os.environ.get("TMDB_API_KEY")
MONGO_URI = os.environ.get("MONGO_URI")
ADMIN_CHANNEL_ID = os.environ.get("ADMIN_CHANNEL_ID") # e.g., -100123456789
ALLOWED_GROUP_ID = os.environ.get("ALLOWED_GROUP_ID") # e.g., -100987654321
TARGET_GROUP_LINK = os.environ.get("TARGET_GROUP_LINK", "https://t.me/your_file_group")

# --- DATABASE SETUP ---
client = MongoClient(MONGO_URI)
db = client['my_movie_db']
files_collection = db['files'] # ඔබේ ෆයිල් තියෙන තැන (Read Only logic)
requests_collection = db['requests'] # Request store කරන තැන (Read/Write)

# --- FLASK SERVER (To keep Render Awake) ---
app = Flask(__name__)
@app.route('/')
def home(): return "Bot is Alive"
def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

# --- HELPERS ---
def search_tmdb(query):
    url = f"https://api.themoviedb.org/3/search/multi?api_key={TMDB_API_KEY}&query={query}"
    response = requests.get(url).json()
    return response.get('results', [])[:5] # Return top 5

def get_tmdb_details(tmdb_id, media_type):
    url = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}?api_key={TMDB_API_KEY}"
    return requests.get(url).json()

def check_file_in_db(title, year):
    # Regex for flexible matching (Case insensitive)
    # වර්ෂය සහ නම හරියටම ගැලපේදැයි බලයි
    query = {
        "file_name": {"$regex": title, "$options": "i"},
        # ඔබේ DB එකේ වර්ෂය තියෙන විදිය අනුව මෙය වෙනස් කරන්න (Description or Filename check)
    }
    # සරලව නම match වෙනවද බලමු (Advanced logic අවශ්‍ය නම් මෙතන වෙනස් කරන්න)
    results = list(files_collection.find(query))
    
    for file in results:
        if year and str(year) in str(file): # වර්ෂය ෆයිල් නමේ හෝ විස්තරේ තිබේදැයි බලයි
            return True
    return False

# --- HANDLERS ---

async def group_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    if chat_id != ALLOWED_GROUP_ID:
        return

    query = update.message.text
    results = search_tmdb(query)
    
    if not results:
        return # No results found

    keyboard = []
    for item in results:
        title = item.get('title') or item.get('name')
        year = (item.get('release_date') or item.get('first_air_date') or "")[:4]
        media_type = item.get('media_type')
        if title:
            btn_text = f"{title} ({year}) - {media_type.upper()}"
            callback_data = f"view_{item['id']}_{media_type}_{year}"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=callback_data)])

    await update.message.reply_text(
        f"Search Results for: {query}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def show_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    _, tmdb_id, media_type, year = query.data.split("_")
    details = get_tmdb_details(tmdb_id, media_type)
    
    title = details.get('title') or details.get('name')
    overview = details.get('overview', 'No description.')
    poster_path = details.get('poster_path')
    image_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else "https://via.placeholder.com/500"

    # Check DB Availability
    is_available = check_file_in_db(title, year)
    
    caption = (
        f"🎬 **{title}** ({year})\n\n"
        f"{overview[:500]}...\n\n"
        f"-----------------------------\n"
    )

    keyboard = []
    if is_available:
        caption += "✅ **Available / ඇත**\n\nඔබට මෙම චිත්‍රපටය අපගේ ගොනු සමූහයෙන් ලබාගත හැක."
        keyboard.append([InlineKeyboardButton("📥 Download form Group", url=TARGET_GROUP_LINK)])
    else:
        caption += "❌ **Not Available / නැත**\n\nමෙම චිත්‍රපටය අපගේ ගොනු අතර නොමැත. ඔබට මෙය Request කළ හැක."
        # Request button logic
        keyboard.append([InlineKeyboardButton("Request This 🗳️", callback_data=f"req_{tmdb_id}_{media_type}_{year}")])

    # Send to Private Chat (Bot PM)
    try:
        await context.bot.send_photo(
            chat_id=query.from_user.id,
            photo=image_url,
            caption=caption,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        if query.message.chat.type != 'private':
            await query.message.reply_text(f"Details sent to PM! check @{context.bot.username}")
    except Exception as e:
        await query.message.reply_text("Please start the bot in private chat first!")

async def handle_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    _, tmdb_id, media_type, year = query.data.split("_")
    
    # Check Active Requests Count
    pending_count = requests_collection.count_documents({"user_id": user_id, "status": "pending"})
    
    if pending_count >= 3:
        # Show options to replace
        user_requests = requests_collection.find({"user_id": user_id, "status": "pending"})
        keyboard = []
        msg = "⚠️ You have reached the limit of 3 requests.\nSelect a request to REMOVE and replace with the new one:\n\nඔබට එකවර ඉල්ලීම් 3ක් පමණක් සිදුකල හැක. අලුත් එක දැමීමට පැරණි එකක් ඉවත් කරන්න."
        
        for req in user_requests:
            btn_text = f"🗑 Remove: {req['title']}"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"del_{req['_id']}_{tmdb_id}_{media_type}_{year}")])
            
        await context.bot.send_message(chat_id=user_id, text=msg, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # Add Request Logic
    await add_request_to_db(context, user_id, tmdb_id, media_type, year, query.from_user.full_name)

async def add_request_to_db(context, user_id, tmdb_id, media_type, year, user_name):
    details = get_tmdb_details(tmdb_id, media_type)
    title = details.get('title') or details.get('name')
    
    req_data = {
        "user_id": user_id,
        "tmdb_id": tmdb_id,
        "title": title,
        "year": year,
        "media_type": media_type,
        "status": "pending",
        "requested_at": datetime.now(),
        "user_name": user_name
    }
    
    result = requests_collection.insert_one(req_data)
    req_id = result.inserted_id

    # Admin Channel Message
    admin_msg = (
        f"🆕 **New Request**\n"
        f"🎬 Name: {title} ({year})\n"
        f"👤 User: {user_name} (`{user_id}`)\n"
        f"🆔 TMDB ID: `{tmdb_id}`"
    )
    admin_kb = [
        [
            InlineKeyboardButton("✅ Done", callback_data=f"adm_done_{req_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"adm_cancel_{req_id}")
        ]
    ]
    
    sent_msg = await context.bot.send_message(
        chat_id=ADMIN_CHANNEL_ID,
        text=admin_msg,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(admin_kb)
    )
    
    # Store message ID to update later
    requests_collection.update_one({"_id": req_id}, {"$set": {"admin_msg_id": sent_msg.message_id}})
    
    await context.bot.send_message(chat_id=user_id, text=f"✅ Request Added: {title}\n\nඉල්ලීම Admin වෙත යොමු කරන ලදි.")

async def replace_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    # del_OLDID_NEWTMDB...
    parts = query.data.split("_")
    old_req_id = parts[1]
    new_tmdb_data = parts[2:] # tmdb, type, year
    
    from bson.objectid import ObjectId
    requests_collection.delete_one({"_id": ObjectId(old_req_id)})
    
    await context.bot.send_message(chat_id=query.from_user.id, text="🗑 Old request removed.")
    await add_request_to_db(context, query.from_user.id, new_tmdb_data[0], new_tmdb_data[1], new_tmdb_data[2], query.from_user.full_name)

# --- ADMIN ACTIONS ---
async def admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    parts = query.data.split("_")
    action = parts[1] # done / cancel
    req_id = parts[2]
    
    from bson.objectid import ObjectId
    req = requests_collection.find_one({"_id": ObjectId(req_id)})
    
    if not req:
        await query.message.edit_text("Request not found in DB.")
        return

    if action == "cancel":
        requests_collection.update_one({"_id": req['_id']}, {"$set": {"status": "cancelled"}})
        await query.message.edit_text(f"❌ Request Cancelled: {req['title']}")
        await context.bot.send_message(chat_id=req['user_id'], text=f"❌ Your request for **{req['title']}** was cancelled.", parse_mode="Markdown")
        
    elif action == "done":
        await mark_request_done(context, req)

async def mark_request_done(context, req):
    # Update DB
    requests_collection.update_one({"_id": req['_id']}, {"$set": {"status": "completed"}})
    
    # Notify User with Detail Card
    details = get_tmdb_details(req['tmdb_id'], req['media_type'])
    title = req['title']
    poster_path = details.get('poster_path')
    image_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else ""
    
    caption = (
        f"✅ **Request Fulfilled!**\n\n"
        f"🎬 **{title}** ({req['year']})\n\n"
        f"ඔබ කල ඉල්ලීම දැන් අපගේ සමූහයට එකතු කර ඇත.\n"
        f"Your requested file is now available!"
    )
    kb = [[InlineKeyboardButton("📥 Download Now", url=TARGET_GROUP_LINK)]]
    
    await context.bot.send_photo(chat_id=req['user_id'], photo=image_url, caption=caption, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
    
    # Update Admin Message if exists
    try:
        await context.bot.edit_message_text(
            chat_id=ADMIN_CHANNEL_ID,
            message_id=req.get('admin_msg_id'),
            text=f"✅ **COMPLETED**\n{title} - Uploaded."
        )
    except:
        pass

# --- BACKGROUND JOB (AUTO DONE) ---
async def check_new_uploads(context: ContextTypes.DEFAULT_TYPE):
    # This runs every X minutes
    pending_reqs = requests_collection.find({"status": "pending"})
    
    for req in pending_reqs:
        # Check if this requested file is now in the 'files' collection
        is_now_available = check_file_in_db(req['title'], req['year'])
        
        if is_now_available:
            await mark_request_done(context, req)

# --- MAIN SETUP ---
def main():
    # Start Flask in separate thread
    Thread(target=run_flask).start()

    application = Application.builder().token(BOT_TOKEN).build()

    # Handlers
    application.add_handler(MessageHandler(filters.TEXT & filters.Chat(int(ALLOWED_GROUP_ID)), group_search))
    application.add_handler(CallbackQueryHandler(show_details, pattern="^view_"))
    application.add_handler(CallbackQueryHandler(handle_request, pattern="^req_"))
    application.add_handler(CallbackQueryHandler(replace_request, pattern="^del_"))
    application.add_handler(CallbackQueryHandler(admin_action, pattern="^adm_"))

    # Job Queue for Auto Done (Runs every 10 mins = 600s)
    job_queue = application.job_queue
    job_queue.run_repeating(check_new_uploads, interval=600, first=10)

    application.run_polling()

if __name__ == '__main__':
    main()
