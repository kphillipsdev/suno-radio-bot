from dotenv import load_dotenv
import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from src.bot import bot

if __name__ == '__main__':
    load_dotenv()
    bot.run(os.getenv('BOT_TOKEN'))