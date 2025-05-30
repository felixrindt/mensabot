import requests
import os
import logging
from argparse import ArgumentParser
import re
from bs4 import BeautifulSoup
from collections import namedtuple
from peewee import SqliteDatabase, IntegerField, Model, BooleanField
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
from urllib.error import HTTPError
import itertools

import subprocess
from email.message import EmailMessage


load_dotenv()
log = logging.getLogger("mensabot")

TZ = pytz.timezone("Europe/Berlin")

parser = ArgumentParser()
parser.add_argument("--database", default="mensabot_clients.sqlite")
parser.add_argument("--from-email")
parser.add_argument("--to-email")


db = SqliteDatabase(None)


class Client(Model):
    chat_id = IntegerField(unique=True)
    only_monday_full_menu = BooleanField(default=False)

    class Meta:
        database = db


def send_email(from_addr, to_addrs, msg_subject, msg_body):
    msg = EmailMessage()
    msg.set_content(msg_body)
    msg["From"] = from_addr
    msg["To"] = to_addrs
    msg["Subject"] = msg_subject
    sendmail_location = "/usr/sbin/sendmail"
    log.info("Sending email to {}".format(to_addrs))
    subprocess.run([sendmail_location, "-t", "-oi"], input=msg.as_bytes())


def ensure_png():
    today = date.today()
    folder = Path("UKEKasinoBot_images")
    folder.mkdir(exist_ok=True)

    # https://www.uke.de/dateien/servicegesellschaften/kge-klinik-gastronomie-eppendorf/2022-kw-45.pdf
    pdf_filename = today.strftime("%Y-kw-%W.pdf")
    pdf_path = folder / Path(pdf_filename)
    if not pdf_path.exists():
        # delete old files
        for path in folder.glob("*.p*"):  # pdf and png ;)
            os.remove(path)

        # get file
        candidates = []
        WEBPAGE = "https://www.uke.de/organisationsstruktur/tochtergesellschaften/klinik-gastronomie-eppendorf/index.html"
        with urllib.request.urlopen(WEBPAGE) as request:
            soup = BeautifulSoup(request.read(), "lxml")
            links = soup.find_all(
                name="a",
                string=re.compile(rf"KW\s*0?{today.isocalendar()[1]}"),
                attrs={
                    "class": "uke-download",
                    "href": re.compile(".pdf$"),
                }
            )
            log.debug(f"souped up {links}")
            if len(links) == 1:
                candidates.append('https://www.uke.de' + links[0].get("href"))

        candidates += [
            f"https://www.uke.de/dateien/servicegesellschaften/kge-klinik-gastronomie-eppendorf/{name}"
            for name in [
                pdf_filename.lower(),
                pdf_filename.upper(),
                pdf_filename.lower().rstrip(".pdf") + "-2.pdf",
                pdf_filename.upper().rstrip(".PDF") + "-2.pdf",
            ]
        ]
        for url in candidates:
            try:
                log.info(f"trying {url=}")
                with urllib.request.urlopen(url) as request, open(
                    pdf_path, "wb"
                ) as writer:
                    writer.write(request.read())
            except HTTPError:
                continue
            else:
                break

    png_filename = today.strftime("%Y-%m-%d.png")
    png_path = folder / Path(png_filename)
    png_name = str(png_path).rstrip(".png")
    if not png_path.exists():
        os.system(f"pdftoppm {pdf_path!s} {png_name} -png -singlefile")
        os.system(
            "convert -limit memory 128mb -density 300x300 -background white -alpha remove "
            + f"{png_path!s} {png_path!s}"
        )

    return png_path


HELP_TEXT = """Soll das Menü nur Montags kommen, schicke /mondays. Um zurück zu Mo-Fr zu wechseln, schicke /weekdays.
Erhalte das Menü sofort mit /menu.
Starte und stoppe den Bot mit /start und /stop.
Hilfe gibt es mit /help.
Bei Fragen und Anregungen schicke eine Nachricht, die mit /feedback beginnt.
"""


class MensaBot(telepot.Bot):
    def __init__(self, *args, **kwargs):
        self.from_email = kwargs.pop("from_email", None)
        self.to_email = kwargs.pop("to_email", None)
        super().__init__(*args, **kwargs)

    def handle(self, msg):
        content_type, chat_type, chat_id = telepot.glance(msg)

        if content_type != "text":
            return

        text = msg["text"]

        if text.startswith("/start"):
            client, new = Client.get_or_create(
                chat_id=chat_id,
                only_monday_full_menu=True,
            )
            if new:
                reply = "Das Menü kommt ab jetzt jeden Tag um 10:30.\n" + HELP_TEXT
            else:
                reply = "Das Menü ist bereits abonniert!\n" + HELP_TEXT
        elif text.startswith("/help"):
            reply = HELP_TEXT
        elif text.startswith("/mondays"):
            try:
                client = Client.get(chat_id=chat_id)
                client.only_monday_full_menu = True
                client.save()
                reply = "Das Menü kommt jetzt nur noch am Montag."
            except Client.DoesNotExist:
                reply = "Das Menü ist gar nicht abonniert.\n" + HELP_TEXT
        elif text.startswith("/weekdays"):
            try:
                client = Client.get(chat_id=chat_id)
                client.only_monday_full_menu = False
                client.save()
                reply = "Das Menü kommt jetzt Montag bis Freitag."
            except Client.DoesNotExist:
                reply = "Das Menü ist gar nicht abonniert.\n" + HELP_TEXT
        elif text.startswith("/stop"):
            try:
                client = Client.get(chat_id=chat_id)
                client.delete_instance()
                reply = "Das Menü wurde abbestellt."
            except Client.DoesNotExist:
                reply = "Das Menü ist bereits abbestellt.\n" + HELP_TEXT
        elif text.startswith("/feedback"):
            if self.to_email and self.from_email:
                content = text.lstrip("/feedback").strip()
                if content:
                    send_email(
                        from_addr=self.from_email,
                        to_addrs=self.to_email,
                        msg_subject="Kasinobot Feedback",
                        msg_body=f"Vom Chat mit der ID {chat_id} kam folgendes Feedback:\n\n{content}",
                    )
                    reply = "Das habe ich weitergegeben."
                else:
                    reply = "Schreib deine Nachricht hinter /feedback"
            else:
                reply = "Es ist kein Feedbackempfänger verfügbar."
        elif text.startswith("/menu") or text.startswith("/fullmenu"):
            try:
                self.sendMessage(chat_id, "Kommt sofort...", parse_mode="markdown")
                path = ensure_png()
                with open(path, "rb") as file:
                    self.sendPhoto(chat_id, file)
            except BlockWasBlockedError:
                Client.delete().where(chat_id=chat_id)
            finally:
                return
        else:
            reply = "Das habe ich nicht verstanden."

        log.info("Sending message to {}".format(chat_id))
        try:
            self.sendMessage(chat_id, reply, parse_mode="markdown")
        except BlockWasBlockedError:
            Client.delete().where(chat_id=chat_id)

    def send_menu_to_clients(self):
        day = datetime.now(TZ).date()

        if day.weekday() >= 5:
            return

        log.info("Sending menu to clients")
        path = ensure_png()
        for client in Client.select():
            if client.only_monday_full_menu and day.weekday() >= 1:
                continue
            log.info("Sending menu to {}".format(client.chat_id))
            try:
                with open(path, "rb") as file:
                    self.sendPhoto(client.chat_id, file)
            except (BotWasBlockedError, BotWasKickedError):
                log.warning("Removing client {}".format(client.chat_id))
                client.delete_instance()
            except TelegramError as e:
                if e.error_code == 403:
                    log.warning("Removing client {}".format(client.chat_id))
                    client.delete_instance()
            except Exception as e:
                logging.exception(
                    "Error sending message to client {}".format(client.chat_id)
                )


def main():
    if not os.environ.get("BOT_TOKEN"):
        raise ValueError("You need to provide the BOT_TOKEN env variable")
    args = parser.parse_args()

    log.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        fmt="%(asctime)s|%(levelname)s|%(name)s|%(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    log.addHandler(stream_handler)

    file_handler = logging.FileHandler("mensabot.log")
    file_handler.setFormatter(formatter)
    log.addHandler(file_handler)

    db.init(args.database)
    Client.create_table(safe=True)

    log.info("Using database {}".format(os.path.abspath(args.database)))
    log.info("Database contains {} active clients".format(Client.select().count()))

    bot = MensaBot(
        os.environ["BOT_TOKEN"], from_email=args.from_email, to_email=args.to_email
    )
    bot.message_loop()
    log.info("Bot runnning")

    schedule.every().day.at("10:30").do(bot.send_menu_to_clients)

    while True:
        try:
            schedule.run_pending()
        except:
            log.exception("Exception during schedule execution")
        sleep(1)


if __name__ == "__main__":

    try:
        main()
    except ValueError:
        log.info("Error, trying image")
        path = ensure_png()
        log.info(f"Saved to {path}")
    except (KeyboardInterrupt, SystemExit):
        log.info("Aborted")
