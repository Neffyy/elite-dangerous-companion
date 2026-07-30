"""
Elite Dangerous Companion
Dark-mode desktop widget combining a browsable new-player progression
guide with a live tracker that tails the game's Journal log
(%USERPROFILE%\\Saved Games\\Frontier Developments\\Elite Dangerous) to
show real credits, rank progress, and location while you play. Falls
back to a bundled sample journal (via the "Load Sample Data" button)
when no journal folder is present yet, so the Tracker tab can be
previewed before you've played a session.
"""

import copy
import ctypes
import json
import os
import re
import threading
import time
import tkinter as tk
import urllib.parse
import webbrowser
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

JOURNAL_DIR = Path.home() / "Saved Games" / "Frontier Developments" / "Elite Dangerous"
POLL_INTERVAL_SEC = 2
TICK_INTERVAL_MS = 1000

# Bump both of these together whenever a real change is pushed to the repo —
# the app compares its own APP_VERSION against the VERSION file living at
# the repo root to know when a newer copy is available.
APP_VERSION = "1.3.0"
APP_REPO = "Neffyy/elite-dangerous-companion"
APP_REPO_URL = f"https://github.com/{APP_REPO}"
APP_VERSION_URL = f"https://raw.githubusercontent.com/{APP_REPO}/main/VERSION"

SETTINGS_PATH = (Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
                  / "EliteDangerousCompanion" / "settings.json")
DEFAULT_GEOMETRY = "600x800+40+40"
DEFAULT_VIEW_MODES = {"guide": "auto", "tracker": "auto", "nav": "auto", "build": "auto", "explore": "auto",
                       "colony": "auto"}
GEOMETRY_RE = re.compile(r"^(\d+)x(\d+)([+-]\d+)([+-]\d+)$")


def load_settings():
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data):
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def clean_geometry(geometry):
    """Normalizes a 'WxH+X+Y' string. Tk on Windows can report a redundant
    '+' before a negative offset after a real mouse-driven move across
    monitors — e.g. '...+2279+-30' instead of the canonical '...+2279-30' —
    which fails strict parsing and would otherwise permanently strand the
    window on the default position every session after that."""
    return (geometry or "").replace("+-", "-")


def geometry_on_screen(geometry):
    """Best-effort check that a saved 'WxH+X+Y' geometry string still lands
    within the current (possibly multi-monitor) virtual desktop, so a saved
    position on a monitor that's no longer connected doesn't strand the
    window off-screen."""
    match = GEOMETRY_RE.match(clean_geometry(geometry))
    if not match:
        return False
    x, y = int(match.group(3)), int(match.group(4))
    try:
        SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
        SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
        vx = ctypes.windll.user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        vy = ctypes.windll.user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        vw = ctypes.windll.user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        vh = ctypes.windll.user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    except (OSError, AttributeError):
        return True  # can't verify — don't block restoring it
    margin = 60  # require enough of the titlebar on-screen to grab it
    return (vx - margin) <= x <= (vx + vw - margin) and (vy - margin) <= y <= (vy + vh - margin)

SPANSH_ROUTE_URL = "https://spansh.co.uk/api/route"
SPANSH_RESULT_URL = "https://spansh.co.uk/api/results/{}"
UA_HEADERS = {"User-Agent": "elite-dangerous-companion/1.0"}
ROUTE_EFFICIENCY = 60
ROUTE_POLL_INTERVAL_SEC = 1.5
ROUTE_POLL_TIMEOUT_SEC = 90

# Real nearby-system lookups (for the Tracker's per-rank "go here" suggestion)
# via EDSM's free public API — no key required.
EDSM_SPHERE_URL = "https://www.edsm.net/api-v1/sphere-systems"
EDSM_RADIUS_LY = 50
# EDSM's sphere-systems endpoint hard-caps radius at 100 ly — above that it
# silently returns an empty/non-list response (or even times out near dense
# areas) instead of a real error, so this needs to be enforced client-side.
EDSM_MAX_RADIUS_LY = 100
# Combat/Trade/Explore/CQC don't have a single "correct" faction to work
# for, so their suggestion is just the nearest populated system. Federation
# and Empire rank-ups specifically require missions from factions aligned
# with that superpower, which EDSM's per-system allegiance field lets us
# filter for directly instead of guessing.
RANK_ALLEGIANCE = {"Federation": "Federation", "Empire": "Empire"}

# Explore tab: "unexplored" systems come from EDSM's per-system bodies
# endpoint — a system with no submitted bodies at all means no commander
# has ever scanned anything there (EDSM returns {} for it), a real signal
# for genuinely virgin systems rather than a guess.
EDSM_BODIES_URL = "https://www.edsm.net/api-system-v1/bodies"
UNEXPLORED_CANDIDATE_LIMIT = 15  # how many nearby systems to check per search

# "Materials on them" covers two distinct real things, both queryable from
# Spansh's public bodies/search API: planetary surface raw materials (for
# Engineers) and ring/asteroid mining hotspot commodities. Lists confirmed
# live against Spansh's own field_values endpoints.
SPANSH_BODIES_URL = "https://spansh.co.uk/api/bodies/search"
SURFACE_MATERIALS = [
    "Antimony", "Arsenic", "Cadmium", "Carbon", "Chromium", "Germanium",
    "Iron", "Manganese", "Mercury", "Molybdenum", "Nickel", "Niobium",
    "Phosphorus", "Polonium", "Ruthenium", "Selenium", "Sulphur",
    "Technetium", "Tellurium", "Tin", "Tungsten", "Vanadium", "Yttrium",
    "Zinc", "Zirconium",
]
RING_SIGNALS = [
    "Alexandrite", "Bauxite", "Benitoite", "Bertrandite", "Bromellite",
    "Cobalt", "Coltan", "Gallite", "Grandidierite", "Hydrogen Peroxide",
    "Indite", "Lepidolite", "Liquid oxygen", "Lithium Hydroxide",
    "Low Temperature Diamonds", "Methane Clathrate",
    "Methanol Monohydrate Crystals", "Monazite", "Musgravite", "Painite",
    "Platinum", "Praseodymium", "Rhodplumsite", "Rutile", "Samarium",
    "Serendibite", "Tritium", "Uraninite", "Void Opal", "Water",
]
MATERIAL_SEARCH_RADIUS_LY = 100

# Real per-system Powerplay control data (which Power, and its control state
# e.g. Stronghold/Fortified/Exploited/Turmoil) isn't exposed by EDSM's system
# endpoints (checked — its "information" block only has allegiance/faction/
# security/economy) but IS a real, live-queryable field on Spansh's systems
# search API, confirmed via a direct test query.
SPANSH_SYSTEMS_URL = "https://spansh.co.uk/api/systems/search"
POWER_HUB_RADIUS_LY = 100

# Data for the Build tab comes live from EDCD's coriolis-data (MIT licensed,
# the dataset behind Coriolis.io/EDSY) rather than being hand-typed here —
# https://github.com/EDCD/coriolis-data. Its module "symbol" field matches
# (case-insensitively) the exact Item/Name strings the game itself writes
# into Journal Loadout/StoredModules events, so ownership can be cross
# checked against real data instead of guesswork.
CORIOLIS_RAW_BASE = "https://raw.githubusercontent.com/EDCD/coriolis-data/master/"

# Journal-internal ship name -> coriolis-data ships/*.json slug. Covers the
# same roster as SHIP_NAMES; ships not listed here can still be browsed
# manually in the Build tab's ship picker.
SHIP_SLUGS = {
    "sidewinder": "sidewinder", "eagle": "eagle", "hauler": "hauler",
    "adder": "adder", "viper": "viper", "viper_mkiv": "viper_mk_iv",
    "cobramkiii": "cobra_mk_iii", "cobramkiv": "cobra_mk_iv",
    "type6": "type_6_transporter", "dolphin": "dolphin",
    "type7": "type_7_transport", "type9": "type_9_heavy",
    "type9_military": "type_10_defender", "typex": "alliance_chieftain",
    "typex_2": "alliance_crusader", "typex_3": "alliance_challenger",
    "asp": "asp", "asp_scout": "asp_scout", "vulture": "vulture",
    "empire_eagle": "imperial_eagle", "empire_courier": "imperial_courier",
    "empire_trader": "imperial_clipper", "federation_dropship": "federal_dropship",
    "federation_dropship_mkii": "federal_assault_ship",
    "federation_gunship": "federal_gunship", "federation_corvette": "federal_corvette",
    "independant_trader": "keelback", "independent_trader": "keelback",
    "diamondbackxl": "diamondback_explorer", "diamondback": "diamondback_scout",
    "orca": "orca", "python": "python", "python_nx": "python_nx",
    "belugaliner": "beluga", "ferdelance": "fer_de_lance",
    "anaconda": "anaconda", "cutter": "imperial_cutter", "mamba": "mamba",
    "krait_mkii": "krait_mkii", "krait_light": "krait_phantom",
    "mandalay": "mandalay",
}

# Standard (core) slots are always these 7 categories in this fixed order.
STANDARD_SLOT_FILES = [
    ("Power Plant", "power_plant"), ("Thrusters", "thrusters"),
    ("Frame Shift Drive", "frame_shift_drive"), ("Life Support", "life_support"),
    ("Power Distributor", "power_distributor"), ("Sensors", "sensors"),
    ("Fuel Tank", "fuel_tank"),
]
# Hardpoint/internal module category filenames, fetched on demand per ship
# (only the categories actually needed) and cached for the session.
HARDPOINT_MODULE_FILES = [
    "abrasion_blaster", "ax_missile_rack", "ax_missile_rack_enhanced",
    "ax_multi_cannon", "ax_multi_cannon_enhanced", "beam_laser", "burst_laser",
    "cannon", "cargo_scanner", "caustic_sink_launcher", "chaff_launcher",
    "electronic_countermeasure", "enzyme_missile_rack", "fragment_cannon",
    "frame_shift_wake_scanner", "guardian_gauss_cannon", "guardian_plasma_charger",
    "guardian_shard_cannon", "heat_sink_launcher", "kill_warrant_scanner",
    "mine_launcher", "mining_laser", "mining_volley_repeater", "missile_rack",
    "missile_rack_advanced", "multi_cannon", "multi_cannon_advanced",
    "nanite_torpedo_pylon", "plasma_accelerator", "point_defence", "pulse_laser",
    "pulse_wave_analyser", "rail_gun", "remote_release_flak_launcher",
    "remote_release_flechette_launcher", "seismic_charge_launcher",
    "shield_booster", "shock_cannon", "shutdown_field_neutraliser",
    "sub_surface_displacement_missile", "torpedo_pylon", "xeno_scanner",
]
INTERNAL_MODULE_FILES = [
    "auto_field_maintenance_unit", "bi_weave_shield_generator",
    "business_passenger_cabin", "cargo_rack", "cargo_rack_large",
    "collector_limpet_controllers", "decontamination_limpet_controller",
    "docking_computer", "economy_passenger_cabin", "fighter_hangar",
    "first_passenger_cabin", "frame_shift_drive_interdictor", "fuel_scoop",
    "fuel_transfer_limpet_controllers", "guardian_fsd_booster",
    "guardian_hull_reinforcement_package", "guardian_module_reinforcement_package",
    "guardian_shield_reinforcement_package", "hatch_breaker_limpet_controller",
    "hull_reinforcement_package", "internal_fuel_tank", "luxury_passenger_cabin",
    "meta_alloy_hull_reinforcement_package", "module_reinforcement_package",
    "multi_limpet_controllers", "planetary_approach_suite",
    "planetary_vehicle_hanger", "pristmatic_shield_generator",
    "prospector_limpet_controllers", "recon_limpet_controllers", "refinery",
    "repair_limpet_controller", "research_limpet_controller", "shield_cell_bank",
    "shield_generator", "supercruise_assist", "surface_scanner",
]

BG = "#121212"
PANEL_BG = "#1c1c1c"
CARD_BG = "#242424"
CARD_BORDER = "#333333"
FG = "#e8e8e8"
MUTED = "#8a8a8a"
ACCENT = "#4fc3f7"
GOOD = "#66bb6a"
WARN = "#ffb74d"
BAD = "#e57373"

FONT_TITLE = ("Segoe UI", 10, "bold")
FONT_BODY = ("Segoe UI", 9)
FONT_SMALL = ("Segoe UI", 8)
FONT_SMALL_BOLD = ("Segoe UI", 8, "bold")

RANK_KEYS = ["Combat", "Trade", "Explore", "CQC", "Federation", "Empire"]

GUIDE_MIN_CARD_W = 260
GUIDE_MAX_COLS = 3
RANK_MIN_TILE_W = 150
RANK_MAX_COLS = 3

RANKS = {
    "Combat": ["Harmless", "Mostly Harmless", "Novice", "Competent", "Expert",
               "Master", "Dangerous", "Deadly", "Elite"],
    "Trade": ["Penniless", "Mostly Penniless", "Peddler", "Dealer", "Merchant",
              "Broker", "Entrepreneur", "Tycoon", "Elite"],
    "Explore": ["Aimless", "Mostly Aimless", "Scout", "Surveyor", "Trailblazer",
                "Pathfinder", "Ranger", "Pioneer", "Elite"],
    "CQC": ["Helpless", "Mostly Helpless", "Amateur", "Semi Professional",
            "Professional", "Champion", "Hero", "Legend", "Elite"],
    "Federation": ["None", "Recruit", "Cadet", "Midshipman", "Petty Officer",
                   "Chief Petty Officer", "Warrant Officer", "Ensign", "Lieutenant",
                   "Lieutenant Commander", "Post Commander", "Post Captain",
                   "Rear Admiral", "Vice Admiral", "Admiral"],
    "Empire": ["None", "Outsider", "Serf", "Master", "Squire", "Knight", "Lord",
               "Baron", "Viscount", "Count", "Earl", "Marquis", "Duke",
               "Prince/Princess", "King/Queen"],
}

SHIP_NAMES = {
    "sidewinder": "Sidewinder", "eagle": "Eagle", "hauler": "Hauler",
    "adder": "Adder", "viper": "Viper Mk III", "viper_mkiv": "Viper Mk IV",
    "cobramkiii": "Cobra Mk III", "cobramkiv": "Cobra Mk IV",
    "type6": "Type-6 Transporter", "dolphin": "Dolphin",
    "type7": "Type-7 Transporter", "type9": "Type-9 Heavy",
    "type9_military": "Type-10 Defender", "typex": "Alliance Chieftain",
    "typex_2": "Alliance Crusader", "typex_3": "Alliance Challenger",
    "asp": "Asp Explorer", "asp_scout": "Asp Scout", "vulture": "Vulture",
    "empire_eagle": "Imperial Eagle", "empire_courier": "Imperial Courier",
    "empire_trader": "Imperial Clipper", "federation_dropship": "Federal Dropship",
    "federation_dropship_mkii": "Federal Assault Ship",
    "federation_gunship": "Federal Gunship", "federation_corvette": "Federal Corvette",
    "independant_trader": "Keelback", "independent_trader": "Keelback",
    "diamondbackxl": "Diamondback Explorer", "diamondback": "Diamondback Scout",
    "orca": "Orca", "python": "Python", "python_nx": "Python Mk II",
    "belugaliner": "Beluga Liner", "ferdelance": "Fer-de-Lance",
    "anaconda": "Anaconda", "cutter": "Imperial Cutter", "mamba": "Mamba",
    "krait_mkii": "Krait Mk II", "krait_light": "Krait Phantom",
    "mandalay": "Mandalay",
}

PATHS = [
    {
        "key": "trading", "title": "Trading", "risk": "Low", "category": "Money", "tier": "Early",
        "summary": "Buy commodities low, sell high between stations. The safest "
                    "early credit source and the easiest to scale once you have "
                    "a bigger cargo hold.",
        "starter_ship": "Type-6 Transporter (early) → Type-9 Heavy (bulk hauler later)",
        "early_steps": [
            "Start with a Hauler, Adder, or Type-6 and check trade routes on a "
            "third-party tool before committing cargo space.",
            "Look for high-margin loops within a few jumps of your start system "
            "— don't chase far routes until you have fuel scoop range.",
            "Play in Solo or Private mode if griefing at busy trade hubs is a concern.",
        ],
        "tips": [
            "Background Simulation states like Boom/Bust swing prices — check "
            "before committing to a route.",
            "Illegal goods pay more but risk scans and fines — stick to legal "
            "trade until you know a station's security level.",
        ],
    },
    {
        "key": "mining", "title": "Mining", "risk": "Low", "category": "Money", "tier": "Early",
        "summary": "Laser or core mining valuable minerals like Painite and Low "
                    "Temperature Diamonds. Steady, AFK-friendly income once "
                    "you're set up.",
        "starter_ship": "Cobra Mk III or Type-6 fitted with mining modules",
        "early_steps": [
            "Fit mining lasers, a refinery, and prospector/collector limpet "
            "controllers on a ship with decent cargo space.",
            "Find a good asteroid belt hotspot for the mineral you're after.",
            "Sell at the nearest station buying that commodity — check demand, "
            "not just price.",
        ],
        "tips": [
            "Core mining (Painite, Void Opals) pays more per ton than laser "
            "mining but needs a seismic charge launcher and sub-surface "
            "displacement missiles.",
            "Mining in a wing splits limpet control but multiplies your total "
            "hauling capacity.",
        ],
    },
    {
        "key": "combat", "title": "Bounty Hunting / Combat", "risk": "Medium", "category": "Money",
        "tier": "Early",
        "summary": "Hunt pirates at Resource Extraction Sites for bounties and "
                    "combat bonds. Faster payouts than trading but needs a "
                    "fight-ready ship.",
        "starter_ship": "Eagle or Viper Mk III (early) → Vulture (excellent mid-game value)",
        "early_steps": [
            "Fit a combat ship with shields, a decent power plant, and matched weapons.",
            "Head to a Low or High RES around a populated system — avoid "
            "Hazardous RES until you're properly equipped.",
            "Only target 'Wanted' ships unless you want a bounty placed on "
            "yourself; redeem vouchers at any station's redemption office.",
        ],
        "tips": [
            "Combat bonds pay more than bounties but only count for the faction "
            "you're supporting in a Nav Beacon or Conflict Zone.",
            "No rush to learn Flight-Assist-off dogfighting — throttle "
            "management alone gets you through the early game.",
        ],
    },
    {
        "key": "exploration", "title": "Exploration", "risk": "Low", "category": "Money", "tier": "Early",
        "summary": "Scan systems and sell the data to Universal Cartographics. "
                    "Very safe, good early income, and can take you thousands "
                    "of light years from the bubble.",
        "starter_ship": "Diamondback Explorer or Asp Explorer",
        "early_steps": [
            "Fit a fuel scoop and a Discovery Scanner / FSS on a long-range ship.",
            "Honk in every system, then FSS/DSS-scan bodies of interest — "
            "first-discovered tags pay a bonus.",
            "Sell exploration data regularly at any station with Universal "
            "Cartographics — a ship loss can lose unsold data.",
        ],
        "tips": [
            "Exobiology (scanning surface organics) adds extra income on top "
            "of straight system scanning.",
            "Neutron star and white dwarf jumps extend your jump range "
            "dramatically for long-range trips.",
        ],
    },
    {
        "key": "missions", "title": "Mission Running", "risk": "Medium", "category": "Money",
        "tier": "Early",
        "summary": "Delivery, passenger, and combat missions from station "
                    "boards. Steady income plus faction reputation and rank "
                    "progress at the same time.",
        "starter_ship": "Any starter ship for deliveries; Dolphin or Orca once focusing on passengers",
        "early_steps": [
            "Check mission boards at multiple nearby stations — stack "
            "compatible delivery or passenger missions in one trip.",
            "Passenger missions can be very lucrative with a cabin-fitted "
            "ship like the Dolphin.",
            "Raising your reputation with a minor faction unlocks higher-"
            "paying missions from them.",
        ],
        "tips": [
            "Wing missions split rewards but let you stack more per trip "
            "with friends.",
            "Watch expiry timers — failed missions can hurt your reputation "
            "with that faction.",
        ],
    },
    {
        "key": "piracy", "title": "Piracy", "risk": "High", "category": "Money", "tier": "Mid",
        "summary": "Interdict traders and demand cargo instead of buying it. "
                    "High risk, high reward, and damaging to your reputation "
                    "with law-abiding factions.",
        "starter_ship": "Viper or Vulture (fast interceptor)",
        "early_steps": [
            "Fit an interdictor, cargo scanner, and weapons that disable "
            "rather than destroy.",
            "Target unshielded or lightly-defended traders in low security systems.",
            "Have an escape plan — security response ships will intervene "
            "in populated systems.",
        ],
        "tips": [
            "Piracy tanks your reputation with the victim's faction and "
            "local security.",
            "Not recommended as a first activity — learn combat and flight "
            "basics via bounty hunting first.",
        ],
    },
    {
        "key": "powerplay", "title": "Powerplay", "risk": "Medium", "category": "Other", "tier": "Mid",
        "summary": "Pledge to a Power and contribute merits toward political control "
                    "of systems. Driven by influence and unique unlocks, not credits.",
        "starter_ship": "Whatever fits the weekly objective — cargo, combat, or exploration",
        "early_steps": [
            "Open the Powerplay tab and pledge to a Power whose territory or "
            "ideology appeals to you.",
            "Complete their weekly cargo, combat, or exploration objectives "
            "to earn merits.",
            "Rank up through their command structure to unlock Power-specific gear.",
        ],
        "tips": [
            "Switching Powers has a cooldown, so pick one you're comfortable "
            "sticking with for a while.",
            "Fortification and undermining mechanics reward coordinated group play.",
        ],
    },
    {
        "key": "engineering", "title": "Engineering", "risk": "Low", "category": "Other", "tier": "Early",
        "summary": "Gather materials and unlock Engineers to modify your ship's "
                    "modules for major performance boosts. About improving your "
                    "ship, not your wallet.",
        "starter_ship": "Whichever ship you're actively trying to improve",
        "early_steps": [
            "Unlock early Engineers like Elvira Martuuk or Felicity Farseer — "
            "their requirements are cheap and simple.",
            "Collect materials from mining, mission rewards, and surface scans "
            "while doing other activities.",
            "Apply blueprints incrementally — a Grade 1 upgrade is often "
            "enough for the early game.",
        ],
        "tips": [
            "Don't wait to fully engineer a ship before flying it — upgrade "
            "as materials allow.",
            "Material Traders let you convert excess materials into the "
            "grade or type you actually need.",
        ],
    },
    {
        "key": "war_support", "title": "Combat Zones / War Support", "risk": "High",
        "category": "Other", "tier": "Mid",
        "summary": "Fight in Conflict Zones to support a faction's war effort during "
                    "a system conflict. About tipping the war, not chasing credits.",
        "starter_ship": "A dedicated combat ship — Vulture, Federal Assault Ship, or better",
        "early_steps": [
            "Check the system map for an active war or conflict between factions.",
            "Join a Low Intensity Conflict Zone first — High and Medium zones "
            "field much tougher NPCs.",
            "Pick a side and destroy enough enemy ships to capture points for it.",
        ],
        "tips": [
            "Conflict Zones matter for the Background Simulation — winning "
            "enough can shift system control.",
            "Bring a ship that can survive sustained fights, not just quick "
            "bounty kills.",
        ],
    },
    {
        "key": "cqc", "title": "CQC (Close Quarters Combat)", "risk": "Low", "category": "Other",
        "tier": "Early",
        "summary": "A separate arcade-style arena combat mode with its own ships, "
                    "rank, and matchmaking. Pure PvP practice with no real economy involved.",
        "starter_ship": "N/A — CQC provides standardized ships for every match",
        "early_steps": [
            "Launch CQC from the main menu — it's entirely separate from the open galaxy.",
            "Play a few matches to learn the arena maps and vehicle handling.",
            "Rank up your CQC rank independently of your main Combat rank.",
        ],
        "tips": [
            "A great low-stakes way to practice dogfighting without risking "
            "your real ship.",
            "CQC rank doesn't affect your main save's combat rank or reputation.",
        ],
    },
    {
        "key": "thargoid_war", "title": "Thargoid War (AX Combat)", "risk": "High",
        "category": "Other", "tier": "Late",
        "summary": "Fight the Thargoid alien threat at besieged systems using "
                    "specialized anti-xeno weapons. A community war effort, not a credit farm.",
        "starter_ship": "A dedicated AX-fit combat ship — Krait Mk II, Corvette, or Anaconda",
        "early_steps": [
            "Check current Thargoid activity via the galaxy map or community "
            "trackers like Aegis.",
            "Start against weaker Scout-class Thargoids before facing Interceptors.",
            "Fit AX weapons — regular weapons barely scratch Thargoid hulls.",
        ],
        "tips": [
            "Coordinate with a wing — Interceptors are extremely dangerous "
            "solo until you're well-practiced.",
            "Contributing to the war effort helps push back Thargoid-occupied "
            "systems for the whole community.",
        ],
    },
]

BUCKET_TO_PATH = {"Trading": "trading", "Combat": "combat",
                   "Exploration": "exploration", "Missions": "missions"}

TIER_FG = "#ba68c8"
TIER_ORDER = ["Early", "Mid", "Late"]
EARLY_CREDIT_MAX = 2_000_000
MID_CREDIT_MAX = 100_000_000

# Community-known reference systems worth pointing new players toward for a
# given activity. Not a claim of "the" optimal system — in-game hotspots and
# conditions shift over time, so this is a starting point to verify in-game,
# not a live-computed answer the way the route itself is.
ACTIVITY_HUBS = {
    "trading": "Robigo",
    "missions": "Robigo",
    "mining": "Borann",
    "exploration": "Colonia",
    "engineering": "Deciat",
}

# Guide "For My Ships" personalization: which owned-ship hull stat is the
# relevant real proxy for each activity (pulled straight from coriolis-data's
# slots block, since fitted loadouts aren't known for ships other than the
# one currently being flown), and where its navigation suggestion should come
# from. Exploration/Mining get a real specific destination; everything else
# with a nav section gets a general nearby hub; Powerplay gets a real nearest-
# system-for-your-pledged-Power lookup (see PowerSystemFinder); CQC gets
# neither — it has no galaxy location at all. Powerplay stays out of
# PATH_STAT_CATEGORY since no hull stat makes one ship better for it than
# another (ship-agnostic messaging already handles a missing key here).
PATH_STAT_CATEGORY = {
    "trading": "cargo", "mining": "cargo", "missions": "cargo", "engineering": "cargo",
    "combat": "hardpoint", "piracy": "hardpoint", "war_support": "hardpoint",
    "thargoid_war": "hardpoint",
    "exploration": "fsd",
}
STAT_CATEGORY_LABEL = {
    "cargo": "internal-slot capacity",
    "hardpoint": "hardpoint capacity",
    "fsd": "Frame Shift Drive class",
}
PATH_NAV_MODE = {
    "exploration": "unexplored", "mining": "material", "powerplay": "power_hub",
    "trading": "hub", "missions": "hub", "combat": "hub", "piracy": "hub",
    "war_support": "hub", "thargoid_war": "hub", "engineering": "hub",
}
MINING_NAV_COMMODITY = "Painite"


def ship_stat_score(ship, category):
    """Real hull-capacity proxy pulled straight from a coriolis-data ship's
    slots block — not a fabricated stat. "fsd" is the Frame Shift Drive
    standard-slot class (the primary driver of jump range), "hardpoint" is
    the sum of hardpoint slot sizes, "cargo" is the sum of internal slot
    classes (special slots like Military report their size via "class")."""
    slots = ship.get("slots", {})
    if category == "fsd":
        standard = slots.get("standard") or []
        return standard[2] if len(standard) > 2 else 0
    if category == "hardpoint":
        return sum(slots.get("hardpoints") or [])
    if category == "cargo":
        total = 0
        for spec in slots.get("internal") or []:
            total += spec.get("class", 0) if isinstance(spec, dict) else (spec or 0)
        return total
    return 0


def estimate_tier(credits):
    if credits is None:
        return None
    if credits < EARLY_CREDIT_MAX:
        return "Early"
    if credits < MID_CREDIT_MAX:
        return "Mid"
    return "Late"


RISK_RANK = {"Low": 0, "Medium": 1, "High": 2}


def recommend_path(tier, dominant_bucket):
    if not tier:
        return None
    candidates = [p for p in PATHS if p.get("tier") == tier]
    if not candidates:
        return None
    if dominant_bucket:
        match = next((p for p in candidates if p["key"] == BUCKET_TO_PATH.get(dominant_bucket)), None)
        if match:
            return match
    # No activity you're already earning from matches this tier — default
    # to the safest tier-appropriate option rather than an arbitrary one
    # (e.g. don't recommend Piracy just because it's first in the list).
    return min(candidates, key=lambda p: (RISK_RANK.get(p["risk"], 1), PATHS.index(p)))


RATING_ORDER = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4, "F": 0, "G": 1, "T": 2}
MOUNT_NAMES = {"F": "Fixed", "G": "Gimbal", "T": "Turret"}
HARDPOINT_SIZE_NAMES = ("Utility", "Small", "Medium", "Large", "Huge")
DISPLAY_NAME_OVERRIDES = {"ax": "AX", "ecm": "ECM"}


def display_name_for_file(fname):
    words = [DISPLAY_NAME_OVERRIDES.get(w, w.capitalize()) for w in fname.split("_")]
    return " ".join(words)


def module_option_label(type_name, m):
    bits = [type_name] if type_name else []
    mount = MOUNT_NAMES.get(m.get("mount"))
    if mount:
        bits.append(mount)
    rating = m.get("rating")
    if rating:
        bits.append(f"Rating {rating}")
    cost = m.get("cost")
    bits.append(f"— {cost:,} CR" if cost else "— no direct purchase")
    return " ".join(bits)


def fits_hardpoint(module_class, slot_size):
    if slot_size == 0:
        return module_class == 0
    return 1 <= module_class <= slot_size


def acquisition_note(symbol, module):
    cost = module.get("cost")
    if "guardian" in symbol.lower():
        return ("Not directly purchasable — unlocked via Guardian sites. Check in-game "
                "or a community guide for current requirements.")
    if cost:
        return f"Buy at any Outfitting station for {cost:,.0f} CR."
    return ("Not directly purchasable — likely unlocked via Engineers, Powerplay, or "
            "missions. Check in-game or a community guide for current requirements.")


SAMPLE_EVENTS = [
    {"timestamp": "2026-07-24T10:00:00Z", "event": "Commander", "Name": "TestCMDR"},
    {"timestamp": "2026-07-24T10:00:01Z", "event": "LoadGame", "Ship": "cobramkiii",
     "Credits": 25000000, "GameMode": "Open"},
    {"timestamp": "2026-07-24T10:00:02Z", "event": "Rank", "Combat": 3, "Trade": 2,
     "Explore": 4, "CQC": 0, "Federation": 2, "Empire": 0},
    {"timestamp": "2026-07-24T10:00:02Z", "event": "Powerplay", "Power": "Aisling Duval",
     "Rank": 3, "Merits": 4200, "Votes": 0, "TimePledged": 2592000},
    {"timestamp": "2026-07-24T10:00:02Z", "event": "Progress", "Combat": 42, "Trade": 15,
     "Explore": 63, "CQC": 0, "Federation": 5, "Empire": 0},
    {"timestamp": "2026-07-24T10:00:03Z", "event": "Location", "StarSystem": "Shinrarta Dezhra",
     "Docked": True, "StationName": "Jameson Memorial"},
    {"timestamp": "2026-07-24T10:00:03Z", "event": "ColonisationConstructionDepot",
     "MarketID": 3901234567, "ConstructionProgress": 0.42, "ConstructionComplete": False,
     "ConstructionFailed": False, "ResourcesRequired": [
         {"Name": "steel", "Name_Localised": "Steel", "RequiredAmount": 5000, "ProvidedAmount": 2200},
         {"Name": "cmmcomposite", "Name_Localised": "CMM Composite", "RequiredAmount": 1200, "ProvidedAmount": 1200},
         {"Name": "waterpurifiers", "Name_Localised": "Water Purifiers", "RequiredAmount": 800, "ProvidedAmount": 150},
     ]},
    {"timestamp": "2026-07-24T10:00:04Z", "event": "Loadout", "Ship": "cobramkiii",
     "MaxJumpRange": 20.7, "CargoCapacity": 22, "Modules": [
         {"Slot": "PowerPlant", "Item": "int_powerplant_size4_class3", "On": True, "Priority": 1, "Health": 1.0},
         {"Slot": "MainEngines", "Item": "int_engine_size4_class3", "On": True, "Priority": 1, "Health": 1.0},
         {"Slot": "FrameShiftDrive", "Item": "int_hyperdrive_size4_class3", "On": True, "Priority": 1, "Health": 1.0},
         {"Slot": "LifeSupport", "Item": "int_lifesupport_size4_class3", "On": True, "Priority": 1, "Health": 1.0},
         {"Slot": "PowerDistributor", "Item": "int_powerdistributor_size4_class3", "On": True, "Priority": 1, "Health": 1.0},
         {"Slot": "Radar", "Item": "int_sensors_size4_class3", "On": True, "Priority": 1, "Health": 1.0},
         {"Slot": "FuelTank", "Item": "int_fueltank_size4_class3", "On": True, "Priority": 1, "Health": 1.0},
         {"Slot": "SmallHardpoint1", "Item": "hpt_pulselaser_fixed_small", "On": True, "Priority": 1, "Health": 1.0},
     ]},
    {"timestamp": "2026-07-24T10:00:05Z", "event": "StoredModules", "StationName": "Jameson Memorial",
     "Items": [
         {"Name": "int_cargorack_size2_class1", "StorageSlot": 1, "StarSystem": "Shinrarta Dezhra"},
     ]},
    {"timestamp": "2026-07-24T10:00:06Z", "event": "StoredShips", "StationName": "Jameson Memorial",
     "StarSystem": "Shinrarta Dezhra",
     "ShipsHere": [
         {"ShipID": 2, "ShipType": "hauler", "Name": "HAULY", "Value": 250000, "Hot": False},
     ],
     "ShipsRemote": [
         {"ShipID": 3, "ShipType": "anaconda", "Name": "BIG BOI", "Value": 150000000, "Hot": False,
          "StarSystem": "Eravate", "ShipMarketID": 128666762, "TransferPrice": 50000, "TransferTime": 600},
         {"ShipID": 4, "ShipType": "vulture", "Name": "", "Value": 6000000, "Hot": False,
          "StarSystem": "Eravate", "ShipMarketID": 128666762, "TransferPrice": 12000, "TransferTime": 300},
     ]},
    {"timestamp": "2026-07-24T10:05:00Z", "event": "MarketBuy", "Type": "gold",
     "Count": 50, "BuyPrice": 9200, "TotalCost": 460000},
    {"timestamp": "2026-07-24T10:20:00Z", "event": "Undocked", "StationName": "Jameson Memorial"},
    {"timestamp": "2026-07-24T10:22:00Z", "event": "FSDJump", "StarSystem": "Eravate"},
    {"timestamp": "2026-07-24T10:40:00Z", "event": "Docked", "StationName": "Cleve Hub",
     "StarSystem": "Eravate"},
    {"timestamp": "2026-07-24T10:41:00Z", "event": "MarketSell", "Type": "gold",
     "Count": 50, "SellPrice": 9800, "TotalSale": 490000},
    {"timestamp": "2026-07-24T11:10:00Z", "event": "MissionCompleted", "Name": "Mission_Courier",
     "Reward": 85000},
    {"timestamp": "2026-07-24T11:30:00Z", "event": "RedeemVoucher", "Type": "bounty",
     "Amount": 62000},
    {"timestamp": "2026-07-24T12:00:00Z", "event": "MultiSellExplorationData",
     "TotalEarnings": 210000},
]


def fmt_credits(n):
    if n is None:
        return "-- CR"
    return f"{n:,.0f} CR"


def ship_display(internal):
    if not internal:
        return "--"
    return SHIP_NAMES.get(internal.lower(), internal.replace("_", " ").title())


def credit_delta(e):
    """Returns (delta, bucket) for journal events that move the wallet, else None."""
    et = e.get("event")
    if et == "MarketSell":
        return e.get("TotalSale", 0), "Trading"
    if et == "MarketBuy":
        return -e.get("TotalCost", 0), "Trading"
    if et == "RedeemVoucher":
        vtype = e.get("Type", "")
        bucket = "Combat" if vtype in ("bounty", "CombatBond") else "Trading"
        return e.get("Amount", 0), bucket
    if et == "MissionCompleted":
        return e.get("Reward", 0), "Missions"
    if et in ("SellExplorationData", "MultiSellExplorationData"):
        return e.get("TotalEarnings", 0), "Exploration"
    if et == "SellOrganicData":
        total = sum(b.get("Value", 0) + b.get("Bonus", 0) for b in e.get("BioData", []))
        return total, "Exploration"
    if et == "ShipyardBuy":
        return -e.get("ShipPrice", 0), "Other"
    if et == "ShipyardSell":
        return e.get("ShipPrice", 0), "Other"
    if et == "ModuleBuy":
        return -e.get("BuyPrice", 0), "Other"
    if et == "ModuleSell":
        return e.get("SellPrice", 0), "Other"
    if et in ("PayFines", "PayLegacyFines"):
        return -e.get("Amount", 0), "Other"
    return None


def new_state():
    return {
        "commander": None,
        "ship_internal": None,
        "max_jump_range": None,
        "credits": None,
        "session_baseline_credits": None,
        "session_start": None,
        "system": None,
        "station": None,
        "ranks": {},
        "progress": {},
        "buckets": {"Trading": 0, "Combat": 0, "Exploration": 0, "Missions": 0, "Other": 0},
        "equipped_modules": {},   # slot name -> item symbol (current ship only)
        "stored_modules": set(),  # item symbols sitting unused in Storage, galaxy-wide
        "cargo_capacity": None,   # current ship only, from Loadout
        "owned_ships": [],        # [{"ship_internal": str, "system": str|None}, ...] from StoredShips
        "powerplay": None,        # {"power","rank","merits","votes","time_pledged"} or None if unpledged
        "colonization_depots": {},  # market_id -> {"system","station","progress","complete",
                                     #               "failed","resources":[{"name","required","provided"}]}
    }


class JournalWatcher:
    """Background-thread tailer for the Elite Dangerous Journal log. Not
    thread-safe by itself — all state access goes through self.lock since
    the poll loop runs on a worker thread while the UI reads it from the
    main thread."""

    def __init__(self):
        self.lock = threading.Lock()
        self.state = new_state()
        self.status = "Searching for journal folder..."
        self.current_file = None
        self.file_pos = 0
        self.demo_mode = False
        self.demo_loaded_at = 0

    def start(self):
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def _loop(self):
        while True:
            self._poll()
            time.sleep(POLL_INTERVAL_SEC)

    def _poll(self):
        if not JOURNAL_DIR.exists():
            with self.lock:
                self.status = "Journal folder not found — showing Guide tab only."
            return
        try:
            files = sorted(JOURNAL_DIR.glob("Journal.*.log"), key=lambda p: p.stat().st_mtime)
        except OSError:
            with self.lock:
                self.status = "Could not read the journal folder."
            return
        if not files:
            with self.lock:
                self.status = "No journal files found yet — play a session first."
            return

        newest = files[-1]
        if self.demo_mode:
            # Don't let a pre-existing (possibly stale/empty) journal file
            # silently overwrite demo data — only step out of demo mode once
            # a file newer than the demo load actually appears, meaning the
            # user genuinely started a new session.
            try:
                is_new_session = newest.stat().st_mtime > self.demo_loaded_at
            except OSError:
                is_new_session = False
            if not is_new_session:
                return
            self.demo_mode = False

        if newest != self.current_file:
            with self.lock:
                self.state = new_state()
            self.current_file = newest
            self.file_pos = 0

        try:
            with open(self.current_file, "r", encoding="utf-8") as f:
                f.seek(self.file_pos)
                lines = f.readlines()
                self.file_pos = f.tell()
        except OSError:
            return

        with self.lock:
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._apply(event)
            self.status = f"Watching {self.current_file.name} — updated {time.strftime('%H:%M:%S')}"

    def load_sample(self):
        with self.lock:
            self.state = new_state()
            self.current_file = None
            self.demo_mode = True
            self.demo_loaded_at = time.time()
            for event in SAMPLE_EVENTS:
                self._apply(event)
            self.status = "Loaded sample data (demo — not from a live game session)."

    def _apply(self, e):
        """Must be called with self.lock held."""
        et = e.get("event")
        if et == "Commander":
            self.state["commander"] = e.get("Name")
        elif et == "LoadGame":
            self.state["ship_internal"] = e.get("Ship") or self.state["ship_internal"]
            credits = e.get("Credits")
            if credits is not None:
                self.state["credits"] = credits
                self.state["session_baseline_credits"] = credits
                self.state["session_start"] = time.time()
        elif et == "Loadout":
            self.state["ship_internal"] = e.get("Ship") or self.state["ship_internal"]
            if e.get("MaxJumpRange"):
                self.state["max_jump_range"] = e["MaxJumpRange"]
            if e.get("CargoCapacity") is not None:
                self.state["cargo_capacity"] = e["CargoCapacity"]
            modules = e.get("Modules")
            if modules is not None:
                self.state["equipped_modules"] = {
                    m["Slot"]: m["Item"] for m in modules if m.get("Slot") and m.get("Item")
                }
        elif et == "StoredModules":
            items = e.get("Items")
            if items is not None:
                self.state["stored_modules"] = {
                    i["Name"] for i in items if i.get("Name")
                }
        elif et == "StoredShips":
            # Fires when the Ships tab is opened at a shipyard — not automatic
            # every session, so this can stay empty for a while. Ships whose
            # ShipType isn't in SHIP_SLUGS are skipped rather than guessed at,
            # since there's no real coriolis-data slot info to size them with.
            entries = (e.get("ShipsHere") or []) + (e.get("ShipsRemote") or [])
            ships = []
            for entry in entries:
                ship_type = (entry.get("ShipType") or "").lower()
                if ship_type not in SHIP_SLUGS:
                    continue
                ships.append({"ship_internal": ship_type, "system": entry.get("StarSystem")})
            self.state["owned_ships"] = ships
        elif et == "Powerplay":
            # Fires once at startup only if the commander is currently
            # pledged — real Power/Rank/Merits, not derivable from Rank or
            # Progress (neither of which carries a Powerplay component).
            self.state["powerplay"] = {
                "power": e.get("Power"),
                "rank": e.get("Rank"),
                "merits": e.get("Merits"),
                "votes": e.get("Votes"),
                "time_pledged": e.get("TimePledged"),
            }
        elif et == "PowerplayLeave":
            self.state["powerplay"] = None
        elif et == "ColonisationConstructionDepot":
            # No StarSystem/StationName on this event itself (confirmed
            # against real EDMC construction-tracker plugin source) — snapshot
            # whatever system/station we're currently docked at, same as how
            # every other station-service event in this app already works.
            market_id = e.get("MarketID")
            if market_id is not None:
                resources = [
                    {"name": r.get("Name_Localised") or r.get("Name", "?"),
                     "required": r.get("RequiredAmount", 0), "provided": r.get("ProvidedAmount", 0)}
                    for r in e.get("ResourcesRequired") or []
                ]
                self.state["colonization_depots"][market_id] = {
                    "system": self.state.get("system"),
                    "station": self.state.get("station"),
                    "progress": e.get("ConstructionProgress", 0.0),
                    "complete": e.get("ConstructionComplete", False),
                    "failed": e.get("ConstructionFailed", False),
                    "resources": resources,
                }
        elif et == "Rank":
            for k in RANK_KEYS:
                if k in e:
                    self.state["ranks"][k] = e[k]
        elif et == "Progress":
            for k in RANK_KEYS:
                if k in e:
                    self.state["progress"][k] = e[k]
        elif et == "Location":
            if e.get("StarSystem"):
                self.state["system"] = e["StarSystem"]
            self.state["station"] = e.get("StationName") if e.get("Docked") else None
        elif et in ("FSDJump", "CarrierJump"):
            if e.get("StarSystem"):
                self.state["system"] = e["StarSystem"]
            self.state["station"] = None
        elif et == "Docked":
            self.state["station"] = e.get("StationName")
            if e.get("StarSystem"):
                self.state["system"] = e["StarSystem"]
        elif et == "Undocked":
            self.state["station"] = None
        else:
            result = credit_delta(e)
            if result and self.state["credits"] is not None:
                delta, bucket = result
                self.state["credits"] += delta
                if delta > 0:
                    self.state["buckets"][bucket] = self.state["buckets"].get(bucket, 0) + delta

    def get_snapshot(self):
        with self.lock:
            return copy.deepcopy(self.state), self.status


def _fetch_json(url, timeout=15):
    """Shared GET-JSON helper used for both Spansh and coriolis-data calls."""
    req = Request(url, headers=UA_HEADERS)
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        # Spansh returns a JSON body (e.g. {"error": "Could not find
        # starting system"}) alongside 4xx statuses — surface that
        # instead of a bare "HTTP Error 400". A plain-text 404 (e.g. a
        # missing coriolis-data file) won't parse, so it re-raises as-is.
        try:
            return json.loads(exc.read().decode("utf-8"))
        except (ValueError, OSError):
            raise exc


class UpdateChecker:
    """Background-thread check for a newer VERSION file at the repo root —
    a plain-text fetch, no GitHub API/auth needed since the repo is public.
    Never downloads or modifies this running copy; only ever surfaces "a
    newer version exists, here's the link" so an update is always a
    deliberate, user-driven action. Not thread-safe by itself — all state
    access goes through self.lock."""

    def __init__(self):
        self.lock = threading.Lock()
        self.status = "idle"  # idle | checking | ready | error
        self.error = None
        self.latest_version = None

    def check(self):
        t = threading.Thread(target=self._worker, daemon=True)
        t.start()

    def _worker(self):
        with self.lock:
            self.status = "checking"
            self.error = None
        try:
            req = Request(APP_VERSION_URL, headers=UA_HEADERS)
            with urlopen(req, timeout=15) as resp:
                latest = resp.read().decode("utf-8").strip()
        except (URLError, OSError) as exc:
            with self.lock:
                self.status, self.error = "error", f"Request failed: {exc}"
            return
        with self.lock:
            self.latest_version = latest
            self.status = "ready"

    def get_snapshot(self):
        with self.lock:
            return self.status, self.error, self.latest_version


class RoutePlotter:
    """Background-thread client for Spansh's public neutron-router API
    (https://spansh.co.uk/api/route — no API key required). Not thread-safe
    by itself — all state access goes through self.lock, since plotting runs
    on a worker thread while the UI polls it from the main thread."""

    def __init__(self):
        self.lock = threading.Lock()
        self.status = "idle"  # idle | submitting | polling | done | error
        self.error = None
        self.result = None

    def plot(self, origin, destination, range_ly):
        t = threading.Thread(target=self._worker, args=(origin, destination, range_ly), daemon=True)
        t.start()

    def _set(self, **kwargs):
        with self.lock:
            for k, v in kwargs.items():
                setattr(self, k, v)

    def get_snapshot(self):
        with self.lock:
            return self.status, self.error, self.result

    def _worker(self, origin, destination, range_ly):
        self._set(status="submitting", error=None, result=None)
        query = urllib.parse.urlencode({
            "from": origin, "to": destination, "range": range_ly, "efficiency": ROUTE_EFFICIENCY,
        })
        try:
            data = _fetch_json(f"{SPANSH_ROUTE_URL}?{query}")
        except (URLError, OSError, ValueError) as exc:
            self._set(status="error", error=f"Request failed: {exc}")
            return
        if "error" in data:
            self._set(status="error", error=data["error"])
            return
        job = data.get("job")
        if not job:
            self._set(status="error", error="Unexpected response from the route service.")
            return

        self._set(status="polling")
        deadline = time.time() + ROUTE_POLL_TIMEOUT_SEC
        while time.time() < deadline:
            time.sleep(ROUTE_POLL_INTERVAL_SEC)
            try:
                data = _fetch_json(SPANSH_RESULT_URL.format(job))
            except (URLError, OSError, ValueError) as exc:
                self._set(status="error", error=f"Request failed: {exc}")
                return
            status = data.get("status")
            if status == "ok":
                self._set(status="done", result=data.get("result"))
                return
            if status not in ("queued", "processing", None):
                self._set(status="error", error=f"Route service returned status '{status}'.")
                return
        self._set(status="error", error="Route plotting timed out — try again.")


class NearbySystemFinder:
    """Background-thread client for EDSM's free public sphere-systems API —
    used to suggest a real nearby populated system for rank-relevant
    activity. Not thread-safe by itself — all state access goes through
    self.lock."""

    def __init__(self):
        self.lock = threading.Lock()
        self.status = "idle"  # idle | loading | ready | error
        self.error = None
        self.origin_system = None
        self.results = []

    def ensure_loaded(self, system_name):
        with self.lock:
            if self.status == "loading":
                return
            if self.origin_system == system_name and self.status == "ready":
                return
        t = threading.Thread(target=self._worker, args=(system_name,), daemon=True)
        t.start()

    def _worker(self, system_name):
        with self.lock:
            self.status = "loading"
            self.error = None
        query = urllib.parse.urlencode({
            "systemName": system_name, "radius": EDSM_RADIUS_LY, "showInformation": 1,
        })
        try:
            data = _fetch_json(f"{EDSM_SPHERE_URL}?{query}")
        except (URLError, OSError, ValueError) as exc:
            with self.lock:
                self.status = "error"
                self.error = f"Request failed: {exc}"
            return
        if not isinstance(data, list):
            with self.lock:
                self.status = "error"
                self.error = "Unexpected response from EDSM."
            return
        with self.lock:
            self.origin_system = system_name
            self.results = data
            self.status = "ready"

    def get_snapshot(self):
        with self.lock:
            return self.status, self.error, self.origin_system, list(self.results)


def nearest_populated_system(results, allegiance=None):
    candidates = []
    for s in results:
        if not s.get("distance"):
            continue  # skip the origin system itself (distance 0) — not a useful suggestion
        info = s.get("information")
        if not info or not info.get("population"):
            continue
        if allegiance and info.get("allegiance") != allegiance:
            continue
        candidates.append(s)
    if not candidates:
        return None
    return min(candidates, key=lambda s: s.get("distance", float("inf")))


class UnexploredFinder:
    """Background-thread client that finds nearby systems no commander has
    ever submitted scan data for, via EDSM's public sphere-systems + per-
    system bodies endpoints (a system with no bodies on record is one
    nobody has scanned — a real signal, not a guess). Not thread-safe by
    itself — all state access goes through self.lock."""

    def __init__(self):
        self.lock = threading.Lock()
        self.status = "idle"  # idle | loading | ready | error
        self.error = None
        self.results = []

    def find(self, origin_system, radius_ly):
        t = threading.Thread(target=self._worker, args=(origin_system, radius_ly), daemon=True)
        t.start()

    def _worker(self, origin_system, radius_ly):
        with self.lock:
            self.status = "loading"
            self.error = None
            self.results = []
        query = urllib.parse.urlencode({
            "systemName": origin_system, "radius": radius_ly, "showInformation": 1,
        })
        try:
            candidates = _fetch_json(f"{EDSM_SPHERE_URL}?{query}")
        except (URLError, OSError, ValueError) as exc:
            with self.lock:
                self.status, self.error = "error", f"Request failed: {exc}"
            return
        if not isinstance(candidates, list):
            with self.lock:
                self.status, self.error = "error", "Unexpected response from EDSM."
            return

        candidates = sorted((c for c in candidates if c.get("distance")),
                             key=lambda c: c["distance"])
        found = []
        for c in candidates[:UNEXPLORED_CANDIDATE_LIMIT]:
            name = c.get("name")
            if not name:
                continue
            try:
                body_query = urllib.parse.urlencode({"systemName": name})
                data = _fetch_json(f"{EDSM_BODIES_URL}?{body_query}")
            except (URLError, OSError, ValueError):
                continue
            if not data or not data.get("bodies"):
                found.append({"name": name, "distance": c.get("distance", 0)})

        with self.lock:
            self.results = found
            self.status = "ready"

    def get_snapshot(self):
        with self.lock:
            return self.status, self.error, list(self.results)


class MaterialSearchFinder:
    """Background-thread client for Spansh's public bodies/search API — used
    for both surface-material and ring-hotspot mining searches near a
    reference system. Not thread-safe by itself — all state access goes
    through self.lock."""

    def __init__(self):
        self.lock = threading.Lock()
        self.status = "idle"  # idle | loading | ready | error
        self.error = None
        self.results = []

    def search(self, reference_system, mode, material_name, radius_ly):
        t = threading.Thread(target=self._worker,
                              args=(reference_system, mode, material_name, radius_ly), daemon=True)
        t.start()

    def _worker(self, reference_system, mode, material_name, radius_ly):
        with self.lock:
            self.status = "loading"
            self.error = None
            self.results = []
        field = "materials" if mode == "surface" else "ring_signals"
        payload = {
            "filters": {
                "distance_to_arrival": {"min": "0", "max": "50000"},
                field: [{"name": material_name, "comparison": "<=>", "value": [0, 999]}],
            },
            "sort": [{"distance": {"direction": "asc"}}],
            "size": 20,
            "reference_system": reference_system,
        }
        try:
            req = Request(SPANSH_BODIES_URL, data=json.dumps(payload).encode("utf-8"),
                          headers={**UA_HEADERS, "Content-Type": "application/json"})
            with urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (URLError, OSError, ValueError) as exc:
            with self.lock:
                self.status, self.error = "error", f"Request failed: {exc}"
            return
        if not isinstance(data, dict) or data.get("error"):
            with self.lock:
                self.status = "error"
                self.error = data.get("error", "Unexpected response") if isinstance(data, dict) else "Unexpected response"
            return

        parsed = []
        for r in data.get("results") or []:
            distance = r.get("distance", 0)
            if radius_ly and distance > radius_ly:
                continue
            parsed.append(self._extract(r, mode, material_name))

        with self.lock:
            self.results = parsed
            self.status = "ready"

    @staticmethod
    def _extract(r, mode, material_name):
        detail = material_name
        if mode == "surface":
            mat = next((m for m in r.get("materials", []) if m.get("name") == material_name), None)
            if mat:
                detail = f"{mat['share']:.1f}% {material_name}"
        else:
            for ring in r.get("rings", []):
                sig = next((s for s in ring.get("signals", []) if s.get("name") == material_name), None)
                if sig:
                    detail = (f"{material_name} x{sig.get('count', '?')} "
                              f"({ring.get('type', '?')} ring, {r.get('reserve_level', '?')} reserves)")
                    break
        return {
            "system": r.get("system_name", "?"),
            "body": r.get("name", "?"),
            "distance": r.get("distance", 0),
            "detail": detail,
        }

    def get_snapshot(self):
        with self.lock:
            return self.status, self.error, list(self.results)


class PowerSystemFinder:
    """Background-thread client for Spansh's public systems/search API — used
    to find the nearest real system associated with the player's pledged
    Power (for Guide personalization's Powerplay card). Not thread-safe by
    itself — all state access goes through self.lock."""

    def __init__(self):
        self.lock = threading.Lock()
        self.status = "idle"  # idle | loading | ready | error
        self.error = None
        self.results = []

    def find(self, reference_system, power_name, radius_ly):
        t = threading.Thread(target=self._worker,
                              args=(reference_system, power_name, radius_ly), daemon=True)
        t.start()

    def _worker(self, reference_system, power_name, radius_ly):
        with self.lock:
            self.status = "loading"
            self.error = None
            self.results = []
        payload = {
            "filters": {"power": {"value": [power_name]}},
            "sort": [{"distance": {"direction": "asc"}}],
            "size": 10,
            "reference_system": reference_system,
        }
        try:
            req = Request(SPANSH_SYSTEMS_URL, data=json.dumps(payload).encode("utf-8"),
                          headers={**UA_HEADERS, "Content-Type": "application/json"})
            with urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (URLError, OSError, ValueError) as exc:
            with self.lock:
                self.status, self.error = "error", f"Request failed: {exc}"
            return
        if not isinstance(data, dict) or data.get("error"):
            with self.lock:
                self.status = "error"
                self.error = data.get("error", "Unexpected response") if isinstance(data, dict) else "Unexpected response"
            return

        parsed = []
        for r in data.get("results") or []:
            distance = r.get("distance", 0)
            if radius_ly and distance > radius_ly:
                continue
            parsed.append({
                "system": r.get("name", "?"),
                "distance": distance,
                "power_state": r.get("power_state") or "?",
                "controlling_power": r.get("controlling_power"),
            })

        with self.lock:
            self.results = parsed
            self.status = "ready"

    def get_snapshot(self):
        with self.lock:
            return self.status, self.error, list(self.results)


def _extract_module_entries(data):
    """coriolis-data module files are {"<group-code>": [...entries]} — pull
    the list out defensively regardless of the exact key name."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        out = []
        for v in data.values():
            if isinstance(v, list):
                out.extend(v)
        return out
    return []


class ShipDataStore:
    """Background-thread fetch+cache client for EDCD's coriolis-data ship
    and module catalogs (CORIOLIS_RAW_BASE). Not thread-safe by itself — all
    state access goes through self.lock. Module catalogs are ship-agnostic
    and cached once for the whole session; only the ship file itself is
    fetched per ship."""

    def __init__(self):
        self.lock = threading.Lock()
        self.ships = {}     # slug -> ship dict
        self.modules = {}   # (category, filename) -> list of module dicts
        self.status = "idle"  # idle | loading | ready | error
        self.error = None
        self.loaded_slug = None

    def _modules_needed(self):
        needed = [("standard", fname) for _, fname in STANDARD_SLOT_FILES]
        needed += [("hardpoints", fname) for fname in HARDPOINT_MODULE_FILES]
        needed += [("internal", fname) for fname in INTERNAL_MODULE_FILES]
        return needed

    def ensure_loaded(self, slug):
        with self.lock:
            if self.status == "loading":
                return
            have_modules = all(key in self.modules for key in self._modules_needed())
            if slug in self.ships and have_modules:
                self.loaded_slug = slug
                self.status = "ready"
                return
        t = threading.Thread(target=self._worker, args=(slug,), daemon=True)
        t.start()

    def _worker(self, slug):
        with self.lock:
            self.status = "loading"
            self.error = None
        try:
            ship = _fetch_json(f"{CORIOLIS_RAW_BASE}ships/{slug}.json")
            with self.lock:
                self.ships[slug] = ship

            for category, fname in self._modules_needed():
                with self.lock:
                    already_have = (category, fname) in self.modules
                if already_have:
                    continue
                data = _fetch_json(f"{CORIOLIS_RAW_BASE}modules/{category}/{fname}.json")
                entries = _extract_module_entries(data)
                with self.lock:
                    self.modules[(category, fname)] = entries
        except (URLError, OSError, ValueError) as exc:
            with self.lock:
                self.status = "error"
                self.error = str(exc)
            return

        with self.lock:
            self.loaded_slug = slug
            self.status = "ready"

    def get_snapshot(self):
        with self.lock:
            return self.status, self.error, self.loaded_slug

    def get_ship(self, slug):
        with self.lock:
            return self.ships.get(slug)

    def get_modules(self, category, fname):
        with self.lock:
            return list(self.modules.get((category, fname), []))


class Bar(tk.Frame):
    """Simple horizontal fill bar (no ttk, to match the app's plain-tk dark theme)."""

    def __init__(self, parent, width=140, height=10):
        super().__init__(parent, bg=PANEL_BG, width=width, height=height,
                          highlightthickness=1, highlightbackground=CARD_BORDER)
        self.pack_propagate(False)
        self._width = width
        self._height = height
        self.fill = tk.Frame(self, bg=ACCENT)
        self.fill.place(x=0, y=0, width=0, height=height)

    def set_pct(self, pct):
        pct = max(0.0, min(100.0, pct or 0))
        self.fill.place_configure(width=int(self._width * pct / 100))


class RankTile(tk.Frame):
    """One rank's name/tier/progress-bar card, plus a live suggestion for
    the nearest real system worth visiting to work on that rank and a
    one-click button to send it to the Nav tab. A single design reflowed
    across however many columns fit the current window width."""

    def __init__(self, parent, key, on_go):
        super().__init__(parent, bg=CARD_BG, highlightthickness=1, highlightbackground=CARD_BORDER)
        self.key = key
        self.suggested_system = None

        tk.Label(self, text=key, font=FONT_SMALL_BOLD, bg=CARD_BG, fg=ACCENT,
                 anchor="w").pack(fill="x", padx=6, pady=(4, 1))
        self.name_var = tk.StringVar(value="--")
        tk.Label(self, textvariable=self.name_var, font=FONT_SMALL, bg=CARD_BG,
                 fg=MUTED, anchor="w").pack(fill="x", padx=6)
        self.bar = Bar(self, width=120, height=8)
        self.bar.pack(anchor="w", padx=6, pady=(3, 3))

        suggest_row = tk.Frame(self, bg=CARD_BG)
        suggest_row.pack(fill="x", padx=6, pady=(0, 6))
        self.suggest_var = tk.StringVar(value="")
        tk.Label(suggest_row, textvariable=self.suggest_var, font=FONT_SMALL, bg=CARD_BG,
                 fg=MUTED, anchor="w", justify="left", wraplength=150).pack(
            side="left", fill="x", expand=True)
        self.go_btn = tk.Label(suggest_row, text="Go", font=FONT_SMALL_BOLD, bg=CARD_BG,
                                fg=MUTED, padx=4)
        self.go_btn.pack(side="right", anchor="n")
        self.go_btn.bind("<Button-1>", lambda e: on_go(self.key, self.suggested_system))

    def grid_at(self, row, col, **grid_opts):
        self.grid(row=row, column=col, **grid_opts)

    def hide(self):
        self.grid_forget()

    def update_display(self, index, pct):
        table = RANKS.get(self.key, [])
        if index is not None and 0 <= index < len(table):
            self.name_var.set(f"{table[index]} ({pct:.0f}%)")
            self.bar.set_pct(pct)
        else:
            self.name_var.set("--")
            self.bar.set_pct(0)

    def update_suggestion(self, text, system):
        self.suggest_var.set(text)
        self.suggested_system = system
        if system:
            self.go_btn.config(fg=ACCENT, cursor="hand2")
        else:
            self.go_btn.config(fg=MUTED, cursor="")


class GuidePanel:
    """One toggleable, click-to-expand progression-path card."""

    RISK_COLOR = {"Low": GOOD, "Medium": WARN, "High": BAD}

    def __init__(self, parent, path, on_toggle, wrap=380, on_go=None):
        self.key = path["key"]
        self.path = path
        self.expanded = False
        self.built = False
        self.wrap = wrap
        self._personal_nav_system = None

        self.frame = tk.Frame(parent, bg=PANEL_BG, bd=0, highlightthickness=1,
                               highlightbackground="#2a2a2a")

        header = tk.Frame(self.frame, bg=PANEL_BG, cursor="hand2")
        header.pack(fill="x", padx=8, pady=(6, 2))

        title_row = tk.Frame(header, bg=PANEL_BG, cursor="hand2")
        title_row.pack(fill="x")
        self.arrow = tk.Label(title_row, text="▸", font=FONT_TITLE, bg=PANEL_BG,
                               fg=MUTED, width=2, cursor="hand2")
        self.arrow.pack(side="left")
        # Titled labeled with a wraplength (kept in sync via set_wrap) so
        # long titles wrap onto a second line instead of overflowing the
        # card at narrow widths.
        self.title_label = tk.Label(title_row, text=path["title"], font=FONT_TITLE,
                                     bg=PANEL_BG, fg=ACCENT, anchor="w", justify="left",
                                     wraplength=wrap, cursor="hand2")
        self.title_label.pack(side="left", fill="x", expand=True)

        badge_row = tk.Frame(header, bg=PANEL_BG, cursor="hand2")
        badge_row.pack(fill="x", pady=(2, 0))
        risk_color = self.RISK_COLOR.get(path["risk"], MUTED)
        tk.Label(badge_row, text=f"{path['risk']} risk", font=FONT_SMALL_BOLD,
                 bg=PANEL_BG, fg=risk_color).pack(side="left")
        if path.get("tier"):
            tk.Label(badge_row, text=f"{path['tier']} game", font=FONT_SMALL_BOLD,
                     bg=PANEL_BG, fg=TIER_FG).pack(side="left", padx=(8, 0))
        if path.get("category") == "Other":
            tk.Label(badge_row, text="non-profit", font=FONT_SMALL, bg=PANEL_BG,
                     fg=MUTED).pack(side="left", padx=(8, 0))

        self.recommend_label = tk.Label(badge_row, text="★ Recommended for you", font=FONT_SMALL_BOLD,
                                         bg=PANEL_BG, fg=ACCENT)

        self.body = tk.Label(self.frame, text=path["summary"], font=FONT_BODY,
                              bg=PANEL_BG, fg=MUTED, justify="left", anchor="w",
                              wraplength=wrap, cursor="hand2")
        self.body.pack(fill="x", padx=8, pady=(0, 8))

        # "For My Ships" personalization — hidden by default, shown/hidden as
        # a block by the Guide tab's sub-tab switch (not per-card state).
        self.personal_frame = tk.Frame(self.frame, bg=PANEL_BG)
        self.personal_ship_var = tk.StringVar(value="")
        self.personal_ship_label = tk.Label(
            self.personal_frame, textvariable=self.personal_ship_var, font=FONT_SMALL,
            bg=PANEL_BG, fg=FG, anchor="w", justify="left", wraplength=wrap)
        self.personal_ship_label.pack(fill="x", padx=8, pady=(0, 2))

        nav_row = tk.Frame(self.personal_frame, bg=PANEL_BG)
        nav_row.pack(fill="x", padx=8, pady=(0, 8))
        self.personal_nav_var = tk.StringVar(value="")
        # Reserve room for the Go button sharing this row (wrap-30 is what
        # title_label uses, but that row has no same-width sibling widget —
        # leaving the Go button unaccounted for here pushed it past the
        # card's right edge).
        self.personal_nav_label = tk.Label(
            nav_row, textvariable=self.personal_nav_var, font=FONT_SMALL, bg=PANEL_BG,
            fg=MUTED, anchor="w", justify="left", wraplength=max(80, wrap - 60))
        self.personal_nav_label.pack(side="left", fill="x", expand=True)
        self.personal_go_btn = tk.Label(nav_row, text="Go", font=FONT_SMALL_BOLD, bg=PANEL_BG,
                                         fg=MUTED, padx=4)
        self.personal_go_btn.pack(side="right", anchor="n")
        if on_go:
            self.personal_go_btn.bind("<Button-1>", lambda e: on_go(self.key, self._personal_nav_system))

        self.detail_frame = tk.Frame(self.frame, bg=PANEL_BG)

        for w in (header, title_row, self.arrow, self.title_label, badge_row, self.body,
                  self.recommend_label):
            w.bind("<Button-1>", lambda e, k=self.key: on_toggle(k))

    def set_recommended(self, is_recommended):
        if is_recommended:
            self.frame.config(highlightbackground=ACCENT, highlightthickness=2)
            if not self.recommend_label.winfo_manager():
                self.recommend_label.pack(side="left", padx=(8, 0))
        else:
            self.frame.config(highlightbackground="#2a2a2a", highlightthickness=1)
            if self.recommend_label.winfo_manager():
                self.recommend_label.pack_forget()

    def grid_at(self, row, col, **grid_opts):
        self.frame.grid(row=row, column=col, **grid_opts)

    def hide(self):
        self.frame.grid_forget()

    def set_wrap(self, wrap):
        wrap = max(120, int(wrap))
        if wrap == self.wrap:
            return
        self.wrap = wrap
        self.body.config(wraplength=wrap)
        self.title_label.config(wraplength=max(90, wrap - 30))
        self.personal_ship_label.config(wraplength=wrap)
        self.personal_nav_label.config(wraplength=max(80, wrap - 60))
        if self.built:
            inner_wrap = max(120, wrap - 10)
            children = self.detail_frame.winfo_children()
            for i, child in enumerate(children):
                if not child.cget("wraplength"):
                    continue  # header labels ("Early steps:"/"Tips:") don't wrap
                child.config(wraplength=wrap if i == 0 else inner_wrap)

    def toggle(self):
        self.expanded = not self.expanded
        self.arrow.config(text="▾" if self.expanded else "▸")
        if self.expanded:
            if not self.built:
                self._build_detail()
                self.built = True
            self.detail_frame.pack(fill="x", padx=8, pady=(0, 8))
        else:
            self.detail_frame.pack_forget()

    def _build_detail(self):
        p = self.path
        inner_wrap = max(120, self.wrap - 10)
        tk.Label(self.detail_frame, text=f"Starter ship: {p['starter_ship']}",
                 font=FONT_SMALL_BOLD, bg=PANEL_BG, fg=FG, anchor="w",
                 justify="left", wraplength=self.wrap).pack(fill="x", pady=(2, 4))
        tk.Label(self.detail_frame, text="Early steps:", font=FONT_SMALL_BOLD,
                 bg=PANEL_BG, fg=ACCENT, anchor="w").pack(fill="x")
        for step in p["early_steps"]:
            tk.Label(self.detail_frame, text=f"• {step}", font=FONT_SMALL,
                     bg=PANEL_BG, fg=FG, anchor="w", justify="left",
                     wraplength=inner_wrap).pack(fill="x", padx=(8, 0), pady=1)
        tk.Label(self.detail_frame, text="Tips:", font=FONT_SMALL_BOLD,
                 bg=PANEL_BG, fg=ACCENT, anchor="w").pack(fill="x", pady=(4, 0))
        for tip in p["tips"]:
            tk.Label(self.detail_frame, text=f"• {tip}", font=FONT_SMALL,
                     bg=PANEL_BG, fg=MUTED, anchor="w", justify="left",
                     wraplength=inner_wrap).pack(fill="x", padx=(8, 0), pady=1)

    def set_personal_mode(self, is_personal):
        if is_personal:
            if not self.personal_frame.winfo_manager():
                # detail_frame is only packed once the card's been expanded
                # at least once — anchor before it when possible, but a plain
                # pack() still lands in the right spot (append order) since
                # this always runs before an unexpanded card's detail_frame
                # gets packed for the first time.
                kwargs = {"fill": "x"}
                if self.detail_frame.winfo_manager():
                    kwargs["before"] = self.detail_frame
                self.personal_frame.pack(**kwargs)
        elif self.personal_frame.winfo_manager():
            self.personal_frame.pack_forget()

    def update_personalization(self, ship_line, nav_line, nav_system):
        self.personal_ship_var.set(ship_line or "")
        self.personal_nav_var.set(nav_line or "")
        self._personal_nav_system = nav_system
        if nav_system:
            self.personal_go_btn.config(fg=ACCENT, cursor="hand2")
        else:
            self.personal_go_btn.config(fg=MUTED, cursor="")

    def flash_highlight(self):
        self.frame.config(highlightbackground=ACCENT, highlightthickness=2)
        self.frame.after(1500, lambda: self.frame.config(
            highlightbackground="#2a2a2a", highlightthickness=1))


def apply_dark_titlebar(root):
    """Best-effort: match the native Windows title bar to the app's dark
    theme via DWM immersive dark mode. Silently does nothing on non-Windows
    or older Windows builds that don't support the attribute."""
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        value = ctypes.c_int(1)
        for attr in (20, 19):
            result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(value), ctypes.sizeof(value))
            if result == 0:
                break
    except (OSError, AttributeError):
        pass


def ensure_single_instance():
    ERROR_ALREADY_EXISTS = 183
    ctypes.windll.kernel32.CreateMutexW(None, False, "EliteCompanionSingleInstance")
    return ctypes.windll.kernel32.GetLastError() != ERROR_ALREADY_EXISTS


class App:
    def __init__(self, root):
        self.root = root
        self.settings = load_settings()

        root.title("Elite Dangerous Companion")
        root.configure(bg=BG)
        saved_geometry = clean_geometry(self.settings.get("window_geometry"))
        root.geometry(saved_geometry if saved_geometry and geometry_on_screen(saved_geometry)
                      else DEFAULT_GEOMETRY)
        root.minsize(420, 320)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.watcher = JournalWatcher()
        self.watcher.start()
        self.route_plotter = RoutePlotter()
        self._nav_last_status = None
        self.nearby_finder = NearbySystemFinder()
        self.ship_store = ShipDataStore()
        self.build_slug = None
        self._build_rendered_slug = None
        self.build_slot_widgets = {}
        self.unexplored_finder = UnexploredFinder()
        self._unexplored_last_status = None
        self.material_finder = MaterialSearchFinder()
        self._material_last_status = None
        self.material_mode = "surface"
        # Separate instances (not shared with the Explore tab's finders above)
        # so the Guide tab's fixed auto-query (current system, Painite) can't
        # clobber whatever the user manually searched for in Explore, or vice
        # versa — both wrap the same finder classes.
        self.guide_unexplored_finder = UnexploredFinder()
        self.guide_material_finder = MaterialSearchFinder()
        self.guide_power_finder = PowerSystemFinder()
        self._guide_unexplored_origin = None
        self._guide_material_origin = None
        self._guide_power_query = None
        self.guide_subtab = "general"
        self._owned_ships_signature = None
        self._ship_recommendations = {}
        self._personal_recs_ready = False
        self._personal_ship_count = 0
        self.update_checker = UpdateChecker()
        self._update_last_status = None

        self.tab_frames = {}
        self.tab_buttons = {}
        self.tab_canvases = {}
        self.active_canvas = None
        self.current_tab = "guide"
        self.view_modes = dict(DEFAULT_VIEW_MODES)
        saved_modes = self.settings.get("view_modes")
        if isinstance(saved_modes, dict):
            for tab_key in DEFAULT_VIEW_MODES:
                if saved_modes.get(tab_key) in ("auto", "list", "grid"):
                    self.view_modes[tab_key] = saved_modes[tab_key]
        self.view_buttons = {}
        self.guide_panels = {}
        self.guide_col_count = 0
        self._guide_last_width = None
        self.risk_vars = {}
        self.category_vars = {}
        self.rank_tiles = {}
        self.rank_col_count = 0
        self._rank_last_width = None
        self._hint_path_key = None
        self._recommended_key = None
        self._suggested_hub = None

        self._build_ui()
        self._tick()

    def _on_close(self):
        try:
            geometry = clean_geometry(self.root.geometry())
        except tk.TclError:
            geometry = None
        settings = {"view_modes": self.view_modes}
        if geometry and GEOMETRY_RE.match(geometry):
            settings["window_geometry"] = geometry
        save_settings(settings)
        self.root.destroy()

    # ---------- UI scaffolding ----------

    def _build_ui(self):
        top = tk.Frame(self.root, bg=BG)
        top.pack(fill="x", padx=10, pady=(8, 0))
        tk.Label(top, text="ELITE DANGEROUS COMPANION", font=("Segoe UI", 11, "bold"),
                 bg=BG, fg=FG).pack(side="left")

        view_box = tk.Frame(top, bg=PANEL_BG, highlightthickness=1,
                             highlightbackground="#2a2a2a")
        view_box.pack(side="right")
        for mode, label in (("auto", "⇲ Auto"), ("list", "☰ List"), ("grid", "▦ Grid")):
            btn = tk.Label(view_box, text=label, font=FONT_SMALL_BOLD, cursor="hand2",
                            padx=8, pady=3)
            btn.pack(side="left")
            btn.bind("<Button-1>", lambda e, m=mode: self._set_view_mode(m))
            self.view_buttons[mode] = btn

        self._build_tab_bar()

        self.update_banner_var = tk.StringVar(value="")
        self.update_banner = tk.Label(self.root, textvariable=self.update_banner_var,
                                       font=FONT_SMALL_BOLD, bg=WARN, fg="#1a1a1a", cursor="hand2",
                                       anchor="w", padx=10, pady=4)
        self.update_banner.bind("<Button-1>", self._on_update_banner_click)
        # Not packed yet — shown only once an update is actually detected.

        tab_container = self.tab_container = tk.Frame(self.root, bg=BG)
        tab_container.pack(fill="both", expand=True)

        self._build_guide_tab(tab_container)
        self._build_tracker_tab(tab_container)
        self._build_nav_tab(tab_container)
        self._build_build_tab(tab_container)
        self._build_explore_tab(tab_container)
        self._build_colonization_tab(tab_container)

        self.root.bind_all("<MouseWheel>", self._on_mousewheel)
        self._switch_tab("guide")
        self._relayout_guide()
        self._relayout_ranks()
        self.guide_canvas.bind("<Configure>", lambda e: self._relayout_guide(e.width), add="+")
        self.tracker_canvas.bind("<Configure>", lambda e: self._relayout_ranks(e.width), add="+")

        # On Windows, content placed inside the canvas-embedded scroll frames
        # can end up laid out correctly but not actually painted until the
        # window is resized — a real resize always fixes it, so nudge the
        # window size by a pixel and back to force a genuine repaint.
        self.root.update_idletasks()
        w, h = self.root.winfo_width(), self.root.winfo_height()
        self.root.geometry(f"{w + 1}x{h}")
        self.root.update_idletasks()
        self.root.geometry(f"{w}x{h}")

        self.update_checker.check()

    def _on_update_banner_click(self, event=None):
        try:
            webbrowser.open(APP_REPO_URL)
        except OSError:
            pass

    def _render_update_banner(self, status, latest_version):
        if status == "ready" and latest_version and latest_version != APP_VERSION:
            self.update_banner_var.set(
                f"Update available: v{latest_version} (you have v{APP_VERSION}) — "
                f"click to view on GitHub.")
            if not self.update_banner.winfo_manager():
                self.update_banner.pack(fill="x", before=self.tab_container)
        elif self.update_banner.winfo_manager():
            self.update_banner.pack_forget()

    def _build_tab_bar(self):
        bar = tk.Frame(self.root, bg=BG)
        bar.pack(fill="x", padx=10, pady=(6, 0))
        for key, label in (("guide", "Guide"), ("tracker", "Tracker"), ("nav", "Nav"), ("build", "Build"),
                           ("explore", "Explore"), ("colony", "Colony")):
            btn = tk.Label(bar, text=label, font=FONT_TITLE, bg=BG, fg=MUTED,
                            cursor="hand2", padx=10, pady=4)
            btn.pack(side="left")
            btn.bind("<Button-1>", lambda e, k=key: self._switch_tab(k))
            self.tab_buttons[key] = btn

    def _make_scrollable(self, parent):
        outer = tk.Frame(parent, bg=BG)
        canvas = tk.Canvas(outer, bg=BG, highlightthickness=0)
        scrollbar = tk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=BG)
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _sync(event=None):
            # Lock the embedded frame's width to the canvas's own viewport
            # width so content reflows/wraps instead of growing past the
            # visible edge (canvases don't auto-fit their embedded window).
            canvas_w = canvas.winfo_width()
            if canvas_w > 1:
                canvas.itemconfigure(win_id, width=canvas_w)
            canvas.configure(scrollregion=canvas.bbox("all"))
            bbox = canvas.bbox("all")
            content_h = (bbox[3] - bbox[1]) if bbox else 0
            needs_scroll = content_h > canvas.winfo_height()
            if needs_scroll and scrollbar.winfo_manager() != "pack":
                scrollbar.pack(side="right", fill="y")
            elif not needs_scroll and scrollbar.winfo_manager() == "pack":
                scrollbar.pack_forget()

        inner.bind("<Configure>", _sync)
        canvas.bind("<Configure>", _sync, add="+")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        return outer, canvas, inner

    def _switch_tab(self, key):
        self.current_tab = key
        for k, frame in self.tab_frames.items():
            if k == key:
                frame.pack(fill="both", expand=True)
            else:
                frame.pack_forget()
        for k, btn in self.tab_buttons.items():
            btn.config(fg=ACCENT if k == key else MUTED, bg=PANEL_BG if k == key else BG)
        self.active_canvas = self.tab_canvases.get(key)
        self._refresh_view_buttons()

    def _on_mousewheel(self, event):
        if self.active_canvas:
            self.active_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _refresh_view_buttons(self):
        """Highlight the Auto/List/Grid toggle to match whichever tab is
        currently active — each tab remembers its own view mode."""
        mode = self.view_modes.get(self.current_tab, "auto")
        for m, btn in self.view_buttons.items():
            active = m == mode
            btn.config(fg=ACCENT if active else MUTED, bg=CARD_BG if active else PANEL_BG)

    def _set_view_mode(self, mode):
        self.view_modes[self.current_tab] = mode
        self._refresh_view_buttons()
        # Force a relayout even if the pixel width hasn't changed, since the
        # column count depends on mode too. Only the active tab needs it —
        # Guide/Tracker are the only tabs with a column layout to redo.
        if self.current_tab == "guide":
            self._guide_last_width = None
            self._relayout_guide()
        elif self.current_tab == "tracker":
            self._rank_last_width = None
            self._relayout_ranks()

    @staticmethod
    def _columns_for(avail_width, min_width, max_cols, mode):
        natural = max(1, min(max_cols, int(avail_width // min_width)))
        if mode == "list":
            return 1
        if mode == "grid":
            return max(2, natural)
        return natural

    @staticmethod
    def _set_grid_columns(container, n, max_cols):
        for i in range(max_cols):
            container.grid_columnconfigure(i, weight=1 if i < n else 0)

    # ---------- Guide tab ----------

    def _build_guide_tab(self, parent):
        frame = tk.Frame(parent, bg=BG)

        hint = tk.Frame(frame, bg=BG)
        hint.pack(fill="x", padx=10)
        tk.Label(hint, text="Click a path to see early steps and tips", font=FONT_SMALL,
                 bg=BG, fg=MUTED).pack(side="left")

        subtab_row = tk.Frame(frame, bg=BG)
        subtab_row.pack(fill="x", padx=10, pady=(4, 0))
        self.guide_subtab_buttons = {}
        for key, label in (("general", "General"), ("personal", "For My Ships")):
            active = key == self.guide_subtab
            btn = tk.Label(subtab_row, text=label, font=FONT_SMALL_BOLD, cursor="hand2",
                            padx=8, pady=3, bg=BG if active else CARD_BG,
                            fg=ACCENT if active else MUTED)
            btn.pack(side="left", padx=(0, 4))
            btn.bind("<Button-1>", lambda e, k=key: self._set_guide_subtab(k))
            self.guide_subtab_buttons[key] = btn

        self.guide_personal_status_var = tk.StringVar(value="")
        self.guide_personal_status_label = tk.Label(
            frame, textvariable=self.guide_personal_status_var, font=FONT_SMALL, bg=BG,
            fg=MUTED, anchor="w", justify="left", wraplength=460)
        # Not packed yet — shown only while the "For My Ships" sub-tab is active.

        self.recommend_var = tk.StringVar(value="")
        self.recommend_banner = tk.Label(frame, textvariable=self.recommend_var, font=FONT_SMALL,
                                          bg=PANEL_BG, fg=ACCENT, anchor="w", justify="left",
                                          wraplength=460, cursor="hand2")
        self.recommend_banner.bind("<Button-1>", self._on_recommend_click)

        self.guide_filter_box = filter_box = tk.LabelFrame(
            frame, text=" Filter ", bg=BG, fg=MUTED, labelanchor="nw", bd=1, relief="solid",
            highlightbackground="#2a2a2a")
        filter_box.pack(fill="x", padx=10, pady=(6, 4))

        risk_row = tk.Frame(filter_box, bg=BG)
        risk_row.pack(fill="x", padx=4, pady=(4, 2))
        tk.Label(risk_row, text="Risk:", font=FONT_SMALL, bg=BG, fg=MUTED,
                 width=8, anchor="w").pack(side="left")
        for risk in ("Low", "Medium", "High"):
            var = tk.BooleanVar(value=True)
            cb = tk.Checkbutton(risk_row, text=risk, bg=BG, fg=FG, selectcolor=PANEL_BG,
                                 activebackground=BG, activeforeground=ACCENT,
                                 font=FONT_SMALL, variable=var, command=self._apply_guide_filter)
            cb.pack(side="left", padx=(0, 12))
            self.risk_vars[risk] = var

        category_row = tk.Frame(filter_box, bg=BG)
        category_row.pack(fill="x", padx=4, pady=(2, 4))
        tk.Label(category_row, text="Focus:", font=FONT_SMALL, bg=BG, fg=MUTED,
                 width=8, anchor="w").pack(side="left")
        for category, label in (("Money", "Money-making"), ("Other", "Other activities")):
            var = tk.BooleanVar(value=True)
            cb = tk.Checkbutton(category_row, text=label, bg=BG, fg=FG, selectcolor=PANEL_BG,
                                 activebackground=BG, activeforeground=ACCENT,
                                 font=FONT_SMALL, variable=var, command=self._apply_guide_filter)
            cb.pack(side="left", padx=(0, 12))
            self.category_vars[category] = var

        outer, canvas, inner = self._make_scrollable(frame)
        outer.pack(fill="both", expand=True, pady=4)

        self.guide_cols_row = tk.Frame(inner, bg=BG)
        self.guide_cols_row.pack(fill="both", expand=True)
        for path in PATHS:
            panel = GuidePanel(self.guide_cols_row, path, self._toggle_guide_panel,
                                wrap=GUIDE_MIN_CARD_W, on_go=self._on_rank_go)
            self.guide_panels[path["key"]] = panel

        self.tab_frames["guide"] = frame
        self.tab_canvases["guide"] = canvas
        self.guide_canvas = canvas

    def _toggle_guide_panel(self, key):
        self.guide_panels[key].toggle()

    def _apply_guide_filter(self):
        self._guide_last_width = None
        self._relayout_guide()

    def _relayout_guide(self, avail_width=None):
        if avail_width is None:
            avail_width = self.guide_canvas.winfo_width()
        if avail_width <= 1:
            avail_width = 560
        n = self._columns_for(avail_width, GUIDE_MIN_CARD_W, GUIDE_MAX_COLS, self.view_modes["guide"])
        if n == self.guide_col_count and self._guide_last_width is not None \
                and abs(avail_width - self._guide_last_width) < 10:
            return
        self._guide_last_width = avail_width
        self.guide_col_count = n
        self._set_grid_columns(self.guide_cols_row, n, GUIDE_MAX_COLS)

        card_w = max(140, avail_width // n - 26)
        visible = [p for p in PATHS if self.risk_vars[p["risk"]].get()
                   and self.category_vars[p["category"]].get()]
        for path in PATHS:
            panel = self.guide_panels[path["key"]]
            if path in visible:
                idx = visible.index(path)
                panel.set_wrap(card_w)
                panel.grid_at(idx // n, idx % n, sticky="nsew", padx=6, pady=5)
            else:
                panel.hide()

    def _set_guide_subtab(self, key):
        if key == self.guide_subtab:
            return
        self.guide_subtab = key
        for k, btn in self.guide_subtab_buttons.items():
            active = k == key
            btn.config(fg=ACCENT if active else MUTED, bg=BG if active else CARD_BG)
        for panel in self.guide_panels.values():
            panel.set_personal_mode(key == "personal")
        if key == "personal":
            if not self.guide_personal_status_label.winfo_manager():
                self.guide_personal_status_label.pack(fill="x", padx=10, pady=(2, 0),
                                                        before=self.guide_filter_box)
        elif self.guide_personal_status_label.winfo_manager():
            self.guide_personal_status_label.pack_forget()

    def _guide_ship_signature(self, state):
        owned = state.get("owned_ships", [])
        current_internal = state.get("ship_internal")
        slugs = {SHIP_SLUGS[o["ship_internal"]] for o in owned if o["ship_internal"] in SHIP_SLUGS}
        if current_internal in SHIP_SLUGS:
            slugs.add(SHIP_SLUGS[current_internal])
        return frozenset(slugs)

    def _poll_personalization(self):
        """Lazy: only fetches/queries anything while the "For My Ships"
        sub-tab is actually open, mirroring the Build tab's on-demand load."""
        if self.guide_subtab != "personal":
            return
        state, _ = self.watcher.get_snapshot()

        signature = self._guide_ship_signature(state)
        if signature != self._owned_ships_signature:
            self._owned_ships_signature = signature
            self._ship_recommendations = {}
            self._personal_recs_ready = False

        missing = [s for s in signature if self.ship_store.get_ship(s) is None]
        if missing:
            status, _error, _loaded = self.ship_store.get_snapshot()
            if status != "loading":
                self.ship_store.ensure_loaded(missing[0])
        elif not self._personal_recs_ready:
            self._compute_ship_recommendations(state)
            self._personal_recs_ready = True

        current_system = state.get("system")
        if current_system and current_system != self._guide_unexplored_origin:
            self._guide_unexplored_origin = current_system
            self.guide_unexplored_finder.find(current_system, min(EDSM_RADIUS_LY, EDSM_MAX_RADIUS_LY))
        if current_system and current_system != self._guide_material_origin:
            self._guide_material_origin = current_system
            self.guide_material_finder.search(current_system, "ring", MINING_NAV_COMMODITY,
                                               MATERIAL_SEARCH_RADIUS_LY)

        power_name = (state.get("powerplay") or {}).get("power")
        power_query = (power_name, current_system) if power_name and current_system else None
        if power_query and power_query != self._guide_power_query:
            self._guide_power_query = power_query
            self.guide_power_finder.find(current_system, power_name, POWER_HUB_RADIUS_LY)
        elif not power_query:
            self._guide_power_query = None

        self._render_guide_personal(state, missing)
        self._render_guide_personal_status(state, missing)

    def _compute_ship_recommendations(self, state):
        owned = state.get("owned_ships", [])
        current_internal = state.get("ship_internal")
        entries = {}
        for o in owned:
            slug = SHIP_SLUGS.get(o["ship_internal"])
            if slug:
                entries[o["ship_internal"]] = slug
        current_slug = SHIP_SLUGS.get(current_internal)
        if current_slug:
            entries[current_internal] = current_slug
        self._personal_ship_count = len(entries)

        recs = {}
        for path_key, category in PATH_STAT_CATEGORY.items():
            best = None
            for internal, slug in entries.items():
                wrapper = self.ship_store.get_ship(slug)
                ship = wrapper.get(slug) if wrapper else None
                if not ship:
                    continue
                score = ship_stat_score(ship, category)
                if best is None or score > best[0]:
                    best = (score, internal)
            recs[path_key] = best
        self._ship_recommendations = recs

    def _personal_ship_line(self, path_key, category, current_internal, loading,
                             cargo_capacity, max_jump_range, fitted_hardpoints):
        if category is None:
            return "Ship-agnostic — whichever owned ship fits the objective works."
        if loading:
            return "Loading your owned ships' component data..."
        rec = self._ship_recommendations.get(path_key)
        if not rec:
            return ("No owned-ship data yet — visit any shipyard's Ships tab in-game, or fly your "
                     "current ship, so this can compare your fleet.")
        score, internal = rec
        name = SHIP_NAMES.get(internal, internal)
        label = STAT_CATEGORY_LABEL.get(category, "capacity")
        if internal == current_internal:
            if category == "cargo" and cargo_capacity is not None:
                return f"Your current ship, the {name}, is your best owned pick — {cargo_capacity:.0f}t cargo fitted."
            if category == "fsd" and max_jump_range is not None:
                return f"Your current ship, the {name}, is your best owned pick — {max_jump_range:.1f} ly jump range fitted."
            if category == "hardpoint" and fitted_hardpoints:
                return f"Your current ship, the {name}, is your best owned pick — {fitted_hardpoints} hardpoint(s) fitted."
            return f"Your current ship, the {name}, is your best owned pick (hull {label}: {score})."
        return f"Best owned ship for this: {name} (hull {label}: {score} — unfitted hull capacity, not a live loadout)."

    def _personal_nav_line(self, nav_mode, current_system, nearest_unexplored,
                            nearest_material, nearest_hub, powerplay=None, nearest_power_system=None):
        if nav_mode is None:
            return "No single destination applies for this — see the tips above.", None
        if not current_system:
            return "Current system unknown yet — play a session or load sample data.", None
        if nav_mode == "power_hub":
            if not powerplay:
                return "Not pledged to a Power yet — pledge in-game to get a destination here.", None
            if nearest_power_system is None:
                return (f"Looking for a system tied to {powerplay['power']} near "
                         f"{current_system}...", None)
            return (f"Nearest {powerplay['power']} system: {nearest_power_system['system']} "
                     f"({nearest_power_system['distance']:.1f} ly, {nearest_power_system['power_state']})",
                    nearest_power_system["system"])
        if nav_mode == "unexplored":
            if nearest_unexplored is None:
                return (f"Looking for an unexplored system near {current_system} "
                         f"(see the Explore tab for full control)...", None)
            return (f"Nearest unexplored: {nearest_unexplored['name']} "
                     f"({nearest_unexplored['distance']:.1f} ly)", nearest_unexplored["name"])
        if nav_mode == "material":
            if nearest_material is None:
                return (f"Looking for a {MINING_NAV_COMMODITY} hotspot near {current_system} "
                         f"(see the Explore tab for other commodities)...", None)
            return (f"Nearest {MINING_NAV_COMMODITY} hotspot: {nearest_material['system']} "
                     f"({nearest_material['distance']:.1f} ly)", nearest_material["system"])
        if nav_mode == "hub":
            if nearest_hub is None:
                return f"Finding a nearby populated system near {current_system}...", None
            return (f"Nearest hub: {nearest_hub['name']} ({nearest_hub['distance']:.1f} ly) — "
                     f"check its board", nearest_hub["name"])
        return "", None

    def _render_guide_personal(self, state, missing_slugs):
        current_internal = state.get("ship_internal")
        current_system = state.get("system")
        cargo_capacity = state.get("cargo_capacity")
        max_jump_range = state.get("max_jump_range")
        equipped = state.get("equipped_modules", {})
        fitted_hardpoints = sum(1 for slot in equipped if "hardpoint" in slot.lower())

        _status, _error, unexplored_results = self.guide_unexplored_finder.get_snapshot()
        nearest_unexplored = min(unexplored_results, key=lambda r: r["distance"]) if unexplored_results else None
        _status, _error, material_results = self.guide_material_finder.get_snapshot()
        nearest_material = min(material_results, key=lambda r: r["distance"]) if material_results else None
        _status, _error, nearby_origin, nearby_results = self.nearby_finder.get_snapshot()
        nearest_hub = nearest_populated_system(nearby_results) if nearby_origin == current_system else None
        _status, _error, power_results = self.guide_power_finder.get_snapshot()
        nearest_power_system = min(power_results, key=lambda r: r["distance"]) if power_results else None
        powerplay = state.get("powerplay")

        loading = bool(missing_slugs)
        for path in PATHS:
            panel = self.guide_panels[path["key"]]
            category = PATH_STAT_CATEGORY.get(path["key"])
            ship_line = self._personal_ship_line(path["key"], category, current_internal, loading,
                                                  cargo_capacity, max_jump_range, fitted_hardpoints)
            nav_mode = PATH_NAV_MODE.get(path["key"])
            nav_line, nav_system = self._personal_nav_line(nav_mode, current_system, nearest_unexplored,
                                                             nearest_material, nearest_hub,
                                                             powerplay, nearest_power_system)
            panel.update_personalization(ship_line, nav_line, nav_system)

    def _render_guide_personal_status(self, state, missing_slugs):
        if missing_slugs:
            text = f"Loading component data for {len(missing_slugs)} owned ship(s)..."
        elif not state.get("owned_ships"):
            text = ("No stored-ship data received yet — visit any shipyard's Ships tab in-game (or "
                     "click Load Sample Data) to personalize this against your whole fleet. Showing "
                     "your current ship only for now.")
        else:
            text = f"Personalized against {self._personal_ship_count} owned ship(s)."
        self.guide_personal_status_var.set(text)

    # ---------- Tracker tab ----------

    def _build_tracker_tab(self, parent):
        frame = tk.Frame(parent, bg=BG)
        outer, canvas, inner = self._make_scrollable(frame)
        outer.pack(fill="both", expand=True)

        info = tk.Frame(inner, bg=PANEL_BG, highlightthickness=1, highlightbackground="#2a2a2a")
        info.pack(fill="x", padx=10, pady=(6, 4))
        self.cmdr_var = tk.StringVar(value="Commander: --")
        self.ship_var = tk.StringVar(value="Ship: --")
        self.loc_var = tk.StringVar(value="Location: --")
        tk.Label(info, textvariable=self.cmdr_var, font=FONT_BODY, bg=PANEL_BG,
                 fg=FG, anchor="w").pack(fill="x", padx=8, pady=(6, 0))
        tk.Label(info, textvariable=self.ship_var, font=FONT_BODY, bg=PANEL_BG,
                 fg=FG, anchor="w").pack(fill="x", padx=8)
        tk.Label(info, textvariable=self.loc_var, font=FONT_BODY, bg=PANEL_BG,
                 fg=FG, anchor="w").pack(fill="x", padx=8, pady=(0, 6))

        credits_box = tk.Frame(inner, bg=PANEL_BG, highlightthickness=1,
                                highlightbackground="#2a2a2a")
        credits_box.pack(fill="x", padx=10, pady=4)
        self.credits_var = tk.StringVar(value="-- CR")
        tk.Label(credits_box, textvariable=self.credits_var, font=("Segoe UI", 16, "bold"),
                 bg=PANEL_BG, fg=FG).pack(anchor="w", padx=8, pady=(6, 0))
        self.credits_delta_var = tk.StringVar(value="Session: --")
        self.credits_delta_label = tk.Label(credits_box, textvariable=self.credits_delta_var,
                                             font=FONT_SMALL, bg=PANEL_BG, fg=MUTED, anchor="w")
        self.credits_delta_label.pack(anchor="w", padx=8, pady=(0, 6))

        self.powerplay_box = tk.Frame(inner, bg=PANEL_BG, highlightthickness=1,
                                       highlightbackground="#2a2a2a")
        self.powerplay_var = tk.StringVar(value="")
        tk.Label(self.powerplay_box, textvariable=self.powerplay_var, font=FONT_BODY,
                 bg=PANEL_BG, fg=FG, anchor="w").pack(fill="x", padx=8, pady=6)
        # Not packed yet — shown only once a real Powerplay pledge is seen
        # (the event only fires at startup if the commander is pledged).

        ranks_box = tk.LabelFrame(inner, text=" Ranks ", bg=BG, fg=MUTED, labelanchor="nw",
                                   bd=1, relief="solid", highlightbackground="#2a2a2a")
        ranks_box.pack(fill="x", padx=10, pady=4)

        self.rank_cols_row = tk.Frame(ranks_box, bg=BG)
        self.rank_cols_row.pack(fill="x")
        for key in RANK_KEYS:
            self.rank_tiles[key] = RankTile(self.rank_cols_row, key, self._on_rank_go)

        self.hint_var = tk.StringVar(value="")
        self.hint_label = tk.Label(inner, textvariable=self.hint_var, font=FONT_SMALL,
                                    bg=BG, fg=ACCENT, anchor="w", justify="left",
                                    wraplength=400, cursor="hand2")
        self.hint_label.pack(fill="x", padx=10, pady=(4, 2))
        self.hint_label.bind("<Button-1>", self._on_hint_click)

        bottom = tk.Frame(inner, bg=BG)
        bottom.pack(fill="x", padx=10, pady=(2, 6))
        self.tracker_status_var = tk.StringVar(value="Searching for journal folder...")
        tk.Label(bottom, textvariable=self.tracker_status_var, font=FONT_SMALL,
                 bg=BG, fg=MUTED, anchor="w", wraplength=280,
                 justify="left").pack(side="left")
        demo_btn = tk.Label(bottom, text="Load Sample Data", font=FONT_SMALL_BOLD,
                             bg=PANEL_BG, fg=ACCENT, cursor="hand2", padx=8, pady=3)
        demo_btn.pack(side="right")
        demo_btn.bind("<Button-1>", lambda e: self.watcher.load_sample())

        self.tab_frames["tracker"] = frame
        self.tab_canvases["tracker"] = canvas
        self.tracker_canvas = canvas

    def _relayout_ranks(self, avail_width=None):
        if avail_width is None:
            avail_width = self.tracker_canvas.winfo_width()
        if avail_width <= 1:
            avail_width = 560
        n = self._columns_for(avail_width, RANK_MIN_TILE_W, RANK_MAX_COLS, self.view_modes["tracker"])
        if n == self.rank_col_count and self._rank_last_width is not None \
                and abs(avail_width - self._rank_last_width) < 10:
            return
        self._rank_last_width = avail_width
        self.rank_col_count = n

        self._set_grid_columns(self.rank_cols_row, n, RANK_MAX_COLS)
        for i, key in enumerate(RANK_KEYS):
            self.rank_tiles[key].grid_at(i // n, i % n, sticky="nsew", padx=3, pady=3)

    def _on_hint_click(self, event):
        if not self._hint_path_key:
            return
        self._switch_tab("guide")
        panel = self.guide_panels.get(self._hint_path_key)
        if panel:
            panel.flash_highlight()

    def _on_recommend_click(self, event):
        if not self._recommended_key:
            return
        panel = self.guide_panels.get(self._recommended_key)
        if panel:
            panel.flash_highlight()

    def _render_recommendation(self, state):
        credits = state.get("credits")
        tier = estimate_tier(credits)
        buckets = state.get("buckets", {})
        positive = {k: v for k, v in buckets.items() if v > 0 and k in BUCKET_TO_PATH}
        dominant_bucket = max(positive, key=positive.get) if positive else None
        rec = recommend_path(tier, dominant_bucket)
        rec_key = rec["key"] if rec else None

        if rec_key != self._recommended_key:
            self._recommended_key = rec_key
            for key, panel in self.guide_panels.items():
                panel.set_recommended(key == rec_key)

        if rec:
            reason = (f"earning mostly from {dominant_bucket}" if dominant_bucket
                      else f"~{credits:,.0f} CR")
            self.recommend_var.set(
                f"🎯 Recommended for you ({tier} game, {reason}): {rec['title']} — click to view.")
            if not self.recommend_banner.winfo_manager():
                self.recommend_banner.pack(fill="x", padx=10, pady=(4, 0), before=self.guide_filter_box)
        elif self.recommend_banner.winfo_manager():
            self.recommend_banner.pack_forget()

    def _on_rank_go(self, key, system_name):
        if not system_name:
            return
        self._switch_tab("nav")
        self.nav_dest_entry.delete(0, "end")
        self.nav_dest_entry.insert(0, system_name)
        self._use_current_system()

    def _render_powerplay_box(self, powerplay):
        if not powerplay or not powerplay.get("power"):
            if self.powerplay_box.winfo_manager():
                self.powerplay_box.pack_forget()
            return
        days = (powerplay.get("time_pledged") or 0) // 86400
        self.powerplay_var.set(
            f"Powerplay: pledged to {powerplay['power']} — Rank {powerplay.get('rank', '?')}, "
            f"{powerplay.get('merits', 0):,} merits ({days}d pledged)")
        if not self.powerplay_box.winfo_manager():
            self.powerplay_box.pack(fill="x", padx=10, pady=4, before=self.rank_cols_row.master)

    def _render_rank_suggestions(self, current_system):
        if not current_system:
            for tile in self.rank_tiles.values():
                tile.update_suggestion("", None)
            return
        status, error, origin, results = self.nearby_finder.get_snapshot()
        for key, tile in self.rank_tiles.items():
            if key == "CQC":
                tile.update_suggestion("CQC is a separate arcade mode — no station needed.", None)
                continue
            if status == "loading" or origin != current_system:
                tile.update_suggestion("Finding a nearby system...", None)
                continue
            if status == "error":
                tile.update_suggestion(f"Lookup failed: {error}", None)
                continue
            allegiance = RANK_ALLEGIANCE.get(key)
            nearest = nearest_populated_system(results, allegiance)
            if not nearest:
                label = f"a {allegiance}-aligned" if allegiance else "a populated"
                tile.update_suggestion(f"No {label} system found within {EDSM_RADIUS_LY} ly.", None)
                continue
            name = nearest.get("name", "?")
            dist = nearest.get("distance", 0)
            if allegiance:
                tile.update_suggestion(f"{name} ({dist:.1f} ly) — {allegiance}-aligned", name)
            else:
                tile.update_suggestion(f"{name} ({dist:.1f} ly) — check its mission board", name)

    # ---------- Nav tab ----------

    def _build_nav_tab(self, parent):
        frame = tk.Frame(parent, bg=BG)
        outer, canvas, inner = self._make_scrollable(frame)
        outer.pack(fill="both", expand=True)

        form = tk.Frame(inner, bg=PANEL_BG, highlightthickness=1, highlightbackground="#2a2a2a")
        form.pack(fill="x", padx=10, pady=(6, 4))

        origin_row = tk.Frame(form, bg=PANEL_BG)
        origin_row.pack(fill="x", padx=8, pady=(8, 3))
        tk.Label(origin_row, text="From:", font=FONT_SMALL_BOLD, bg=PANEL_BG, fg=FG,
                 width=9, anchor="w").pack(side="left")
        self.nav_origin_entry = tk.Entry(origin_row, bg=BG, fg=FG, insertbackground=FG,
                                          relief="flat", font=FONT_BODY)
        self.nav_origin_entry.pack(side="left", fill="x", expand=True, ipady=2)
        use_loc_btn = tk.Label(origin_row, text="Use current", font=FONT_SMALL, bg=CARD_BG,
                                fg=ACCENT, cursor="hand2", padx=6, pady=2)
        use_loc_btn.pack(side="left", padx=(6, 0))
        use_loc_btn.bind("<Button-1>", self._use_current_system)

        dest_row = tk.Frame(form, bg=PANEL_BG)
        dest_row.pack(fill="x", padx=8, pady=3)
        tk.Label(dest_row, text="To:", font=FONT_SMALL_BOLD, bg=PANEL_BG, fg=FG,
                 width=9, anchor="w").pack(side="left")
        self.nav_dest_entry = tk.Entry(dest_row, bg=BG, fg=FG, insertbackground=FG,
                                        relief="flat", font=FONT_BODY)
        self.nav_dest_entry.pack(side="left", fill="x", expand=True, ipady=2)

        range_row = tk.Frame(form, bg=PANEL_BG)
        range_row.pack(fill="x", padx=8, pady=3)
        tk.Label(range_row, text="Jump range:", font=FONT_SMALL_BOLD, bg=PANEL_BG, fg=FG,
                 width=9, anchor="w").pack(side="left")
        self.nav_range_entry = tk.Entry(range_row, bg=BG, fg=FG, insertbackground=FG,
                                         relief="flat", font=FONT_BODY, width=8)
        self.nav_range_entry.pack(side="left", ipady=2)
        tk.Label(range_row, text="ly", font=FONT_SMALL, bg=PANEL_BG, fg=MUTED).pack(
            side="left", padx=(4, 8))
        use_range_btn = tk.Label(range_row, text="Use ship range", font=FONT_SMALL, bg=CARD_BG,
                                  fg=ACCENT, cursor="hand2", padx=6, pady=2)
        use_range_btn.pack(side="left")
        use_range_btn.bind("<Button-1>", self._use_ship_range)

        plot_row = tk.Frame(form, bg=PANEL_BG)
        plot_row.pack(fill="x", padx=8, pady=(6, 8))
        plot_btn = tk.Label(plot_row, text="Plot Route", font=FONT_SMALL_BOLD, bg=ACCENT,
                             fg="#0a0a0a", cursor="hand2", padx=10, pady=4)
        plot_btn.pack(side="left")
        plot_btn.bind("<Button-1>", self._plot_route)
        self.nav_status_var = tk.StringVar(
            value="Enter a starting system, destination, and jump range, then Plot Route.")
        tk.Label(plot_row, textvariable=self.nav_status_var, font=FONT_SMALL, bg=PANEL_BG,
                 fg=MUTED, anchor="w", justify="left", wraplength=380).pack(
            side="left", padx=(10, 0), fill="x", expand=True)

        note = tk.Label(inner, text="Routes are plotted via Spansh's public neutron router "
                                     "(spansh.co.uk) — requires an internet connection.",
                         font=FONT_SMALL, bg=BG, fg=MUTED, anchor="w", justify="left",
                         wraplength=460)
        note.pack(fill="x", padx=10, pady=(0, 4))

        self.nav_suggest_box = tk.Frame(inner, bg=PANEL_BG, highlightthickness=1,
                                         highlightbackground="#2a2a2a")
        self.nav_suggestion_var = tk.StringVar(value="")
        tk.Label(self.nav_suggest_box, textvariable=self.nav_suggestion_var, font=FONT_SMALL,
                 bg=PANEL_BG, fg=MUTED, anchor="w", justify="left", wraplength=330).pack(
            side="left", padx=8, pady=6, fill="x", expand=True)
        use_suggestion_btn = tk.Label(self.nav_suggest_box, text="Use as destination",
                                       font=FONT_SMALL_BOLD, bg=CARD_BG, fg=ACCENT,
                                       cursor="hand2", padx=6, pady=3)
        use_suggestion_btn.pack(side="left", padx=8)
        use_suggestion_btn.bind("<Button-1>", self._use_suggested_destination)

        self.nav_summary_var = tk.StringVar(value="")
        tk.Label(inner, textvariable=self.nav_summary_var, font=FONT_SMALL_BOLD, bg=BG,
                 fg=FG, anchor="w").pack(fill="x", padx=10, pady=(2, 2))

        self.nav_results_frame = tk.Frame(inner, bg=BG)
        self.nav_results_frame.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        self.tab_frames["nav"] = frame
        self.tab_canvases["nav"] = canvas
        self.nav_canvas = canvas

    def _use_current_system(self, event=None):
        state, _ = self.watcher.get_snapshot()
        system = state.get("system")
        if not system:
            self.nav_status_var.set("No current system known yet — play a session or load sample data.")
            return
        self.nav_origin_entry.delete(0, "end")
        self.nav_origin_entry.insert(0, system)

    def _use_ship_range(self, event=None):
        state, _ = self.watcher.get_snapshot()
        rng = state.get("max_jump_range")
        if not rng:
            self.nav_status_var.set("No ship jump range known yet — play a session or load sample data.")
            return
        self.nav_range_entry.delete(0, "end")
        self.nav_range_entry.insert(0, f"{rng:.2f}")

    def _use_suggested_destination(self, event=None):
        if not self._suggested_hub:
            return
        self.nav_dest_entry.delete(0, "end")
        self.nav_dest_entry.insert(0, self._suggested_hub)

    def _render_nav_suggestion(self, state):
        credits = state.get("credits")
        tier = estimate_tier(credits)
        buckets = state.get("buckets", {})
        positive = {k: v for k, v in buckets.items() if v > 0 and k in BUCKET_TO_PATH}
        dominant_bucket = max(positive, key=positive.get) if positive else None
        rec = recommend_path(tier, dominant_bucket)
        hub = ACTIVITY_HUBS.get(rec["key"]) if rec else None

        if hub:
            self._suggested_hub = hub
            self.nav_suggestion_var.set(
                f"For {rec['title']}: {hub} is a well-known reference system for this activity "
                f"— worth verifying current conditions in-game before committing.")
            if not self.nav_suggest_box.winfo_manager():
                self.nav_suggest_box.pack(fill="x", padx=10, pady=(0, 4), before=self.nav_results_frame)
        else:
            self._suggested_hub = None
            if self.nav_suggest_box.winfo_manager():
                self.nav_suggest_box.pack_forget()

    def _plot_route(self, event=None):
        origin = self.nav_origin_entry.get().strip()
        dest = self.nav_dest_entry.get().strip()
        range_txt = self.nav_range_entry.get().strip()
        if not origin or not dest:
            self.nav_status_var.set("Enter both a starting system and a destination.")
            return
        try:
            range_ly = float(range_txt)
            if range_ly <= 0:
                raise ValueError
        except ValueError:
            self.nav_status_var.set("Jump range must be a positive number of light years.")
            return

        self.nav_status_var.set(f"Submitting route request: {origin} → {dest}...")
        self.nav_summary_var.set("")
        for w in self.nav_results_frame.winfo_children():
            w.destroy()
        self._nav_last_status = None
        self.route_plotter.plot(origin, dest, range_ly)

    def _render_route(self, status, error, result):
        if status in ("submitting", "polling"):
            self.nav_status_var.set("Plotting route — this can take a while for long routes...")
            return
        if status == "error":
            self.nav_status_var.set(f"Error: {error}")
            return
        if status != "done" or not result:
            return

        jumps = result.get("system_jumps", [])
        total_jumps = sum(wp.get("jumps", 0) for wp in jumps)
        distance = result.get("distance", 0)
        self.nav_status_var.set(
            f"Route found — {len(jumps)} waypoint(s), {total_jumps} jump(s), "
            f"{distance:,.1f} ly total.")
        self.nav_summary_var.set(
            f"{result.get('source_system', '?')} → {result.get('destination_system', '?')}")

        for w in self.nav_results_frame.winfo_children():
            w.destroy()
        for i, wp in enumerate(jumps):
            row = tk.Frame(self.nav_results_frame, bg=CARD_BG, highlightthickness=1,
                            highlightbackground=CARD_BORDER)
            row.pack(fill="x", pady=2)
            name = wp.get("system", "?")
            is_neutron = bool(wp.get("neutron_star"))
            label_text = f"{i}. {name}" + ("  ⚡ neutron boost" if is_neutron else "")
            tk.Label(row, text=label_text, font=FONT_SMALL_BOLD, bg=CARD_BG,
                     fg=WARN if is_neutron else FG, anchor="w").pack(
                side="left", padx=6, pady=4)
            if i == 0:
                sub = "start"
            else:
                sub = f"{wp.get('jumps', 0)} jump(s) · {wp.get('distance_jumped', 0):,.1f} ly"
            tk.Label(row, text=sub, font=FONT_SMALL, bg=CARD_BG, fg=MUTED,
                     anchor="e").pack(side="right", padx=6)

    # ---------- Build tab ----------

    def _build_build_tab(self, parent):
        frame = tk.Frame(parent, bg=BG)
        outer, canvas, inner = self._make_scrollable(frame)
        outer.pack(fill="both", expand=True)

        top = tk.Frame(inner, bg=PANEL_BG, highlightthickness=1, highlightbackground="#2a2a2a")
        top.pack(fill="x", padx=10, pady=(6, 4))

        ship_row = tk.Frame(top, bg=PANEL_BG)
        ship_row.pack(fill="x", padx=8, pady=(8, 3))
        tk.Label(ship_row, text="Ship:", font=FONT_SMALL_BOLD, bg=PANEL_BG, fg=FG,
                 width=6, anchor="w").pack(side="left")
        ship_names = sorted(SHIP_NAMES[k] for k in SHIP_SLUGS)
        self.build_ship_var = tk.StringVar(value="")
        build_ship_menu = tk.OptionMenu(ship_row, self.build_ship_var, *ship_names,
                                         command=self._on_build_ship_picked)
        build_ship_menu.config(bg=BG, fg=FG, activebackground=CARD_BG, activeforeground=ACCENT,
                                highlightthickness=0, font=FONT_BODY, anchor="w")
        build_ship_menu["menu"].config(bg=BG, fg=FG)
        build_ship_menu.pack(side="left", fill="x", expand=True, padx=(0, 8))
        use_current_btn = tk.Label(ship_row, text="Use current ship", font=FONT_SMALL_BOLD,
                                    bg=CARD_BG, fg=ACCENT, cursor="hand2", padx=6, pady=2)
        use_current_btn.pack(side="left")
        use_current_btn.bind("<Button-1>", self._use_current_ship_for_build)

        self.build_status_var = tk.StringVar(value="Pick a ship to see its exact component slots.")
        tk.Label(top, textvariable=self.build_status_var, font=FONT_SMALL, bg=PANEL_BG, fg=MUTED,
                 anchor="w", justify="left", wraplength=440).pack(fill="x", padx=8, pady=(0, 8))

        self.build_summary_var = tk.StringVar(value="")
        tk.Label(inner, textvariable=self.build_summary_var, font=FONT_SMALL_BOLD, bg=BG, fg=FG,
                 anchor="w").pack(fill="x", padx=10, pady=(0, 2))

        note = tk.Label(inner, text="Component data is fetched live from EDCD's coriolis-data "
                                     "(github.com/EDCD/coriolis-data) — requires an internet "
                                     "connection. Ownership compares against your equipped Journal "
                                     "loadout and Storage; Engineer-unlock specifics aren't covered.",
                         font=FONT_SMALL, bg=BG, fg=MUTED, anchor="w", justify="left", wraplength=460)
        note.pack(fill="x", padx=10, pady=(0, 4))

        self.build_slots_frame = tk.Frame(inner, bg=BG)
        self.build_slots_frame.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        self.tab_frames["build"] = frame
        self.tab_canvases["build"] = canvas
        self.build_canvas = canvas

    def _use_current_ship_for_build(self, event=None):
        state, _ = self.watcher.get_snapshot()
        internal = state.get("ship_internal")
        if not internal or internal not in SHIP_SLUGS:
            self.build_status_var.set(
                "Current ship unknown or not in the supported list yet — pick one manually.")
            return
        display = SHIP_NAMES.get(internal, internal)
        self.build_ship_var.set(display)
        self._load_build_ship(SHIP_SLUGS[internal], display)

    def _on_build_ship_picked(self, display_name):
        internal = next((k for k in SHIP_SLUGS if SHIP_NAMES.get(k) == display_name), None)
        if not internal:
            return
        self._load_build_ship(SHIP_SLUGS[internal], display_name)

    def _load_build_ship(self, slug, display_name):
        self.build_slug = slug
        self._build_rendered_slug = None
        self.build_status_var.set(f"Loading {display_name} component data...")
        self.build_summary_var.set("")
        self.build_slot_widgets = {}
        for w in self.build_slots_frame.winfo_children():
            w.destroy()
        self.ship_store.ensure_loaded(slug)

    def _standard_slot_rows(self, ship):
        rows = []
        sizes = ship["slots"]["standard"]
        for i, (label, fname) in enumerate(STANDARD_SLOT_FILES):
            size = sizes[i] if i < len(sizes) else None
            catalog = self.ship_store.get_modules("standard", fname)
            mods = sorted((m for m in catalog if m.get("class") == size),
                          key=lambda m: RATING_ORDER.get(m.get("rating"), 9))
            options = [(module_option_label(None, m), m["symbol"], m) for m in mods]
            rows.append((f"std:{i}", f"{label} (Class {size})", options))
        return rows

    def _hardpoint_slot_rows(self, ship):
        rows = []
        sizes = ship["slots"]["hardpoints"]
        catalog = []
        for fname in HARDPOINT_MODULE_FILES:
            type_name = display_name_for_file(fname)
            for m in self.ship_store.get_modules("hardpoints", fname):
                catalog.append((type_name, m))
        for i, size in enumerate(sizes):
            mods = [(t, m) for t, m in catalog if fits_hardpoint(m.get("class", -1), size)]
            mods.sort(key=lambda tm: (tm[0], tm[1].get("class", 0), RATING_ORDER.get(tm[1].get("rating"), 9)))
            options = [(module_option_label(t, m), m["symbol"], m) for t, m in mods]
            size_name = HARDPOINT_SIZE_NAMES[size] if 0 <= size < len(HARDPOINT_SIZE_NAMES) else f"Size {size}"
            rows.append((f"hp:{i}", f"{size_name} Hardpoint {i + 1}", options))
        return rows

    def _internal_slot_rows(self, ship):
        rows = []
        catalog = []
        for fname in INTERNAL_MODULE_FILES:
            type_name = display_name_for_file(fname)
            for m in self.ship_store.get_modules("internal", fname):
                catalog.append((type_name, m))
        for i, spec in enumerate(ship["slots"]["internal"]):
            if isinstance(spec, dict):
                size, slot_name, eligible = spec.get("class"), spec.get("name", "Internal"), spec.get("eligible")
            else:
                size, slot_name, eligible = spec, "Internal", None
            if eligible:
                mods = [(t, m) for t, m in catalog if m.get("class", 0) <= size and m.get("grp") in eligible]
            else:
                mods = [(t, m) for t, m in catalog if m.get("class", 0) <= size]
            mods.sort(key=lambda tm: (tm[0], tm[1].get("class", 0), RATING_ORDER.get(tm[1].get("rating"), 9)))
            options = [(module_option_label(t, m), m["symbol"], m) for t, m in mods]
            rows.append((f"int:{i}", f"{slot_name} Slot {i + 1} (Class {size})", options))
        return rows

    def _render_build_slots(self):
        wrapper = self.ship_store.get_ship(self.build_slug)
        ship = wrapper.get(self.build_slug) if wrapper else None
        if not ship:
            self.build_status_var.set("Could not load ship data.")
            return
        self._build_rendered_slug = self.build_slug
        slots = ship["slots"]
        self.build_status_var.set(
            f"Loaded {ship['properties']['name']} — {len(slots['standard'])} standard, "
            f"{len(slots['hardpoints'])} hardpoint, {len(slots['internal'])} internal slots.")

        for w in self.build_slots_frame.winfo_children():
            w.destroy()
        self.build_slot_widgets = {}

        self._build_section(self.build_slots_frame, "Standard", self._standard_slot_rows(ship))
        self._build_section(self.build_slots_frame, "Hardpoints", self._hardpoint_slot_rows(ship))
        self._build_section(self.build_slots_frame, "Internal", self._internal_slot_rows(ship))

        self._refresh_build_ownership()

    def _build_section(self, parent, title, rows):
        if not rows:
            return
        box = tk.LabelFrame(parent, text=f" {title} ", bg=BG, fg=MUTED, labelanchor="nw",
                             bd=1, relief="solid", highlightbackground="#2a2a2a")
        box.pack(fill="x", pady=(0, 6))
        for slot_key, header_label, options in rows:
            self._add_slot_row(box, slot_key, header_label, options)

    def _add_slot_row(self, container, slot_key, header_label, options):
        row = tk.Frame(container, bg=PANEL_BG, highlightthickness=1, highlightbackground=CARD_BORDER)
        row.pack(fill="x", padx=6, pady=3)

        top = tk.Frame(row, bg=PANEL_BG)
        top.pack(fill="x", padx=6, pady=(4, 2))
        tk.Label(top, text=header_label, font=FONT_SMALL_BOLD, bg=PANEL_BG, fg=ACCENT,
                 anchor="w").pack(side="left")
        tag_var = tk.StringVar(value="")
        tag_label = tk.Label(top, textvariable=tag_var, font=FONT_SMALL_BOLD, bg=PANEL_BG, fg=MUTED)
        tag_label.pack(side="right")

        if not options:
            tk.Label(row, text="No compatible modules found in the fetched catalog.", font=FONT_SMALL,
                     bg=PANEL_BG, fg=MUTED, anchor="w").pack(fill="x", padx=6, pady=(0, 4))
            return

        var = tk.StringVar(value=options[0][0])
        menu = tk.OptionMenu(row, var, *[o[0] for o in options])
        menu.config(bg=BG, fg=FG, activebackground=CARD_BG, activeforeground=ACCENT,
                    highlightthickness=0, font=FONT_SMALL, anchor="w")
        menu["menu"].config(bg=BG, fg=FG)
        menu.pack(fill="x", padx=6, pady=(0, 2))

        note_var = tk.StringVar(value="")
        tk.Label(row, textvariable=note_var, font=FONT_SMALL, bg=PANEL_BG, fg=MUTED, anchor="w",
                 justify="left", wraplength=420).pack(fill="x", padx=6, pady=(0, 4))

        self.build_slot_widgets[slot_key] = {
            "var": var, "tag_var": tag_var, "tag_label": tag_label, "note_var": note_var,
            "label_to_symbol": {o[0]: o[1] for o in options},
            "label_to_module": {o[0]: o[2] for o in options},
        }
        var.trace_add("write", lambda *_a, k=slot_key: self._refresh_slot_ownership(k))

    def _refresh_build_ownership(self):
        for slot_key in self.build_slot_widgets:
            self._refresh_slot_ownership(slot_key)
        self._update_build_summary()

    def _refresh_slot_ownership(self, slot_key):
        widgets = self.build_slot_widgets.get(slot_key)
        if not widgets:
            return
        symbol = widgets["label_to_symbol"].get(widgets["var"].get())
        if not symbol:
            return
        state, _ = self.watcher.get_snapshot()
        owned = {v.lower() for v in state.get("equipped_modules", {}).values()}
        owned |= {s.lower() for s in state.get("stored_modules", set())}
        is_owned = symbol.lower() in owned
        if is_owned:
            widgets["tag_var"].set("✓ Owned")
            widgets["tag_label"].config(fg=GOOD)
            widgets["note_var"].set("")
        else:
            widgets["tag_var"].set("Not owned")
            widgets["tag_label"].config(fg=WARN)
            module = widgets["label_to_module"].get(widgets["var"].get(), {})
            widgets["note_var"].set(acquisition_note(symbol, module))
        self._update_build_summary()

    def _update_build_summary(self):
        if not self.build_slot_widgets:
            return
        total = len(self.build_slot_widgets)
        owned_count = sum(1 for w in self.build_slot_widgets.values()
                           if w["tag_var"].get().startswith("✓"))
        self.build_summary_var.set(f"{owned_count} of {total} slots filled with modules you already own.")

    # ---------- Explore tab ----------

    def _build_explore_tab(self, parent):
        frame = tk.Frame(parent, bg=BG)
        outer, canvas, inner = self._make_scrollable(frame)
        outer.pack(fill="both", expand=True)

        # --- Unexplored systems ---
        unexplored_box = tk.LabelFrame(inner, text=" Unexplored Systems ", bg=BG, fg=MUTED,
                                        labelanchor="nw", bd=1, relief="solid",
                                        highlightbackground="#2a2a2a")
        unexplored_box.pack(fill="x", padx=10, pady=(6, 4))

        row1 = tk.Frame(unexplored_box, bg=BG)
        row1.pack(fill="x", padx=8, pady=(8, 3))
        tk.Label(row1, text="From:", font=FONT_SMALL_BOLD, bg=BG, fg=FG,
                 width=8, anchor="w").pack(side="left")
        self.unexplored_origin_entry = tk.Entry(row1, bg=PANEL_BG, fg=FG, insertbackground=FG,
                                                  relief="flat", font=FONT_BODY)
        self.unexplored_origin_entry.pack(side="left", fill="x", expand=True, ipady=2)
        use_current_btn1 = tk.Label(row1, text="Use current", font=FONT_SMALL_BOLD, bg=CARD_BG,
                                     fg=ACCENT, cursor="hand2", padx=6, pady=2)
        use_current_btn1.pack(side="left", padx=(6, 0))
        use_current_btn1.bind("<Button-1>", lambda e: self._use_current_system_into(self.unexplored_origin_entry))

        row2 = tk.Frame(unexplored_box, bg=BG)
        row2.pack(fill="x", padx=8, pady=3)
        tk.Label(row2, text="Radius:", font=FONT_SMALL_BOLD, bg=BG, fg=FG,
                 width=8, anchor="w").pack(side="left")
        self.unexplored_radius_entry = tk.Entry(row2, bg=PANEL_BG, fg=FG, insertbackground=FG,
                                                  relief="flat", font=FONT_BODY, width=6)
        self.unexplored_radius_entry.insert(0, str(EDSM_RADIUS_LY))
        self.unexplored_radius_entry.pack(side="left", ipady=2)
        tk.Label(row2, text="ly", font=FONT_SMALL, bg=BG, fg=MUTED).pack(side="left", padx=(4, 8))
        find_btn = tk.Label(row2, text="Find", font=FONT_SMALL_BOLD, bg=ACCENT, fg="#0a0a0a",
                             cursor="hand2", padx=10, pady=3)
        find_btn.pack(side="left")
        find_btn.bind("<Button-1>", self._find_unexplored)

        self.unexplored_status_var = tk.StringVar(
            value=f"Checks nearby systems against EDSM — real, but slow (one request per candidate). "
                  f"Max radius {EDSM_MAX_RADIUS_LY} ly (EDSM's own limit).")
        tk.Label(unexplored_box, textvariable=self.unexplored_status_var, font=FONT_SMALL, bg=BG,
                 fg=MUTED, anchor="w", justify="left", wraplength=440).pack(fill="x", padx=8, pady=(0, 4))

        self.unexplored_results_frame = tk.Frame(unexplored_box, bg=BG)
        self.unexplored_results_frame.pack(fill="x", padx=8, pady=(0, 8))

        # --- Materials search ---
        materials_box = tk.LabelFrame(inner, text=" Materials Search ", bg=BG, fg=MUTED,
                                       labelanchor="nw", bd=1, relief="solid",
                                       highlightbackground="#2a2a2a")
        materials_box.pack(fill="x", padx=10, pady=(4, 4))

        mode_row = tk.Frame(materials_box, bg=BG)
        mode_row.pack(fill="x", padx=8, pady=(8, 3))
        tk.Label(mode_row, text="Mode:", font=FONT_SMALL_BOLD, bg=BG, fg=FG,
                 width=8, anchor="w").pack(side="left")
        self.material_mode_buttons = {}
        for mode, label in (("surface", "Surface Material"), ("ring", "Ring Hotspot")):
            btn = tk.Label(mode_row, text=label, font=FONT_SMALL_BOLD, cursor="hand2",
                            padx=8, pady=2, bg=CARD_BG, fg=MUTED)
            btn.pack(side="left", padx=(0, 4))
            btn.bind("<Button-1>", lambda e, m=mode: self._set_material_mode(m))
            self.material_mode_buttons[mode] = btn

        mat_row = tk.Frame(materials_box, bg=BG)
        mat_row.pack(fill="x", padx=8, pady=3)
        tk.Label(mat_row, text="Material:", font=FONT_SMALL_BOLD, bg=BG, fg=FG,
                 width=8, anchor="w").pack(side="left")
        self.material_var = tk.StringVar(value=SURFACE_MATERIALS[0])
        self.material_menu = tk.OptionMenu(mat_row, self.material_var, *SURFACE_MATERIALS)
        self.material_menu.config(bg=PANEL_BG, fg=FG, activebackground=CARD_BG, activeforeground=ACCENT,
                                   highlightthickness=0, font=FONT_BODY, anchor="w")
        self.material_menu["menu"].config(bg=BG, fg=FG)
        self.material_menu.pack(side="left", fill="x", expand=True)

        row3 = tk.Frame(materials_box, bg=BG)
        row3.pack(fill="x", padx=8, pady=3)
        tk.Label(row3, text="From:", font=FONT_SMALL_BOLD, bg=BG, fg=FG,
                 width=8, anchor="w").pack(side="left")
        self.material_origin_entry = tk.Entry(row3, bg=PANEL_BG, fg=FG, insertbackground=FG,
                                                relief="flat", font=FONT_BODY)
        self.material_origin_entry.pack(side="left", fill="x", expand=True, ipady=2)
        use_current_btn2 = tk.Label(row3, text="Use current", font=FONT_SMALL_BOLD, bg=CARD_BG,
                                     fg=ACCENT, cursor="hand2", padx=6, pady=2)
        use_current_btn2.pack(side="left", padx=(6, 0))
        use_current_btn2.bind("<Button-1>", lambda e: self._use_current_system_into(self.material_origin_entry))

        row4 = tk.Frame(materials_box, bg=BG)
        row4.pack(fill="x", padx=8, pady=3)
        tk.Label(row4, text="Radius:", font=FONT_SMALL_BOLD, bg=BG, fg=FG,
                 width=8, anchor="w").pack(side="left")
        self.material_radius_entry = tk.Entry(row4, bg=PANEL_BG, fg=FG, insertbackground=FG,
                                                relief="flat", font=FONT_BODY, width=6)
        self.material_radius_entry.insert(0, str(MATERIAL_SEARCH_RADIUS_LY))
        self.material_radius_entry.pack(side="left", ipady=2)
        tk.Label(row4, text="ly", font=FONT_SMALL, bg=BG, fg=MUTED).pack(side="left", padx=(4, 8))
        search_btn = tk.Label(row4, text="Search", font=FONT_SMALL_BOLD, bg=ACCENT, fg="#0a0a0a",
                               cursor="hand2", padx=10, pady=3)
        search_btn.pack(side="left")
        search_btn.bind("<Button-1>", self._search_materials)

        self.material_status_var = tk.StringVar(
            value="Surface materials and ring hotspots are fetched live from Spansh's public data.")
        tk.Label(materials_box, textvariable=self.material_status_var, font=FONT_SMALL, bg=BG,
                 fg=MUTED, anchor="w", justify="left", wraplength=440).pack(fill="x", padx=8, pady=(0, 4))

        self.material_results_frame = tk.Frame(materials_box, bg=BG)
        self.material_results_frame.pack(fill="x", padx=8, pady=(0, 8))

        self._set_material_mode("surface")

        self.tab_frames["explore"] = frame
        self.tab_canvases["explore"] = canvas
        self.explore_canvas = canvas

    def _use_current_system_into(self, entry):
        state, _ = self.watcher.get_snapshot()
        system = state.get("system")
        if not system:
            return
        entry.delete(0, "end")
        entry.insert(0, system)

    def _set_material_mode(self, mode):
        self.material_mode = mode
        for m, btn in self.material_mode_buttons.items():
            active = m == mode
            btn.config(fg=ACCENT if active else MUTED, bg=BG if active else CARD_BG)
        options = SURFACE_MATERIALS if mode == "surface" else RING_SIGNALS
        menu = self.material_menu["menu"]
        menu.delete(0, "end")
        for name in options:
            menu.add_command(label=name, command=lambda n=name: self.material_var.set(n))
        self.material_var.set(options[0])

    def _find_unexplored(self, event=None):
        origin = self.unexplored_origin_entry.get().strip()
        if not origin:
            self.unexplored_status_var.set("Enter a starting system (or click Use current).")
            return
        try:
            radius = float(self.unexplored_radius_entry.get().strip())
            if radius <= 0:
                raise ValueError
        except ValueError:
            self.unexplored_status_var.set("Radius must be a positive number of light years.")
            return
        if radius > EDSM_MAX_RADIUS_LY:
            self.unexplored_status_var.set(
                f"Radius must be {EDSM_MAX_RADIUS_LY} ly or less — EDSM's system lookup doesn't "
                f"support larger searches.")
            return
        self.unexplored_status_var.set(f"Checking systems near {origin} (this can take a moment)...")
        for w in self.unexplored_results_frame.winfo_children():
            w.destroy()
        self._unexplored_last_status = None
        self.unexplored_finder.find(origin, radius)

    def _search_materials(self, event=None):
        origin = self.material_origin_entry.get().strip()
        if not origin:
            self.material_status_var.set("Enter a starting system (or click Use current).")
            return
        try:
            radius = float(self.material_radius_entry.get().strip())
            if radius <= 0:
                raise ValueError
        except ValueError:
            self.material_status_var.set("Radius must be a positive number of light years.")
            return
        material = self.material_var.get()
        self.material_status_var.set(f"Searching for {material} near {origin}...")
        for w in self.material_results_frame.winfo_children():
            w.destroy()
        self._material_last_status = None
        self.material_finder.search(origin, self.material_mode, material, radius)

    def _on_explore_go(self, system_name):
        if not system_name:
            return
        self._switch_tab("nav")
        self.nav_dest_entry.delete(0, "end")
        self.nav_dest_entry.insert(0, system_name)
        self._use_current_system()

    def _render_unexplored_results(self, status, error, results):
        if status == "loading":
            self.unexplored_status_var.set("Checking systems (this can take a moment)...")
            return
        if status == "error":
            self.unexplored_status_var.set(f"Error: {error}")
            return
        if status != "ready":
            return
        for w in self.unexplored_results_frame.winfo_children():
            w.destroy()
        if not results:
            self.unexplored_status_var.set(
                "No unvisited systems found in range — try a larger radius or a more remote origin "
                "(well-traveled areas near the bubble are usually fully charted).")
            return
        self.unexplored_status_var.set(f"{len(results)} unvisited system(s) found.")
        for r in results:
            row = tk.Frame(self.unexplored_results_frame, bg=CARD_BG, highlightthickness=1,
                            highlightbackground=CARD_BORDER)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=r["name"], font=FONT_SMALL_BOLD, bg=CARD_BG, fg=FG,
                     anchor="w").pack(side="left", padx=6, pady=4)
            tk.Label(row, text=f"{r['distance']:.1f} ly", font=FONT_SMALL, bg=CARD_BG, fg=MUTED,
                     anchor="e").pack(side="right", padx=(0, 6))
            go_btn = tk.Label(row, text="Go", font=FONT_SMALL_BOLD, bg=CARD_BG, fg=ACCENT,
                               cursor="hand2", padx=6)
            go_btn.pack(side="right")
            go_btn.bind("<Button-1>", lambda e, n=r["name"]: self._on_explore_go(n))

    def _render_material_results(self, status, error, results):
        if status == "loading":
            self.material_status_var.set("Searching (this can take a moment)...")
            return
        if status == "error":
            self.material_status_var.set(f"Error: {error}")
            return
        if status != "ready":
            return
        for w in self.material_results_frame.winfo_children():
            w.destroy()
        if not results:
            self.material_status_var.set("No matches found within range — try a larger radius.")
            return
        self.material_status_var.set(f"{len(results)} match(es) found.")
        for r in results:
            row = tk.Frame(self.material_results_frame, bg=CARD_BG, highlightthickness=1,
                            highlightbackground=CARD_BORDER)
            row.pack(fill="x", pady=2)
            text_col = tk.Frame(row, bg=CARD_BG)
            text_col.pack(side="left", fill="x", expand=True, padx=6, pady=4)
            tk.Label(text_col, text=f"{r['system']} — {r['body']}", font=FONT_SMALL_BOLD,
                     bg=CARD_BG, fg=FG, anchor="w").pack(fill="x")
            tk.Label(text_col, text=r["detail"], font=FONT_SMALL, bg=CARD_BG, fg=MUTED,
                     anchor="w").pack(fill="x")
            tk.Label(row, text=f"{r['distance']:.1f} ly", font=FONT_SMALL, bg=CARD_BG, fg=MUTED,
                     anchor="e").pack(side="right", padx=(0, 6))
            go_btn = tk.Label(row, text="Go", font=FONT_SMALL_BOLD, bg=CARD_BG, fg=ACCENT,
                               cursor="hand2", padx=6)
            go_btn.pack(side="right")
            go_btn.bind("<Button-1>", lambda e, n=r["system"]: self._on_explore_go(n))

    # ---------- Colonization tab ----------

    def _build_colonization_tab(self, parent):
        frame = tk.Frame(parent, bg=BG)
        outer, canvas, inner = self._make_scrollable(frame)
        outer.pack(fill="both", expand=True)

        tk.Label(inner, text="Tracks your own active construction site(s) — this only shows real "
                              "sites you've actually docked at (via the Journal's construction-depot "
                              "event); there's no public data source for discovering colonization "
                              "opportunities elsewhere in the galaxy.",
                 font=FONT_SMALL, bg=BG, fg=MUTED, anchor="w", justify="left",
                 wraplength=460).pack(fill="x", padx=10, pady=(6, 4))

        self.colony_status_var = tk.StringVar(
            value="No active construction site tracked yet — dock at one to populate this.")
        self.colony_status_label = tk.Label(inner, textvariable=self.colony_status_var, font=FONT_SMALL,
                                             bg=BG, fg=MUTED, anchor="w", justify="left", wraplength=460)
        self.colony_status_label.pack(fill="x", padx=10, pady=(0, 4))

        self.colony_cards_frame = tk.Frame(inner, bg=BG)
        self.colony_cards_frame.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        self._colony_rendered_depots = None

        self.tab_frames["colony"] = frame
        self.tab_canvases["colony"] = canvas
        self.colony_canvas = canvas

    def _render_colonization(self, depots):
        # depots is a dict keyed by market_id — compare against a snapshot
        # signature so this only rebuilds widgets when something real changed.
        signature = tuple(sorted(
            (mid, d["progress"], d["complete"], d["failed"],
             tuple((r["name"], r["required"], r["provided"]) for r in d["resources"]))
            for mid, d in depots.items()
        ))
        if signature == self._colony_rendered_depots:
            return
        self._colony_rendered_depots = signature

        for w in self.colony_cards_frame.winfo_children():
            w.destroy()

        if not depots:
            self.colony_status_var.set(
                "No active construction site tracked yet — dock at one to populate this.")
            return
        self.colony_status_var.set(f"Tracking {len(depots)} construction site(s).")

        for market_id, depot in depots.items():
            card = tk.Frame(self.colony_cards_frame, bg=PANEL_BG, highlightthickness=1,
                             highlightbackground="#2a2a2a")
            card.pack(fill="x", pady=4)

            loc_bits = [b for b in (depot.get("system"), depot.get("station")) if b]
            title = " — ".join(loc_bits) if loc_bits else f"Construction site (MarketID {market_id})"
            header_row = tk.Frame(card, bg=PANEL_BG)
            header_row.pack(fill="x", padx=8, pady=(6, 2))
            tk.Label(header_row, text=title, font=FONT_TITLE, bg=PANEL_BG, fg=ACCENT,
                     anchor="w").pack(side="left")
            if depot.get("complete"):
                tk.Label(header_row, text="Complete", font=FONT_SMALL_BOLD, bg=PANEL_BG,
                         fg=GOOD).pack(side="right")
            elif depot.get("failed"):
                tk.Label(header_row, text="Failed", font=FONT_SMALL_BOLD, bg=PANEL_BG,
                         fg=BAD).pack(side="right")

            pct = (depot.get("progress") or 0) * 100
            bar_row = tk.Frame(card, bg=PANEL_BG)
            bar_row.pack(fill="x", padx=8, pady=(0, 4))
            bar = Bar(bar_row, width=200, height=10)
            bar.set_pct(pct)
            bar.pack(side="left")
            tk.Label(bar_row, text=f"{pct:.0f}% complete", font=FONT_SMALL, bg=PANEL_BG,
                     fg=MUTED).pack(side="left", padx=(8, 0))

            resources = depot.get("resources") or []
            remaining = [r for r in resources if r["provided"] < r["required"]]
            if remaining:
                tk.Label(card, text="Still needed:", font=FONT_SMALL_BOLD, bg=PANEL_BG, fg=FG,
                         anchor="w").pack(fill="x", padx=8, pady=(2, 0))
                for r in sorted(remaining, key=lambda r: r["required"] - r["provided"], reverse=True):
                    still = r["required"] - r["provided"]
                    tk.Label(card, text=f"• {r['name']}: {still:,} more needed "
                                         f"({r['provided']:,} / {r['required']:,})",
                             font=FONT_SMALL, bg=PANEL_BG, fg=MUTED, anchor="w").pack(
                        fill="x", padx=(16, 8), pady=1)
            else:
                tk.Label(card, text="All required commodities delivered.", font=FONT_SMALL,
                         bg=PANEL_BG, fg=GOOD, anchor="w").pack(fill="x", padx=8, pady=(2, 6))
            tk.Frame(card, bg=PANEL_BG, height=6).pack()

    # ---------- Tick ----------

    def _tick(self):
        state, status = self.watcher.get_snapshot()
        self.tracker_status_var.set(status)

        self.cmdr_var.set(f"Commander: {state.get('commander') or '--'}")
        self.ship_var.set(f"Ship: {ship_display(state.get('ship_internal'))}")
        loc_bits = [b for b in (state.get("system"), state.get("station")) if b]
        self.loc_var.set("Location: " + (" — ".join(loc_bits) if loc_bits else "--"))
        self._render_powerplay_box(state.get("powerplay"))

        credits = state.get("credits")
        self.credits_var.set(fmt_credits(credits))
        baseline = state.get("session_baseline_credits")
        start = state.get("session_start")
        if credits is not None and baseline is not None:
            delta = credits - baseline
            rate_txt = "--"
            if start and (time.time() - start) > 30:
                rate = delta / ((time.time() - start) / 3600)
                rate_txt = fmt_credits(rate) + "/hr"
            sign = "+" if delta >= 0 else ""
            self.credits_delta_var.set(f"Session: {sign}{fmt_credits(delta)}  ({rate_txt})")
            self.credits_delta_label.config(fg=GOOD if delta >= 0 else BAD)
        else:
            self.credits_delta_var.set("Session: --")
            self.credits_delta_label.config(fg=MUTED)

        ranks = state.get("ranks", {})
        progress = state.get("progress", {})
        for key in RANK_KEYS:
            idx = ranks.get(key)
            pct = progress.get(key, 0) if idx is not None else 0
            self.rank_tiles[key].update_display(idx, pct)

        current_system = state.get("system")
        if current_system:
            self.nearby_finder.ensure_loaded(current_system)
        self._render_rank_suggestions(current_system)

        buckets = state.get("buckets", {})
        positive = {k: v for k, v in buckets.items() if v > 0 and k in BUCKET_TO_PATH}
        if positive:
            leader = max(positive, key=positive.get)
            path_key = BUCKET_TO_PATH[leader]
            path_title = next((p["title"] for p in PATHS if p["key"] == path_key), leader)
            self.hint_var.set(
                f"You're earning mostly from {leader} this session — "
                f"see the {path_title} card in the Guide tab.")
            self._hint_path_key = path_key
        else:
            self.hint_var.set("")
            self._hint_path_key = None

        route_status, route_error, route_result = self.route_plotter.get_snapshot()
        if route_status != self._nav_last_status:
            self._nav_last_status = route_status
            self._render_route(route_status, route_error, route_result)

        self._render_recommendation(state)
        self._render_nav_suggestion(state)
        self._poll_personalization()
        self._render_colonization(state.get("colonization_depots", {}))

        if self.build_slug:
            ship_status, ship_error, ship_loaded_slug = self.ship_store.get_snapshot()
            if ship_status == "ready" and ship_loaded_slug == self.build_slug \
                    and self._build_rendered_slug != self.build_slug:
                self._render_build_slots()
            elif ship_status == "error":
                self.build_status_var.set(f"Error loading ship data: {ship_error}")
        if self.build_slot_widgets:
            self._refresh_build_ownership()

        unexplored_status, unexplored_error, unexplored_results = self.unexplored_finder.get_snapshot()
        if unexplored_status != self._unexplored_last_status:
            self._unexplored_last_status = unexplored_status
            self._render_unexplored_results(unexplored_status, unexplored_error, unexplored_results)

        material_status, material_error, material_results = self.material_finder.get_snapshot()
        if material_status != self._material_last_status:
            self._material_last_status = material_status
            self._render_material_results(material_status, material_error, material_results)

        update_status, _update_error, latest_version = self.update_checker.get_snapshot()
        if update_status != self._update_last_status:
            self._update_last_status = update_status
            self._render_update_banner(update_status, latest_version)

        self.root.after(TICK_INTERVAL_MS, self._tick)


def main():
    if not ensure_single_instance():
        return
    root = tk.Tk()
    apply_dark_titlebar(root)
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
