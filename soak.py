import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
import logging
import math

from obrbot import hook
from obrbot.event import EventType

plugin_info = {
    "plugin_category": "channel-specific",
    "command_category_name": "doge-soak"
}

doge_nick = 'DogeWallet'
doge_channel = '#doge-coin'
soak_buildup_time = 15

minutes_active = 6

logger = logging.getLogger('obrbot')

balance_key = 'obrbot:plugins:obr-soak:balance'
soaked_key = 'obrbot:plugins:obr-soak:soaked'
timer_key = 'obrbot:plugins:obr-soak:timer'
timer_running = False


def format_delta(delta):
    """
    :type delta: timedelta
    """
    seconds = math.ceil(delta.total_seconds())

    if seconds < 0:
        seconds = -seconds
        sign = "negative "
    else:
        sign = ""

    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)

    if days > 0:
        return "{}{} days {} hours {} minutes {} seconds".format(sign, days, hours, minutes, seconds)
    elif hours > 0:
        return "{}{} hours {} minutes {} seconds".format(sign, hours, minutes, seconds)
    elif minutes > 0:
        return "{}{} minutes {} seconds".format(sign, minutes, seconds)
    else:
        return "{}{} seconds".format(sign, seconds)


@asyncio.coroutine
def get_balance(event):
    """
    :type event: obrbot.event.Event
    """
    raw_result = yield from event.async(event.db.get, balance_key)
    if raw_result is None:
        return 0
    else:
        return Decimal(raw_result.decode())


@asyncio.coroutine
def add_balance(event, balance):
    """
    :type event: obrbot.event.Event
    """
    logger.info("Adding {} to balance".format(balance))
    raw_result = yield from event.async(event.db.incrbyfloat, balance_key, balance)
    return Decimal(raw_result)


@asyncio.coroutine
def set_balance(event, balance):
    """
    :type event: obrbot.event.Event
    """
    raw_result = yield from event.async(event.db.set, balance_key, balance)
    return raw_result


@asyncio.coroutine
def get_soaked(event):
    """
    :type event: obrbot.event.Event
    """
    raw_result = yield from event.async(event.db.get, soaked_key)
    return Decimal(raw_result.decode())


@asyncio.coroutine
def add_soaked(event, balance):
    """
    :type event: obrbot.event.Event
    """
    raw_result = yield from event.async(event.db.incrbyfloat, soaked_key, balance)
    return Decimal(raw_result)


@asyncio.coroutine
def get_next_soak_time(event):
    """
    :type event: obrbot.event.Event
    """
    raw_result = yield from event.async(event.db.get, timer_key)
    if raw_result is None:
        soak_time = datetime.utcnow() + timedelta(seconds=soak_buildup_time)
        # turn date into timedelta so we can get a timestamp
        delta_since_epoch = soak_time - datetime.utcfromtimestamp(0)
        timestamp = delta_since_epoch.total_seconds()
        yield from event.async(event.db.set, timer_key, timestamp)
    else:
        soak_time = datetime.utcfromtimestamp(float(raw_result))
    return soak_time


@asyncio.coroutine
def reset_soak_timer(event):
    """
    :type event: obrbot.event.Event
    """
    yield from event.async(event.db.delete, timer_key)


@asyncio.coroutine
def get_active(event):
    users_counted = set()
    min_time = datetime.utcnow() - timedelta(minutes=minutes_active)
    channel = event.conn.channels[doge_channel]
    for event_type, nick, *rest in (yield from channel.get_history(event, min_time)):
        if event_type is EventType.message and nick != doge_nick:
            users_counted.add(nick.lower())
    logger.info("{} active according to log".format(len(users_counted)))
    return len(users_counted)


@asyncio.coroutine
def get_active_doge_wallet(event):
    """
    :type event: obrbot.event.Event
    """
    event.message("active", target=doge_nick)
    active = (yield from event.conn.wait_for("^Active Shibes: ([0-9]*)$", nick=doge_nick, chan=doge_nick)).group(1)

    logger.info("{} active according to DogeWallet")
    return int(active)


@asyncio.coroutine
def update_balance(event):
    """
    :type event: obrbot.event.Event
    """
    stored_balance = yield from get_balance(event)

    event.message("balance", target=doge_nick)
    balance = Decimal((yield from event.conn.wait_for("^([0-9]*\.?[0-9]*)$", nick=doge_nick, chan=doge_nick)).group(1))

    if stored_balance != balance:
        logger.info("Updated balance from {} to {}".format(stored_balance, balance))
        yield from set_balance(event, balance)

    return balance


@asyncio.coroutine
def wait_for_soaking(event):
    soaked_future = event.conn.wait_for(
        "{} is soaking [0-9]* shibes with [0-9\.]* Doge each. Total: ([0-9\.]*)"
        .format(event.conn.bot_nick), nick=doge_nick)

    failed_not_enough_doge = event.conn.wait_for("Not enough doge.", nick=doge_nick, chan=doge_nick)
    failed_rate_limited = event.conn.wait_for("Please wait [0-9\.]+ seconds to send commands.",
                                              nick=doge_nick, chan=doge_nick)

    done, pending = yield from asyncio.wait([soaked_future, failed_not_enough_doge, failed_rate_limited],
                                            loop=event.loop, return_when=asyncio.FIRST_COMPLETED, timeout=20)

    for future in pending:
        future.cancel()  # we don't care about these ones

    if soaked_future in done:
        return True, (yield from soaked_future)
    elif failed_not_enough_doge in done:
        return False, "Not enough doge."
    elif failed_rate_limited in done:
        return False, "Rate limited."
    else:
        return False, "{} failed to respond.".format(doge_nick)


@asyncio.coroutine
def soak(event, soak_time):
    """
    Soaks all balance. This will sleep until the specified soak time.
    :type event: obrbot.event.Event
    :type soak_time: datetime
    """
    global timer_running
    timer_running = True
    now = datetime.utcnow()
    if soak_time > now:
        # if the soak time is in the future, wait for it
        yield from asyncio.sleep((soak_time - now).total_seconds(), loop=event.loop)
    yield from reset_soak_timer(event)
    timer_running = False

    balance = yield from get_balance(event)
    doge_wallet_active = yield from get_active_doge_wallet(event)
    active = yield from get_active(event)
    if active < doge_wallet_active:
        event.message("Ping Dabo: My active count ({}) is less than DogeWallet's ({})".format(active,
                                                                                              doge_wallet_active))
        active = doge_wallet_active

    balance = yield from update_balance(event)
    soaking_per_person = int(balance / active)

    if soaking_per_person <= 0:
        event.message("Not enough doge to go around, saving for next soak.")
        return

    event.message(".soak {}".format(soaking_per_person))

    # Second is a match object if successful, error message string otherwise
    success, second = yield from wait_for_soaking(event)

    if success:
        match = second
        soaked_amount = Decimal(match.group(1))
        yield from add_balance(event, -soaked_amount)
        yield from add_soaked(event, soaked_amount)
    else:
        event.message("Soak failed: {}".format(second))
        if second == "Not enough doge.":
            event.message("My active count: {}, DogeWallet's active count: {}".format(active, doge_wallet_active))


@asyncio.coroutine
def add_doge(event, amount_added, sender=None):
    """
    :type event: obrbot.event.Event
    """
    balance = yield from add_balance(event, amount_added)

    active = yield from get_active(event)
    if balance < active:
        if sender is not None:
            event.message("Thanks for the tip, {}! I'll soak it when I get at least {} more doge".format(
                sender, active - balance))
        return

    soak_time = yield from get_next_soak_time(event)
    time_delta = soak_time - datetime.utcnow()

    if sender is not None:
        event.message("Thanks for the tip, {}! Soaking in {}!".format(sender, format_delta(time_delta)))

    if timer_running:
        return  # no need to start multiple timers
    asyncio.async(soak(event, soak_time), loop=event.loop)


@asyncio.coroutine
@hook.regex("([^ ]*) is soaking [0-9]* shibes with ([0-9\.]*) Doge each. Total: [0-9\.]*")
def soaked_regex(match, event):
    """
    :type match: re.__Match[str]
    :type event: obrbot.event.Event
    """
    if event.nick != doge_nick:
        return

    sender = match.group(1)

    if sender.lower() == event.conn.bot_nick.lower():
        return

    second = yield from event.conn.wait_for("(.*)", nick=event.nick, chan=event.chan_name)

    if event.conn.bot_nick.lower() not in second.group(1).lower().split():
        return  # we aren't being soaked

    amount = int(match.group(2))
    yield from add_doge(event, amount)


@asyncio.coroutine
@hook.regex("\[Wow\!\] ([^ ]*) sent ([^ ]*) ([0-9*]\.?[0-9]*) Doge")
def tipped(match, event):
    if match.group(2).lower() != event.conn.bot_nick.lower():
        return  # if we weren't tipped
    sender = match.group(1)
    amount = Decimal(match.group(3))
    yield from add_doge(event, amount, sender)


@asyncio.coroutine
@hook.command("balance", autohelp=False)
def balance_command(event):
    """
    :type event: obrbot.event.Event
    """
    balance = yield from get_balance(event)
    return "Balance: {}".format(balance)


@asyncio.coroutine
@hook.command("update-balance", autohelp=False)
def update_balance_command(event):
    """
    :type event: obrbot.event.Event
    """
    balance = yield from update_balance(event)
    return "Balance: {}".format(balance)


@asyncio.coroutine
@hook.command("soaked", autohelp=False)
def soaked_command(event):
    """
    :type event: obrbot.event.Event
    """
    balance = yield from get_soaked(event)
    return "Total Soaked: {}".format(balance)


@asyncio.coroutine
@hook.command("active", autohelp=False)
def active_command(event):
    """
    :type event: obrbot.event.Event
    """
    active = yield from get_active(event)
    return "Active: {}".format(active)
