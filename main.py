import logging
import re
import time
from copy import deepcopy
from enum import Enum
from typing import List, Optional
from datetime import datetime
import threading
import traceback
import html
import json
import itertools

import peewee
import telegram.error
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, Bot, MAX_MESSAGE_LENGTH, BotCommand, ParseMode
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext, CallbackQueryHandler
from jinja2 import Template
from peewee import SqliteDatabase, Model, DateTimeField, CharField, FixedCharField, IntegerField, BooleanField

from cowinapi import CoWinAPI, VaccinationCenter, CoWinTooManyRequests
from secrets import TELEGRAM_BOT_TOKEN, DEVELOPER_CHAT_ID

PINCODE_PREFIX_REGEX = r'^\s*(pincode)?\s*(?P<pincode_mg>\d+)\s*'
AGE_BUTTON_REGEX = r'^age: (?P<age_mg>\d+)'
CMD_BUTTON_REGEX = r'^cmd: (?P<cmd_mg>.+)'
DISABLE_TEXT_REGEX = r'\s*disable|stop|pause\s*'

# All the really complex configs:
# Following says, how often we should poll CoWin APIs for age group 18-44. In seconds
MIN_18_WORKER_INTERVAL = 60
# Following says, how often we should poll CoWin APIs for age group 45+. In seconds
MIN_45_WORKER_INTERVAL = 60 * 10  # 10 minutes
# Following decides, should we send a notification to user about 45+ or not.
# If we have sent an alert in the last 30 minutes, we will not bother them
MIN_45_NOTIFICATION_DELAY = 60 * 30
# Whenever an exception occurs, we sleep for these many seconds hoping things will be fine
# when we wake up. This surprisingly works most of the times.
EXCEPTION_SLEEP_INTERVAL = 10
# the amount of time we sleep in background workers whenever we hit their APIs
COWIN_API_DELAY_INTERVAL = 60 * 1 # 1 minute
# the amount of time we sleep when we get 403 from CoWin
LIMIT_EXCEEDED_DELAY_INTERVAL = 60 * 3  # 3 minutes
# allowing a user to add upto 3 nearby pincodes
MAX_PINCODES = 5
# Time Interval between two requests sent to the cowin server
# Will atleast take 3 seconds for all the fetch request for a particular user 
PINCODE_INFO_FETCH_INTERVAL = 0.25    # 0.25 second

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)

logger = logging.getLogger(__name__)

CoWinAPIObj = CoWinAPI()

db = SqliteDatabase('users.db', pragmas={
    'journal_mode': 'wal',
    'cache_size': -1 * 64000,  # 64MB
    'foreign_keys': 1,
    'ignore_check_constraints': 0
})


class AgeRangePref(Enum):
    Unknown = 0
    MinAge18 = 1
    MinAge45 = 2
    MinAgeAny = 3

    def __str__(self) -> str:
        if self == AgeRangePref.MinAge18:
            return "18-44"
        elif self == AgeRangePref.MinAge45:
            return "45+"
        else:
            return "Both"


class EnumField(IntegerField):

    def __init__(self, choices, *args, **kwargs):
        super(IntegerField, self).__init__(*args, **kwargs)
        self.choices = choices

    def db_value(self, value):
        return value.value

    def python_value(self, value):
        return self.choices(value)


# storage classes
class User(Model):
    created_at = DateTimeField(default=datetime.now)
    updated_at = DateTimeField(default=datetime.now)
    deleted_at = DateTimeField(null=True)
    last_alert_sent_at: datetime = DateTimeField(default=datetime.now)
    total_alerts_sent = IntegerField(default=0)
    telegram_id = CharField(max_length=220, unique=True)
    chat_id = CharField(max_length=220)
    pincode: str = FixedCharField(null=True, index=True)
    age_limit: AgeRangePref = EnumField(choices=AgeRangePref, default=AgeRangePref.Unknown)
    enabled = BooleanField(default=False, index=True)

    class Meta:
        database = db


def sanitise_msg(msg: str) -> str:
    """
    Telegram messages can't be more than `MAX_MESSAGE_LENGTH` bytes. So, this method truncates the message body
    with appropriate size and adds a footer saying message was truncated.

    CAUTION: This does a really naive truncation which might end up breaking a valid markdown / html to an invalid one
    and Telegram will reject that message.
    """
    if len(msg) < MAX_MESSAGE_LENGTH:
        return msg

    help_text = "\n\n (message truncated due to size)"
    msg_length = MAX_MESSAGE_LENGTH - len(help_text)
    return msg[:msg_length] + help_text


def get_main_buttons() -> List[InlineKeyboardButton]:
    return [
        InlineKeyboardButton("🛎️ Setup Alert", callback_data='cmd: setup_alert'),
        InlineKeyboardButton("🕵️ Check Open Slots", callback_data='cmd: check_slots'),
    ]


def get_age_kb() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("18-44", callback_data='age: 1'),
            InlineKeyboardButton("45+", callback_data='age: 2'),
            InlineKeyboardButton("Both", callback_data='age: 3'),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            *get_main_buttons()
        ],
        [
            InlineKeyboardButton("💡 Help", callback_data='cmd: help'),
            InlineKeyboardButton("🔒 Privacy Policy", callback_data='cmd: privacy')
        ],
    ])


def start(update: Update, _: CallbackContext) -> None:
    """
    Handles /start, the very first message the user gets whenever they start interacting with this bot
    """
    msg = """
    Hey there!🖖
    Welcome to Cowin Slot Alert Bot.
    👉 I will check weekly slot availability at your given pincodes and alert you when one becomes available.
    👉 To start, either click 🛎️ *Setup Alert* or 🕵️ *Check Open Slots*.
    👉 If you are a first time user, I will ask for your age and a pincode.
    👉 You can simply add more pincodes by sending me valid pincodes but you will get slot availability status for *atmost 5 pincodes* at once."""
    update.message.reply_text(msg, reply_markup=get_main_keyboard(), parse_mode="markdown")


def cmd_button_handler(update: Update, ctx: CallbackContext) -> None:
    """
    Whenever we send buttons to user, we also include callback data. 
    For commands, we usually send the data in the form
    of `cmd: <cmd_name>`. Check `get_main_buttons` or `get_main_keyboard` methods to see how the data is sent.

    When user clicks on those buttons, we also get the callback data. 
    Following figures out appropriate command to run
    """
    query = update.callback_query
    query.answer()

    if cmd := ctx.match.groupdict().get("cmd_mg"):
        cmd = cmd.strip()
        if cmd == "setup_alert":
            setup_alert_command(update, ctx)
            return
        elif cmd == "check_slots":
            check_slots_command(update, ctx)
            return
        elif cmd == "privacy":
            privacy_policy_handler(update, ctx)
            return
        elif cmd == "help":
            help_handler(update, ctx)
            return
        else:
            update.effective_chat.send_message("cmd not implemented yet")
            return


def get_help_text_short() -> str:
    return """\n👉 This bot will help you to check current available slots in one week and also, alert you when one becomes available. To start, either click on "🛎️ Setup Alert" or "🕵️ Check Open Slots". For first time users, bot will ask for age preference and pincode."""  ## noqa


def get_help_text() -> str:
    return """\n\n👉 *Setup Alert*\nUse this to setup an alert, it will send a message as soon as a slot becomes available. Select the age preference and provide the area pincode(s) of the vaccination center you would like to monitor. Do note that 18-44 slots are monitored more often than 45+.\nClick on /pause to stop alerts and /resume to enable them back.\n\n👉 *Check Open Slots*\nUse this to check the slots availability manually.\n\n👉 *Age Preference*\nTo change age preference, click on /age\n\n👉 *Pincode*\nClick on /pincode to change the pincode. Alternatively, you can send pincode any time and bot will update it.\n\n👉 *Delete*\nClick on /delete if you would like delete all your information."""  ## noqa


def help_handler(update: Update, _: CallbackContext):
    header = "💡 *Help*\n"
    msg = header + get_help_text_short() + get_help_text()
    update.effective_chat.send_message(msg, parse_mode="markdown",
                                       reply_markup=InlineKeyboardMarkup([[*get_main_buttons()]]))


def delete_cmd_handler(update: Update, _: CallbackContext):
    user: User
    try:
        user = User.get(User.telegram_id == update.effective_user.id, User.deleted_at.is_null(True))
    except peewee.DoesNotExist:
        update.effective_chat.send_message("Oops, none of your data exists to delete.")
        return

    user.deleted_at = datetime.now()
    user.enabled = False
    user.pincode = None
    user.age_limit = AgeRangePref.Unknown
    user.save()
    update.effective_chat.send_message("👉 Your previous data has been deleted. Click on /start to restart the bot.")


def help_command(update: Update, ctx: CallbackContext) -> None:
    help_handler(update, ctx)


def privacy_policy_handler(update: Update, _: CallbackContext):
    header = "🔒 Privacy Policy\n\n"
    msg = F"CoWin Slot Alert Bot stores minimal and only the information which is necessary. This includes:\n" \
          "  • Telegram account user id ({id})\n" \
          "  • The pincodes to search in CoWin site\n" \
          "  • Age preference\n" \
          "\nThe bot *does not have access* to your real name or phone number." \
          "\n\nClick on /delete to delete all your data."
    msg = header + msg.format(id=update.effective_user.id)
    update.effective_chat.send_message(msg, parse_mode="markdown")


def age_command(update: Update, _: CallbackContext):
    update.effective_chat.send_message("👉 Select your age preference", reply_markup=get_age_kb())
    return


def pincode_command(update: Update, _: CallbackContext):
    update.effective_chat.send_message("👉 Enter a pincode: ")


def get_pincodes_command(update: Update, _: CallbackContext) -> None:
    """
    Gets the list of pincodes for which the user has registered
    """
    user: User
    user, _ = get_or_create_user(telegram_id=update.effective_user.id, chat_id=update.effective_chat.id)
    msg = ""
    if user is None:
        msg = "Invalid User! 😦\n Please Start again by typing \start command"
    elif user.pincode is None:
        msg = "Sorry, you haven't entered any pincode yet."
    else:
        msg = "👉 Here is a list of your pincodes:\n" + str(user.pincode.split(":"))
    update.effective_chat.send_message(msg)
    return


def check_if_preferences_are_set(update: Update, ctx: CallbackContext) -> Optional[User]:
    """
    Checks if preferences for the current user are set or not. 
    If not set, asks them to set. If they are set, then
    returns the `User` object from DB.
    """
    user: User
    user, _ = get_or_create_user(telegram_id=update.effective_user.id, chat_id=update.effective_chat.id)
    if user.age_limit is None or user.age_limit == AgeRangePref.Unknown:
        age_command(update, ctx)
        return
    if user.pincode is None:
        pincode_command(update, ctx)
        return
    return user


def setup_alert_command(update: Update, ctx: CallbackContext) -> None:
    user = check_if_preferences_are_set(update, ctx)
    if not user:
        return
    user.enabled = True
    user.save()

    msg = "🔔 I have setup alerts for you.\nYou can sit back and relax😄\n"
    msg_18 = " • For age group 18-44, as soon as a slot becomes available I will send you a message.\n"
    msg_45 = " • For age group 45+, I will check slots availability for every 15 minutes and send a message if any slot is available.\n"
    if user.age_limit == AgeRangePref.MinAge18:
        msg = msg + msg_18
    elif user.age_limit == AgeRangePref.MinAge45:
        msg = msg + msg_45
    else:
        msg = msg + msg_18 + msg_45
    update.effective_chat.send_message(msg + "\nClick on /pause to pause the alerts.")


def disable_alert_command(update: Update, _: CallbackContext) -> None:
    user: User
    user, _ = get_or_create_user(telegram_id=update.effective_user.id, chat_id=update.effective_chat.id)
    user.enabled = False
    user.save()
    update.effective_chat.send_message("🔕 I have disabled alerts for you. Click on /resume to resume the alerts")


def get_available_centers_by_pin(pincode: str) -> List[VaccinationCenter]:
    vaccination_centers = CoWinAPIObj.calender_by_pin(pincode, CoWinAPI.today())
    if vaccination_centers:
        vaccination_centers = [vc for vc in vaccination_centers if vc.has_available_sessions()]
    else:
        vaccination_centers = []
    return vaccination_centers


def get_formatted_message(centers: List[VaccinationCenter], age_limit: AgeRangePref) -> str:
    """
    Given a list of vaccination centers, this method returns a nicely formatted message 
    which can be sent to the user

    param: age_limit is only used for display purposes. If the user's selected preference is both
    then we should show the age limit of the vaccination center
    """
    header = F"\n👉 Visit [CoWin Registration Site](https://selfregistration.cowin.gov.in/) for registration"
    # Some pincodes have more than 10 centers, in that case we just limit it to 10 and send those only.
    if len(centers) > 10:
        header = F"Showing 10 centers out of {len(centers)}. Check [CoWin Site](https://www.cowin.gov.in/home) for full list\n"  ## noqa

    display_age = True if age_limit == AgeRangePref.MinAgeAny else False

    # TODO: Fix this shit
    template = """
    {%- for c in centers[:10] %}
    *{{ c.name }}* {%- if c.fee_type == 'Paid' %}*(Paid)*{%- endif %}:{% for s in c.get_available_sessions() %}
        • {{s.date}}: {{s.capacity}}{%- if display_age %} *({{s.min_age_limit}}+)*{%- endif %}{% endfor %}
    {% endfor %}
    """
    tm = Template(template)
    return header + tm.render(centers=centers, display_age=display_age)


def filter_centers_by_age_limit(age_limit: AgeRangePref, centers: List[VaccinationCenter]) -> List[VaccinationCenter]:
    """
    filter_centers_by_age_limit filters the centers based on the age preferences set by the user

    If there's no filtering required, then it just returns the centers list as it is. If it needs to filter out centers,
    then it makes a deep copy of `centers`, modifies it and returns that
    """
    if not centers:
        return centers

    # if user hasn't set any age preferences, then just show everything
    if age_limit in [None, AgeRangePref.MinAgeAny, AgeRangePref.Unknown]:
        return centers

    filter_age: int
    if age_limit == AgeRangePref.MinAge18:
        filter_age = 18
    else:
        filter_age = 45

    # TODO: FIX THIS! This makes a deep copy of Vaccination Center objects
    centers_copy: List[VaccinationCenter] = deepcopy(centers)
    for vc in centers_copy:
        vc.sessions = vc.get_available_sessions_by_age_limit(filter_age)

    results: List[VaccinationCenter] = [vc for vc in centers_copy if vc.has_available_sessions()]
    return results


def get_message_header(user: User) -> str:
    return F"Following slots are available (pincode(s): {user.pincode}, age preference: {user.age_limit})\n"

def parse_pin_code_string(s: str) -> List[str]:
    lst = s.split(":")
    if len(lst) >= MAX_PINCODES:
        return lst[-MAX_PINCODES:]
    return lst

def check_slots_command(update: Update, ctx: CallbackContext) -> None:
    user = check_if_preferences_are_set(update, ctx)
    if not user:
        return
    vaccination_centers: List[VaccinationCenter]
    try:
        user_pincodes = parse_pin_code_string(user.pincode)
        regular_list = []
        for pincode in user_pincodes:
            time.sleep(PINCODE_INFO_FETCH_INTERVAL)
            regular_list.append(get_available_centers_by_pin(pincode))

        if not any(regular_list):
            update.effective_chat.send_message(F"Hey sorry 😅, seems there are no free slots available currently for \n(pincode(s): {user.pincode}, age preference: {user.age_limit})")
            return
        
        vaccination_centers = list(itertools.chain(*regular_list))
    except CoWinTooManyRequests:
        update.effective_chat.send_message(
            F"Hey sorry 😅, I wasn't able to reach [CoWin Site](https://www.cowin.gov.in/home) at this moment. "
            "Please try after few minutes.", parse_mode="markdown")
        return
    vaccination_centers = filter_centers_by_age_limit(user.age_limit, vaccination_centers)
    if not vaccination_centers:
        update.effective_chat.send_message(
            F"Hey sorry 😅, seems there are no free slots available currently for \n(pincode(s): {user.pincode}, age preference: {user.age_limit})")
        return

    msg: str = get_formatted_message(centers=vaccination_centers, age_limit=user.age_limit)
    msg = get_message_header(user=user) + msg
    update.effective_chat.send_message(sanitise_msg(msg), parse_mode='markdown')
    return


def default(update: Update, _: CallbackContext) -> None:
    update.message.reply_text("Sorry, I did not understand. Click on /help to know valid commands.")


def get_or_create_user(telegram_id: str, chat_id) -> (User, bool):
    return User.get_or_create(telegram_id=telegram_id,
                              defaults={'chat_id': chat_id})


def set_age_preference(update: Update, ctx: CallbackContext) -> None:
    query = update.callback_query
    query.answer()

    age_pref = ctx.match.groupdict().get("age_mg")
    if age_pref is None:
        return

    user: User
    user, _ = get_or_create_user(telegram_id=update.effective_user.id, chat_id=update.effective_chat.id)
    user.age_limit = AgeRangePref(int(age_pref))
    user.updated_at = datetime.now()
    user.deleted_at = None
    user.save()

    if user.pincode:
        update.effective_chat.send_message(F"I have set your age preference to {user.age_limit} 😉",
                                           reply_markup=InlineKeyboardMarkup([[*get_main_buttons()]]))
    else:
        update.effective_chat.send_message(
            F"I have set your age preference to {user.age_limit}.\nPlease enter one pincode to proceed")


def set_pincode(update: Update, ctx: CallbackContext) -> None:
    pincode = ctx.match.groupdict().get("pincode_mg")
    if not pincode:
        return
    pincode = pincode.strip()
    # validating pincode is the third difficult problem of computer science
    if pincode in ["000000", "111111", "123456"] or not len(pincode) == 6:
        update.effective_chat.send_message("Uh oh! That doesn't look like a valid pincode.\n"
                                           "Please enter a *valid pincode* to proceed")
        return
    user: User
    user, _ = get_or_create_user(telegram_id=update.effective_user.id, chat_id=update.effective_chat.id)

    if user.pincode is not None:
        lst = user.pincode.split(":")
        if len(lst) >= MAX_PINCODES:
            lst = lst[-(MAX_PINCODES-1):]
            pcs = "".join(str(pc + str(":")) for pc in lst)
            user.pincode = pcs + pincode
        else:
            user.pincode += str(str(":") + pincode)
    else:
        user.pincode = pincode

    user.updated_at = datetime.now()
    user.deleted_at = None
    user.save()

    msg: str = F"I have updated your pincode list with {pincode}. If you'd like to change it, send a valid pincode " \
               "any time to me."
    reply_markup: InlineKeyboardMarkup
    if user.age_limit is None or user.age_limit == AgeRangePref.Unknown:
        reply_markup = get_age_kb()
        msg = msg + "\n\nSelect age preference:"
    else:
        reply_markup = InlineKeyboardMarkup([[*get_main_buttons()]])
    update.effective_chat.send_message(msg, reply_markup=reply_markup)


def send_alert_to_user(bot: telegram.Bot, user: User, centers: List[VaccinationCenter]) -> None:
    if not centers:
        return
    msg: str = get_formatted_message(centers=centers, age_limit=user.age_limit)
    msg = "🛎️ *[ALERT!]* \n" + get_message_header(user=user) + msg + \
          "\n Click on /pause to disable the notifications"
    try:
        bot.send_message(chat_id=user.chat_id, text=sanitise_msg(msg), parse_mode='markdown')
    except telegram.error.Unauthorized:
        # looks like this user blocked us. simply disable them
        user.enabled = False
        user.save()
    else:
        user.last_alert_sent_at = datetime.now()
        user.total_alerts_sent += 1
        user.save()


def periodic_background_worker():
    while True:
        try:
            time.sleep(MIN_45_WORKER_INTERVAL)  # sleep for 10 mins
            logger.info("starting a bg worker - periodic_background_worker")
            background_worker(age_limit=AgeRangePref.MinAge45)
            logger.info("bg worker executed successfully - periodic_background_worker")
        except CoWinTooManyRequests:
            logging.error("got 403 - too many requests - periodic_background_worker")
            time.sleep(LIMIT_EXCEEDED_DELAY_INTERVAL)
        except Exception as e:
            logger.exception("periodic_background_worker", exc_info=e)
            time.sleep(EXCEPTION_SLEEP_INTERVAL)


def frequent_background_worker():
    while True:
        try:
            logger.info("starting a bg worker - frequent_background_worker")
            background_worker(age_limit=AgeRangePref.MinAge18)
            logger.info("bg worker executed successfully - frequent_background_worker")
            time.sleep(MIN_18_WORKER_INTERVAL)  # sleep for 30 seconds
        except CoWinTooManyRequests:
            logging.error("got 403 - too many requests - frequent_background_worker")
            time.sleep(LIMIT_EXCEEDED_DELAY_INTERVAL)
        except Exception as e:
            logger.exception("frequent_background_worker", exc_info=e)
            time.sleep(EXCEPTION_SLEEP_INTERVAL)


def background_worker(age_limit: AgeRangePref):
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    time_now = datetime.now()
    # find all distinct pincodes where pincode is not null and at least one user exists with alerts enabled
    query = User.select(User.pincode).where(
        (User.pincode.is_null(False)) & (User.enabled == True) & (
                (User.age_limit == AgeRangePref.MinAgeAny) | (User.age_limit == age_limit))).distinct()
    # TODO: Quick hack to load all pincodes in memory
    query = list(query)
    for distinct_user in query:
        vaccination_centers = []
        regular_list = []
        lst = distinct_user.pincode.split(":")
        for pincode in lst:
            regular_list.append(get_available_centers_by_pin(pincode))
        
        if not any(regular_list):
            continue
        
        vaccination_centers = list(itertools.chain(*regular_list))
        if not vaccination_centers:
            continue

        # sleep, since we have hit CoWin APIs
        time.sleep(COWIN_API_DELAY_INTERVAL)

        # find all users for this pincode and alerts enabled
        user_query = User.select().where(
            (User.pincode == distinct_user.pincode) & (User.enabled == True) & (
                    (User.age_limit == AgeRangePref.MinAgeAny) | (User.age_limit == age_limit)
            ))
        for user in user_query:
            delta = time_now - user.last_alert_sent_at
            # if user age limit is 45, then we shouldn't ping them too often
            if user.age_limit == AgeRangePref.MinAge45:
                if delta.seconds < MIN_45_NOTIFICATION_DELAY:
                    continue
                filtered_centers = filter_centers_by_age_limit(user.age_limit, vaccination_centers)
                if not filtered_centers:
                    continue
                send_alert_to_user(bot, user, filtered_centers)

            # for users with age limit of 18, we send the alert
            if user.age_limit == AgeRangePref.MinAge18:
                filtered_centers = filter_centers_by_age_limit(user.age_limit, vaccination_centers)
                if not filtered_centers:
                    continue
                filtered_centers = filter_centers_by_age_limit(user.age_limit, vaccination_centers)
                if not filtered_centers:
                    continue
                send_alert_to_user(bot, user, filtered_centers)

            # here comes the tricky part. for users who have set up both
            # we would want to send 18-44 alerts more often than 45+
            if user.age_limit == AgeRangePref.MinAgeAny:
                filtered_centers: List[VaccinationCenter]
                if delta.seconds < MIN_45_NOTIFICATION_DELAY:
                    # include only 18-44 results
                    filtered_centers = filter_centers_by_age_limit(AgeRangePref.MinAge18, vaccination_centers)
                else:
                    # include both results
                    filtered_centers = filter_centers_by_age_limit(user.age_limit, vaccination_centers)
                if not filtered_centers:
                    continue
                send_alert_to_user(bot, user, filtered_centers)


# works in the background to remove all the deleted user rows permanently
def clean_up() -> None:
    # delete all the users permanently whose deleted_at value is not null
    User.delete().where(User.deleted_at.is_null(False))


# source: https://github.com/python-telegram-bot/python-telegram-bot/blob/master/examples/errorhandlerbot.py
def error_handler(update: object, context: CallbackContext) -> None:
    logger.error(msg="Exception while handling an update:", exc_info=context.error)
    tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
    tb_string = ''.join(tb_list)

    update_str = update.to_dict() if isinstance(update, Update) else str(update)
    message = (
        f'An exception was raised while handling an update\n'
        f'<pre>update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))}'
        '</pre>\n\n'
        f'<pre>context.chat_data = {html.escape(str(context.chat_data))}</pre>\n\n'
        f'<pre>context.user_data = {html.escape(str(context.user_data))}</pre>\n\n'
        f'<pre>{html.escape(tb_string)}</pre>'
    )

    try:
        context.bot.send_message(chat_id=DEVELOPER_CHAT_ID, text=message, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.exception("error_handler", exc_info=e)


def main() -> None:
    # initialise bot and set commands
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    bot.set_my_commands([
        BotCommand(command='start', description='start the bot session'),
        BotCommand(command='age', description='change age preference'),
        BotCommand(command='addpincode', description='add pincode'),
        BotCommand(command='getpincodes', description='get registered pincodes'),
        BotCommand(command='alert', description='enable alerts on new slots'),
        BotCommand(command='resume', description='resume alerts on new slots'),
        BotCommand(command='pause', description='disable alerts on new slots'),  
        BotCommand(command='delete', description='delete user data'),
        BotCommand(command='help', description='provide help on how to use the bot'),
    ])

    # connect and create tables
    db.connect()
    db.create_tables([User, ])
    # create the required index
    # TODO:
    # User.add_index(User.enabled, User.pincode,
    #                where=((User.enabled == True) & (User.pincode.is_null(False))))

    # initialise the handler
    updater = Updater(TELEGRAM_BOT_TOKEN)

    # Add handlers
    updater.dispatcher.add_handler(CommandHandler("start", start))
    updater.dispatcher.add_handler(CommandHandler("age", age_command))
    updater.dispatcher.add_handler(CommandHandler("addpincode", pincode_command))
    updater.dispatcher.add_handler(CommandHandler("getpincodes", get_pincodes_command))
    updater.dispatcher.add_handler(CommandHandler("alert", setup_alert_command))
    updater.dispatcher.add_handler(CommandHandler("resume", setup_alert_command))
    updater.dispatcher.add_handler(CommandHandler("pause", disable_alert_command))
    updater.dispatcher.add_handler(CommandHandler("delete", delete_cmd_handler))
    updater.dispatcher.add_handler(CommandHandler("help", help_command))

    updater.dispatcher.add_handler(CallbackQueryHandler(set_age_preference, pattern=AGE_BUTTON_REGEX))
    updater.dispatcher.add_handler(CallbackQueryHandler(cmd_button_handler, pattern=CMD_BUTTON_REGEX))
    updater.dispatcher.add_handler(MessageHandler(Filters.regex(
        re.compile(PINCODE_PREFIX_REGEX, re.IGNORECASE)), set_pincode))
    updater.dispatcher.add_handler(MessageHandler(Filters.regex(
        re.compile(DISABLE_TEXT_REGEX, re.IGNORECASE)), disable_alert_command))
    updater.dispatcher.add_handler(MessageHandler(~Filters.command, default))
    updater.dispatcher.add_error_handler(error_handler)

    # launch two background threads, one for slow worker (age group 45+) and another for fast one (age group 18-44)
    threading.Thread(target=frequent_background_worker).start()
    threading.Thread(target=periodic_background_worker).start()

    # Start the Bot
    updater.start_polling()
    # block it, baby
    updater.idle()


if __name__ == '__main__':
    main()
