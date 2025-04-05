from telegram import Update
from telegram.ext import (Updater, CommandHandler, MessageHandler, Filters,
                          CallbackContext)
import configparser
import logging
from ChatGPT_HKBU import HKBU_ChatGPT
from pymongo import MongoClient
from typing import Optional
from bson import ObjectId

# Global variables
global mongo_client, db, config, chatgpt


def main():
    # Load configuration
    global config, mongo_client, db, chatgpt
    config = configparser.ConfigParser()
    config.read('config.ini')

    # Initialize logging
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )

    # Initialize MongoDB connection
    try:
        mongo_client = MongoClient(
            config['MONGODB']['CONN_STRING'],
            serverSelectionTimeoutMS=5000
        )
        mongo_client.server_info()  # Test connection
        db = mongo_client[config['MONGODB']['DB_NAME']]
        logging.info(f"Successfully connected to MongoDB. Shard key field: {config['MONGODB']['SHARD_KEY_FIELD']}")
    except Exception as e:
        logging.error(f"MongoDB connection failed: {e}")
        raise

    # Initialize Telegram Bot
    updater = Updater(token=config['TELEGRAM']['ACCESS_TOKEN'], use_context=True)
    dispatcher = updater.dispatcher

    # Register command handlers
    dispatcher.add_handler(CommandHandler("add", add))
    dispatcher.add_handler(CommandHandler("help", help_command))
    dispatcher.add_handler(CommandHandler("hello", hello))

    # Initialize ChatGPT
    chatgpt = HKBU_ChatGPT()
    chatgpt_handler = MessageHandler(Filters.text & (~Filters.command), equip_chatgpt)
    dispatcher.add_handler(chatgpt_handler)

    # Start the Bot
    updater.start_polling()
    logging.info("Bot is running...")
    updater.idle()


def help_command(update: Update, context: CallbackContext) -> None:
    try:
        update.message.reply_text(
            '🌟 Available commands:\n'
            '/add <keyword> - Count statistics\n'
            '/hello <name> - Greet someone\n'
            '/help - Show help\n\n'
            '💡 Features:\n'
            '1. Send "recommend activities" to get interest-based suggestions\n'
            '2. Send "find partners" to find partners with shared interests'
        )
    except Exception as e:
        logging.error(f"Help command error:{e}")


def add(update: Update, context: CallbackContext) -> None:
    global config, mongo_client, db, chatgpt
    try:
        if not context.args:
            raise ValueError("Missing keyword")

        keyword = ' '.join(context.args)
        shard_key = config['MONGODB']['SHARD_KEY_FIELD']

        # Build query with shard key
        query = {"_id": keyword}
        if shard_key != "_id":
            query[shard_key] = keyword  # Add shard key field

        result = db.counters.update_one(
            query,
            {"$inc": {"count": 1}},
            upsert=True
        )

        # Get latest count
        doc = db.counters.find_one({"_id": keyword})
        new_count = doc["count"] if doc else 1

        update.message.reply_text(f'✅ [{keyword}] Count updated: {new_count}')

    except Exception as e:
        logging.error(f"Add command error: {e}")
        update.message.reply_text('❌ Usage: /add <keyword>')


def hello(update: Update, context: CallbackContext) -> None:
    global config, mongo_client, db, chatgpt
    try:
        name = ' '.join(context.args) or 'friend'
        update.message.reply_text(f'👋 Hello, {name}!')
    except:
        update.message.reply_text('❌ Usage: /hello <name>')


def equip_chatgpt(update: Update, context: CallbackContext):
    global config, mongo_client, db, chatgpt
    try:
        user_id = update.effective_user.id
        message_text = update.message.text.strip().lower()
        shard_key = config['MONGODB']['SHARD_KEY_FIELD']

        # Build base query (with shard key)
        base_query = {"_id": user_id}
        if shard_key != "_id":
            base_query[shard_key] = user_id

        # Handle special commands
        if message_text == 'recommend activities':
            db.user_states.update_one(
                base_query,
                {"$set": {"state": "waiting_interest"}},
                upsert=True
            )
            update.message.reply_text('🎯 Please tell me your interest (e.g., programming, photography):')
            return

        if message_text == 'find partners':
            if not update.effective_user.username:
                update.message.reply_text('❌ Please set a Telegram username first (Settings → Username)')
                return

            db.user_states.update_one(
                base_query,
                {"$set": {"state": "waiting_match_interest"}},
                upsert=True
            )
            update.message.reply_text('🤝 Please enter the interest you want to match:')
            return

        # Handle state flow
        user_state = db.user_states.find_one(base_query)
        current_state = user_state["state"] if user_state else None

        if current_state == 'waiting_interest':
            interest = message_text
            prompt = f'Recommend 3 online activities about {interest}. Requirements:\n- Include activity name, description, time, and participation link\n- Output in Chinese list format'
            reply = chatgpt.submit(prompt)
            db.user_states.delete_one(base_query)
            update.message.reply_text(f'🎁 Recommended activities for you:\n\n{reply}')
            return

        if current_state == 'waiting_match_interest':
            interest = message_text
            username = update.effective_user.username

            # Save user data (with shard key)
            user_data = {
                "_id": user_id,
                "interest": interest,
                "username": username,
                "status": "available"
            }
            if shard_key != "_id":
                user_data[shard_key] = user_id

            db.users.update_one(
                {"_id": user_id},
                {"$set": user_data},
                upsert=True
            )

            # Add to match pool
            db.match_pool.update_one(
                {"interest": interest},
                {"$addToSet": {"users": user_id}},
                upsert=True
            )

            db.user_states.delete_one(base_query)
            update.message.reply_text('🔍 Added to match pool. Searching for partners...')
            match_users(update, context)
            return

        # Default ChatGPT handling
        reply = chatgpt.submit(message_text)
        update.message.reply_text(reply)

    except Exception as e:
        logging.error(f"Error processing message: {e}", exc_info=True)
        update.message.reply_text('⚠️ Service temporarily unavailable. Please try again later')


def match_users(update: Update, context: CallbackContext):
    global config, mongo_client, db, chatgpt
    try:
        shard_key = config['MONGODB']['SHARD_KEY_FIELD']

        # Find all matchable interest groups
        for pool in db.match_pool.find({"users.1": {"$exists": True}}):
            interest = pool["interest"]
            user_ids = pool["users"][:2]  # Take first two users

            # Get user info
            users = list(db.users.find({"_id": {"$in": user_ids}}))
            if len(users) < 2:
                continue

            user1, user2 = users[0], users[1]

            try:
                # Send match notification
                context.bot.send_message(
                    user1["_id"],
                    f'🎉 Match successful!\nShared interest: {interest}\nPartner username: @{user2["username"]}'
                )
                context.bot.send_message(
                    user2["_id"],
                    f'🎉 Match successful!\nShared interest: {interest}\nPartner username: @{user1["username"]}'
                )

                # Remove users from match pool
                db.match_pool.update_one(
                    {"_id": pool["_id"]},
                    {"$pull": {"users": {"$in": user_ids}}}
                )

                # Update user status
                db.users.update_many(
                    {"_id": {"$in": user_ids}},
                    {"$set": {"status": "matched"}}
                )

            except Exception as e:
                logging.error(f"Failed to send message: {e}")
                # Remove invalid users
                db.match_pool.update_one(
                    {"_id": pool["_id"]},
                    {"$pull": {"users": {"$in": user_ids}}}
                )

    except Exception as e:
        logging.error(f"Error during matching: {e}", exc_info=True)


if __name__ == '__main__':
    main()