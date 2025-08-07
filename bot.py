import os
import random
import logging
import datetime
import asyncio
from typing import Dict, Any, Optional
from motor.motor_asyncio import AsyncIOMotorClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.error import TelegramError
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Load environment variables
API_TOKEN = os.getenv('TELEGRAM_API_TOKEN')
SOURCE_CHANNEL = os.getenv('SOURCE_CHANNEL')  # Channel to fetch videos from
ADMIN_ID = int(os.getenv('ADMIN_ID')) if os.getenv('ADMIN_ID') else None
DAILY_LIMIT = int(os.getenv('DAILY_LIMIT', 5))
MONGO_URI = "mongodb+srv://movie:movie@movie.tylkv.mongodb.net/?retryWrites=true&w=majority&appName=movie"
DB_NAME = "telegram_bot_db"

# Webhook configuration for Koyeb
WEBHOOK_URL = os.getenv('WEBHOOK_URL')
PORT = int(os.getenv('PORT', 8000))
LISTEN_ADDRESS = '0.0.0.0'

# Global database client and collections
db_client = None
db = None
users_collection = None
videos_collection = None

async def connect_to_mongodb():
    """Connects to MongoDB and sets up global collections."""
    global db_client, db, users_collection, videos_collection
    try:
        db_client = AsyncIOMotorClient(MONGO_URI)
        # Test the connection
        await db_client.admin.command('ping')
        db = db_client[DB_NAME]
        users_collection = db['users']
        videos_collection = db['videos']
        logger.info("Successfully connected to MongoDB.")
        return True
    except Exception as e:
        logger.error(f"Failed to connect to MongoDB: {e}")
        return False

async def fetch_videos_from_channel(context: ContextTypes.DEFAULT_TYPE):
    """
    Placeholder function to fetch videos from the source channel.
    The actual video file IDs are now stored in the database.
    """
    if not SOURCE_CHANNEL:
        logger.warning("SOURCE_CHANNEL not configured")
        return
    
    try:
        chat = await context.bot.get_chat(SOURCE_CHANNEL)
        logger.info(f"Fetching videos from channel: {chat.title}")
    except TelegramError as e:
        logger.error(f"Error accessing source channel {SOURCE_CHANNEL}: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a welcome message and main menu keyboard to the user."""
    user = update.effective_user
    user_id = user.id
    
    welcome_message = f"Welcome, {user.mention_markdown_v2()}\\!\n\n"
    welcome_message += f"Your User ID: `{user_id}`\n\n"
    welcome_message += "This bot sends random videos from our collection\\.\n"
    welcome_message += "Use the buttons below to get videos or upload new ones\\."

    keyboard = [
        [InlineKeyboardButton("Get Random Video", callback_data='get_video')],
        [InlineKeyboardButton("Upload Video", callback_data='upload_video')],
        [InlineKeyboardButton("Trending Videos", callback_data='trending_videos')]
    ]
    
    if ADMIN_ID and user_id == ADMIN_ID:
        keyboard.append([InlineKeyboardButton("📡 Admin Panel", callback_data='admin_panel')])
    
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(welcome_message, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles button presses from the inline keyboard."""
    query = update.callback_query
    await query.answer()

    if query.data == 'get_video':
        user_id = query.from_user.id
        
        try:
            user_doc = await users_collection.find_one({'user_id': user_id})
            
            if not user_doc:
                user_doc = {
                    'user_id': user_id,
                    'daily_count': 0,
                    'last_reset': datetime.date.today().isoformat(),
                    'uploaded_videos': 0
                }
                await users_collection.insert_one(user_doc)
            
            # Reset daily count if it's a new day
            if user_doc['last_reset'] != datetime.date.today().isoformat():
                await users_collection.update_one(
                    {'user_id': user_id},
                    {'$set': {'daily_count': 0, 'last_reset': datetime.date.today().isoformat()}}
                )
                user_doc['daily_count'] = 0
                user_doc['last_reset'] = datetime.date.today().isoformat()
            
            # Check daily limit
            if user_doc['daily_count'] >= DAILY_LIMIT:
                await query.edit_message_text(
                    text=f"⏰ You have reached your daily limit of {DAILY_LIMIT} videos.\n"
                         f"Please try again tomorrow!"
                )
                return

            # Get all videos from collection
            all_videos = []
            async for doc in videos_collection.find({}):
                all_videos.append(doc['file_id'])
            
            if not all_videos:
                await query.edit_message_text(text="📹 No videos available at the moment.\nPlease upload some videos first!")
                return

            # Send random video
            random_video_id = random.choice(all_videos)
            await query.edit_message_text(text="📹 Here is your random video:")
            
            sent_message = await context.bot.send_video(
                chat_id=query.message.chat_id, 
                video=random_video_id,
                protect_content=True
            )
            
            # Schedule message deletion after 5 minutes
            context.job_queue.run_once(
                delete_message,
                300,
                data={'chat_id': query.message.chat_id, 'message_id': sent_message.message_id}
            )

            # Update user's daily count
            await users_collection.update_one(
                {'user_id': user_id},
                {'$inc': {'daily_count': 1}}
            )
            
            remaining = DAILY_LIMIT - (user_doc['daily_count'] + 1)
            if remaining > 0:
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=f"✅ Video sent! You have {remaining} videos left today."
                )
            else:
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text="✅ Video sent! You've reached your daily limit. See you tomorrow!"
                )
                
        except Exception as e:
            logger.error(f"Error in get_video: {e}")
            await query.edit_message_text(text="❌ Sorry, there was an error processing your request.")

    elif query.data == 'upload_video':
        await query.edit_message_text(text="📤 Please send me the video you want to upload.")

    elif query.data == 'trending_videos':
        try:
            trending_videos = []
            async for doc in videos_collection.find({'is_trending': True}):
                trending_videos.append(doc['file_id'])
            
            if trending_videos:
                await query.edit_message_text(text="🔥 Here are the trending videos:")
                for video_id in trending_videos[:3]:  # Limit to 3 trending videos
                    try:
                        sent_message = await context.bot.send_video(
                            chat_id=query.message.chat_id, 
                            video=video_id,
                            protect_content=True
                        )
                        
                        context.job_queue.run_once(
                            delete_message,
                            300,
                            data={'chat_id': query.message.chat_id, 'message_id': sent_message.message_id}
                        )
                    except TelegramError as e:
                        logger.error(f"Error sending trending video {video_id}: {e}")
            else:
                await query.edit_message_text(text="📹 No trending videos available at the moment.")
        except Exception as e:
            logger.error(f"Error in trending_videos: {e}")
            await query.edit_message_text(text="❌ Error loading trending videos.")

    elif query.data == 'admin_panel':
        if not ADMIN_ID or query.from_user.id != ADMIN_ID:
            await query.edit_message_text(text="❌ Access denied. Admin only.")
            return
            
        admin_keyboard = [
            [InlineKeyboardButton("📡 Broadcast Message", callback_data='broadcast_menu')],
            [InlineKeyboardButton("📊 Bot Statistics", callback_data='admin_stats')],
            [InlineKeyboardButton("🔥 Manage Trending", callback_data='manage_trending')],
            [InlineKeyboardButton("🔙 Back to Main", callback_data='back_to_main')]
        ]
        reply_markup = InlineKeyboardMarkup(admin_keyboard)
        
        await query.edit_message_text(
            text="🛠 **Admin Panel**\n\nChoose an option:",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )

    elif query.data == 'broadcast_menu':
        if not ADMIN_ID or query.from_user.id != ADMIN_ID:
            await query.edit_message_text(text="❌ Access denied.")
            return
            
        broadcast_keyboard = [
            [InlineKeyboardButton("📝 Text Message", callback_data='broadcast_text')],
            [InlineKeyboardButton("🖼 Image Broadcast", callback_data='broadcast_image')],
            [InlineKeyboardButton("🎥 Video Broadcast", callback_data='broadcast_video')],
            [InlineKeyboardButton("🔙 Back to Admin", callback_data='admin_panel')]
        ]
        reply_markup = InlineKeyboardMarkup(broadcast_keyboard)
        
        await query.edit_message_text(
            text="📡 **Broadcast Menu**\n\n"
                 "Choose the type of content to broadcast:\n\n"
                 "• **Text**: Send a text message to all users\n"
                 "• **Image**: Send an image to all users\n"
                 "• **Video**: Send a video to all users",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )

    elif query.data == 'broadcast_text':
        if not ADMIN_ID or query.from_user.id != ADMIN_ID:
            await query.edit_message_text(text="❌ Access denied.")
            return
            
        context.user_data['broadcast_mode'] = 'text'
        await query.edit_message_text(
            text="📝 **Text Broadcast Mode**\n\n"
                 "Send me the text message you want to broadcast to all users.\n\n"
                 "Use /cancel to cancel this operation."
        )

    elif query.data == 'broadcast_image':
        if not ADMIN_ID or query.from_user.id != ADMIN_ID:
            await query.edit_message_text(text="❌ Access denied.")
            return
            
        context.user_data['broadcast_mode'] = 'image'
        await query.edit_message_text(
            text="🖼 **Image Broadcast Mode**\n\n"
                 "Send me the image you want to broadcast to all users.\n"
                 "You can include a caption with the image.\n\n"
                 "Use /cancel to cancel this operation."
        )

    elif query.data == 'broadcast_video':
        if not ADMIN_ID or query.from_user.id != ADMIN_ID:
            await query.edit_message_text(text="❌ Access denied.")
            return
            
        context.user_data['broadcast_mode'] = 'video'
        await query.edit_message_text(
            text="🎥 **Video Broadcast Mode**\n\n"
                 "Send me the video you want to broadcast to all users.\n"
                 "You can include a caption with the video.\n\n"
                 "Use /cancel to cancel this operation."
        )

    elif query.data == 'admin_stats':
        if not ADMIN_ID or query.from_user.id != ADMIN_ID:
            await query.edit_message_text(text="❌ Access denied.")
            return
            
        try:
            total_users = await users_collection.count_documents({})
            total_videos = await videos_collection.count_documents({})
            trending_count = await videos_collection.count_documents({'is_trending': True})

            today_iso = datetime.date.today().isoformat()
            active_today = await users_collection.count_documents({
                'last_reset': today_iso,
                'daily_count': {'$gt': 0}
            })
            
            stats_text = f"📊 **Bot Statistics**\n\n"
            stats_text += f"👥 Total users: {total_users}\n"
            stats_text += f"🔥 Active today: {active_today}\n"
            stats_text += f"📹 Total videos: {total_videos}\n"
            stats_text += f"⭐ Trending videos: {trending_count}\n"
            stats_text += f"⚙️ Daily limit: {DAILY_LIMIT}\n"
            stats_text += f"🤖 Auto-delete: 5 minutes"
            
            back_keyboard = [[InlineKeyboardButton("🔙 Back to Admin", callback_data='admin_panel')]]
            reply_markup = InlineKeyboardMarkup(back_keyboard)
            
            await query.edit_message_text(
                text=stats_text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"Error in admin_stats: {e}")
            await query.edit_message_text(text="❌ Error loading statistics.")

    elif query.data == 'manage_trending':
        if not ADMIN_ID or query.from_user.id != ADMIN_ID:
            await query.edit_message_text(text="❌ Access denied.")
            return
            
        try:
            trending_count = await videos_collection.count_documents({'is_trending': True})
            
            trending_keyboard = [
                [InlineKeyboardButton("➕ Add Trending", callback_data='add_trending')],
                [InlineKeyboardButton("🗑 Clear All", callback_data='clear_trending')],
                [InlineKeyboardButton("🔙 Back to Admin", callback_data='admin_panel')]
            ]
            reply_markup = InlineKeyboardMarkup(trending_keyboard)
            
            await query.edit_message_text(
                text=f"🔥 **Trending Management**\n\n"
                     f"Current trending videos: {trending_count}\n\n"
                     f"• Add new trending videos\n"
                     f"• Clear all trending videos",
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"Error in manage_trending: {e}")
            await query.edit_message_text(text="❌ Error loading trending management.")

    elif query.data == 'add_trending':
        if not ADMIN_ID or query.from_user.id != ADMIN_ID:
            await query.edit_message_text(text="❌ Access denied.")
            return
            
        context.user_data['trending_mode'] = True
        await query.edit_message_text(
            text="🔥 **Add Trending Video**\n\n"
                 "Send me a video to add to trending list.\n\n"
                 "Use /cancel to cancel this operation."
        )

    elif query.data == 'clear_trending':
        if not ADMIN_ID or query.from_user.id != ADMIN_ID:
            await query.edit_message_text(text="❌ Access denied.")
            return
            
        try:
            result = await videos_collection.update_many(
                {'is_trending': True},
                {'$set': {'is_trending': False}}
            )
            await query.edit_message_text(
                text=f"✅ Cleared {result.modified_count} trending videos successfully!"
            )
        except Exception as e:
            logger.error(f"Error clearing trending videos: {e}")
            await query.edit_message_text(
                text="❌ Error clearing trending videos."
            )

    elif query.data == 'back_to_main':
        user = query.from_user
        welcome_message = f"Welcome back, {user.mention_markdown_v2()}\\!\n\n"
        welcome_message += f"Your User ID: `{user.id}`\n\n"
        welcome_message += "This bot sends random videos from our collection\\.\n"
        welcome_message += "Use the buttons below to get videos or upload new ones\\."

        keyboard = [
            [InlineKeyboardButton("Get Random Video", callback_data='get_video')],
            [InlineKeyboardButton("Upload Video", callback_data='upload_video')],
            [InlineKeyboardButton("Trending Videos", callback_data='trending_videos')]
        ]
        
        if ADMIN_ID and user.id == ADMIN_ID:
            keyboard.append([InlineKeyboardButton("📡 Admin Panel", callback_data='admin_panel')])
        
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(welcome_message, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)

async def upload_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles video uploads from users or admin for broadcast/trending."""
    if not update.message:
        logger.error("No message in update")
        return
    
    user_id = update.message.from_user.id
    
    # Handle admin operations first
    if ADMIN_ID and user_id == ADMIN_ID:
        if context.user_data.get('broadcast_mode') or context.user_data.get('trending_mode'):
            await handle_admin_content(update, context)
            return
    
    video = update.message.video
    if video:
        try:
            # Check if video already exists
            existing_video = await videos_collection.find_one({'file_id': video.file_id})
            if existing_video:
                await update.message.reply_text("This video has already been uploaded.")
                return

            # Add video to collection
            await videos_collection.insert_one({
                'file_id': video.file_id,
                'is_trending': False,
                'upload_timestamp': datetime.datetime.now(),
                'uploaded_by': user_id
            })
            
            # Update user's upload count
            await users_collection.update_one(
                {'user_id': user_id},
                {'$inc': {'uploaded_videos': 1}},
                upsert=True
            )
            
            # Get counts for response
            total_videos = await videos_collection.count_documents({})
            user_doc = await users_collection.find_one({'user_id': user_id})
            uploaded_videos = user_doc['uploaded_videos'] if user_doc else 0

            await update.message.reply_text(
                f"✅ Video uploaded successfully!\n"
                f"📊 Total videos uploaded by you: {uploaded_videos}\n"
                f"📹 Total videos in collection: {total_videos}"
            )
            
            logger.info(f"User {user_id} uploaded a video. Total videos: {total_videos}")
            
        except Exception as e:
            logger.error(f"Error uploading video: {e}")
            await update.message.reply_text("❌ Sorry, there was an error uploading your video.")
    else:
        await update.message.reply_text("❌ Please send a valid video file.")

async def handle_admin_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles content (video, photo, text) sent by admin for broadcast or trending."""
    if not ADMIN_ID or update.message.from_user.id != ADMIN_ID:
        return
    
    broadcast_mode = context.user_data.get('broadcast_mode')
    trending_mode = context.user_data.get('trending_mode')
    
    # Handle trending mode
    if trending_mode:
        video = update.message.video
        if video:
            try:
                # Add or update video as trending
                result = await videos_collection.update_one(
                    {'file_id': video.file_id},
                    {
                        '$set': {
                            'is_trending': True,
                            'upload_timestamp': datetime.datetime.now(),
                            'uploaded_by': update.message.from_user.id
                        }
                    },
                    upsert=True
                )
                
                await update.message.reply_text("✅ Video added to trending list successfully!")
                context.user_data.pop('trending_mode', None)
                
            except Exception as e:
                logger.error(f"Error adding trending video: {e}")
                await update.message.reply_text("❌ Error adding video to trending list.")
        else:
            await update.message.reply_text("❌ Please send a video file.")
        return
    
    # Handle broadcast mode
    if not broadcast_mode:
        return
    
    try:
        # Get all users for broadcasting
        all_users = []
        async for doc in users_collection.find({}, {'user_id': 1}):
            all_users.append(doc['user_id'])
        
        if not all_users:
            await update.message.reply_text("❌ No users found to broadcast to.")
            return
        
        success_count = 0
        failed_count = 0
        
        progress_msg = await update.message.reply_text(
            f"📡 Starting broadcast to {len(all_users)} users...\n⏳ Please wait..."
        )
        
        # Handle different broadcast types
        if broadcast_mode == 'text':
            text_to_send = update.message.text
            
            for user_id in all_users:
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=f"📢 **Admin Announcement**\n\n{text_to_send}",
                        parse_mode=ParseMode.MARKDOWN
                    )
                    success_count += 1
                except TelegramError as e:
                    logger.error(f"Error broadcasting text to user {user_id}: {e}")
                    failed_count += 1
                
                await asyncio.sleep(0.05)  # Rate limiting
        
        elif broadcast_mode == 'image' and update.message.photo:
            photo = update.message.photo[-1]
            caption = update.message.caption or ""
            broadcast_caption = f"📢 **Admin Announcement**\n\n{caption}" if caption else "📢 **Admin Announcement**"
            
            for user_id in all_users:
                try:
                    await context.bot.send_photo(
                        chat_id=user_id,
                        photo=photo.file_id,
                        caption=broadcast_caption,
                        parse_mode=ParseMode.MARKDOWN,
                        protect_content=True
                    )
                    success_count += 1
                except TelegramError as e:
                    logger.error(f"Error broadcasting image to user {user_id}: {e}")
                    failed_count += 1
                
                await asyncio.sleep(0.05)
        
        elif broadcast_mode == 'video' and update.message.video:
            video = update.message.video
            caption = update.message.caption or ""
            broadcast_caption = f"📢 **Admin Announcement**\n\n{caption}" if caption else "📢 **Admin Announcement**"
            
            for user_id in all_users:
                try:
                    await context.bot.send_video(
                        chat_id=user_id,
                        video=video.file_id,
                        caption=broadcast_caption,
                        parse_mode=ParseMode.MARKDOWN,
                        protect_content=True
                    )
                    success_count += 1
                except TelegramError as e:
                    logger.error(f"Error broadcasting video to user {user_id}: {e}")
                    failed_count += 1
                
                await asyncio.sleep(0.05)
        
        else:
            await update.message.reply_text(
                f"❌ Invalid content type for {broadcast_mode} broadcast.\n"
                f"Please send the correct type of content."
            )
            return
        
        # Update progress message with results
        await progress_msg.edit_text(
            f"📡 **Broadcast Completed!**\n\n"
            f"✅ Successfully sent: {success_count}\n"
            f"❌ Failed: {failed_count}\n"
            f"📊 Total users: {len(all_users)}\n\n"
            f"Broadcast mode: {broadcast_mode.capitalize()}"
        )
        
        context.user_data.pop('broadcast_mode', None)
        
    except Exception as e:
        logger.error(f"Error during broadcast: {e}")
        await update.message.reply_text(
            f"❌ **Broadcast Error**\n\n"
            f"An error occurred during broadcast: {str(e)}"
        )

async def cancel_operation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancels any ongoing admin operation (broadcast, trending add)."""
    if not ADMIN_ID or update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Only admin can use this command.")
        return
    
    context.user_data.pop('broadcast_mode', None)
    context.user_data.pop('trending_mode', None)
    
    await update.message.reply_text(
        "✅ **Operation Cancelled**\n\n"
        "All ongoing operations have been cancelled.\n"
        "Use /start to return to the main menu."
    )

async def delete_message(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Deletes a message after a specified delay using JobQueue."""
    job_data = context.job.data
    try:
        await context.bot.delete_message(
            chat_id=job_data['chat_id'], 
            message_id=job_data['message_id']
        )
        logger.info(f"Auto-deleted message {job_data['message_id']} from chat {job_data['chat_id']}")
    except TelegramError as e:
        logger.error(f"Error deleting message: {e}")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shows bot statistics for users or admin."""
    user_id = update.message.from_user.id
    
    try:
        user_doc = await users_collection.find_one({'user_id': user_id})
        
        daily_count = user_doc['daily_count'] if user_doc else 0
        uploaded_videos = user_doc['uploaded_videos'] if user_doc else 0
        remaining = max(0, DAILY_LIMIT - daily_count)
        
        stats_text = f"📊 Your Stats:\n"
        stats_text += f"🆔 User ID: `{user_id}`\n"
        stats_text += f"📹 Videos watched today: {daily_count}/{DAILY_LIMIT}\n"
        stats_text += f"⏳ Remaining today: {remaining}\n"
        stats_text += f"📤 Videos uploaded: {uploaded_videos}"

        if ADMIN_ID and user_id == ADMIN_ID:
            total_users = await users_collection.count_documents({})
            total_videos = await videos_collection.count_documents({})
            trending_count = await videos_collection.count_documents({'is_trending': True})
            
            stats_text += f"\n\n📊 **Bot Admin Statistics:**\n"
            stats_text += f"👥 Total users: {total_users}\n"
            stats_text += f"📹 Total videos in collection: {total_videos}\n"
            stats_text += f"🔥 Trending videos: {trending_count}\n"
            stats_text += f"⚙️ Global Daily Limit: {DAILY_LIMIT}\n"
            stats_text += f"ℹ️ Your personal daily video usage is also capped at {DAILY_LIMIT}."

        await update.message.reply_text(stats_text, parse_mode=ParseMode.MARKDOWN)
        
    except Exception as e:
        logger.error(f"Error in stats: {e}")
        await update.message.reply_text("❌ Error loading statistics.")

async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles incoming photo messages, primarily for admin broadcast."""
    if ADMIN_ID and update.message.from_user.id == ADMIN_ID and context.user_data.get('broadcast_mode') == 'image':
        await handle_admin_content(update, context)
    else:
        await update.message.reply_text("📸 Thanks for the photo! Currently, I only support video uploads or admin broadcasts.")

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles incoming text messages, primarily for admin broadcast."""
    if ADMIN_ID and update.message.from_user.id == ADMIN_ID and context.user_data.get('broadcast_mode') == 'text':
        await handle_admin_content(update, context)
    else:
        await update.message.reply_text("💬 I'm not configured to respond to general text messages yet. Please use the buttons or send a video!")

async def post_init(application: Application) -> None:
    """Post-initialization hook to connect to MongoDB."""
    connection_success = await connect_to_mongodb()
    if not connection_success:
        logger.error("Failed to connect to MongoDB. Bot may not function properly.")
        # You could decide to exit here if MongoDB is critical
        # import sys
        # sys.exit(1)

def main() -> None:
    """Starts the bot and sets up all handlers."""
    if not API_TOKEN:
        logger.error("TELEGRAM_API_TOKEN not found in environment variables")
        return
    if not WEBHOOK_URL:
        logger.error("WEBHOOK_URL not found in environment variables. Webhook deployment requires this.")
        return
    
    # Create application with updated builder pattern
    application = (
        Application.builder()
        .token(API_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Add command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("cancel", cancel_operation))
    
    # Add callback query handler
    application.add_handler(CallbackQueryHandler(button))
    
    # Add message handlers
    application.add_handler(MessageHandler(filters.VIDEO, upload_video))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    
    # Add a periodic cleanup job (optional)
    if application.job_queue:
        application.job_queue.run_repeating(
            lambda context: logger.info("Bot is running..."),
            interval=3600,  # Every hour
            first=3600,
        )
    
    logger.info(f"Starting bot in webhook mode on {LISTEN_ADDRESS}:{PORT}...")
    logger.info(f"Webhook URL: {WEBHOOK_URL}")

    try:
        # Start webhook with error handling
        application.run_webhook(
            listen=LISTEN_ADDRESS,
            port=PORT,
            url_path="",
            webhook_url=WEBHOOK_URL,
            drop_pending_updates=True
        )
    except Exception as e:
        logger.error(f"Error starting webhook: {e}")
        raise

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise
