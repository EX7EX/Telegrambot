import requests
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes, CallbackContext
import pymongo
from pymongo import MongoClient, ASCENDING, DESCENDING
from dotenv import load_dotenv
import os
import logging
from datetime import datetime, date
from apscheduler.schedulers.background import BackgroundScheduler
import firebase_admin
from firebase_admin import credentials, messaging

# Load environment variables from .env file
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
MONGO_URI = os.getenv('MONGO_URI')
DB_NAME = os.getenv('DB_NAME', 'Cluster0')

# MongoDB connection handling
try:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    client.admin.command('ping')  # Test connection
    db = client[DB_NAME]
    users_collection = db['users']
    tasks_collection = db['tasks']
    logs_collection = db['audit_logs']
    logging.info(f"Successfully connected to MongoDB database: {DB_NAME}")
except pymongo.errors.PyMongoError as e:
    logging.critical(f"Failed to connect to MongoDB: {e}")
    exit(1)

# Set up logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Ensure Telegram Bot Token is provided
if not TELEGRAM_BOT_TOKEN:
    logging.critical("No Telegram Bot Token found. Please check your .env file.")
    exit(1)

# Define UTC timezone
utc = pytz.UTC

# Helper function: Check if user has joined the required channel
async def has_joined_channel(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id="@simplco", user_id=user_id)
        return member.status in ['member', 'administrator', 'creator']
    except Exception as e:
        logging.warning(f"Error checking channel membership for user {user_id}: {e}")
        return False

# Updated start command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    username = update.effective_user.username or f"User_{user_id}"

    # Ensure the user joins the required channel
    if not await has_joined_channel(context, user_id):
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Please join @simplco to access all bot features!",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Join Channel", url="https://t.me/simplco")]
            ])
        )
        return

    # Initialize user record if not exists
    user_record = users_collection.find_one({"user_id": user_id})
    if not user_record:
        users_collection.insert_one({
            "user_id": user_id,
            "username": username,
            "balance": 0,
            "wallet": 0,
            "rank": None,
            "joined_channel": True,
            "last_claimed": None,
            "tasks_completed": []
        })
        logging.info(f"New user registered: {username} (ID: {user_id})")
    else:
        users_collection.update_one({"user_id": user_id}, {"$set": {"joined_channel": True}})

    # Send main menu
    reply_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("Launch Mini App", url="t.me/SimplQ_bot/rebltasks")],
        [InlineKeyboardButton("Invite Friends", callback_data='invite_friends')],
        [InlineKeyboardButton("Leaderboard", callback_data='leaderboard')],
        [InlineKeyboardButton("Balance", callback_data='balance')],
        [InlineKeyboardButton("Daily Rewards", callback_data='daily_rewards')],
        [InlineKeyboardButton("Wallet", callback_data='wallet')],
        [InlineKeyboardButton("Ranking", callback_data='ranking')]
    ])

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Welcome to the bot! Choose an option:",
        reply_markup=reply_markup
    )

# Inline Button Handler
async def button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    handler_mapping = {
        'invite_friends': invite_friends,
        'leaderboard': leaderboard,
        'balance': balance,
        'wallet': wallet,
        'ranking': ranking,
        'daily_rewards': daily_rewards
    }

    handler = handler_mapping.get(query.data)
    if handler:
        await handler(update, context)
    else:
        fallback_message = "Unknown action. Please choose a valid option from the menu."
        logging.error(f"Unknown callback query: {query.data}")
        await query.message.reply_text(fallback_message)

# Balance Handler
async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.callback_query.from_user.id

    # Fetch user data from MongoDB
    user = users_collection.find_one({"user_id": user_id})
    if user:
        balance = user.get("balance", 0)
        await update.callback_query.message.reply_text(f"Your current balance is {balance} $REBLCOINS.")
    else:
        await update.callback_query.message.reply_text("No user record found. Please register using /start.")

# Invite Friends Handler
async def invite_friends(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.callback_query.from_user
    referral_id = user.username if user.username else str(user.id)
    referral_link = f"https://t.me/SimplQ_bot?start={referral_id}"
    await update.callback_query.message.reply_text(f"Share this link with your friends: {referral_link}")

# Leaderboard Handler
async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        top_users = users_collection.find().sort("balance", pymongo.DESCENDING).limit(10)
        leaderboard_text = "🏆 Leaderboard 🏆\n\n"
        for i, user in enumerate(top_users, 1):
            username = user.get('username', 'Anonymous')
            leaderboard_text += f"{i}. {username}: {user.get('balance', 0)} $REBLCOINS\n"
        await update.callback_query.message.reply_text(leaderboard_text)
    except Exception as e:
        logging.error(f"Error retrieving leaderboard: {e}")
        await update.callback_query.message.reply_text("Error retrieving leaderboard data.")

# Daily Rewards Handler
async def daily_rewards(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.callback_query.from_user.id
    today = utc.localize(datetime.combine(date.today(), datetime.min.time()))

    user = users_collection.find_one({"user_id": user_id})
    if not user:
        await update.callback_query.message.reply_text("No user record found. Please register using /start.")
        return

    last_claimed = user.get("last_claimed")
    if last_claimed:
        last_claimed = utc.localize(last_claimed) if not last_claimed.tzinfo else last_claimed
        if last_claimed.date() == today.date():
            await update.callback_query.message.reply_text("You've already claimed your daily reward today.")
            return

    users_collection.update_one(
        {"user_id": user_id},
        {"$set": {"balance": user.get("balance", 0) + 10, "last_claimed": today}}
    )
    await update.callback_query.message.reply_text("You have successfully claimed 10 $REBLCOINS!")

# Ranking Handler (Optimized for scalability)
async def ranking(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.callback_query.from_user.id
    user_rank = list(users_collection.aggregate([
        {"$setWindowFields": {
            "partitionBy": None,
            "sortBy": {"balance": -1},
            "output": {
                "rank": {"$rank": {}}
            }
        }},
        {"$match": {"user_id": user_id}}
    ]))

    if user_rank:
        user = user_rank[0]
        rank = user.get("rank")
        balance = user.get("balance", 0)
        await update.callback_query.message.reply_text(f"Your rank: {rank}\nYour balance: {balance} $REBLCOINS.")
    else:
        await update.callback_query.message.reply_text("No user record found. Please register using /start.")

# Wallet Handler
async def wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.callback_query.from_user.id

    # Fetch user data
    user = users_collection.find_one({"user_id": user_id})
    if user:
        wallet = user.get("wallet", 0)
        balance = user.get("balance", 0)
        await update.callback_query.message.reply_text(f"Your wallet: {wallet}\nYour balance: {balance} $REBLCOINS.")
    else:
        await update.callback_query.message.reply_text("No user record found. Please register using /start.")

# Send push notification
def send_push_notification(token, title, body):
    """Send a push notification."""
    # Initialize Firebase Admin SDK
    cred = credentials.Certificate('/Users/mac/Documents/WORKSPACE/untitled folder/journal/private keys/firebase/rebltasks-d156e-c10cc3a3732e.json')
    firebase_admin.initialize_app(cred)

    # Create the message
    message = messaging.Message(
        notification=messaging.Notification(
            title=title,
            body=body
        ),
        token=token  # User's device token
    )

    # Send the message
    response = messaging.send(message)
    print(f"Push notification sent: {response}")

# Scheduled Tasks
def daily_reminder():
    """Send daily reminders to all users."""
    users = users_collection.find()
    for user in users:
        send_push_notification(user.get("device_token"), "Reminder", "Claim your daily reward!")

# Set up scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(daily_reminder, 'interval', days=1)
scheduler.start()

# Telegram Bot Setup
def main():
    # Telegram Bot application setup
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button))
    
    # Start the bot
    application.run_polling()

if __name__ == "__main__":
    main()