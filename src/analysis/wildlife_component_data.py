from __future__ import annotations

from typing import Iterable

from smoke_faa_wildlife import text, truthy


COMPONENTS: dict[str, tuple[list[str], list[str]]] = {
    "radome": (["STR_RAD"], ["DAM_RAD"]),
    "windshield": (["STR_WINDSHLD"], ["DAM_WINDSHLD"]),
    "nose": (["STR_NOSE"], ["DAM_NOSE"]),
    "engine": (
        ["STR_ENG1", "STR_ENG2", "STR_ENG3", "STR_ENG4"],
        ["DAM_ENG1", "DAM_ENG2", "DAM_ENG3", "DAM_ENG4"],
    ),
    "propeller": (["STR_PROP"], ["DAM_PROP"]),
    "wing_rotor": (["STR_WING_ROT"], ["DAM_WING_ROT"]),
    "fuselage": (["STR_FUSE"], ["DAM_FUSE"]),
    "landing_gear": (["STR_LG"], ["DAM_LG"]),
    "tail": (["STR_TAIL"], ["DAM_TAIL"]),
    "lights": (["STR_LGHTS"], ["DAM_LGHTS"]),
    "other": (["STR_OTHER"], ["DAM_OTHER"]),
}


def component_rows(events: Iterable[dict]) -> list[dict]:
    rows: list[dict] = []
    for event in events:
        event_id = text(event.get("INDX_NR"))
        if not event_id:
            continue
        for component, (struck_fields, damage_fields) in COMPONENTS.items():
            if not any(truthy(event.get(field)) for field in struck_fields):
                continue
            rows.append({
                "event_id": event_id,
                "year": int(event.get("_YEAR") or 0),
                "month": int(event.get("_MONTH") or 0),
                "incident_date": text(event.get("INCIDENT_DATE")),
                "incident_time": text(event.get("TIME")),
                "component": component,
                "part_damage": any(truthy(event.get(field)) for field in damage_fields),
                "event_hard": bool(event.get("_HARD_EVENT")),
                "cost": float(event.get("_COST") or 0.0),
                "aos": float(event.get("_AOS") or 0.0),
                "phase_bucket": event.get("_PHASE_BUCKET") or "unknown",
                "phase_of_flight": text(event.get("PHASE_OF_FLIGHT")),
                "size": event.get("_SIZE") or "UNKNOWN",
                "aircraft_mass_class": text(event.get("AC_MASS")) or "UNKNOWN",
                "species": text(event.get("SPECIES")),
                "species_id": text(event.get("SPECIES_ID")) or text(event.get("SPECIES")) or "UNKNOWN",
                "airport_id": text(event.get("AIRPORT_ID")) or "UNKNOWN",
                "airport": text(event.get("AIRPORT")),
                "state": text(event.get("STATE")),
                "operator": text(event.get("OPERATOR")),
                "aircraft": text(event.get("AIRCRAFT")),
                "damage_level": text(event.get("DAMAGE_LEVEL")),
                "effect": text(event.get("EFFECT")),
            })
    return rows
