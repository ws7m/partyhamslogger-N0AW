"""ContestBot: a pure, deterministic "banter" engine for the network chat.

Given a snapshot of per-station activity (plus the previous snapshot), it decides
what fun, ham-radio-flavoured automated message to post — cheering a station whose
rate is climbing, gently ribbing one that's gone quiet, dropping one of a hundred
terrible puns (or, for a station with lines of its own, a personal one), or (on
Field Day, near the top of the hour) nudging a station about the WWV "power hour".
It is intentionally Qt-free, randomness-free, and wall-clock-free: every decision
is a pure function of its inputs (the caller passes
a monotonically increasing ``counter`` and, for time-aware bits, the current
minute), so the whole thing is trivially unit-testable.

The UI layer (main window) builds the snapshots on a timer, **throttles** posts to
roughly one every :data:`BANTER_COOLDOWN_MIN` minutes (so the bot isn't noisy),
and posts whatever string this returns to the chat. Because there are 100 puns and
posts are ~20 minutes apart, a given line won't recur for many hours.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Name we sign automated messages with, so everyone can tell it's the bot.
BOT_NAME = "ContestBot"

#: A 15-minute rate must climb by at least this many QSOs to count as "heating up".
HEATING_UP_DELTA = 3

#: No new QSO for at least this many minutes => a station is "slacking".
SLACKING_AGE_MIN = 20.0

#: A station must have logged something already before we rib it for going quiet
#: (so we don't pick on someone who simply hasn't started yet).
SLACKING_MIN_TOTAL = 1

#: How often (minutes) the UI should let the bot speak — keeps it un-spammy.
BANTER_COOLDOWN_MIN = 20

#: Minutes past the hour for the Field Day "WWV power hour" nudge.
WWV_MINUTE = 50


@dataclass(frozen=True)
class StationSnapshot:
    """A plain, immutable view of one station's activity at a moment in time."""

    operator: str
    rate_15: int = 0
    total: int = 0
    last_qso_age_min: float | None = None  # None => never worked anyone
    #: The station callsign, when it differs from ``operator`` (which may hold a
    #: name). Personal lines are matched against either — see :data:`PERSONAL`.
    call: str = ""


# Cheers for a station whose 15-minute rate just jumped. ``{op}`` => operator.
HEATING_UP = (
    "{op} is really making waves — that rate is positively frequency-ent!",
    "Whoa, {op} is heating up the bands faster than a hot tube amp. CQ indeed!",
    "{op} is on a roll — someone check their dits, they're running away!",
    "Look at {op} go! That pile-up is no QRP operation. 73 to the competition.",
    "{op} just kicked the rate into overdrive. Resistance is fertile!",
    "Ohm my! {op} is conducting a masterclass in working 'em fast.",
    "{op} is cooking with RF now — watt a performance!",
    "Somebody hand {op} a QSL card factory, they can't stop running 'em.",
    "{op} is climbing the rate ladder — clearly nobody told them the bands are 'dead'.",
    "{op} is putting out so much signal the SWR meter filed a complaint. Brilliant!",
)

# Gentle ribbing for a station that's gone quiet. ``{op}`` => operator.
SLACKING = (
    "Earth to {op}… your antenna asleep? No QSOs in a while — don't get it in a twist!",
    "{op}, the bands miss you. Did you wander off for a coffee and a long ragchew?",
    "Hello {op}? Long path? Your rate flatlined — even the band noise is bored.",
    "{op} has gone QRT-ish. Wouldn't you know it — the bands go dead the second you nap.",
    "Psst, {op}: CQ is a verb. Twist that dial and make some contacts!",
    "{op}, your logbook is gathering QRM dust. Time to key up and elmer the airwaves!",
    "We've lost {op} to the great void of zero QSOs. Did the feedline eat you?",
    "{op}, if you wanted a rest you should've picked a slower hobby. Get on the air!",
    "Anyone seen {op}? Last heard chasing a phantom 6m opening. Come back, the run is hot!",
    "{op}, your rate dropped to QRP-zero. Crank the wick and work somebody!",
)

# Field Day "WWV power hour" nudge near the top of the hour. ``{op}`` => operator.
WWV_POWER_HOUR = (
    "{op}, the WWV power hour is almost upon us — warm up that filter and key up at the top!",
    "Heads up {op}: WWV power hour at the top of the hour. Mark your watches!",
    "{op}, are you ready for the WWV power hour? Synchronize your dits!",
    "Ten to the hour, {op} — ready for the WWV power hour? Last one to 599 buys the coffee.",
    "{op}, WWV power hour incoming. Set your clock, steel your nerves, run that rate!",
    "Calling {op} — ready for the WWV power hour? The ionosphere waits for no one.",
)

#: How often a fallback post is a personal line rather than a generic pun, when
#: someone with personal lines is on the network. One in four keeps them a
#: surprise (~80 minutes apart at the default cooldown) rather than a running gag.
PERSONAL_EVERY = 4

# Lines aimed at one specific station, keyed by callsign. Used in place of a
# generic pun while that operator is on the network. ``{op}`` => operator label,
# though a line addressed to someone by name needs no placeholder.
PERSONAL: dict[str, tuple[str, ...]] = {
    "WW8L": (
        "Tim, are you sure you have paralleled enough generators? "
        "This could be a long activation!",
        "Tim, don't forget to rotate the radials before calling CQ parks in the dark...",
    ),
    "W8XAL": (
        "Dave, do you really need to run at 65 WPM? That's kinda fast...",
    ),
}


# A hundred generic, ham-flavoured one-liners dropped when nothing else is up.
PUNS = (
    "Two antennas got married. The wedding was meh, but the reception was excellent. 73!",
    "I'd tell you a UDP joke, but you might not get it. So here's a ham one instead.",
    "Why did the contester bring a ladder? To reach the high bands, of course!",
    "Remember: a balun a day keeps the common-mode away. Stay grounded out there.",
    "QSL? More like QS-yes! Keep those cards coming, folks.",
    "Don't get your antenna in a twist — the gain will come back around.",
    "Some say the bands are dead. We say: just resonant at a different frequency!",
    "Be an elmer today: work a newbie, share a 73, and pass the good vibes down the line.",
    "Frequency-ently asked question: are we having fun yet? The answer is affirmative.",
    "Watt's up, everyone? Just here to ohm-it-up and keep the current conversation going.",
    "My favourite mode? Whichever one's working. Resistance to fun is futile!",
    "Keep calm and CQ on. The pile-ups won't call themselves.",
    "Propagation tip: a positive attitude has excellent gain in every direction. 73!",
    "If at first you don't succeed, QSY and try again. The bands are forgiving.",
    "Stay tuned, stay matched, and may your SWR be ever in your favour.",
    "I told my antenna a joke. It didn't laugh, but the reception was great.",
    "Why do hams never get lost? They always know their grid square.",
    "My coax is so old it remembers spark gap.",
    "I'm reading a book on helium — impossible to put down, unlike my mic in a pileup.",
    "CW operators do it with rhythm. Dit dit.",
    "SSB: where everyone sounds like they're talking through a sock. Lovely sock, though.",
    "The band's so quiet you can hear the electrons sulking.",
    "My SWR is 1:1 and so is my excitement. Perfectly matched.",
    "Worked all states? I've barely worked all the rooms in my house.",
    "Field Day forecast: 100% chance of bug spray and dropped contacts.",
    "I put up a new dipole. The neighbours put up with it. Teamwork!",
    "Why did the QSO go to therapy? Too many unresolved exchanges.",
    "Roses are red, violets are blue, my RST is 599 and so are you.",
    "The only thing spreading faster than the band opening is the coffee at the op table.",
    "I don't always work DX, but when I do, the band closes immediately.",
    "My logbook has more entries than my diary. Priorities, people.",
    "Solar flux up, attitude up, dupes down. That's the dream.",
    "A wise elmer once said: it's not the watts, it's how you wiggle them.",
    "The bands are a buffet — load up while the propagation's hot.",
    "I asked my rig for a contact. It said 'QRZ?' Rude, but fair.",
    "Grounding: because nobody likes a shocking surprise.",
    "They say silence is golden. On 20 meters it's just nobody answering your CQ.",
    "My antenna analyzer and I have trust issues.",
    "CQ contest at 3 a.m.? Yes, I'm aware. The points don't sleep.",
    "A handheld walks into a bar. Bartender: 'we don't serve spurious emissions.'",
    "Why bring string to a contest? To work some long-wire DX.",
    "Logging tip: the QSO you didn't log never happened. Spooky.",
    "The S-meter pegged. So did my grin.",
    "Propagation is the universe deciding whether you get to have fun today.",
    "I've got 99 problems but a pitch ain't one — perfect zero beat.",
    "Dead band? No such thing. Just a band practising mindfulness.",
    "My favourite exchange: 'you're 59' — even when you're clearly not.",
    "Antenna height: the one number hams will happily exaggerate.",
    "Coffee, coax, and CQ — the three C's of a good Field Day.",
    "I worked a station so weak even the noise floor felt sorry for him.",
    "Tuning up across the band: a crime in some countries, a hobby in mine.",
    "The RF gods demand a sacrifice. I offer this slightly-too-long patch cable.",
    "Nothing humbles you like a 5-watt station outrunning your kilowatt.",
    "My handheld has more memory channels than I have friends. We don't discuss it.",
    "SWR 3:1? That's not a fault, that's a personality.",
    "The band opened, the pileup roared, and my dog left the room. Worth it.",
    "Why be normal when you can be resonant?",
    "A good operator listens twice and transmits once.",
    "The DX is always weaker on the other side of the QSB.",
    "I named my amplifier 'Bias' — it always runs a little hot.",
    "The early ham catches the gray line.",
    "Ragchew: the original podcast, just with more static.",
    "My antenna fell down. I'm calling it a temporary NVIS experiment.",
    "Smoke test passed: no smoke. Bar was on the floor; we cleared it.",
    "They told me to ground my station. Now it won't stop talking about being centered.",
    "73 is just 'best wishes' with better SWR.",
    "I keep my keyer fast and my coffee faster.",
    "When in doubt, blame the feedline. It's usually right.",
    "The bands don't care about your excuses, but they respect a good antenna.",
    "Worked split for the first time. Felt like a wizard. Was just confused.",
    "My ATU is the bravest little box I own.",
    "CQ DX, CQ DX — and the only reply is my own echo off the ionosphere.",
    "A pileup is just enthusiastic chaos with callsigns.",
    "I don't need a gym; I have a tower to climb and a rotor that's stuck.",
    "The most powerful mode is enthusiasm. Closely followed by FT8, sadly.",
    "Static crashes: nature's way of saying 'talk louder.'",
    "My bureau card arrived — took longer than the contact's marriage.",
    "The band's hot, the rate's climbing, and a neighbour's TV just gave up.",
    "Patience is a virtue; on a DX pileup it's a survival skill.",
    "I told my exchange to the void and the void came back 59 001.",
    "A balanced antenna is a happy antenna. Be like the antenna.",
    "The rig hummed, the band buzzed, and the magic smoke stayed put. Victory.",
    "Some chase points; I chase that one perfect S9 report.",
    "My logbook says I'm popular. My logbook is a generous liar.",
    "Six meters: dead for months, then suddenly Italy. Classic six.",
    "I'd explain SWR but it would just reflect badly on me.",
    "The contest never ends; it just QSYs to next weekend.",
    "Keep your dits short and your friendships long.",
    "A watt saved is a watt earned, said no contester ever.",
    "The antenna farm is the only farm where you harvest decibels.",
    "I worked the world from my backyard. The world has no idea where my backyard is.",
    "CW is just texting for people with great timing.",
    "The ionosphere is moody, but she always comes around at gray line.",
    "My rig has 1000 menus. I use three. We coexist.",
    "Nothing says 'I love this hobby' like soldering at midnight.",
    "The best DX is the friend you ragchew along the way.",
    "Crank the antenna, not the ego. The band rewards humility.",
    "My new vertical radiates in all directions — mostly toward the neighbour's complaints.",
    "Worked a DXpedition on the first call. I'm framing this moment, not the QSL.",
    "May your noise be low, your DX be loud, and your coffee never empty. 73!",
)


def _personal_lines(named: list[StationSnapshot]) -> list[tuple[str, str]]:
    """``(template, operator_label)`` for every present station with personal lines.

    Matched on the station call *or* the operator label, since ``operator`` holds
    a callsign by default but can be set to a name. Sorted so the choice stays
    deterministic regardless of roster order.
    """
    out: list[tuple[str, str]] = []
    for station in named:
        for key in (station.call, station.operator):
            lines = PERSONAL.get(key.strip().upper()) if key else None
            if lines:
                out.extend((line, station.operator) for line in lines)
                break  # a station matched by call must not match again by operator
    out.sort()
    return out


def _pick(pool: tuple[str, ...], counter: int) -> str:
    """Deterministically pick one item from ``pool`` using ``counter``."""
    return pool[counter % len(pool)]


def choose_message(
    snapshot: list[StationSnapshot],
    previous: list[StationSnapshot] | None,
    counter: int,
    *,
    minute_of_hour: int | None = None,
    field_day: bool = False,
) -> str | None:
    """Decide what ContestBot should say, and return it (or ``None``).

    Pure and deterministic. Priority: the Field Day WWV power-hour nudge (near the
    top of the hour), then cheer a station heating up, then rib a quiet one, then
    fall back to a generic pun. Returns ``None`` only when there's no named station
    to talk to. Throttling (how *often* this is acted on) is the caller's job — see
    :data:`BANTER_COOLDOWN_MIN`.

    ``counter`` rotates deterministically through each pool. ``minute_of_hour`` and
    ``field_day`` drive the WWV nudge. The returned string is already prefixed with
    the bot name, e.g. ``"ContestBot: …"``.
    """
    named = [s for s in snapshot if s.operator]
    prev_by_op = {s.operator: s for s in (previous or [])}

    # (a) Field Day: near the top of the hour, nudge a station about WWV.
    if field_day and minute_of_hour == WWV_MINUTE and named:
        op = named[counter % len(named)].operator
        return _say(_pick(WWV_POWER_HOUR, counter).format(op=op))

    # (b) Heating up — biggest 15-minute rate jump since last check wins.
    best_op, best_delta = None, 0
    for s in named:
        before = prev_by_op.get(s.operator)
        if before is None:
            continue
        delta = s.rate_15 - before.rate_15
        if delta >= HEATING_UP_DELTA and delta > best_delta:
            best_op, best_delta = s.operator, delta
    if best_op is not None:
        return _say(_pick(HEATING_UP, counter).format(op=best_op))

    # (c) Slacking — the station idle the longest (past the threshold) wins.
    idle_op, idle_age = None, SLACKING_AGE_MIN
    for s in named:
        if s.total < SLACKING_MIN_TOTAL:
            continue
        age = s.last_qso_age_min
        if age is not None and age >= idle_age:
            idle_op, idle_age = s.operator, age
    if idle_op is not None:
        return _say(_pick(SLACKING, counter).format(op=idle_op))

    # (d) Fallback whenever there's someone around to enjoy it: a generic pun,
    #     or — every PERSONAL_EVERY-th time — a line aimed at a specific station.
    if named:
        personal = _personal_lines(named)
        if personal and counter % PERSONAL_EVERY == 0:
            line, op = personal[(counter // PERSONAL_EVERY) % len(personal)]
            return _say(line.format(op=op))
        return _say(_pick(PUNS, counter))
    return None


def _say(text: str) -> str:
    """Stamp a message with the bot name so it reads as automated in chat."""
    return f"{BOT_NAME}: {text}"
