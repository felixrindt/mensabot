import requests
import os
import logging
from argparse import ArgumentParser
import re
from bs4 import BeautifulSoup
from collections import namedtuple
from peewee import SqliteDatabase, IntegerField, Model
import telepot
from telepot.exception import BotWasBlockedError, BotWasKickedError, TelegramError
from time import sleep
from functools import lru_cache
from datetime import datetime, timedelta, date
import pytz
import schedule
import dateutil.parser
import retrying
from emoji import emojize
from dotenv import load_dotenv

from pathlib import Path
import urllib.request
import itertools


load_dotenv()
log = logging.getLogger('mensabot')

TZ = pytz.timezone('Europe/Berlin')

parser = ArgumentParser()
parser.add_argument('--database', default='mensabot_clients.sqlite')



db = SqliteDatabase(None)


class Client(Model):
    chat_id = IntegerField(unique=True)

    class Meta:
        database = db


def ensure_png():
    today = date.today()
    folder = Path("UKEKasinoBot")
    folder.mkdir(exist_ok=True)

    pdf_filename = today.strftime("%Y_KW_%W.pdf")
    pdf_path = folder / Path(pdf_filename)
    if not pdf_path.exists():
        # delete old files
        for path in folder.glob("*.p*"):  # pdf and png ;)
            os.remove(path)
        
        # get file
        url = f"http://uke-healthkitchen.de/fileadmin/PDFs/{pdf_filename}"
        with urllib.request.urlopen(url) as request, open(pdf_path, "wb") as writer:
            writer.write(request.read())

    png_filename = today.strftime("%Y-%m-%d.png")
    png_path = folder / Path(png_filename)
    if not png_path.exists():
        os.system(
            "convert -limit memory 128mb -density 300x300 -background white -alpha remove " +
            f"{pdf_path!s} {png_path!s}"
        )

    return png_path




class MensaBot(telepot.Bot):

    def handle(self, msg):
        content_type, chat_type, chat_id = telepot.glance(msg)

        if chat_type == 'private':
            start = 'Du bekommst '
        else:
            start = 'Ihr bekommt '

        if content_type != 'text':
            return

        text = msg['text']

        if text.startswith('/start'):
            client, new = Client.get_or_create(chat_id=chat_id)

            if new:
                reply = start + 'ab jetzt jeden Tag um 11 das Menü'
            else:
                reply = start + 'das Menü schon!'

        elif text.startswith('/stop'):
            try:
                client = Client.get(chat_id=chat_id)
                client.delete_instance()
                reply = start + 'das Menü ab jetzt nicht mehr'
            except Client.DoesNotExist:
                reply = start + 'das Menü doch gar nicht'

        elif text.startswith('/menu') or text.startswith('/fullmenu'):
            self.sendMessage(chat_id, "Kommt sofort...", parse_mode='markdown')
            path = ensure_png()
            with open(path, "rb") as file:
                self.sendPhoto(chat_id, file)
            return
        else:
            reply = 'Das habe ich nicht verstanden'

        log.info('Sending message to {}'.format(chat_id))
        self.sendMessage(chat_id, reply, parse_mode='markdown')

    def send_menu_to_clients(self):
        day = datetime.now(TZ).date()
        
        if day.weekday() >= 5:
            return

        path = ensure_png()
        for client in Client.select():
            log.info('Sending menu to {}'.format(client.chat_id))
            try:
                with open(path, "rb") as file:
                    self.sendPhoto(client.chat_id, file)
            except (BotWasBlockedError, BotWasKickedError):
                log.warning('Removing client {}'.format(client.chat_id))
                client.delete_instance()
            except TelegramError as e:
                if e.error_code == 403:
                    log.warning('Removing client {}'.format(client.chat_id))
                    client.delete_instance()
            except Exception as e:
                logging.exception('Error sending message to client {}'.format(client.chat_id))


def main():
    args = parser.parse_args()

    log.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        fmt='%(asctime)s|%(levelname)s|%(name)s|%(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    log.addHandler(stream_handler)

    file_handler = logging.FileHandler('mensabot.log')
    file_handler.setFormatter(formatter)
    log.addHandler(file_handler)

    db.init(args.database)
    Client.create_table(safe=True)

    log.info("Using database {}".format(os.path.abspath(args.database)))
    log.info("Database contains {} active clients".format(Client.select().count()))

    bot = MensaBot(os.environ['BOT_TOKEN'])
    bot.message_loop()
    log.info('Bot runnning')

    schedule.every().day.at('10:30').do(bot.send_menu_to_clients)

    while True:
        try:
            schedule.run_pending()
        except:
            log.exception('Exception during schedule execution')
        sleep(1)


if __name__ == '__main__':

    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        log.info('Aborted')
