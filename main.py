import os
import random
from datetime import datetime, timedelta
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram import Update, ReplyKeyboardMarkup
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.service_account import Credentials
from googleapiclient.http import MediaIoBaseDownload
import requests
from flask import Flask, request

# Environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", 3))  # Default limit is 3 if not set
TEMP_VIDEO_PATH = os.getenv("TEMP_VIDEO_PATH", "New folder")
NOWPAYMENTS_API_KEY = os.getenv("NOWPAYMENTS_API_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # Webhook URL for Render deployment

# Initialize Google Drive API
credentials = Credentials.from_service_account_info(
    eval(GOOGLE_SERVICE_ACCOUNT_JSON),  # Parse the JSON string from environment variable
    scopes=["https://www.googleapis.com/auth/drive.readonly"]
)
drive_service = build('drive', 'v3', credentials=credentials)

# Ensure the temporary folder exists
os.makedirs(TEMP_VIDEO_PATH, exist_ok=True)

# Track user limits and subscriptions
user_limits = {}
user_subscriptions = {}

# Flask app for webhook
app = Flask(__name__)

# Function to get list of video file ids from Google Drive
def get_video_files():
    try:
        results = drive_service.files().list(q="mimeType contains 'video/'", fields="files(id, name)").execute()
        items = results.get('files', [])
        return items
    except HttpError as error:
        print(f"An error occurred: {error}")
        return []

# Function to download video from Google Drive
def download_video(file_id):
    try:
        request = drive_service.files().get_media(fileId=file_id)
        file_path = os.path.join(TEMP_VIDEO_PATH, f"{file_id}.mp4")
        with open(file_path, 'wb') as f:
            downloader = MediaIoBaseDownload(f, request)
            done = False
            while not done:
                status, done = downloader.next_chunk()
            return file_path
    except HttpError as error:
        print(f"An error occurred: {error}")
        return None

# Function to clean up the temporary folder after video has been sent
def clean_temp_folder(file_path):
    if os.path.exists(file_path):
        os.remove(file_path)

# Create a payment link
def create_payment(user_id):
    url = "https://api.nowpayments.io/v1/invoice"
    headers = {
        "x-api-key": NOWPAYMENTS_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "price_amount": 99,  # Price in INR
        "price_currency": "inr",
        "order_id": f"user_{user_id}_{int(datetime.now().timestamp())}",
        "order_description": "Premium Plan for 1 month",
        "success_url": f"https://t.me/<your_bot_username>",  # Replace with your bot link
        "ipn_callback_url": WEBHOOK_URL,  # Render webhook URL for payment notifications
    }
    response = requests.post(url, json=payload, headers=headers)
    if response.status_code == 200:
        return response.json().get("invoice_url")
    else:
        print(f"Error creating payment: {response.text}")
        return None

# Handle webhook notifications
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    if data.get('payment_status') == 'finished':
        order_id = data.get('order_id')
        user_id = int(order_id.split("_")[1])  # Extract user ID from order ID
        # Activate premium plan
        user_subscriptions[user_id] = datetime.now() + timedelta(days=30)  # Premium valid for 1 month
        print(f"Payment successful for user {user_id}")
    return "OK", 200

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a welcome message with reply keyboard."""
    chat_id = update.effective_chat.id

    # Define the reply keyboard buttons
    reply_keyboard = [["View Plan 💵", "Get Video 🍒"]]

    # Create the reply keyboard markup
    reply_markup = ReplyKeyboardMarkup(reply_keyboard, resize_keyboard=True)

    await context.bot.send_message(chat_id=chat_id, text="Welcome! Choose an option below:", reply_markup=reply_markup)

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate and send a payment link for purchasing premium."""
    chat_id = update.effective_chat.id
    payment_url = create_payment(chat_id)
    if payment_url:
        await context.bot.send_message(chat_id=chat_id, text=f"Buy premium for ₹99 using this link: {payment_url}")
    else:
        await context.bot.send_message(chat_id=chat_id, text="Failed to generate payment link. Please try again later.")

async def handle_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle user replies from the reply keyboard."""
    user_text = update.message.text
    user_id = update.effective_user.id

    if user_text == "Get Video 🍒":
        await send_video(update, context, user_id)
    elif user_text == "View Plan 💵":
        await update.message.reply_text("Buy premium for ₹99 using /buy command.")
    else:
        await update.message.reply_text("Please use the provided buttons.")

async def send_video(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Send a random video, prioritizing unsent videos and respecting daily limits."""
    global user_limits, user_subscriptions

    # Check if user is premium
    now = datetime.now()
    if user_id in user_subscriptions and user_subscriptions[user_id] > now:
        limit = 100  # Premium users have 100 videos per day
    else:
        limit = DAILY_LIMIT

    # Initialize user data if not present
    if user_id not in user_limits:
        user_limits[user_id] = {
            "count": 0,
            "reset_time": now,
            "sent_videos": set()
        }

    user_data = user_limits[user_id]

    # Reset daily limit if the time has passed
    if now >= user_data["reset_time"]:
        user_data["count"] = 0
        user_data["reset_time"] = now + timedelta(days=1)
        user_data["sent_videos"] = set()

    # Check if daily limit is reached
    if user_data["count"] >= limit:
        remaining_time = (user_data["reset_time"] - now).seconds // 3600
        await update.message.reply_text(f"Daily limit reached! Wait {remaining_time} hours for more videos or purchase premium using /buy command.")
        return

    # Get the list of videos from Google Drive
    video_files = get_video_files()

    if not video_files:
        await update.message.reply_text("No videos found in your Google Drive folder.")
        return

    # Remove already sent videos from the list
    unsent_videos = [video for video in video_files if video['id'] not in user_data["sent_videos"]]

    if not unsent_videos:
        await update.message.reply_text("All videos have been sent. Please try again tomorrow or purchase premium.")
        return

    # Select the next unsent video
    selected_video = random.choice(unsent_videos)
    video_file_id = selected_video['id']

    # Download the video
    video_path = download_video(video_file_id)
    if not video_path:
        await update.message.reply_text("Failed to download the video.")
        return

    try:
        # Open the video file and send it
        with open(video_path, "rb") as video_file:
            await context.bot.send_video(chat_id=update.effective_chat.id, video=video_file)

        # Update user data
        user_data["count"] += 1
        user_data["sent_videos"].add(video_file_id)

        # Clean up the temporary folder after sending the video
        clean_temp_folder(video_path)

    except Exception as e:
        await update.message.reply_text(f"Failed to send video: {e}")

def main():
    """Start the bot."""
    print("Bot is running... Press Ctrl+C to stop.")

    # Create the application
    application = Application.builder().token(BOT_TOKEN).build()

    # Add command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("buy", buy))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_reply))

    # Run the bot
    application.run_polling()

if __name__ == "__main__":
    main()

# Run the Flask app
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
