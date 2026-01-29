import os
import logging
import asyncio
import re
from datetime import datetime
from threading import Thread
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes, ConversationHandler
from pymongo import MongoClient
import requests
from bson.objectid import ObjectId
from urllib.parse import quote

# --- CONFIGURATION (Load from Env Vars) ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
TMDB_API_KEY = os.environ.get("TMDB_API_KEY")
MONGO_URI = os.environ.get("MONGO_URI")
ADMIN_CHANNEL_ID = os.environ.get("ADMIN_CHANNEL_ID")  # e.g., -100123456789
ALLOWED_GROUP_ID = os.environ.get("ALLOWED_GROUP_ID")  # e.g., -100987654321
TARGET_GROUP_LINK = os.environ.get("TARGET_GROUP_LINK", "https://t.me/your_file_group")

# --- DATABASE SETUP ---
client = MongoClient(MONGO_URI)
db_files = client['autofilter']  # Files database
db_requests = client['movie_requests_db']  # Separate database for requests

files_collection = db_files['royal_files']  # Your files collection
requests_collection = db_requests['requestbot']  # Requests collection

# --- FLASK SERVER (To keep Render Awake) ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is Alive"

def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

# --- LOGGING SETUP ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- HELPER FUNCTIONS ---
def search_tmdb(query):
    """Search TMDB for movies/TV shows"""
    url = f"https://api.themoviedb.org/3/search/multi?api_key={TMDB_API_KEY}&query={query}&include_adult=false"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        results = response.json().get('results', [])
        # Filter out adult content and return top 10
        filtered_results = [r for r in results if not r.get('adult', False)][:10]
        return filtered_results
    except Exception as e:
        logger.error(f"TMDB Search Error: {e}")
        return []

def get_tmdb_details(tmdb_id, media_type):
    """Get detailed information from TMDB"""
    url = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}?api_key={TMDB_API_KEY}&append_to_response=credits"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"TMDB Details Error: {e}")
        return {}

def clean_title(title):
    """Clean title for better matching"""
    if not title:
        return ""
    # Remove special characters, extra spaces, convert to lowercase
    title = re.sub(r'[^\w\s]', ' ', title)
    title = re.sub(r'\s+', ' ', title)
    return title.strip().lower()

def extract_year_from_filename(filename):
    """Extract year from filename using regex"""
    year_match = re.search(r'\b(19|20)\d{2}\b', str(filename))
    return year_match.group(0) if year_match else None

def check_file_in_db(title, year):
    """Check if file exists in database with exact matching"""
    try:
        cleaned_title = clean_title(title)
        
        # Build regex patterns for exact word matching
        title_words = cleaned_title.split()
        title_patterns = []
        
        for word in title_words:
            if len(word) > 2:  # Ignore very short words
                # Word boundary regex for exact word matching
                title_patterns.append(r'\b' + re.escape(word) + r'\b')
        
        if not title_patterns:
            return False
        
        # Combine patterns - all words must be present
        combined_title_pattern = '(?=.*' + ')(?=.*'.join(title_patterns) + ')'
        
        # Search in database
        query = {
            "$or": [
                {"file_name": {"$regex": combined_title_pattern, "$options": "i"}},
                {"caption": {"$regex": combined_title_pattern, "$options": "i"}}
            ]
        }
        
        results = list(files_collection.find(query))
        
        for file in results:
            # Extract year from file
            file_year = extract_year_from_filename(file.get('file_name', ''))
            if not file_year and 'caption' in file:
                file_year = extract_year_from_filename(file.get('caption', ''))
            
            # Check year match
            if year and file_year:
                if str(year) == str(file_year):
                    return True
            elif not year:  # If no year specified, just check title
                return True
        
        return False
    except Exception as e:
        logger.error(f"Database check error: {e}")
        return False

def format_movie_details(details):
    """Format movie details for display"""
    title = details.get('title') or details.get('name', 'N/A')
    year = (details.get('release_date') or details.get('first_air_date', '')[:4]) or 'N/A'
    
    # Get credits
    credits = details.get('credits', {})
    cast = credits.get('cast', [])
    crew = credits.get('crew', [])
    
    # Extract director
    directors = [p['name'] for p in crew if p.get('job') == 'Director']
    director = directors[0] if directors else 'N/A'
    
    # Extract main actors/actresses
    actors = [p['name'] for p in cast[:3] if p.get('gender') in [1, 2]]
    
    # Other details
    rating = details.get('vote_average', 'N/A')
    languages = [lang['name'] for lang in details.get('spoken_languages', [])[:3]]
    countries = [country['name'] for country in details.get('production_countries', [])[:3]]
    plot = details.get('overview', 'No description available.')
    
    # Format the details
    details_text = f"""
🎬 **{title}** ({year})

⭐ **Rating:** {rating}/10
🗓️ **Release Date:** {details.get('release_date', 'N/A')}
📁 **Type:** {details.get('media_type', 'movie').upper()}

👨‍💼 **Director:** {director}
🎭 **Cast:** {', '.join(actors) if actors else 'N/A'}

🗣️ **Languages:** {', '.join(languages) if languages else 'N/A'}
🌍 **Countries:** {', '.join(countries) if countries else 'N/A'}

📝 **Plot:**
{plot[:400]}{'...' if len(plot) > 400 else ''}
"""
    return details_text

# --- BOT HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message when /start is issued"""
    user = update.effective_user
    
    welcome_text = f"""
🎉 **Welcome {user.first_name}!** 🎉

I'm your Movie Search & Request Bot. Here's what I can do:

🔍 **Search Movies** - Send me any movie/TV show name in the group
📥 **Check Availability** - I'll tell you if it's available in our collection
📋 **Request System** - Request movies that aren't available
🔄 **Auto Updates** - Get notified when your requested movie is available
📊 **Track Requests** - Monitor your pending requests

📌 **How to use:**
1. Add me to your movie group
2. Search for movies by typing their names
3. Request movies that aren't available

🌟 **Features:**
• Supports movies & TV shows
• Detailed information cards
• Smart search with TMDB
• Request tracking system
• Auto-notification when available

Start by searching for a movie in the group! 🎬
"""
    
    await update.message.reply_text(welcome_text, parse_mode="Markdown")
    
    # Log user start
    logger.info(f"User started bot: {user.id} - {user.username}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send help message"""
    help_text = """
🤖 **Bot Help Guide**

**Available Commands:**
/start - Start the bot
/help - Show this help message
/myrequests - Check your pending requests
/cancelrequest - Cancel a pending request

**How to Search:**
1. In the group, simply type the movie name
2. Bot will show search results
3. Click on any result to see details

**Request System:**
• You can have up to 3 pending requests
• Requested movies go to admin for approval
• You'll be notified when available
• Auto-check every 10 minutes for availability

**Support:** Contact admin for assistance.
"""
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def my_requests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user's pending requests"""
    user_id = update.effective_user.id
    
    # Get user's pending requests
    pending_requests = list(requests_collection.find({
        "user_id": user_id,
        "status": "pending"
    }))
    
    if not pending_requests:
        await update.message.reply_text("📭 You have no pending requests.")
        return
    
    message = "📋 **Your Pending Requests:**\n\n"
    keyboard = []
    
    for i, req in enumerate(pending_requests, 1):
        message += f"{i}. **{req['title']}** ({req['year']})\n"
        message += f"   └ Requested on: {req['requested_at'].strftime('%Y-%m-%d')}\n\n"
        
        # Add cancel button for each request
        keyboard.append([
            InlineKeyboardButton(
                f"❌ Cancel {req['title'][:15]}...",
                callback_data=f"user_cancel_{req['_id']}"
            )
        ])
    
    await update.message.reply_text(
        message,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None
    )

async def group_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle movie search in group"""
    chat_id = str(update.effective_chat.id)
    if chat_id != ALLOWED_GROUP_ID:
        return
    
    query = update.message.text.strip()
    
    # Ignore short queries
    if len(query) < 2:
        return
    
    # Show searching message
    searching_msg = await update.message.reply_text(f"🔍 Searching for: **{query}**", parse_mode="Markdown")
    
    # Search TMDB
    results = search_tmdb(query)
    
    if not results:
        await searching_msg.edit_text(f"❌ No results found for: **{query}**", parse_mode="Markdown")
        return
    
    # Create keyboard with results
    keyboard = []
    for item in results:
        title = item.get('title') or item.get('name')
        year = (item.get('release_date') or item.get('first_air_date') or "")[:4]
        media_type = item.get('media_type', 'movie')
        
        if title:
            # Truncate long titles
            display_title = title[:30] + "..." if len(title) > 30 else title
            btn_text = f"{display_title} ({year})"
            callback_data = f"view_{item['id']}_{media_type}_{year}"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=callback_data)])
    
    # Add search help button
    keyboard.append([InlineKeyboardButton("❓ Search Help", callback_data="search_help")])
    
    await searching_msg.edit_text(
        f"🎬 **Search Results for:** `{query}`\n\nSelect a movie for details:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def show_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show movie details and availability"""
    query = update.callback_query
    await query.answer()
    
    # Check if user has started bot in private
    try:
        # Try to send a test message to user
        await context.bot.send_chat_action(chat_id=query.from_user.id, action="typing")
    except Exception as e:
        # User hasn't started bot, send start button
        keyboard = [[InlineKeyboardButton("🤖 Start Bot in PM", url=f"https://t.me/{context.bot.username}?start=start")]]
        await query.message.reply_text(
            "⚠️ **Please start the bot in private chat first!**\n\n"
            "Click the button below to start the bot, then try again:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    # Extract data from callback
    _, tmdb_id, media_type, year = query.data.split("_")
    
    # Get movie details
    details = get_tmdb_details(tmdb_id, media_type)
    if not details:
        await query.message.reply_text("❌ Error fetching movie details.")
        return
    
    title = details.get('title') or details.get('name')
    
    # Format detailed information
    details_text = format_movie_details(details)
    
    # Check availability in database
    is_available = check_file_in_db(title, year)
    
    # Add availability status
    details_text += f"\n{'─' * 30}\n"
    
    keyboard = []
    if is_available:
        details_text += "✅ **STATUS: AVAILABLE**\n\nYou can download this from our group."
        keyboard.append([InlineKeyboardButton("📥 Download Now", url=TARGET_GROUP_LINK)])
    else:
        details_text += "❌ **STATUS: NOT AVAILABLE**\n\nThis movie is not in our collection yet."
        
        # Check user's request limit
        user_pending = requests_collection.count_documents({
            "user_id": query.from_user.id,
            "status": "pending"
        })
        
        if user_pending >= 3:
            details_text += f"\n\n⚠️ **Request Limit Reached:** You have {user_pending}/3 pending requests."
            keyboard.append([InlineKeyboardButton("📋 My Requests", callback_data="show_my_requests")])
        else:
            # Check if already requested by this user
            existing_request = requests_collection.find_one({
                "user_id": query.from_user.id,
                "tmdb_id": tmdb_id,
                "status": "pending"
            })
            
            if existing_request:
                details_text += "\n\n📝 **You have already requested this movie.**"
                keyboard.append([InlineKeyboardButton("📋 My Requests", callback_data="show_my_requests")])
            else:
                keyboard.append([
                    InlineKeyboardButton("📝 Request Movie", callback_data=f"req_{tmdb_id}_{media_type}_{year}")
                ])
    
    # Get poster image
    poster_path = details.get('poster_path')
    image_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None
    
    try:
        if image_url:
            await context.bot.send_photo(
                chat_id=query.from_user.id,
                photo=image_url,
                caption=details_text,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None
            )
        else:
            await context.bot.send_message(
                chat_id=query.from_user.id,
                text=details_text,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None
            )
        
        # Notify in group if not private chat
        if query.message.chat.type != 'private':
            await query.message.reply_text(
                f"📨 Details sent to private message! Check @{context.bot.username}",
                reply_to_message_id=query.message.message_id
            )
            
    except Exception as e:
        logger.error(f"Error sending details: {e}")
        await query.message.reply_text("❌ Error sending details. Please try again.")

async def handle_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle movie request from user"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    user_name = query.from_user.full_name
    
    # Extract data
    _, tmdb_id, media_type, year = query.data.split("_")
    
    # Get movie details
    details = get_tmdb_details(tmdb_id, media_type)
    title = details.get('title') or details.get('name')
    
    # Check current pending requests
    pending_count = requests_collection.count_documents({
        "user_id": user_id,
        "status": "pending"
    })
    
    if pending_count >= 3:
        # Show options to remove existing requests
        user_requests = list(requests_collection.find({
            "user_id": user_id,
            "status": "pending"
        }))
        
        message = "⚠️ **Request Limit Reached!**\n\n"
        message += "You can only have 3 pending requests at a time.\n"
        message += "Select a request to remove:\n\n"
        
        keyboard = []
        for req in user_requests:
            btn_text = f"🗑 Remove: {req['title'][:20]}..."
            keyboard.append([InlineKeyboardButton(
                btn_text,
                callback_data=f"replace_{req['_id']}_{tmdb_id}_{media_type}_{year}"
            )])
        
        keyboard.append([InlineKeyboardButton("📋 View My Requests", callback_data="show_my_requests")])
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")])
        
        await context.bot.send_message(
            chat_id=user_id,
            text=message,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    # Add new request
    req_data = {
        "user_id": user_id,
        "user_name": user_name,
        "tmdb_id": tmdb_id,
        "title": title,
        "year": year,
        "media_type": media_type,
        "status": "pending",
        "requested_at": datetime.now(),
        "last_checked": datetime.now()
    }
    
    result = requests_collection.insert_one(req_data)
    req_id = result.inserted_id
    
    # Send notification to admin channel
    admin_message = f"""
🆕 **NEW MOVIE REQUEST**

🎬 **Title:** {title} ({year})
📁 **Type:** {media_type.upper()}
👤 **User:** {user_name} (`{user_id}`)
🆔 **Request ID:** `{req_id}`
🕐 **Time:** {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

📌 **TMDB Link:** https://www.themoviedb.org/{media_type}/{tmdb_id}
"""
    
    admin_keyboard = [
        [
            InlineKeyboardButton("✅ Mark as Available", callback_data=f"admin_done_{req_id}"),
            InlineKeyboardButton("❌ Reject Request", callback_data=f"admin_cancel_{req_id}")
        ],
        [
            InlineKeyboardButton("👁️ View Details", url=f"https://www.themoviedb.org/{media_type}/{tmdb_id}")
        ]
    ]
    
    try:
        sent_msg = await context.bot.send_message(
            chat_id=ADMIN_CHANNEL_ID,
            text=admin_message,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(admin_keyboard)
        )
        
        # Store admin message ID
        requests_collection.update_one(
            {"_id": req_id},
            {"$set": {"admin_msg_id": sent_msg.message_id}}
        )
        
    except Exception as e:
        logger.error(f"Error sending to admin channel: {e}")
    
    # Notify user
    await context.bot.send_message(
        chat_id=user_id,
        text=f"✅ **Request Submitted!**\n\n"
             f"**{title}** ({year})\n\n"
             f"Your request has been sent to the admin team. "
             f"You'll be notified when it becomes available.\n\n"
             f"📊 **Your pending requests:** {pending_count + 1}/3",
        parse_mode="Markdown"
    )

async def replace_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Replace an existing request with a new one"""
    query = update.callback_query
    await query.answer()
    
    data_parts = query.data.split("_")
    old_req_id = ObjectId(data_parts[1])
    new_tmdb_id = data_parts[2]
    new_media_type = data_parts[3]
    new_year = data_parts[4]
    
    # Get old request details
    old_req = requests_collection.find_one({"_id": old_req_id})
    
    # Delete old request
    requests_collection.delete_one({"_id": old_req_id})
    
    # Get new movie details
    details = get_tmdb_details(new_tmdb_id, new_media_type)
    new_title = details.get('title') or details.get('name')
    
    # Add new request
    req_data = {
        "user_id": query.from_user.id,
        "user_name": query.from_user.full_name,
        "tmdb_id": new_tmdb_id,
        "title": new_title,
        "year": new_year,
        "media_type": new_media_type,
        "status": "pending",
        "requested_at": datetime.now(),
        "last_checked": datetime.now()
    }
    
    result = requests_collection.insert_one(req_data)
    new_req_id = result.inserted_id
    
    # Update admin channel if old request was there
    if old_req and 'admin_msg_id' in old_req:
        try:
            await context.bot.edit_message_text(
                chat_id=ADMIN_CHANNEL_ID,
                message_id=old_req['admin_msg_id'],
                text=f"🔄 **REQUEST REPLACED**\n\n"
                     f"Old: {old_req['title']}\n"
                     f"New: {new_title}\n\n"
                     f"👤 User: {query.from_user.full_name}",
                parse_mode="Markdown"
            )
        except:
            pass
    
    # Send new request to admin
    admin_message = f"""
🔄 **REPLACED REQUEST - NEW**

🎬 **Title:** {new_title} ({new_year})
📁 **Type:** {new_media_type.upper()}
👤 **User:** {query.from_user.full_name} (`{query.from_user.id}`)
🆔 **Request ID:** `{new_req_id}`
🕐 **Time:** {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

📌 **TMDB Link:** https://www.themoviedb.org/{new_media_type}/{new_tmdb_id}
"""
    
    admin_keyboard = [
        [
            InlineKeyboardButton("✅ Mark as Available", callback_data=f"admin_done_{new_req_id}"),
            InlineKeyboardButton("❌ Reject Request", callback_data=f"admin_cancel_{new_req_id}")
        ]
    ]
    
    try:
        sent_msg = await context.bot.send_message(
            chat_id=ADMIN_CHANNEL_ID,
            text=admin_message,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(admin_keyboard)
        )
        
        requests_collection.update_one(
            {"_id": new_req_id},
            {"$set": {"admin_msg_id": sent_msg.message_id}}
        )
        
    except Exception as e:
        logger.error(f"Error updating admin channel: {e}")
    
    await query.edit_message_text(
        text=f"✅ **Request Replaced!**\n\n"
             f"🗑 Removed: {old_req['title']}\n"
             f"📝 Added: {new_title}\n\n"
             f"Your new request has been submitted.",
        parse_mode="Markdown"
    )

async def admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin actions"""
    query = update.callback_query
    await query.answer()
    
    data_parts = query.data.split("_")
    action = data_parts[1]
    req_id = ObjectId(data_parts[2])
    
    req = requests_collection.find_one({"_id": req_id})
    if not req:
        await query.edit_message_text("❌ Request not found in database.")
        return
    
    if action == "cancel":
        # Mark as cancelled
        requests_collection.update_one(
            {"_id": req_id},
            {"$set": {"status": "cancelled", "updated_at": datetime.now()}}
        )
        
        await query.edit_message_text(
            f"❌ **Request Cancelled**\n\n"
            f"Title: {req['title']}\n"
            f"User: {req['user_name']}",
            parse_mode="Markdown"
        )
        
        # Notify user
        try:
            await context.bot.send_message(
                chat_id=req['user_id'],
                text=f"❌ **Request Cancelled**\n\n"
                     f"Your request for **{req['title']}** has been cancelled by admin.",
                parse_mode="Markdown"
            )
        except:
            pass
        
    elif action == "done":
        # Mark as completed
        requests_collection.update_one(
            {"_id": req_id},
            {"$set": {"status": "completed", "updated_at": datetime.now()}}
        )
        
        # Notify user
        details = get_tmdb_details(req['tmdb_id'], req['media_type'])
        title = req['title']
        poster_path = details.get('poster_path')
        image_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None
        
        user_message = f"""
🎉 **GOOD NEWS!** 🎉

Your requested movie is now available!

🎬 **{title}** ({req['year']})

You can now download it from our group.

👇 Click below to go to the group:
"""
        
        keyboard = [[InlineKeyboardButton("📥 Download Now", url=TARGET_GROUP_LINK)]]
        
        try:
            if image_url:
                await context.bot.send_photo(
                    chat_id=req['user_id'],
                    photo=image_url,
                    caption=user_message,
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            else:
                await context.bot.send_message(
                    chat_id=req['user_id'],
                    text=user_message,
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
        except Exception as e:
            logger.error(f"Error notifying user: {e}")
        
        # Update admin message
        await query.edit_message_text(
            f"✅ **REQUEST COMPLETED**\n\n"
            f"🎬 {title}\n"
            f"👤 {req['user_name']}\n"
            f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
            f"User has been notified.",
            parse_mode="Markdown"
        )

async def auto_check_requests(context: ContextTypes.DEFAULT_TYPE):
    """Automatically check if requested movies are now available"""
    logger.info("Running auto-check for requests...")
    
    # Get all pending requests
    pending_requests = list(requests_collection.find({
        "status": "pending",
        "last_checked": {"$lt": datetime.now()}  # Check older than now
    }).limit(50))
    
    for req in pending_requests:
        try:
            # Check if movie is now available
            is_available = check_file_in_db(req['title'], req['year'])
            
            if is_available:
                # Mark as completed
                requests_collection.update_one(
                    {"_id": req['_id']},
                    {"$set": {
                        "status": "completed",
                        "updated_at": datetime.now(),
                        "auto_completed": True
                    }}
                )
                
                # Notify user
                details = get_tmdb_details(req['tmdb_id'], req['media_type'])
                title = req['title']
                
                user_message = f"""
🎉 **AUTO-UPDATE: MOVIE AVAILABLE!** 🎉

Your requested movie is now available in our collection!

🎬 **{title}** ({req['year']})

Click below to download:
"""
                
                keyboard = [[InlineKeyboardButton("📥 Download Now", url=TARGET_GROUP_LINK)]]
                
                try:
                    await context.bot.send_message(
                        chat_id=req['user_id'],
                        text=user_message,
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
                    logger.info(f"Auto-notified user {req['user_id']} for {title}")
                except Exception as e:
                    logger.error(f"Error auto-notifying user: {e}")
                
                # Update admin message if exists
                if 'admin_msg_id' in req:
                    try:
                        await context.bot.edit_message_text(
                            chat_id=ADMIN_CHANNEL_ID,
                            message_id=req['admin_msg_id'],
                            text=f"✅ **AUTO-COMPLETED**\n\n"
                                 f"🎬 {title}\n"
                                 f"👤 {req['user_name']}\n"
                                 f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
                                 f"File detected in database. User notified.",
                            parse_mode="Markdown"
                        )
                    except:
                        pass
            
            # Update last checked time
            requests_collection.update_one(
                {"_id": req['_id']},
                {"$set": {"last_checked": datetime.now()}}
            )
            
        except Exception as e:
            logger.error(f"Error in auto-check for request {req['_id']}: {e}")
            continue

async def user_cancel_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allow users to cancel their own requests"""
    query = update.callback_query
    await query.answer()
    
    req_id = ObjectId(query.data.split("_")[2])
    
    # Find and delete request
    req = requests_collection.find_one({"_id": req_id})
    if req and req['user_id'] == query.from_user.id:
        requests_collection.delete_one({"_id": req_id})
        
        # Update admin message if exists
        if 'admin_msg_id' in req:
            try:
                await context.bot.edit_message_text(
                    chat_id=ADMIN_CHANNEL_ID,
                    message_id=req['admin_msg_id'],
                    text=f"🗑 **USER CANCELLED**\n\n"
                         f"Title: {req['title']}\n"
                         f"User: {req['user_name']}\n"
                         f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                    parse_mode="Markdown"
                )
            except:
                pass
        
        await query.edit_message_text(
            f"🗑 **Request Cancelled**\n\n"
            f"Successfully cancelled request for:\n"
            f"**{req['title']}**",
            parse_mode="Markdown"
        )
    else:
        await query.edit_message_text("❌ Request not found or unauthorized.")

async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle miscellaneous button callbacks"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "show_my_requests":
        await my_requests_callback(update, context)
    elif query.data == "cancel_action":
        await query.edit_message_text("Action cancelled.")
    elif query.data == "search_help":
        await query.edit_message_text(
            "🔍 **Search Help**\n\n"
            "• Type the exact movie name for best results\n"
            "• Include year if you know it (e.g., 'Inception 2010')\n"
            "• You can search for TV shows too\n"
            "• Bot shows up to 10 results\n\n"
            "Try searching now!",
            parse_mode="Markdown"
        )

async def my_requests_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user's requests via callback"""
    query = update.callback_query
    user_id = query.from_user.id
    
    pending_requests = list(requests_collection.find({
        "user_id": user_id,
        "status": "pending"
    }))
    
    if not pending_requests:
        await query.edit_message_text("📭 You have no pending requests.")
        return
    
    message = "📋 **Your Pending Requests:**\n\n"
    keyboard = []
    
    for i, req in enumerate(pending_requests, 1):
        message += f"{i}. **{req['title']}** ({req['year']})\n"
        message += f"   └ Requested: {req['requested_at'].strftime('%Y-%m-%d')}\n\n"
        
        keyboard.append([
            InlineKeyboardButton(
                f"❌ Cancel {req['title'][:15]}...",
                callback_data=f"user_cancel_{req['_id']}"
            )
        ])
    
    await query.edit_message_text(
        message,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# --- MAIN FUNCTION ---
def main():
    """Start the bot"""
    # Start Flask server in separate thread
    Thread(target=run_flask, daemon=True).start()
    
    # Create Application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Add command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("myrequests", my_requests))
    
    # Add message handlers
    application.add_handler(MessageHandler(
        filters.TEXT & filters.Chat(int(ALLOWED_GROUP_ID)) & ~filters.COMMAND,
        group_search
    ))
    
    # Add callback query handlers
    application.add_handler(CallbackQueryHandler(show_details, pattern="^view_"))
    application.add_handler(CallbackQueryHandler(handle_request, pattern="^req_"))
    application.add_handler(CallbackQueryHandler(replace_request, pattern="^replace_"))
    application.add_handler(CallbackQueryHandler(admin_action, pattern="^admin_"))
    application.add_handler(CallbackQueryHandler(user_cancel_request, pattern="^user_cancel_"))
    application.add_handler(CallbackQueryHandler(button_callback_handler))
    
    # Set up job queue for auto-checking requests (every 10 minutes)
    job_queue = application.job_queue
    if job_queue:
        job_queue.run_repeating(auto_check_requests, interval=600, first=30)
    
    # Start the bot
    logger.info("Bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
