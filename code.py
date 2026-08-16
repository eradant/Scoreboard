# SPDX-FileCopyrightText: 2023 Liz Clark for Adafruit Industries
#
# SPDX-License-Identifier: MIT
"""ESPN scoreboard for the MatrixPortal S3 driving a 128x64 RGB matrix.

One HTTP request per league per refresh collects every game a followed team
appears in, and a single display group is rewritten as those games cycle, so
memory use stays flat no matter how many teams are in the list.
"""

import gc
import os
import time

import board
import displayio
import framebufferio
import microcontroller
import neopixel
import rgbmatrix
import terminalio
import wifi

import adafruit_connection_manager
import adafruit_json_stream as json_stream
import adafruit_requests
from adafruit_datetime import datetime, timedelta
from adafruit_display_text import bitmap_label
from adafruit_ticks import ticks_add, ticks_diff, ticks_ms

# adafruit_json_stream before 0.9.0 never marks an object as finished after
# handing back the value of its *last* key. The parser then fast-forwards
# looking for a closing brace it has already passed, eats the parent's brace,
# and every event after the first comes back as the wrong type. This code reads
# status.type.shortDetail, which is exactly that last key, so an old copy of the
# library breaks it. Check loudly rather than fail three layers down.
_JSON_STREAM_VERSION = getattr(json_stream, "__version__", "0.0.0")
try:
    _JSON_STREAM_PARTS = tuple(int(part) for part in _JSON_STREAM_VERSION.split(".")[:2])
except ValueError:
    _JSON_STREAM_PARTS = (0, 0)
if _JSON_STREAM_PARTS < (0, 9):
    print(
        "WARNING: adafruit_json_stream {} is too old, scores will not parse. "
        "Copy 0.9.0 or newer into /lib.".format(_JSON_STREAM_VERSION)
    )

# --------------------------------------------------------------------- config

# One entry per league: sport, league, logo folder, and the ESPN team
# abbreviations to follow. Teams appear on screen in the order listed.
LEAGUES = (
    (
        "football",
        "nfl",
        "/team0_logos",
        (
            "GB", "CHI", "DET", "MIN",       # NFC North
            "LAR", "SEA", "SF", "ARI",       # NFC West
            "DAL", "NYG", "PHI", "WSH",      # NFC East
            "ATL", "CAR", "NO", "TB",        # NFC South
            "BAL", "PIT", "CIN", "CLE",      # AFC North
            "TEN", "JAX", "HOU", "IND",      # AFC South
            "MIA", "BUF", "NE", "NYJ",       # AFC East
            "DEN", "LV", "KC", "LAC",        # AFC West
        ),
    ),
    # ("baseball", "mlb", "/team1_logos", ("SF", "OAK")),
    # ("hockey", "nhl", "/team3_logos", ("SJ", "VGK")),
)

TIMEZONE_OFFSET = -8        # hours from UTC
TIMEZONE_NAME = "PST"
FONT_COLOR = 0xFFFFFF

# ESPN's leader categories, and the position tag each one gets on screen.
# Anything not listed here is skipped.
LEADER_TAGS = (("PYDS", "QB"), ("RYDS", "RB"), ("RECYDS", "WR"))
# "L. Altmyer" -> "L.ALTMYER". 12 keeps the widest real line inside the panel:
# "WR J.SMITH-NJIG 167Y" is 20 of the 21 characters that fit across 128px.
NAME_CAP = 12

DISPLAY_SECONDS = 10        # how long each team stays on screen
ROTATE_SECONDS = 2          # how long each bottom-row line stays up
IDLE_FETCH_SECONDS = 300    # between refreshes when nothing is being played
LIVE_FETCH_SECONDS = 45     # between refreshes while a followed game is live
RETRY_FETCH_SECONDS = 30    # after a failed refresh
RECONNECT_AFTER = 3         # consecutive failures before rebuilding the network
REBOOT_AFTER = 10           # consecutive failures before giving up and resetting

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/{}/{}/scoreboard"
# adafruit_json_stream reads the response in chunks this big. Larger chunks mean
# far fewer socket reads over a ~250 KB scoreboard.
CHUNK_SIZE = 512
# ESPN sits behind Akamai, which 403s any User-Agent it does not recognise and
# answers with an HTML error page. adafruit_requests otherwise sends
# "Adafruit CircuitPython", which is on the wrong side of that filter, and the
# HTML body then blows up the JSON parser. Any well-known client string works;
# curl's is the least surprising. If fetches start returning HTTP 403, this is
# the line to change.
REQUEST_HEADERS = {"User-Agent": "curl/8.7.1"}
REQUEST_TIMEOUT = 30

# ---------------------------------------------------------------------- setup

displayio.release_displays()

BASE_WIDTH = 64
BASE_HEIGHT = 32
CHAIN_ACROSS = 2
TILE_DOWN = 2
DISPLAY_WIDTH = BASE_WIDTH * CHAIN_ACROSS
DISPLAY_HEIGHT = BASE_HEIGHT * TILE_DOWN

matrix = rgbmatrix.RGBMatrix(
    width=DISPLAY_WIDTH, height=DISPLAY_HEIGHT, bit_depth=3,
    rgb_pins=[
        board.MTX_R1, board.MTX_G1, board.MTX_B1,
        board.MTX_R2, board.MTX_G2, board.MTX_B2,
    ],
    addr_pins=[
        board.MTX_ADDRA, board.MTX_ADDRB, board.MTX_ADDRC, board.MTX_ADDRD,
    ],
    clock_pin=board.MTX_CLK,
    latch_pin=board.MTX_LAT,
    output_enable_pin=board.MTX_OE,
    tile=TILE_DOWN, serpentine=True,
    doublebuffer=False,
)
display = framebufferio.FramebufferDisplay(matrix)

pixel = neopixel.NeoPixel(board.NEOPIXEL, 1, brightness=0.1, auto_write=True)
STATUS_IDLE = (0, 0, 0)
STATUS_FETCHING = (0, 0, 255)
STATUS_ERROR = (255, 0, 0)

# One entry per game, in the order the scoreboard lists them (chronological),
# holding only pre-rendered strings:
#   (folder, away, home, score, clock, network, info, away_record, home_record)
# A game is listed once even when both teams are followed, so the away side is
# always on the left. A followed team on a bye simply has no entry.
games = []

# ------------------------------------------------------------------ the scene

# Stand-in for a logo slot that has nothing in it, so a missing .bmp can never
# take the display down.
BLANK_BITMAP = displayio.Bitmap(1, 1, 1)
BLANK_PALETTE = displayio.Palette(1)
BLANK_PALETTE[0] = 0x000000

LEFT_LOGO_X = 2
RIGHT_LOGO_X = 94


def _make_label(anchor_point, anchored_position):
    label = bitmap_label.Label(terminalio.FONT, color=FONT_COLOR, text="")
    label.anchor_point = anchor_point
    label.anchored_position = anchored_position
    return label


# Four text rows. The logos occupy y=0..31 but only at the far left and right,
# so the 60px channel between them is free for the score and the game clock.
# The band below the logos is full width and carries the records, the network
# and the status line.
#
#   [away logo]        14 - 16        [home logo]
#                      Q3 4:21
#     0-1                FOX                 1-0
#                        LIVE
score_label = _make_label((0.5, 0.0), (DISPLAY_WIDTH // 2, 2))
clock_label = _make_label((0.5, 0.0), (DISPLAY_WIDTH // 2, 16))
away_record_label = _make_label((0.0, 0.5), (5, 42))
home_record_label = _make_label((1.0, 0.5), (DISPLAY_WIDTH - 4, 42))
network_label = _make_label((0.5, 0.5), (DISPLAY_WIDTH // 2, 42))
info_label = _make_label((0.5, 1.0), (DISPLAY_WIDTH // 2, DISPLAY_HEIGHT))

LEFT_LOGO_SLOT = 0
RIGHT_LOGO_SLOT = 1

scene = displayio.Group()
scene.append(displayio.TileGrid(BLANK_BITMAP, pixel_shader=BLANK_PALETTE))
scene.append(displayio.TileGrid(BLANK_BITMAP, pixel_shader=BLANK_PALETTE))
scene.append(score_label)
scene.append(clock_label)
scene.append(away_record_label)
scene.append(home_record_label)
scene.append(network_label)
scene.append(info_label)
display.root_group = scene


def set_logo(slot, path, x):
    """Put a logo in one of the two slots, or blank the slot if path is None.

    An OnDiskBitmap holds its file open for as long as it is referenced, so the
    old one is dropped and collected before the new one is opened. That keeps
    exactly two logo files open no matter how long the board runs.
    """
    scene[slot] = displayio.TileGrid(BLANK_BITMAP, pixel_shader=BLANK_PALETTE)
    gc.collect()
    if path is None:
        return
    try:
        bitmap = displayio.OnDiskBitmap(path)
    except OSError:
        print("no logo file:", path)
        return
    scene[slot] = displayio.TileGrid(bitmap, pixel_shader=bitmap.pixel_shader, x=x)


def show_message(message, headline=""):
    """Take over the screen with a status message instead of a game."""
    set_logo(LEFT_LOGO_SLOT, None, LEFT_LOGO_X)
    set_logo(RIGHT_LOGO_SLOT, None, RIGHT_LOGO_X)
    score_label.text = headline
    clock_label.text = ""
    away_record_label.text = ""
    home_record_label.text = ""
    network_label.text = ""
    info_label.text = message


def show_game(index):
    """Draw the game at `index`, away team on the left, home team on the right."""
    folder, away, home, score, clock, network, rotation, away_record, home_record = games[index]
    set_logo(LEFT_LOGO_SLOT, "{}/{}.bmp".format(folder, away), LEFT_LOGO_X)
    set_logo(RIGHT_LOGO_SLOT, "{}/{}.bmp".format(folder, home), RIGHT_LOGO_X)
    score_label.text = score
    clock_label.text = clock
    away_record_label.text = away_record
    home_record_label.text = home_record
    network_label.text = network
    info_label.text = rotation[0]


# ----------------------------------------------------------------- networking

SSID = os.getenv("CIRCUITPY_WIFI_SSID")
PASSWORD = os.getenv("CIRCUITPY_WIFI_PASSWORD")

pool = adafruit_connection_manager.get_radio_socketpool(wifi.radio)
ssl_context = adafruit_connection_manager.get_radio_ssl_context(wifi.radio)
requests = adafruit_requests.Session(pool, ssl_context)


def connect_wifi():
    """Return True once the radio is associated. Safe to call every loop."""
    if wifi.radio.connected:
        return True
    print("connecting to", SSID)
    try:
        wifi.radio.connect(SSID, PASSWORD)
    # pylint: disable=broad-except
    except Exception as error:
        print("wifi connect failed:", error)
        return False
    print("connected, ip", wifi.radio.ipv4_address)
    return True


def reset_connections():
    """Drop every pooled socket so the next request starts from scratch."""
    print("resetting sockets")
    try:
        adafruit_connection_manager.connection_manager_close_all(release_references=True)
    # pylint: disable=broad-except
    except Exception as error:
        print("socket reset failed:", error)
    try:
        wifi.radio.enabled = False
        time.sleep(1)
        wifi.radio.enabled = True
    # pylint: disable=broad-except
    except Exception as error:
        print("radio bounce failed:", error)
    gc.collect()


# ------------------------------------------------------------------- fetching


def local_kickoff(utc_string):
    """'2026-08-13T23:00Z' -> '8/13 - 3:00 PM PST' in the configured timezone."""
    try:
        moment = datetime(
            int(utc_string[0:4]), int(utc_string[5:7]), int(utc_string[8:10]),
            int(utc_string[11:13]), int(utc_string[14:16]),
        )
    except (ValueError, IndexError):
        return "TBD"
    local = moment + timedelta(hours=TIMEZONE_OFFSET)
    # 0 -> 12 AM and 12 -> 12 PM, which plain `hour - 12` gets wrong at midnight.
    hour = local.hour % 12 or 12
    meridiem = "AM" if local.hour < 12 else "PM"
    return "{}/{} - {}:{:02} {} {}".format(
        local.month, local.day, hour, local.minute, meridiem, TIMEZONE_NAME
    )


def period_text(period, clock):
    """'Q3 4:21' for regulation, 'OT 4:21' and '2OT 4:21' beyond it.

    Quarter-shaped, so it suits football and basketball. Hockey and baseball
    would want their own wording here.
    """
    if period <= 4:
        stage = "Q{}".format(period)
    elif period == 5:
        stage = "OT"
    else:
        stage = "{}OT".format(period - 4)
    return "{} {}".format(stage, clock)


def compact_name(name):
    """'L. Altmyer' -> 'L.ALTMYER', capped so a row can never overflow."""
    return name.replace(". ", ".").upper()[:NAME_CAP]


def stat_parts(display_value):
    """Pull yards, TDs and INTs out of '13/22, 130 YDS, 1 TD, 2 INT'.

    Segments only appear when non-zero, and they are not in a fixed position,
    so each one is matched by its suffix rather than by index.
    """
    yards = touchdowns = interceptions = None
    for part in display_value.split(", "):
        if part.endswith(" YDS"):
            yards = part[:-4]
        elif part.endswith(" TD"):
            touchdowns = part[:-3]
        elif part.endswith(" INT"):
            interceptions = part[:-4]
    return yards, touchdowns, interceptions


def read_leaders(competition):
    """Bottom-row lines for a finished game's passing, rushing and receiving leaders.

    Call this only when the game is final. `leaders` is absent before kickoff,
    and asking for a key that is not there makes the parser consume the rest of
    the competition, which would take `broadcast` down with it.

    The quarterback needs two lines: name with yards, then TDs and INTs. All
    three on one row comes to 27 characters, and only 21 fit across the panel.
    """
    lines = []
    for category in competition["leaders"]:
        abbreviation = category["abbreviation"]
        tag = None
        for known, label in LEADER_TAGS:
            if known == abbreviation:
                tag = label
                break
        if tag is None:
            continue
        for leader in category["leaders"]:
            display_value = leader["displayValue"]
            name = compact_name(leader["athlete"]["shortName"])
            yards, touchdowns, interceptions = stat_parts(display_value)
            lines.append("{} {} {}Y".format(tag, name, yards or "0"))
            if tag == "QB":
                lines.append("QB {} TD, {} INT".format(touchdowns or "0",
                                                       interceptions or "0"))
            # Only the top name in each category; the rest of the list is
            # skipped when the iterator moves on.
            break
    return lines


def fetch_league(sport, league, followed, results, folder):
    """Stream one scoreboard, appending every game a followed team appears in.

    Keys are read strictly in the order ESPN emits them, which is what
    adafruit_json_stream requires. Only fields present on every game are read:
    asking for an absent key makes the parser consume the rest of the object,
    which would corrupt everything after it. That rules out `winner` and
    `linescores` (missing before kickoff) and `odds` (scheduled games only).

    Returns True if a followed team is playing right now.
    """
    url = SCOREBOARD_URL.format(sport, league)
    print("fetching", url)
    live = False
    response = requests.get(url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT)
    try:
        # Check this before parsing: an error response is an HTML page, and
        # feeding that to the JSON parser reports a confusing syntax error
        # instead of the status code that actually explains the problem.
        if response.status_code != 200:
            raise RuntimeError("HTTP {} for {}".format(response.status_code, league))
        scoreboard = json_stream.load(response.iter_content(CHUNK_SIZE))
        for event in scoreboard["events"]:
            kickoff = event["date"]
            for competition in event["competitions"]:
                home = away = None
                home_score = away_score = "0"
                home_record = away_record = ""
                # competitor: homeAway -> team.abbreviation -> score -> records
                for competitor in competition["competitors"]:
                    side = competitor["homeAway"]
                    abbreviation = competitor["team"]["abbreviation"]
                    score = competitor["score"]
                    record = ""
                    # Overall W-L is the entry typed "total"; the others are
                    # home/road splits. Read every entry so the list finishes.
                    for entry in competitor["records"]:
                        if entry["type"] == "total":
                            record = entry["summary"]
                    if side == "home":
                        home, home_score, home_record = abbreviation, score, record
                    else:
                        away, away_score, away_record = abbreviation, score, record
                # competition: competitors -> status -> broadcast
                status = competition["status"]
                display_clock = status["displayClock"]
                period = status["period"]
                status_type = status["type"]
                state = status_type["state"]
                detail = status_type["shortDetail"]
                # leaders sits between status and broadcast, so it has to be
                # read here, and only for the games that actually carry it.
                if state == "post":
                    leader_lines = read_leaders(competition)
                else:
                    leader_lines = []
                network = competition["broadcast"]

                if home is None or away is None:
                    continue
                if home not in followed and away not in followed:
                    continue

                if state == "pre":
                    score_text, clock_text = "VS", ""
                    rotation = (local_kickoff(kickoff),)
                elif state == "in":
                    live = True
                    score_text = "{} - {}".format(away_score, home_score)
                    clock_text = period_text(period, display_clock)
                    # The clock row already carries period and time, so the
                    # bottom row says what state we are in instead of repeating.
                    rotation = ("LIVE",)
                else:
                    score_text = "{} - {}".format(away_score, home_score)
                    clock_text = ""
                    # Final first, then each leader in turn.
                    rotation = tuple([detail] + leader_lines)
                results.append((
                    folder, away, home, score_text, clock_text,
                    network, rotation, away_record, home_record,
                ))
    finally:
        try:
            response.close()
        # pylint: disable=broad-except
        except Exception as error:
            # adafruit_requests occasionally throws on close with a chunked
            # response it did not read to the end. Nothing here depends on it.
            print("close failed:", error)
    return live


def refresh():
    """Refresh every league. Returns (everything_succeeded, a_game_is_live)."""
    fresh = []
    complete = True
    live = False
    for sport, league, folder, teams in LEAGUES:
        try:
            if fetch_league(sport, league, frozenset(teams), fresh, folder):
                live = True
        # pylint: disable=broad-except
        except Exception as error:
            print("fetch failed for {}: {}".format(league, error))
            complete = False
        gc.collect()

    # Only replace the schedule on a clean sweep. A partial list would drop
    # games that are still going on, so the previous one stays up instead.
    if complete:
        games[:] = fresh
    elif not games:
        games[:] = fresh
    print("refresh ok={} live={} games={} free={}".format(
        complete, live, len(games), gc.mem_free()))
    return complete, live


# ------------------------------------------------------------------ main loop

show_message("CONNECTING", "SCORES")
while not connect_wifi():
    time.sleep(5)

show_message("LOADING", "SCORES")

display_index = 0
rotation_index = 0
failures = 0
next_fetch = ticks_ms()
next_display = ticks_add(ticks_ms(), DISPLAY_SECONDS * 1000)
next_rotate = ticks_add(ticks_ms(), ROTATE_SECONDS * 1000)

while True:
    try:
        if ticks_diff(ticks_ms(), next_fetch) >= 0:
            pixel.fill(STATUS_FETCHING)
            complete = False
            if connect_wifi():
                complete, live = refresh()
            else:
                live = False

            if complete:
                failures = 0
                interval = LIVE_FETCH_SECONDS if live else IDLE_FETCH_SECONDS
                pixel.fill(STATUS_IDLE)
            else:
                failures += 1
                interval = RETRY_FETCH_SECONDS
                pixel.fill(STATUS_ERROR)
                print("refresh failure", failures)
                if failures >= REBOOT_AFTER:
                    print("too many failures, resetting")
                    time.sleep(5)
                    microcontroller.reset()
                if failures % RECONNECT_AFTER == 0:
                    reset_connections()

            # Show the freshly fetched numbers right away instead of waiting out
            # the rest of the display interval. The list length changes between
            # refreshes, so clamp the index rather than trusting the old one.
            if games:
                display_index = display_index % len(games)
                show_game(display_index)
                rotation_index = 0
            else:
                show_message("NO GAMES" if complete else "NO DATA", "SCORES")
            next_fetch = ticks_add(ticks_ms(), interval * 1000)
            next_display = ticks_add(ticks_ms(), DISPLAY_SECONDS * 1000)
            next_rotate = ticks_add(ticks_ms(), ROTATE_SECONDS * 1000)

        if games and ticks_diff(ticks_ms(), next_display) >= 0:
            display_index = (display_index + 1) % len(games)
            show_game(display_index)
            rotation_index = 0
            # Rebased on the clock rather than the old deadline, so a slow fetch
            # cannot leave a backlog of updates to fire all at once.
            next_display = ticks_add(ticks_ms(), DISPLAY_SECONDS * 1000)
            next_rotate = ticks_add(ticks_ms(), ROTATE_SECONDS * 1000)

        # Step the bottom row through the leader lines. Only one label changes,
        # so this costs nothing next to a full redraw.
        if games and ticks_diff(ticks_ms(), next_rotate) >= 0:
            rotation = games[display_index][6]
            if len(rotation) > 1:
                rotation_index = (rotation_index + 1) % len(rotation)
                info_label.text = rotation[rotation_index]
            next_rotate = ticks_add(ticks_ms(), ROTATE_SECONDS * 1000)

        time.sleep(0.1)

    # pylint: disable=broad-except
    except Exception as error:
        # Keep the last good screen up and try again rather than rebooting on
        # every transient network hiccup.
        print("loop error:", error)
        failures += 1
        pixel.fill(STATUS_ERROR)
        if failures >= REBOOT_AFTER:
            print("too many failures, resetting")
            time.sleep(5)
            microcontroller.reset()
        gc.collect()
        time.sleep(5)
        next_fetch = ticks_add(ticks_ms(), RETRY_FETCH_SECONDS * 1000)
