"""
Commander state rebuilt by replaying journal events.

This is the extension point of the journal layer: to track something new, add a function decorated with
`@handles("EventName", ...)`. Handlers receive (state, event_dict, datetime) and mutate the CommanderState;
`replay()` folds a time-ordered event list into it. Nothing here touches the database.
"""

from dataclasses import dataclass, field

from . import files as jfiles

HANDLERS = {}


def handles(*event_names):
    def register(fn):
        for name in event_names:
            HANDLERS.setdefault(name, []).append(fn)
        return fn
    return register


@dataclass
class CommanderState:
    commander: str | None = None
    fid: str | None = None
    game_mode: str | None = None
    credits: int | None = None
    credits_as_of: str | None = None              # LoadGame time: the balance is not tracked afterwards
    ship: dict = field(default_factory=dict)
    system: str | None = None
    system_address: int | None = None
    star_pos: list | None = None
    system_info: dict = field(default_factory=dict)
    body: str | None = None
    body_type: str | None = None
    station: dict | None = None                    # station/carrier docked at, or last left (see `docked`)
    docked: bool = False
    landed: bool = False
    supercruise: bool = False
    game_running: bool = False
    last_event: str | None = None
    last_event_time: str | None = None
    location_time: str | None = None

    def to_dict(self):
        return {
            "commander": self.commander, "game_mode": self.game_mode,
            "credits": self.credits, "credits_as_of": self.credits_as_of,
            "ship": self.ship,
            "system": self.system, "system_address": self.system_address, "star_pos": self.star_pos,
            "system_info": self.system_info, "body": self.body, "body_type": self.body_type,
            "station": self.station, "docked": self.docked, "landed": self.landed, "supercruise": self.supercruise,
            "game_running": self.game_running,
            "last_event": self.last_event, "last_event_time": self.last_event_time,
            "location_time": self.location_time,
        }


def replay(events, state=None):
    """Fold (datetime, event) pairs, oldest first, into a CommanderState."""
    state = state or CommanderState()
    for dt, e in events:
        name = e.get("event")
        state.last_event, state.last_event_time = name, jfiles.iso(dt)
        for fn in HANDLERS.get(name, ()):
            fn(state, e, dt)
    return state


def _station_from(e):
    if not e.get("StationName"):
        return None
    faction = e.get("StationFaction")
    return {
        "name": e["StationName"], "type": e.get("StationType"), "market_id": e.get("MarketID"),
        "faction": faction.get("Name") if isinstance(faction, dict) else None,
        "economy": e.get("StationEconomy_Localised") or e.get("StationEconomy"),
        "government": e.get("StationGovernment_Localised"),
        "services": e.get("StationServices"), "dist_from_star_ls": e.get("DistFromStarLS"),
    }


def _set_system(state, e, dt):
    state.system = e.get("StarSystem", state.system)
    state.system_address = e.get("SystemAddress", state.system_address)
    if e.get("StarPos"):
        state.star_pos = e["StarPos"]
    info = {
        "allegiance": e.get("SystemAllegiance"),
        "economy": e.get("SystemEconomy_Localised") or e.get("SystemEconomy"),
        "second_economy": e.get("SystemSecondEconomy_Localised") or e.get("SystemSecondEconomy"),
        "government": e.get("SystemGovernment_Localised") or e.get("SystemGovernment"),
        "security": e.get("SystemSecurity_Localised") or e.get("SystemSecurity"),
        "population": e.get("Population"),
        "controlling_faction": (e.get("SystemFaction") or {}).get("Name"),
    }
    state.system_info = {k: v for k, v in info.items() if v not in (None, "")}
    state.location_time = jfiles.iso(dt)


@handles("Fileheader")
def _fileheader(state, e, dt):
    state.game_running = True


@handles("Commander")
def _commander(state, e, dt):
    state.commander = e.get("Name", state.commander)
    state.fid = e.get("FID", state.fid)


@handles("LoadGame")
def _load_game(state, e, dt):
    state.game_running = True
    state.commander = e.get("Commander", state.commander)
    state.fid = e.get("FID", state.fid)
    state.game_mode = e.get("GameMode", state.game_mode)
    if e.get("Credits") is not None:
        state.credits, state.credits_as_of = e["Credits"], jfiles.iso(dt)
    state.ship = {"type": e.get("Ship_Localised") or e.get("Ship"), "key": e.get("Ship"),
                  "name": e.get("ShipName"), "ident": e.get("ShipIdent"), "id": e.get("ShipID")}


@handles("Loadout")
def _loadout(state, e, dt):
    state.ship = dict(state.ship, key=e.get("Ship"), name=e.get("ShipName"), ident=e.get("ShipIdent"),
                      id=e.get("ShipID"), cargo_capacity=e.get("CargoCapacity"), max_jump_range=e.get("MaxJumpRange"))


@handles("ShipyardSwap")
def _shipyard_swap(state, e, dt):
    state.ship = {"type": e.get("ShipType_Localised") or e.get("ShipType"), "key": e.get("ShipType"),
                  "id": e.get("ShipID")}


@handles("Location", "CarrierJump")
def _location(state, e, dt):
    _set_system(state, e, dt)
    state.body, state.body_type = e.get("Body"), e.get("BodyType")
    state.docked = bool(e.get("Docked", e.get("event") == "CarrierJump"))
    state.station = _station_from(e) if state.docked else None
    state.landed = False
    state.supercruise = False


@handles("FSDJump")
def _fsd_jump(state, e, dt):
    _set_system(state, e, dt)
    state.body, state.body_type = e.get("Body"), e.get("BodyType")
    state.docked = state.landed = False
    state.station = None
    state.supercruise = True


@handles("Docked")
def _docked(state, e, dt):
    state.docked, state.supercruise, state.landed = True, False, False
    state.station = _station_from(e)
    state.system = e.get("StarSystem", state.system)
    state.system_address = e.get("SystemAddress", state.system_address)


@handles("Undocked")
def _undocked(state, e, dt):
    state.docked = False


@handles("Touchdown")
def _touchdown(state, e, dt):
    if e.get("PlayerControlled", True):
        state.landed = True


@handles("Liftoff")
def _liftoff(state, e, dt):
    if e.get("PlayerControlled", True):
        state.landed = False


@handles("SupercruiseEntry")
def _sc_entry(state, e, dt):
    state.supercruise = True
    state.system = e.get("StarSystem", state.system)
    state.system_address = e.get("SystemAddress", state.system_address)


@handles("SupercruiseExit")
def _sc_exit(state, e, dt):
    state.supercruise = False
    state.body, state.body_type = e.get("Body", state.body), e.get("BodyType", state.body_type)
    state.system = e.get("StarSystem", state.system)
    state.system_address = e.get("SystemAddress", state.system_address)


@handles("Shutdown")
def _shutdown(state, e, dt):
    state.game_running = False


def current_state(directory, paths=None):
    """
    CommanderState from the newest journal. If that file has no location yet (e.g. a fresh session still at the
    main menu) older files are replayed first, newest last, until one provides it.
    Returns (state, newest_path).
    """
    paths = paths or jfiles.require_journal_files(directory)
    chain = []
    for path in reversed(paths):
        events = jfiles.read_events(path)
        chain.append(events)
        if any(e.get("event") in ("Location", "FSDJump", "CarrierJump") for _, e in events):
            break
    state = CommanderState()
    for events in reversed(chain):
        replay(events, state)
    return state, paths[-1]
