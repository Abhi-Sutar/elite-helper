"""Status.json: the game's live ship status file (rewritten constantly while playing). Stdlib only."""

from . import files as jfiles

# Bit positions of the `Flags` field, per the journal manual.
FLAG_BITS = {
    0: "docked", 1: "landed", 2: "landing_gear_down", 3: "shields_up", 4: "supercruise", 5: "flight_assist_off",
    6: "hardpoints_deployed", 7: "in_wing", 8: "lights_on", 9: "cargo_scoop_deployed", 10: "silent_running",
    11: "scooping_fuel", 12: "srv_handbrake", 13: "srv_turret_view", 14: "srv_turret_retracted",
    15: "srv_drive_assist", 16: "fsd_masslocked", 17: "fsd_charging", 18: "fsd_cooldown", 19: "low_fuel",
    20: "overheating", 21: "has_lat_long", 22: "in_danger", 23: "being_interdicted", 24: "in_main_ship",
    25: "in_fighter", 26: "in_srv", 27: "hud_analysis_mode", 28: "night_vision", 29: "altitude_from_average_radius",
    30: "fsd_jump", 31: "srv_high_beam",
}


def decode_flags(flags):
    return [name for bit, name in FLAG_BITS.items() if flags & (1 << bit)]


def read_status(directory, now=None):
    """Decoded Status.json with its age, or None if the file is missing/unreadable."""
    from datetime import datetime, timezone
    raw, mtime = jfiles.read_json_file(directory, "Status.json")
    if raw is None:
        return None
    now = now or datetime.now(timezone.utc)
    fuel = raw.get("Fuel") or {}
    dest = raw.get("Destination") or {}
    out = {
        "timestamp": raw.get("timestamp"), "age_seconds": round((now - mtime).total_seconds()),
        "flags": decode_flags(raw.get("Flags", 0)),
        "balance": raw.get("Balance"), "pips": raw.get("Pips"), "gui_focus": raw.get("GuiFocus"),
        "fuel_main": fuel.get("FuelMain"), "fuel_reservoir": fuel.get("FuelReservoir"),
        "cargo": raw.get("Cargo"), "legal_state": raw.get("LegalState"),
        "destination": {"system": dest.get("System"), "body": dest.get("Body"), "name": dest.get("Name")}
        if dest else None,
        "body_name": raw.get("BodyName"), "latitude": raw.get("Latitude"), "longitude": raw.get("Longitude"),
    }
    return {k: v for k, v in out.items() if v is not None}
